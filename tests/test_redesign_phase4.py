"""UI redesign Phase 4 — report experience acceptance tests (plan §7, B7).

Three backend boundaries this phase introduces:

  1. GET /api/reports/<run_id> — the web report view's data source. Same auth
     ladder as the PDF download (401 / 404 / 403), and the payload is
     CUSTOMER-SAFE: final_report + sources only, never documents (uploaded raw
     text), artifacts, or activity (machinery). A blank report_path (history
     imports, crashed runs) is a friendly 404, not a stack trace.
  2. /api/status/<job_id> + /api/files/... ownership: once a job snapshot is
     tied to a company (company_id set), only a session owning that company may
     read it; ownerless jobs keep the original open capability-URL behavior
     (pre-login flows + the Phase 8 file tests rely on it).
  3. B7 delivery deep links: the 企业微信 markdown and the email body both
     carry the /reports/<run_id> web link alongside the PDF download.

Same isolation pattern as test_redesign_phase2.py: a fresh CustomerStore wired
into app.CUSTOMERS, a throwaway SQLite DB, sessions established directly on the
test client, and MEMORY_DIR redirected into tmp.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot is one-time & idempotent
from agent_team import db, delivery, metrics_store  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.memory import SharedMemory  # noqa: E402
from agent_team.models import RUN_SUCCEEDED  # noqa: E402

OWNER = "owner@corp.com"
INTRUDER = "intruder@other.com"

REPORT_MD = (
    "# 经营周报\n\n## 核心结论\n- 本周销售额 1,250,000 元【1】，环比 +12.4%【2】\n\n"
    "## 数据来源与核验\n见附录。\n"
)
SOURCES = [
    {"anchor": 1, "claim_cn": "本周销售额 1,250,000 元", "source_file": "week.csv",
     "source_detail": "B2:B9 求和", "computed_by": "data_analyst"},
    {"anchor": 2, "claim_cn": "环比 +12.4%", "source_file": "metrics_store",
     "source_detail": "周环比", "computed_by": "trend_engine"},
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh DB + customer store + tmp memory dir; returns the shared handles."""
    db.init_db(str(tmp_path / "phase4.db"))
    monkeypatch.setattr(app, "MEMORY_DIR", str(tmp_path / "mem"))
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    store.create_customer([], name="Owner", starting_balance=100.0,
                          email=OWNER, password="password1")
    store.create_customer([], name="Intruder", starting_balance=100.0,
                          email=INTRUDER, password="password1")
    monkeypatch.setattr(app, "CUSTOMERS", store)
    company_id = metrics_store.get_or_create_company(OWNER, company_name="OwnerCo")
    return {"store": store, "company_id": company_id, "mem_dir": str(tmp_path / "mem")}


def _client_for(store, email):
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return client


def _finished_run(env, job_id="job4abc", with_snapshot=True):
    """A succeeded run for the owner company, with (optionally) its job snapshot
    written into the redirected memory store — exactly what the orchestrator's
    _finish_run leaves behind (report_path = the JOB ID)."""
    run_id = metrics_store.create_run(env["company_id"], "生成上周经营周报")
    metrics_store.finish_run(run_id, RUN_SUCCEEDED, total_cost_yuan=1.23,
                             report_path=job_id)
    if with_snapshot:
        memory = SharedMemory(job_id, store_dir=env["mem_dir"], goal="生成上周经营周报")
        memory.add_document({"name": "week.csv", "ext": ".csv", "chars": 42,
                             "text": "周次,销售额\n2026-W23,1250000", "meta": {}})
        memory.add_artifact("data_analyst", "internal working notes")
        memory.log("token_manager", "Budget check passed.", type="info")
        memory.set_company_id(env["company_id"])
        memory.set_run_id(run_id)
        memory.set_sources(SOURCES, "ok")
        memory.set_unknown_terms(["KA 渠道"])
        memory.set_final_report(REPORT_MD)
    return run_id


# ---------------------------------------------------------------------------
# 1. GET /api/reports/<run_id> — auth ladder + customer-safe payload
# ---------------------------------------------------------------------------
def test_report_detail_requires_login(env):
    run_id = _finished_run(env)
    assert app.app.test_client().get("/api/reports/%s" % run_id).status_code == 401


def test_report_detail_unknown_id_is_404(env):
    client = _client_for(env["store"], OWNER)
    assert client.get("/api/reports/999999").status_code == 404


def test_report_detail_cross_account_is_403(env):
    run_id = _finished_run(env)
    client = _client_for(env["store"], INTRUDER)
    resp = client.get("/api/reports/%s" % run_id)
    assert resp.status_code == 403
    assert "final_report" not in (resp.get_json() or {})


def test_report_detail_owner_gets_customer_safe_payload(env):
    run_id = _finished_run(env)
    client = _client_for(env["store"], OWNER)
    resp = client.get("/api/reports/%s" % run_id)
    assert resp.status_code == 200
    data = resp.get_json()

    # The report-facing fields are present...
    assert data["goal_text"] == "生成上周经营周报"
    assert data["status"] == RUN_SUCCEEDED
    assert "核心结论" in data["final_report"]
    assert data["sources"] == SOURCES
    assert data["sources_status"] == "ok"
    assert data["unknown_terms"] == ["KA 渠道"]
    assert "has_pdf" in data and "download_url" in data

    # ...and the machinery / raw uploads are NOT (plan §2 rule 1).
    for forbidden in ("documents", "artifacts", "activity"):
        assert forbidden not in data, forbidden


def test_report_detail_blank_report_path_is_friendly_404(env):
    run_id = metrics_store.create_run(env["company_id"], "历史数据导入")
    metrics_store.finish_run(run_id, RUN_SUCCEEDED, report_path="")  # import-style row
    client = _client_for(env["store"], OWNER)
    resp = client.get("/api/reports/%s" % run_id)
    assert resp.status_code == 404
    assert "报告内容不可用" in (resp.get_json() or {}).get("error", "")


def test_report_detail_missing_snapshot_is_friendly_404(env):
    run_id = _finished_run(env, job_id="vanished1", with_snapshot=False)
    client = _client_for(env["store"], OWNER)
    resp = client.get("/api/reports/%s" % run_id)
    assert resp.status_code == 404
    assert "报告内容不可用" in (resp.get_json() or {}).get("error", "")


# ---------------------------------------------------------------------------
# 2. /api/status/<job_id> ownership (and the same gate on /api/files)
# ---------------------------------------------------------------------------
def test_status_of_owned_job_requires_owner_session(env):
    _finished_run(env, job_id="ownedjob1")

    # Logged out → 401, never the snapshot (it holds the uploaded raw text).
    anon = app.app.test_client().get("/api/status/ownedjob1")
    assert anon.status_code == 401
    assert "activity" not in (anon.get_json() or {})

    # A different logged-in account → 403.
    intruder = _client_for(env["store"], INTRUDER)
    assert intruder.get("/api/status/ownedjob1").status_code == 403

    # The owner still polls normally.
    owner = _client_for(env["store"], OWNER)
    resp = owner.get("/api/status/ownedjob1")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "done"


def test_status_of_ownerless_job_stays_open(env):
    SharedMemory("freejob99", store_dir=env["mem_dir"], goal="demo")  # no company_id
    resp = app.app.test_client().get("/api/status/freejob99")
    assert resp.status_code == 200
    assert resp.get_json()["job_id"] == "freejob99"


def test_files_of_owned_job_require_owner_session(env, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path / "outputs"))
    _finished_run(env, job_id="ownedjob2")
    jdir = tmp_path / "outputs" / "ownedjob2"
    jdir.mkdir(parents=True)
    (jdir / "deck.svg").write_bytes(b"<svg/>")

    assert app.app.test_client().get("/api/files/ownedjob2/deck.svg").status_code == 401
    intruder = _client_for(env["store"], INTRUDER)
    assert intruder.get("/api/files/ownedjob2/deck.svg").status_code == 403
    owner = _client_for(env["store"], OWNER)
    assert owner.get("/api/files/ownedjob2/deck.svg").status_code == 200


# ---------------------------------------------------------------------------
# 3. B7 — delivery messages carry the /reports/<run_id> web link
# ---------------------------------------------------------------------------
def test_delivery_messages_carry_web_report_link(env, monkeypatch):
    run_id = _finished_run(env, job_id="job4link")
    metrics_store.merge_profile(env["company_id"],
                                {"wechat_webhook": "https://example.test/hook"})

    captured = {}

    def fake_send_email(to_addr, subject, body_text, attachment_path=None, **kw):
        captured["email_body"] = body_text
        return True, None

    def fake_send_wechat(webhook_url, markdown_text, timeout=15):
        captured["wechat_md"] = markdown_text
        return True, None

    monkeypatch.setattr(delivery, "send_email", fake_send_email)
    monkeypatch.setattr(delivery, "send_wechat_webhook", fake_send_wechat)
    monkeypatch.setenv("APP_BASE_URL", "https://app.example.cn")

    results = delivery.deliver_report(
        company_id=env["company_id"], run_id=run_id, channels=["email", "wechat"],
        pdf_path=None, report_markdown=REPORT_MD, company_name="OwnerCo",
        recipient_email=OWNER)
    assert all(r["ok"] for r in results)

    view_link = "https://app.example.cn/reports/%s" % run_id
    pdf_link = "https://app.example.cn/api/reports/%s/download" % run_id

    assert view_link in captured["email_body"]
    assert "在线查看报告（需登录）" in captured["email_body"]
    assert pdf_link in captured["email_body"]

    assert "[在线查看报告（需登录）](%s)" % view_link in captured["wechat_md"]
    assert "[下载 PDF](%s)" % pdf_link in captured["wechat_md"]

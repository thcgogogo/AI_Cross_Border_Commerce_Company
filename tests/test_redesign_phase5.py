"""UI redesign Phase 5 — 定时与送达 + 工作台 acceptance tests (plan §7, B5)
plus the Phase-4 hardening items shipped in the same session.

What this suite pins down:

  1. B1 — GET /api/scheduled-jobs carries a STATELESS next_run_at per job
     (computed from the cron expression, never live APScheduler state): present
     for an active weekly job, null for inactive jobs and malformed crons.
  2. B2 / D3 — scheduling is 企业版 (pro) only: free/basic accounts get a
     structured 402 on POST /api/scheduled-jobs AND /api/onboarding/schedule;
     pro succeeds; a fire after a pro→basic downgrade skips and records
     last_status "skipped_plan_feature".
  3. B3 — POST /api/delivery/test: 401 logged out; wechat without a webhook is
     a friendly ok:false; failures NEVER echo the webhook URL; email with SMTP
     unset answers "SMTP 未配置".
  4. B4 — the data inbox: GET shape, /api/run-grade upload guards, the
     copy-forward batch semantics (adding a file keeps what was staged),
     removal, and 401 logged out.
  5. Hardening — non-admin /api/status responses carry NO machinery feed lines
     (token_manager / "Received COMPANY MEMORY…" / "Token budget reached…",
     replaced by the friendly 已达本次生成上限 line) while is_admin gets the
     raw feed; /api/reports rows carry has_content and never total_cost_yuan /
     report_path; an anonymous /api/status probe of a MISSING job id gets 401,
     not 404 (existence is not disclosed pre-login).

Same isolation pattern as the phase-2/4 suites: a fresh CustomerStore wired
into app.CUSTOMERS, a throwaway SQLite DB, sessions established directly on
the test client, and the storage dirs redirected into tmp.
"""
import io
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot is one-time & idempotent
from agent_team import config, db, delivery, metrics_store  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.memory import SharedMemory  # noqa: E402
from agent_team.models import (  # noqa: E402
    RUN_SUCCEEDED, SUB_ACTIVE, Subscription)

OWNER = "owner@corp.com"
OTHER = "other@corp.com"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh DB + customer store + tmp storage dirs; returns shared handles."""
    db.init_db(str(tmp_path / "phase5.db"))
    monkeypatch.setattr(app, "MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setattr(metrics_store, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(metrics_store, "REPORTS_DIR", str(tmp_path / "reports"))
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    store.create_customer([], name="Owner", starting_balance=1000.0,
                          email=OWNER, password="password1")
    store.create_customer([], name="Other", starting_balance=1000.0,
                          email=OTHER, password="password1")
    monkeypatch.setattr(app, "CUSTOMERS", store)
    company_id = metrics_store.get_or_create_company(OWNER, company_name="OwnerCo")
    return {"store": store, "company_id": company_id,
            "mem_dir": str(tmp_path / "mem")}


def _client_for(store, email):
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return client


def _subscribe(account_id, plan_code):
    """Activate a subscription directly (the paid path is covered by phase 9)."""
    now = datetime.utcnow()
    with db.session_scope() as s:
        sub = (s.query(Subscription)
               .filter(Subscription.account_id == str(account_id)).one_or_none())
        if sub is None:
            s.add(Subscription(account_id=str(account_id), plan_code=plan_code,
                               status=SUB_ACTIVE, current_period_start=now,
                               current_period_end=now + timedelta(days=30),
                               cancel_at_period_end=False,
                               created_at=now, updated_at=now))
        else:
            sub.plan_code = plan_code
            sub.status = SUB_ACTIVE
            sub.current_period_end = now + timedelta(days=30)


# Named weekday on purpose — APScheduler's from_crontab numbers day_of_week
# 0=Monday (non-standard), so numeric "1" actually fired TUESDAY. "mon" means
# Monday under both conventions (matches app.ONBOARDING_WEEKLY_CRON).
WEEKLY_JOB = {"name_cn": "每周经营周报", "cron_expr": "0 8 * * mon",
              "goal_template": "生成上周经营周报",
              "delivery_channels": ["email"], "active": True}


# ---------------------------------------------------------------------------
# 1. B1 — next_run_at
# ---------------------------------------------------------------------------
def test_next_run_at_present_for_active_weekly_job(env):
    _subscribe(env["store"].find_by_email(OWNER)["id"], "pro")
    client = _client_for(env["store"], OWNER)
    resp = client.post("/api/scheduled-jobs", json=WEEKLY_JOB)
    assert resp.status_code == 200
    job = resp.get_json()["jobs"][0]
    nxt = job["next_run_at"]
    assert nxt, "an active weekly job must expose its next fire time"
    # Monday 08:00 in the scheduler's wall clock (Asia/Shanghai, UTC+8).
    assert "T08:00:00" in nxt and nxt.endswith("+08:00")
    parsed = datetime.fromisoformat(nxt)
    assert parsed.weekday() == 0  # Monday

    # GET answers the same enriched shape.
    listed = client.get("/api/scheduled-jobs").get_json()["jobs"]
    assert listed[0]["next_run_at"] == nxt


def test_next_run_at_null_for_inactive_or_malformed(env):
    _subscribe(env["store"].find_by_email(OWNER)["id"], "pro")
    client = _client_for(env["store"], OWNER)
    client.post("/api/scheduled-jobs", json=dict(WEEKLY_JOB, active=False))
    # A malformed cron can't get past the route's validation — write it
    # straight to the store (legacy/hand-edited rows must not 500 the list).
    metrics_store.create_scheduled_job(env["company_id"], "坏任务", "not a cron",
                                       active=True)
    jobs = client.get("/api/scheduled-jobs").get_json()["jobs"]
    by_name = {j["name_cn"]: j for j in jobs}
    assert by_name["每周经营周报"]["next_run_at"] is None      # inactive
    assert by_name["坏任务"]["next_run_at"] is None            # malformed


# ---------------------------------------------------------------------------
# 2. B2 / D3 — plan gates on every creation surface + the fire-time skip
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("plan_code", [None, "basic"])  # None ⇒ free (no sub)
def test_non_pro_plans_get_402_on_schedule_creation(env, plan_code):
    if plan_code:
        _subscribe(env["store"].find_by_email(OWNER)["id"], plan_code)
    client = _client_for(env["store"], OWNER)

    resp = client.post("/api/scheduled-jobs", json=WEEKLY_JOB)
    assert resp.status_code == 402
    body = resp.get_json()
    assert body["code"] == "upgrade_required"
    assert body["feature"] == "scheduled_delivery"
    assert body["required_plan"] == "pro"
    assert "企业版" in body["error"]

    # The wizard's one-click surface refuses identically (it used to silently
    # create the Monday job for free accounts).
    resp = client.post("/api/onboarding/schedule", json={"channels": ["email"]})
    assert resp.status_code == 402
    assert resp.get_json()["code"] == "upgrade_required"
    assert metrics_store.list_scheduled_jobs(env["company_id"]) == []


def test_pro_plan_creates_jobs_on_both_surfaces(env):
    _subscribe(env["store"].find_by_email(OWNER)["id"], "pro")
    client = _client_for(env["store"], OWNER)
    assert client.post("/api/scheduled-jobs", json=WEEKLY_JOB).status_code == 200
    resp = client.post("/api/onboarding/schedule", json={"channels": ["email"]})
    assert resp.status_code == 200
    assert resp.get_json()["job_id"]


def test_reactivation_is_gated_after_downgrade(env):
    account_id = env["store"].find_by_email(OWNER)["id"]
    _subscribe(account_id, "pro")
    client = _client_for(env["store"], OWNER)
    job_id = client.post("/api/scheduled-jobs", json=WEEKLY_JOB).get_json()["job_id"]
    client.post("/api/scheduled-jobs/update", json={"id": job_id, "active": False})

    _subscribe(account_id, "basic")  # the downgrade
    resp = client.post("/api/scheduled-jobs/update",
                       json={"id": job_id, "active": True})
    assert resp.status_code == 402
    # Managing (deactivating / deleting) stays open for the downgraded account.
    assert client.post("/api/scheduled-jobs/update",
                       json={"id": job_id, "active": False}).status_code == 200


def test_fire_after_downgrade_skips_and_records_plan_feature(env):
    account_id = env["store"].find_by_email(OWNER)["id"]
    _subscribe(account_id, "pro")
    job_id = metrics_store.create_scheduled_job(
        env["company_id"], "每周经营周报", "0 8 * * mon",
        goal_template="生成上周经营周报", delivery_channels=["email"], active=True)

    _subscribe(account_id, "basic")  # pro→basic with a still-active job
    job = metrics_store.get_scheduled_job(job_id)
    result = app._execute_scheduled_job(job)

    assert result["ran"] is False
    assert result["status"] == "skipped_plan_feature"
    assert result["run_id"] is None
    assert metrics_store.list_runs(env["company_id"]) == []  # nothing opened
    assert (metrics_store.get_scheduled_job(job_id)["last_status"]
            == "skipped_plan_feature")


# ---------------------------------------------------------------------------
# 3. B3 — POST /api/delivery/test
# ---------------------------------------------------------------------------
def test_delivery_test_requires_login(env):
    resp = app.app.test_client().post("/api/delivery/test",
                                      json={"channel": "email"})
    assert resp.status_code == 401


def test_delivery_test_wechat_without_webhook_is_friendly(env):
    client = _client_for(env["store"], OWNER)
    resp = client.post("/api/delivery/test", json={"channel": "wechat"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is False
    assert "未配置企业微信群机器人" in body["error"]


def test_delivery_test_failure_never_echoes_webhook(env, monkeypatch):
    secret_hook = "https://qyapi.weixin.example/cgi-bin/webhook/send?key=SECRET-KEY-123"
    metrics_store.merge_profile(env["company_id"], {"wechat_webhook": secret_hook})

    def _boom(req, timeout=15):
        raise OSError("connection refused")

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _boom)
    client = _client_for(env["store"], OWNER)
    resp = client.post("/api/delivery/test", json={"channel": "wechat"})
    body_text = resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is False and body["error"]
    for fragment in ("SECRET-KEY-123", "qyapi.weixin", secret_hook):
        assert fragment not in body_text


def test_delivery_test_email_without_smtp_is_friendly(env, monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    client = _client_for(env["store"], OWNER)
    resp = client.post("/api/delivery/test", json={"channel": "email"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"] == "SMTP 未配置"


def test_delivery_test_unknown_channel_is_400(env):
    client = _client_for(env["store"], OWNER)
    assert client.post("/api/delivery/test",
                       json={"channel": "pigeon"}).status_code == 400


# ---------------------------------------------------------------------------
# 4. B4 — the data inbox (数据收件箱)
# ---------------------------------------------------------------------------
def _upload(client, *files):
    return client.post(
        "/api/uploads",
        data={"files": [(io.BytesIO(content), name) for name, content in files]},
        content_type="multipart/form-data")


def test_uploads_require_login(env):
    anon = app.app.test_client()
    assert anon.get("/api/uploads/latest").status_code == 401
    assert anon.post("/api/uploads", json={"remove": ["x.csv"]}).status_code == 401


def test_uploads_get_shape_and_add(env):
    client = _client_for(env["store"], OWNER)
    # Empty inbox first — the shape is stable.
    empty = client.get("/api/uploads/latest").get_json()
    assert empty == {"batch": None, "batch_time": None, "files": []}

    resp = _upload(client, ("week1.csv", b"a,b\n1,2\n"))
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["batch"] and data["batch_time"]
    datetime.fromisoformat(data["batch_time"])  # parseable ISO
    assert [f["name"] for f in data["files"]] == ["week1.csv"]
    assert data["files"][0]["size"] > 0

    # GET answers the same staged batch.
    again = client.get("/api/uploads/latest").get_json()
    assert again["batch"] == data["batch"]
    assert [f["name"] for f in again["files"]] == ["week1.csv"]


def test_uploads_guards_oversize_and_executable(env, monkeypatch):
    client = _client_for(env["store"], OWNER)
    _upload(client, ("keep.csv", b"a,b\n1,2\n"))

    cap = config.MAX_UPLOAD_BYTES_PER_FILE
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES_PER_FILE", 8)
    resp = _upload(client, ("big.csv", b"0123456789ABCDEF"))
    assert resp.status_code == 400
    assert "上限" in resp.get_json()["error"]
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES_PER_FILE", cap)  # restore

    resp = _upload(client, ("evil.csv", b"\x7fELF\x02\x01\x01" + b"\x00" * 16))
    assert resp.status_code == 400
    assert "可执行程序" in resp.get_json()["error"]

    # A refused request leaves the staged batch untouched.
    files = client.get("/api/uploads/latest").get_json()["files"]
    assert [f["name"] for f in files] == ["keep.csv"]


def test_uploads_copy_forward_and_remove(env):
    client = _client_for(env["store"], OWNER)
    _upload(client, ("week1.csv", b"a,b\n1,2\n"))
    # Adding a SECOND file must keep the first (a new batch is created and the
    # previous batch's files are copied forward — the critical semantics).
    data = _upload(client, ("week2.csv", b"a,b\n3,4\n")).get_json()
    assert sorted(f["name"] for f in data["files"]) == ["week1.csv", "week2.csv"]

    # Removal: copy-forward minus the removed name (JSON body, no files).
    resp = client.post("/api/uploads", json={"remove": ["week1.csv"]})
    assert resp.status_code == 200
    assert [f["name"] for f in resp.get_json()["files"]] == ["week2.csv"]

    # Nothing to do is a 400, not a silent empty batch.
    assert client.post("/api/uploads", json={}).status_code == 400


def test_uploads_preserve_cjk_names_and_block_traversal(env):
    # File names are the inbox's load-bearing info: 六月销售明细.csv must list
    # under its real name (werkzeug's secure_filename would mangle it to "csv",
    # colliding every Chinese-named file)…
    client = _client_for(env["store"], OWNER)
    data = _upload(client, ("六月销售明细.csv", b"a,b\n1,2\n")).get_json()
    assert [f["name"] for f in data["files"]] == ["六月销售明细.csv"]
    # …while traversal attempts still flatten to a plain basename.
    data = _upload(client, ("../../evil.csv", b"x\n")).get_json()
    names = {f["name"] for f in data["files"]}
    assert "evil.csv" in names and not any("/" in n or ".." in n for n in names)


def test_uploads_total_cap_counts_carried_files(env, monkeypatch):
    client = _client_for(env["store"], OWNER)
    monkeypatch.setattr(config, "MAX_UPLOAD_FILES_PER_RUN", 2)
    _upload(client, ("a.csv", b"x\n"), ("b.csv", b"y\n"))
    resp = _upload(client, ("c.csv", b"z\n"))   # carry 2 + new 1 > cap 2
    assert resp.status_code == 400
    assert "最多保留" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# 5. Hardening — the customer-safe feed, the reports payload, the 401 probe
# ---------------------------------------------------------------------------
def _owned_job_with_machinery(env, job_id="job5feed"):
    memory = SharedMemory(job_id, store_dir=env["mem_dir"], goal="生成上周经营周报")
    memory.set_company_id(env["company_id"])
    memory.log("system", "Job created. Goal logged. 1 file(s) attached.", type="info")
    memory.log("token_manager", "Budget check passed (¥12.30 left).", type="info")
    memory.log("report_writer",
               "Received COMPANY MEMORY block (1234 chars). First 200 chars: "
               "【内部提示词内容】历史指标与口径……", type="info")
    memory.log("data_analyst",
               "Token budget reached — stopping before spending more. budget=agent_team",
               type="warning")
    memory.log("report_writer", "报告撰写完成。", type="info")
    return memory


def test_status_feed_is_filtered_for_customers_but_raw_for_admin(env, monkeypatch):
    _owned_job_with_machinery(env)
    client = _client_for(env["store"], OWNER)

    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    snap = client.get("/api/status/job5feed").get_json()
    msgs = [a["message"] for a in snap["activity"]]
    agents = [a["agent"] for a in snap["activity"]]
    assert "token_manager" not in agents
    assert not any(m.startswith("Received COMPANY MEMORY") for m in msgs)
    assert not any("Token budget reached" in m for m in msgs)
    assert any("已达本次生成上限" in m for m in msgs)   # the friendly replacement
    assert "报告撰写完成。" in msgs                      # real progress survives

    # The founder (is_admin) sees the raw machinery feed — same job, same owner.
    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    snap = client.get("/api/status/job5feed").get_json()
    msgs = [a["message"] for a in snap["activity"]]
    agents = [a["agent"] for a in snap["activity"]]
    assert "token_manager" in agents
    assert any(m.startswith("Received COMPANY MEMORY") for m in msgs)
    assert any(m.startswith("Token budget reached") for m in msgs)


def test_reports_list_is_customer_safe(env):
    rid_with = metrics_store.create_run(env["company_id"], "生成上周经营周报")
    metrics_store.finish_run(rid_with, RUN_SUCCEEDED, total_cost_yuan=9.87,
                             report_path="job5feed")
    _owned_job_with_machinery(env)  # writes the job5feed snapshot
    rid_without = metrics_store.create_run(env["company_id"], "历史数据导入")
    metrics_store.finish_run(rid_without, RUN_SUCCEEDED, report_path="")

    client = _client_for(env["store"], OWNER)
    rows = client.get("/api/reports").get_json()["reports"]
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        assert "total_cost_yuan" not in r   # internal COGS never leaves
        assert "report_path" not in r       # raw job ids never leave
        assert "has_content" in r
    assert by_id[rid_with]["has_content"] is True
    assert by_id[rid_without]["has_content"] is False


def test_status_of_missing_job_is_401_anonymously(env):
    # Anonymous probes can't distinguish existing from missing job ids: login
    # comes first (the /api/files semantics), so a missing id answers 401…
    anon = app.app.test_client()
    assert anon.get("/api/status/no-such-job").status_code == 401
    # …and a logged-in session still gets the honest 404.
    client = _client_for(env["store"], OWNER)
    assert client.get("/api/status/no-such-job").status_code == 404


def test_corrupt_snapshot_degrades_to_friendly_404(env):
    rid = metrics_store.create_run(env["company_id"], "生成上周经营周报")
    metrics_store.finish_run(rid, RUN_SUCCEEDED, report_path="corruptjob1")
    os.makedirs(env["mem_dir"], exist_ok=True)
    with open(os.path.join(env["mem_dir"], "corruptjob1.json"), "w") as f:
        f.write("{ this is not json")
    client = _client_for(env["store"], OWNER)
    resp = client.get("/api/reports/%s" % rid)
    assert resp.status_code == 404
    assert "报告内容不可用" in (resp.get_json() or {}).get("error", "")

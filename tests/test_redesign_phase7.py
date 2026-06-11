"""UI redesign Phase 7 — 账户与套餐 + billing alignment acceptance tests
(plan §3.8, §6 B8 + B4, §7 Phase 7) plus the Phase-6 carryover hardening.

Part A — carryover hardening:
  A1. Non-finite metric guard: a metric row whose value parses to NaN/Infinity
      (float() happily accepts "NaN"/"Infinity"/"1e400") is dropped at write
      time, and /api/company-brain/summary stays valid JSON.
  A2. The import endpoints are rate-limited like every other write route, and
      the estimate's temp upload dir is deleted (it used to leave 12×25MB per
      call behind — a logged-in disk-DoS).
  A3. brain summary: a DB failure answers 503, never 200-with-zeros (a company
      with months of data must not see the 等待第一份报告 empty state).
  A4. A real import job opens its run with kind='import' — pinned so the B1
      quota fix can't silently regress.
  A5. The wechat webhook host is re-validated at SEND time (a row saved before
      the E3 save-time check must not be an SSRF sink), and budget_cny (the
      internal cost cap) is founder-only on WRITE as well as read.

Part B — B8 billing realignment (added in the B8 commit):
      subscribers are gated by plan quota, never the invisible ¥ balance;
      free accounts keep balance-as-cost-cap; past_due stays locked;
      internal cost is still recorded for subscriber runs.

Part C — B4 单次 ¥29 (added in the B4 commit): a paid single_report order
      grants one report credit that raises the effective quota; the credit is
      consumed by the run that exceeds the base quota; refunds decrement and
      are refused once consumed.

Same isolation pattern as the phase-5/6 suites: throwaway SQLite per test,
fresh CustomerStore wired into app.CUSTOMERS, storage dirs in tmp.
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
from agent_team.models import (  # noqa: E402
    RUN_KIND_IMPORT, RUN_SUCCEEDED, SUB_ACTIVE, Subscription)

OWNER = "owner@corp.com"
OTHER = "other@corp.com"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh DB + customer store + tmp storage dirs; returns shared handles."""
    db.init_db(str(tmp_path / "phase7.db"))
    monkeypatch.setattr(app, "MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(app, "UPLOAD_DIR", str(tmp_path / "rawuploads"))
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
            "upload_dir": str(tmp_path / "rawuploads")}


def _client_for(store, email):
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return client


def _subscribe(account_id, plan_code, period_end=None):
    now = datetime.utcnow()
    with db.session_scope() as s:
        sub = (s.query(Subscription)
               .filter(Subscription.account_id == str(account_id)).one_or_none())
        if sub is None:
            s.add(Subscription(account_id=str(account_id), plan_code=plan_code,
                               status=SUB_ACTIVE, current_period_start=now,
                               current_period_end=period_end or (now + timedelta(days=30)),
                               cancel_at_period_end=False,
                               created_at=now, updated_at=now))
        else:
            sub.plan_code = plan_code
            sub.status = SUB_ACTIVE
            sub.current_period_end = period_end or (now + timedelta(days=30))


def _set_balance(store, email, amount):
    """Force an account's ¥ balance (the invisible internal cost cap)."""
    from agent_team.models import Account
    acc_id = store.find_by_email(email)["id"]
    with db.session_scope() as s:
        s.get(Account, acc_id).balance_yuan = amount


class _StaticLLM:
    """A network-free llm whose complete() always returns `reply`."""

    def __init__(self, reply="[]"):
        self.reply = reply
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        return self.reply


# ===========================================================================
# Part A — Phase-6 carryover hardening
# ===========================================================================
def test_coerce_float_rejects_non_finite_values():
    # float() parses all of these; storing any of them makes jsonify emit a
    # bare NaN/Infinity token — invalid JSON that blanks the brain page (A1).
    for bad in ("NaN", "Infinity", "-Infinity", "1e400", float("nan"),
                float("inf"), float("-inf")):
        assert metrics_store._coerce_float(bad) is None, bad
    assert metrics_store._coerce_float("12,345.5") == 12345.5
    assert metrics_store._coerce_float("33%") == 33.0


def test_infinity_metric_row_is_skipped_and_summary_stays_valid_json(env):
    cid = env["company_id"]
    rid = metrics_store.create_run(cid, "weekly report")
    reply = json.dumps([
        {"metric_name": "total_sales", "metric_name_cn": "总销售额",
         "value": "Infinity", "unit": "CNY", "period_label": "2026-W23"},
        {"metric_name": "orders", "metric_name_cn": "订单数",
         "value": 120, "unit": "orders", "period_label": "2026-W23"},
    ])
    inserted = metrics_store.extract_metrics_to_db(
        "ANALYSIS", cid, rid, llm=_StaticLLM(reply))
    assert inserted == 1  # the Infinity row was dropped at write time

    client = _client_for(env["store"], OWNER)
    resp = client.get("/api/company-brain/summary")
    assert resp.status_code == 200
    body = resp.get_json()          # would be None if the payload were invalid JSON
    assert body is not None
    assert body["metrics_count"] == 1


def test_import_endpoints_are_rate_limited():
    # flask-limiter marks decorated views; the two import routes were the only
    # remaining unthrottled write routes (A2).
    marked = {str(n) for n in app.limiter._marked_for_limiting}
    assert any("brain_import_estimate" in n for n in marked)
    assert any("brain_import_start" in n for n in marked)


def test_import_estimate_cleans_up_its_temp_dir(env):
    client = _client_for(env["store"], OWNER)
    resp = client.post(
        "/api/company-brain/import/estimate",
        data={"files": [(io.BytesIO("date,sales\n2026-06-01,100\n".encode()),
                         "六月销售明细.csv")]},
        content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.get_json()["file_count"] == 1
    leftovers = [d for d in os.listdir(env["upload_dir"])
                 if d.startswith("import_")] if os.path.isdir(env["upload_dir"]) else []
    assert leftovers == []  # nothing left behind per estimate call (A2)


def test_brain_summary_db_failure_is_503_not_fake_zeros(env, monkeypatch):
    client = _client_for(env["store"], OWNER)

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(app.metrics_store, "get_or_create_company", _boom)
    resp = client.get("/api/company-brain/summary")
    assert resp.status_code == 503  # never 200 + zeros (A3)
    assert "暂时无法加载" in resp.get_json()["error"]


def test_import_job_opens_run_with_kind_import(env):
    # Pins B1: deleting kind=RUN_KIND_IMPORT at the call site used to leave the
    # suite green while imports silently ate the quota again (A4).
    cid = env["company_id"]
    acc_id = env["store"].find_by_email(OWNER)["id"]
    job = {"job_id": "t1", "company_id": cid, "status": "running",
           "total": 1, "done": 0, "cost": 0.0,
           "files": [{"name": "a.csv", "status": "pending", "metrics": 0}]}
    app._run_import_job(job, acc_id, cid, [("a.csv", "date,sales\n1,100\n")],
                        llm=_StaticLLM("[]"))
    assert job["status"] == "done"
    import_runs = metrics_store.list_runs(cid, kind=RUN_KIND_IMPORT)
    assert len(import_runs) == 1
    assert metrics_store.list_runs(cid) == []          # not a report run
    assert metrics_store.count_runs_total(cid) == 0    # quota untouched


def test_wechat_send_revalidates_host_for_preexisting_rows(env, monkeypatch):
    # A row saved before the E3 save-time check (or written straight into the
    # DB) must not make the delivery-test button an SSRF cannon (A5).
    metrics_store.merge_profile(env["company_id"],
                                {"wechat_webhook": "https://evil.example.com/hook"})

    def _must_not_dial(*a, **k):
        raise AssertionError("urlopen must not be reached for an invalid host")

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _must_not_dial)
    client = _client_for(env["store"], OWNER)
    resp = client.post("/api/delivery/test", json={"channel": "wechat"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is False
    assert "evil.example.com" not in resp.get_data(as_text=True)


def test_budget_cny_write_is_founder_only(env, monkeypatch):
    acc_id = env["store"].find_by_email(OWNER)["id"]
    _subscribe(acc_id, "pro")  # scheduling is 企业版-only (D3)
    client = _client_for(env["store"], OWNER)

    payload = {"name_cn": "每周经营周报", "cron_expr": "0 8 * * mon",
               "delivery_channels": ["email"], "budget_cny": 0.01}
    resp = client.post("/api/scheduled-jobs", json=payload)
    assert resp.status_code == 200
    job_id = resp.get_json()["job_id"]
    assert metrics_store.get_scheduled_job(job_id)["budget_cny"] is None  # dropped

    # The update path drops it too.
    resp = client.post("/api/scheduled-jobs/update",
                       json={"id": job_id, "budget_cny": 0.01})
    assert resp.status_code == 200
    assert metrics_store.get_scheduled_job(job_id)["budget_cny"] is None

    # A founder session may set it.
    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    resp = client.post("/api/scheduled-jobs/update",
                       json={"id": job_id, "budget_cny": 3.5})
    assert resp.status_code == 200
    assert metrics_store.get_scheduled_job(job_id)["budget_cny"] == 3.5

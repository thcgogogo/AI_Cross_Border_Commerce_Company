"""UI redesign Phase 10 — final polish + Definition-of-Done hardening.

What this suite pins down:

  A6. The dead "余额不足" customer delivery path is RETIRED. The old branch
      pushed "余额不足，本次周报未生成" to the customer's 企业微信/邮箱 —
      machinery copy (post-B8 customers have no visible ¥ balance to top up).
      Post-D3 the branch is unreachable (free accounts never pass the
      scheduled_delivery feature gate), but the gate order has changed once
      already — so the behaviour is pinned: a fire refused by the billing
      gate records its distinct skip status and delivers NOTHING over any
      channel; the notice helpers are gone from the delivery module.

(A5 — the /api/me per-model identity strip — is asserted as an extension of
the phase-8 A2 test, next to the original strip assertions.)

Same isolation pattern as the phase-5/8 suites: a fresh CustomerStore wired
into app.CUSTOMERS, a throwaway SQLite DB, storage dirs redirected into tmp.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot is one-time & idempotent
from agent_team import db, delivery, metrics_store  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.memory import SharedMemory  # noqa: E402
from agent_team.models import SUB_ACTIVE, Subscription  # noqa: E402

OWNER = "owner@corp.com"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh DB + customer store + tmp storage dirs; returns shared handles."""
    db.init_db(str(tmp_path / "phase10.db"))
    monkeypatch.setattr(app, "MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(metrics_store, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(metrics_store, "REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("ADMIN_EMAILS", "")
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    store.create_customer([], name="Owner", starting_balance=1000.0,
                          email=OWNER, password="password1")
    monkeypatch.setattr(app, "CUSTOMERS", store)
    company_id = metrics_store.get_or_create_company(OWNER, company_name="OwnerCo")
    return {"store": store, "company_id": company_id,
            "mem_dir": str(tmp_path / "mem")}


def _subscribe(account_id, plan_code):
    """Activate a subscription directly (the paid path is covered by phase 9)."""
    now = datetime.utcnow()
    with db.session_scope() as s:
        s.add(Subscription(account_id=account_id, plan_code=plan_code,
                           status=SUB_ACTIVE, current_period_start=now,
                           current_period_end=now + timedelta(days=30)))


# ===========================================================================
# A6 — a billing-gated scheduled fire records its skip, delivers NOTHING
# ===========================================================================
def test_gated_fire_records_skip_and_delivers_nothing(env, monkeypatch):
    account_id = env["store"].find_by_email(OWNER)["id"]
    _subscribe(account_id, "pro")            # passes the D3 feature gate
    job_id = metrics_store.create_scheduled_job(
        env["company_id"], "每周经营周报", "0 8 * * mon",
        goal_template="生成上周经营周报",
        delivery_channels=["email", "wechat"], active=True)

    sent = []
    monkeypatch.setattr(delivery, "send_email",
                        lambda *a, **k: sent.append("email"))
    monkeypatch.setattr(delivery, "send_wechat_webhook",
                        lambda *a, **k: sent.append("wechat"))
    # Post-B8 a pro account cannot organically hit "no_balance" — force the
    # gate code to pin what happens IF the gate order ever regresses.
    monkeypatch.setattr(app, "_plan_gate",
                        lambda customer: (False, "免费体验额度已用完。", "no_balance"))

    result = app._execute_scheduled_job(metrics_store.get_scheduled_job(job_id))

    assert result["ran"] is False
    assert result["run_id"] is None
    assert result["status"] == "skipped_no_balance"    # distinct + auditable
    assert (metrics_store.get_scheduled_job(job_id)["last_status"]
            == "skipped_no_balance")
    assert sent == []                                  # nothing reached a customer
    assert metrics_store.list_runs(env["company_id"]) == []


def test_quota_gated_fire_still_skips_silently(env, monkeypatch):
    # The sibling branch (locked / quota) keeps its own last_status — and is
    # equally silent toward the customer.
    account_id = env["store"].find_by_email(OWNER)["id"]
    _subscribe(account_id, "pro")
    job_id = metrics_store.create_scheduled_job(
        env["company_id"], "每周经营周报", "0 8 * * mon",
        goal_template="生成上周经营周报", delivery_channels=["email"], active=True)

    sent = []
    monkeypatch.setattr(delivery, "send_email",
                        lambda *a, **k: sent.append("email"))
    monkeypatch.setattr(app, "_plan_gate",
                        lambda customer: (False, "本月报告额度已用完。", "quota"))

    result = app._execute_scheduled_job(metrics_store.get_scheduled_job(job_id))

    assert result["status"] == "skipped_plan_limit"
    assert sent == []


def test_insufficient_balance_machinery_is_gone_from_delivery():
    # The module no longer carries the machinery copy or its sender — the
    # only way "余额不足" could reach a customer again is by re-adding code.
    assert not hasattr(delivery, "INSUFFICIENT_BALANCE_MSG")
    assert not hasattr(delivery, "notify_insufficient_balance")


# ===========================================================================
# DoD sweep — /api/meta machinery is founder-only
# ===========================================================================
def _client_for(store, email):
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return client


def test_api_meta_strips_catalog_machinery_for_customers(env):
    # The DoD grep sweep (§11: no "token"/"/1M" in any customer payload)
    # caught /api/meta serving the model-function catalog ("per 1M tokens"
    # unit labels), the US$→¥ rate, and START_BALANCE_CNY — the trial's
    # internal cost cap the brief says is NEVER displayed. Only the founder
    # 模型与定价 page consumes them.
    for client in (app.app.test_client(),                 # anonymous
                   _client_for(env["store"], OWNER)):     # customer session
        resp = client.get("/api/meta")
        meta = resp.get_json()
        for field in ("kinds", "kind_order", "usd_to_cny", "start_balance_cny"):
            assert field not in meta, field
        raw = resp.get_data(as_text=True)
        for needle in ("token", "1M", "start_balance"):
            assert needle not in raw, needle
        # What every surface legitimately needs survives.
        assert meta["product_name"]
        assert meta["currency_symbol"]
        assert meta["report_goal_templates"]


def test_api_meta_keeps_catalog_for_founder(env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    meta = _client_for(env["store"], OWNER).get("/api/meta").get_json()
    for field in ("kinds", "kind_order", "usd_to_cny", "start_balance_cny"):
        assert field in meta, field
    assert meta["kinds"]["text"]["callable"] is True


# ===========================================================================
# DoD sweep — a failed run's raw provider error never reaches a customer
# ===========================================================================
RAW_ERROR = ("LLMError: Claude API error (200): {'type': 'error', 'error': "
             "{'type': 'overloaded_error', 'message': 'Overloaded'}, "
             "'request_id': 'req_011Cxxxx'}")


def _failed_job(env, job_id="job10err"):
    memory = SharedMemory(job_id, store_dir=env["mem_dir"], goal="生成上周经营周报")
    memory.set_company_id(env["company_id"])
    memory.log("system", "Job created. Goal logged. 1 file(s) attached.", type="info")
    memory.log("report_writer", "Hit an error: %s" % RAW_ERROR, type="error")
    memory.set_error(RAW_ERROR)
    return memory


def test_run_error_is_friendly_for_customers_raw_for_founder(env, monkeypatch):
    # Found live in the Phase-10 DoD run: a transient upstream overload put
    # "LLMError: Claude API error … overloaded_error … request_id" straight
    # into the customer's 生成出错 banner — provider machinery on a customer
    # surface. Customers now get one friendly line; the founder keeps the raw.
    _failed_job(env)
    client = _client_for(env["store"], OWNER)

    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    resp = client.get("/api/status/job10err")
    snap = resp.get_json()
    raw = resp.get_data(as_text=True)
    assert snap["status"] == "error"
    assert "请稍后" in snap["error"]                      # friendly + actionable
    for needle in ("Claude", "LLMError", "overloaded", "request_id", "API"):
        assert needle not in raw, needle                 # devtools-clean
    assert any("临时问题" in a["message"] for a in snap["activity"])

    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    snap = client.get("/api/status/job10err").get_json()
    assert snap["error"] == RAW_ERROR                    # founder debugs with the raw
    assert any("Claude API error" in a["message"] for a in snap["activity"])

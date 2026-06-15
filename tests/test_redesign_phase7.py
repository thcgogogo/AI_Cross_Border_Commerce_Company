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


@pytest.fixture(autouse=True)
def _enable_report_quota(monkeypatch):
    # Report-delivery pricing is opt-in since 2026-06-15 (REPORT_QUOTA_ENABLED, default
    # OFF). This module verifies the quota/credit/lock gate, so enable it for every test.
    monkeypatch.setenv("REPORT_QUOTA_ENABLED", "1")


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


# ===========================================================================
# Part B — B8 billing realignment: subscribers quota-gated, never balance-gated
# ===========================================================================
def test_subscriber_with_zero_balance_can_still_start_a_run(env):
    # THE B8 acceptance: a paying customer must never hit a 402 because the
    # invisible demo credit ran out. The empty-goal 400 proves every billing
    # gate let the request through (same trick as the Phase 9 suite).
    acc_id = env["store"].find_by_email(OWNER)["id"]
    _subscribe(acc_id, "basic")
    _set_balance(env["store"], OWNER, 0)
    client = _client_for(env["store"], OWNER)
    resp = client.post("/api/run", data={"goal": ""})
    assert resp.status_code == 400  # past the gates; refused only for no goal


def test_free_account_with_spent_balance_is_blocked_without_topup_talk(env):
    # FREE accounts keep the balance gate — it is the trial's invisible cost
    # cap — but the refusal sells the upgrade and never mentions ¥/充值.
    _set_balance(env["store"], OWNER, 0)
    client = _client_for(env["store"], OWNER)
    resp = client.post("/api/run", data={"goal": "做个经营周报"})
    assert resp.status_code == 402
    msg = resp.get_json()["error"]
    assert "升级套餐" in msg
    for machinery in ("余额", "充值", "Top up", "¥"):
        assert machinery not in msg


def test_subscriber_run_cost_is_recorded_but_balance_untouched(env):
    acc_id = env["store"].find_by_email(OWNER)["id"]
    _subscribe(acc_id, "pro")
    _set_balance(env["store"], OWNER, 0)

    bill = app._make_biller(acc_id)
    bill(config.HAIKU, 100_000, 20_000)   # must NOT raise despite ¥0 balance

    acct = env["store"].get_by_id(acc_id)
    assert acct["balance"] == 0.0         # flat plan: balance never decremented
    assert acct["spent"] > 0              # ...but COGS is still fully metered
    assert acct["requests"] == 1
    assert acct["usage"], "the usage log keeps recording subscriber calls"


def test_free_account_biller_still_cuts_off_at_zero(env):
    from agent_team.token_manager import BudgetExceededError
    acc_id = env["store"].find_by_email(OWNER)["id"]  # no subscription → free
    _set_balance(env["store"], OWNER, 0.01)

    bill = app._make_biller(acc_id)
    with pytest.raises(BudgetExceededError):
        bill(config.HAIKU, 1_000_000, 200_000)  # drains the ¥0.01 cap
    assert env["store"].get_by_id(acc_id)["balance"] == 0.0  # clamped, never negative


def test_past_due_subscriber_with_leftover_balance_stays_locked(env):
    # past_due → locked is PRESERVED by B8 (the Phase 9 suite pins the same
    # behavior end-to-end through the daily tick; this is the direct gate check).
    acc_id = env["store"].find_by_email(OWNER)["id"]
    _subscribe(acc_id, "basic",
               period_end=datetime.utcnow() - timedelta(days=10))  # past grace
    _set_balance(env["store"], OWNER, 100.0)  # leftover ¥ must not unlock
    client = _client_for(env["store"], OWNER)
    resp = client.post("/api/run", data={"goal": "做个经营周报"})
    assert resp.status_code == 402
    assert "订阅已逾期" in resp.get_json()["error"]


def test_import_start_no_longer_blocked_by_subscriber_zero_balance(env, monkeypatch):
    acc_id = env["store"].find_by_email(OWNER)["id"]
    _subscribe(acc_id, "basic")
    _set_balance(env["store"], OWNER, 0)
    monkeypatch.setattr(app, "ModelRouter",
                        lambda **kw: _StaticLLM("[]"))  # no real provider call
    client = _client_for(env["store"], OWNER)
    resp = client.post(
        "/api/company-brain/import",
        data={"files": [(io.BytesIO(b"date,sales\n2026-01-01,5\n"), "q1.csv")]},
        content_type="multipart/form-data")
    assert resp.status_code == 200   # used to be 402 "余额不足，请先充值"
    assert resp.get_json()["job_id"]


def test_v1_gateway_stays_balance_gated(env):
    # The founder/API storefront path is deliberately UNCHANGED by B8: accessed
    # by key (not subscription), it still refuses a spent balance with 402 —
    # even when the same account holds an active subscription.
    out = env["store"].create_customer([config.HAIKU], name="Api",
                                       starting_balance=0.0,
                                       email="api@corp.com", password="password1")
    _subscribe(env["store"].find_by_email("api@corp.com")["id"], "pro")
    client = app.app.test_client()
    resp = client.post("/v1/chat/completions",
                       headers={"Authorization": "Bearer %s" % out["api_key"]},
                       json={"model": config.HAIKU,
                             "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 402
    assert resp.get_json()["error"]["code"] == "insufficient_balance"


# ===========================================================================
# Part C — B4 单次 ¥29: purchase → credit → run → consume; refund-safe
# ===========================================================================
def _pay(client, order_no, amount, times=1):
    """Deliver a properly SIGNED mock notify, `times` times (phase-9 pattern)."""
    from agent_team import payments
    fields = payments.MockProvider().notify_fields(order_no, amount)
    return [client.post("/pay/notify/mock", data=fields) for _ in range(times)]


def _finished_report_runs(cid, n):
    for _ in range(n):
        rid = metrics_store.create_run(cid, "生成上周经营周报")
        metrics_store.finish_run(rid, RUN_SUCCEEDED)


def test_single_report_purchase_grants_credit_run_then_consumes(env):
    cid = env["company_id"]
    acc_id = env["store"].find_by_email(OWNER)["id"]
    client = _client_for(env["store"], OWNER)

    # Free trial exhausted: 2 lifetime runs → the friendly 402 upsell.
    _finished_report_runs(cid, 2)
    resp = client.post("/api/run", data={"goal": "做个经营周报"})
    assert resp.status_code == 402
    assert "免费体验额度已用完" in resp.get_json()["error"]

    # Buy 单次 ¥29 — server-side price, mock-cashier flow unchanged.
    order = client.post("/api/pay/create", json={"type": "single_report"}).get_json()
    assert order["amount"] == config.SINGLE_REPORT_PRICE_YUAN == 29.0
    responses = _pay(client, order["order_no"], 29.0, times=3)  # replayed notify
    assert [r.status_code for r in responses] == [200, 200, 200]

    acct = env["store"].get_by_id(acc_id)
    assert acct["report_credits"] == 1          # exactly one, despite 3 notifies
    # The latent bug C3 guards against: a ¥29 order must NOT grant a 'basic'
    # subscription via the plan_code=None fallthrough.
    assert metrics_store.get_subscription(acc_id) is None
    # The balance was NOT credited either (it's not a topup).
    assert acct["balance"] == 1000.0

    # Quota gate now open (2 used < 2+1 effective): empty-goal 400 = gates passed.
    assert client.post("/api/run", data={"goal": ""}).status_code == 400

    # The 3rd counting run lands beyond the base quota → consumes the credit
    # AND is stamped credit_funded, so it stops counting against the base
    # quota (the metered total returns to 2).
    _finished_report_runs(cid, 1)
    assert env["store"].get_by_id(acc_id)["report_credits"] == 0
    assert metrics_store.count_runs_total(cid) == 2

    # One-shot: with the credit consumed the gate closes again.
    resp = client.post("/api/run", data={"goal": "做个经营周报"})
    assert resp.status_code == 402

    # A SECOND ¥29 purchase must work too — without the credit_funded stamp
    # the consumed run counted forever and this second purchase bought nothing.
    order2 = client.post("/api/pay/create", json={"type": "single_report"}).get_json()
    _pay(client, order2["order_no"], 29.0)
    assert client.post("/api/run", data={"goal": ""}).status_code == 400  # gate open
    _finished_report_runs(cid, 1)   # 4th report, rides the 2nd credit
    assert env["store"].get_by_id(acc_id)["report_credits"] == 0
    assert metrics_store.count_runs_total(cid) == 2   # still only base-quota runs
    assert client.post("/api/run", data={"goal": "做个经营周报"}).status_code == 402


def test_runs_within_base_quota_never_consume_credits(env):
    # Free user with 2 lifetime + 1 credit runs 3 TOTAL: the first two runs sit
    # inside the base quota and must not touch the purchased credit (C4).
    cid = env["company_id"]
    acc_id = env["store"].find_by_email(OWNER)["id"]
    client = _client_for(env["store"], OWNER)
    order = client.post("/api/pay/create", json={"type": "single_report"}).get_json()
    _pay(client, order["order_no"], 29.0)
    assert env["store"].get_by_id(acc_id)["report_credits"] == 1

    _finished_report_runs(cid, 1)
    assert env["store"].get_by_id(acc_id)["report_credits"] == 1  # run 1: untouched
    _finished_report_runs(cid, 1)
    assert env["store"].get_by_id(acc_id)["report_credits"] == 1  # run 2: untouched
    _finished_report_runs(cid, 1)
    assert env["store"].get_by_id(acc_id)["report_credits"] == 0  # run 3: consumed


def test_failed_run_never_consumes_a_credit(env):
    from agent_team.models import RUN_FAILED
    cid = env["company_id"]
    acc_id = env["store"].find_by_email(OWNER)["id"]
    client = _client_for(env["store"], OWNER)
    order = client.post("/api/pay/create", json={"type": "single_report"}).get_json()
    _pay(client, order["order_no"], 29.0)

    _finished_report_runs(cid, 2)
    rid = metrics_store.create_run(cid, "生成上周经营周报")
    metrics_store.finish_run(rid, RUN_FAILED)   # the 3rd run FAILED
    assert env["store"].get_by_id(acc_id)["report_credits"] == 1  # ¥29 kept


def test_single_report_refund_decrements_and_blocks_when_consumed(env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", OTHER)
    cid = env["company_id"]
    acc_id = env["store"].find_by_email(OWNER)["id"]
    client = _client_for(env["store"], OWNER)
    admin = _client_for(env["store"], OTHER)

    # Unconsumed credit: partial refunds refused, full refund claws it back.
    order1 = client.post("/api/pay/create", json={"type": "single_report"}).get_json()
    _pay(client, order1["order_no"], 29.0)
    r = admin.post("/admin/orders", data={"action": "refund",
                                          "order_no": order1["order_no"],
                                          "amount": "10"})
    assert r.status_code == 400
    assert "全额退款" in r.get_data(as_text=True)
    r = admin.post("/admin/orders", data={"action": "refund",
                                          "order_no": order1["order_no"],
                                          "amount": "29"})
    assert r.status_code == 200
    assert env["store"].get_by_id(acc_id)["report_credits"] == 0
    assert metrics_store.get_order(order1["order_no"])["status"] == "refunded"

    # Consumed credit: the refund is refused with the 已被消费-style error.
    order2 = client.post("/api/pay/create", json={"type": "single_report"}).get_json()
    _pay(client, order2["order_no"], 29.0)
    _finished_report_runs(cid, 3)   # 2 base + 1 credit → credit consumed
    assert env["store"].get_by_id(acc_id)["report_credits"] == 0
    r = admin.post("/admin/orders", data={"action": "refund",
                                          "order_no": order2["order_no"],
                                          "amount": "29"})
    assert r.status_code == 400
    assert "已被消费" in r.get_data(as_text=True)
    assert metrics_store.get_order(order2["order_no"])["status"] == "paid"


def test_migration_0006_adds_report_credits_to_legacy_accounts(tmp_path):
    """Simulate a database deployed before Phase 7 (accounts WITHOUT
    report_credits), stamped at 0005, then run the real `alembic upgrade head`
    and inspect what arrived (the 0005 test's pattern)."""
    import sqlite3
    import subprocess
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE accounts (
            id VARCHAR(32) PRIMARY KEY, email VARCHAR(320), phone VARCHAR(32),
            name VARCHAR(255), company VARCHAR(255), password_hash VARCHAR(255),
            key_hash VARCHAR(64), key_prefix VARCHAR(32), key_last4 VARCHAR(8),
            models_json TEXT, balance_yuan NUMERIC(12,2),
            spent_yuan NUMERIC(12,2) NOT NULL, requests INTEGER NOT NULL,
            usage_json TEXT, status VARCHAR(9) NOT NULL,
            rpm_limit INTEGER NOT NULL, daily_cap_yuan NUMERIC(12,2),
            session_token VARCHAR(32), created_at DATETIME NOT NULL);
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, goal_text TEXT,
            started_at DATETIME NOT NULL, finished_at DATETIME,
            status VARCHAR(14) NOT NULL,
            kind VARCHAR(32) NOT NULL DEFAULT 'report',
            total_cost_yuan FLOAT, report_path VARCHAR(255));
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
        INSERT INTO alembic_version VALUES ('0005_phase6_run_kind');
        INSERT INTO accounts (id, email, spent_yuan, requests, status,
                              rpm_limit, created_at)
            VALUES ('cus_legacy01', 'legacy@corp.com', 0, 0, 'active', 60,
                    '2026-01-01');
        INSERT INTO runs (company_id, goal_text, started_at, status)
            VALUES (1, '生成上周经营周报', '2026-01-03', 'succeeded');
    """)
    conn.commit()
    conn.close()

    # Pin to the 0006 revision (not "head") — this test verifies 0006's effect
    # specifically, so it must not also pull in later migrations (0007+).
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0006_phase7_single_report"],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"})
    assert proc.returncode == 0, proc.stderr

    conn = sqlite3.connect(db_path)
    cols = {row[1]: row for row in conn.execute("PRAGMA table_info(accounts)")}
    assert "report_credits" in cols
    notnull, default = cols["report_credits"][3], cols["report_credits"][4]
    assert notnull == 1
    assert "0" in str(default)
    # runs.credit_funded arrives NOT NULL DEFAULT 0 and legacy rows read False.
    rcols = {row[1]: row for row in conn.execute("PRAGMA table_info(runs)")}
    assert "credit_funded" in rcols
    assert rcols["credit_funded"][3] == 1
    assert conn.execute("SELECT credit_funded FROM runs").fetchone()[0] == 0
    # The legacy account reads 0 credits, and the version landed at head (0006).
    assert conn.execute(
        "SELECT report_credits FROM accounts").fetchone()[0] == 0
    assert conn.execute(
        "SELECT version_num FROM alembic_version").fetchone()[0].startswith("0006")
    conn.close()


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

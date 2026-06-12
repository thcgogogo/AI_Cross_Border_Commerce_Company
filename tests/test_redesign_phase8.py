"""UI redesign Phase 8 — 新手引导 rework (6 steps, plan §3.10) + the Phase-7
carryover hardening, pinned in the order the phase shipped them:

Part A — carryover hardening:
  A1. Gate order: a FREE account holding a paid 单次报告 credit must never be
      refused by the invisible-balance gate (its run is credit-funded, not
      demo-funded), and a credit-funded free run must not draw the demo
      balance down — START_BALANCE_CNY meters genuine free-trial runs only.
      Import back-fills never ride a credit (the balance stays their cap).
  A2. /api/me machinery strip: a customer session's payload carries no ¥
      balance / key fragments / spent / requests / usage log / per-model
      pricing; the founder keeps the full view; the model-picker WRITE is
      refused 403 server-side for non-admins (it was client-gated only).
      Follow-up: /api/fusion (a /founder/* view whose response carries
      cost/balance/token counts) gets the same server-side 403 gate.
  A3. The machinery DATA endpoints get the same server-side gate: the
      /api/tokens family (raw platform ¥ COGS status + the spend-cap WRITES),
      /api/models (catalog with per-1M-token ¥ pricing), /api/providers and
      the /api/agents/model write all 403 non-founders — they had NO auth at
      all, only the client's isAdmin() hid them. /api/agents stays reachable
      (the customer feed needs the org chart) but serves it WITHOUT the
      per-agent model ids for non-founder sessions.

Part B — the six-step wizard backend:
  B1. Numbering: /api/onboarding/skip accepts 1..6 (7 is invalid), the state
      tracks six steps, and `done` requires action on step 6.
  B2. The NEW step ④ 导入历史数据 funnel event: recorded as step-4 COMPLETED
      only for wizard-context imports (onboarding=1) and only when the job
      finishes SUCCEEDED — a 企业大脑-page import writes no funnel rows, and a
      budget-stopped job records nothing (skips go through the skip route).
  B3. Funnel: step-6 completions are counted (the BETWEEN 1 AND 6 SQL) under
      the shifted labels.

Isolation matches the phase-7 suite: throwaway SQLite per test, fresh
CustomerStore wired into app.CUSTOMERS, storage dirs in tmp.
"""
import io
import sys
import time
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot is one-time & idempotent
from admin import queries  # noqa: E402
from agent_team import config, db, metrics_store, providers  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.models import (  # noqa: E402
    RUN_KIND_IMPORT, RUN_SUCCEEDED, Account)
from agent_team.token_manager import (  # noqa: E402
    BudgetExceededError, TokenManager)

OWNER = "owner@corp.com"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh DB + customer store + tmp storage dirs; returns shared handles."""
    db.init_db(str(tmp_path / "phase8.db"))
    monkeypatch.setattr(app, "MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(app, "UPLOAD_DIR", str(tmp_path / "rawuploads"))
    monkeypatch.setattr(metrics_store, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(metrics_store, "REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("ADMIN_EMAILS", "")   # nobody is a founder unless a test says so
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    store.create_customer([], name="Owner", starting_balance=1000.0,
                          email=OWNER, password="password1")
    monkeypatch.setattr(app, "CUSTOMERS", store)
    company_id = metrics_store.get_or_create_company(OWNER, company_name="OwnerCo")
    return {"store": store, "company_id": company_id}


def _client_for(store, email):
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return client


def _set_balance(store, email, amount):
    """Force an account's ¥ balance (the invisible internal cost cap)."""
    acc_id = store.find_by_email(email)["id"]
    with db.session_scope() as s:
        s.get(Account, acc_id).balance_yuan = amount


def _grant_credits(store, email, n):
    """Hand the account `n` unconsumed 单次报告 credits (the B4 wallet column),
    exactly as a paid ¥29 order would."""
    acc_id = store.find_by_email(email)["id"]
    with db.session_scope() as s:
        s.get(Account, acc_id).report_credits = n


def _finished_report_runs(cid, n):
    for _ in range(n):
        rid = metrics_store.create_run(cid, "生成上周经营周报")
        metrics_store.finish_run(rid, RUN_SUCCEEDED)


class _StaticLLM:
    """A network-free llm whose complete() always returns `reply`."""

    def __init__(self, reply="[]"):
        self.reply = reply

    def complete(self, **kwargs):
        return self.reply


class _BudgetBlownLLM:
    """An llm whose first call reports the account budget as spent."""

    def complete(self, **kwargs):
        raise BudgetExceededError("account", "Account balance is spent (¥0.00).")


# ===========================================================================
# Part A1 — gate order + credit-funded billing
# ===========================================================================
def test_free_account_with_credit_and_spent_balance_passes_gate(env):
    # The verified Phase-7 bug: trial exhausted (¥0 demo balance) + a PAID ¥29
    # credit in hand → the old no_balance gate fired first and refused the run
    # as "升级套餐", selling the customer an unredeemable credit.
    cid = env["company_id"]
    _finished_report_runs(cid, 2)                      # lifetime trial used up
    _set_balance(env["store"], OWNER, 0)
    _grant_credits(env["store"], OWNER, 1)

    customer = env["store"].find_by_email(OWNER)
    ok, msg, code = app._plan_gate(customer)
    assert ok is True and msg is None and code is None

    # Through HTTP: gates pass (empty-goal 400 = the 402s are behind us).
    client = _client_for(env["store"], OWNER)
    assert client.post("/api/run", data={"goal": ""}).status_code == 400


def test_free_account_without_credit_keeps_the_no_balance_gate(env):
    # Regression guard: the A1 reorder must not open the trial cost cap for
    # accounts with NO purchased credit.
    _set_balance(env["store"], OWNER, 0)
    customer = env["store"].find_by_email(OWNER)
    ok, msg, code = app._plan_gate(customer)
    assert ok is False and code == "no_balance"
    assert "升级套餐" in msg
    for machinery in ("余额", "充值", "¥"):
        assert machinery not in msg


def test_credit_exhausted_after_consumption_closes_the_gate_again(env):
    # Once the credit is consumed (3rd run beyond the 2-lifetime quota) the
    # gate closes again — and the refusal copy offers the repeat ¥29 purchase,
    # which A1 makes redeemable even on a spent trial balance.
    cid = env["company_id"]
    _set_balance(env["store"], OWNER, 0)
    _grant_credits(env["store"], OWNER, 1)
    _finished_report_runs(cid, 3)                      # 3rd consumed the credit
    assert env["store"].find_by_email(OWNER)["report_credits"] == 0
    ok, msg, code = app._plan_gate(env["store"].find_by_email(OWNER))
    assert ok is False and code == "no_balance"
    assert "免费体验额度已用完" in msg and "单次购买" in msg
    for machinery in ("余额", "充值", "¥"):
        assert machinery not in msg


def test_credit_funded_free_run_never_draws_demo_balance(env):
    # Trial exhausted, balance ¥5 left, one credit: the 3rd (credit-funded)
    # run is metered like a subscriber run — spent/usage recorded, the demo
    # balance UNTOUCHED, and no BudgetExceededError cut-off.
    cid = env["company_id"]
    acc_id = env["store"].find_by_email(OWNER)["id"]
    _finished_report_runs(cid, 2)
    _set_balance(env["store"], OWNER, 5.0)
    _grant_credits(env["store"], OWNER, 1)
    metrics_store.create_run(cid, "第三份周报")        # in-flight credit run

    bill = app._make_biller(acc_id)
    bill(config.HAIKU, 1_000_000, 200_000)             # would cost > ¥5

    acct = env["store"].get_by_id(acc_id)
    assert acct["balance"] == 5.0                      # the ¥ cap untouched
    assert acct["spent"] > 0 and acct["requests"] == 1 # COGS fully metered
    assert acct["usage"]


def test_credit_funded_run_at_zero_balance_is_not_cut_off(env):
    # The task's literal scenario: ¥0 invisible balance + 1 credit → the run
    # proceeds, is never cut off by the spent balance, and finish_run consumes
    # the credit (stamped credit_funded → quota meter back at the base).
    cid = env["company_id"]
    acc_id = env["store"].find_by_email(OWNER)["id"]
    _finished_report_runs(cid, 2)
    _set_balance(env["store"], OWNER, 0)
    _grant_credits(env["store"], OWNER, 1)
    rid = metrics_store.create_run(cid, "第三份周报")

    bill = app._make_biller(acc_id)
    bill(config.HAIKU, 100_000, 20_000)                # must NOT raise at ¥0

    metrics_store.finish_run(rid, RUN_SUCCEEDED)
    acct = env["store"].get_by_id(acc_id)
    assert acct["balance"] == 0.0
    assert acct["report_credits"] == 0                 # the ¥29 was redeemed
    assert metrics_store.count_runs_total(cid) == 2    # stamped credit_funded


def test_within_quota_free_run_still_charges_the_demo_balance(env):
    # An unconsumed credit must not turn the whole trial free: a run INSIDE
    # the 2-lifetime quota is demo-funded and keeps drawing the ¥ cap down.
    cid = env["company_id"]
    acc_id = env["store"].find_by_email(OWNER)["id"]
    _set_balance(env["store"], OWNER, 5.0)
    _grant_credits(env["store"], OWNER, 1)
    metrics_store.create_run(cid, "第一份周报")        # in-flight run 1 of 2

    bill = app._make_biller(acc_id)
    bill(config.HAIKU, 100_000, 20_000)

    assert env["store"].get_by_id(acc_id)["balance"] < 5.0


def test_import_backfill_never_rides_a_credit(env):
    # 历史数据导入 consumes no credit, so the free trial's balance stays its
    # cost cap: at ¥0 the import biller still cuts off immediately.
    acc_id = env["store"].find_by_email(OWNER)["id"]
    _set_balance(env["store"], OWNER, 0)
    _grant_credits(env["store"], OWNER, 1)
    _finished_report_runs(env["company_id"], 2)

    bill = app._make_biller(acc_id, run_kind=RUN_KIND_IMPORT)
    with pytest.raises(BudgetExceededError):
        bill(config.HAIKU, 100_000, 20_000)


# ===========================================================================
# Part A2 — /api/me machinery strip + founder-only model picker
# ===========================================================================
MACHINERY_FIELDS = ("balance", "spent", "requests", "usage",
                    "key_prefix", "key_last4")
MODEL_PRICING_FIELDS = ("input_per_mtok", "output_per_mtok", "price", "price_unit")


def test_api_me_strips_machinery_for_customer_sessions(env):
    env["store"].set_models(env["store"].find_by_email(OWNER)["id"],
                            [config.HAIKU])   # so pricing WOULD leak if unstripped
    client = _client_for(env["store"], OWNER)
    me = client.get("/api/me").get_json()["customer"]
    assert me["is_admin"] is False
    for field in MACHINERY_FIELDS:
        assert field not in me, field
    # Phase 10 A5 — beyond pricing, the model identity fields (raw model id,
    # label, provider, tier, context size) are stripped too; only display-kind
    # fields may survive on a customer session.
    for m in me["models"]:
        for field in MODEL_PRICING_FIELDS + ("id", "label", "provider",
                                              "tier", "context"):
            assert field not in m, field
        assert set(m) <= {"kind", "kind_label", "approx", "available"}
    # What the customer UI legitimately needs survives.
    assert me["email"] == OWNER
    assert me["report_credits"] == 0
    # Belt-and-braces: the raw body (what devtools shows) names none of it —
    # neither the field names nor the values (model id / provider name).
    raw = client.get("/api/me").get_data(as_text=True)
    for needle in MACHINERY_FIELDS + MODEL_PRICING_FIELDS + (
            config.HAIKU, "anthropic", "Haiku", '"provider"', '"tier"'):
        assert needle not in raw, needle


def test_api_me_keeps_full_payload_for_founder(env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    env["store"].set_models(env["store"].find_by_email(OWNER)["id"],
                            [config.HAIKU])
    client = _client_for(env["store"], OWNER)
    me = client.get("/api/me").get_json()["customer"]
    assert me["is_admin"] is True
    for field in MACHINERY_FIELDS:
        assert field in me, field
    assert me["balance"] == 1000.0
    assert any("input_per_mtok" in m for m in me["models"])


def test_login_response_is_stripped_for_customers_too(env):
    # The strip lives in _account_view, so EVERY session payload (login,
    # signup, /api/me) is covered — not just /api/me.
    client = app.app.test_client()
    resp = client.post("/api/login", json={"email": OWNER,
                                           "password": "password1"})
    assert resp.status_code == 200
    me = resp.get_json()["customer"]
    for field in MACHINERY_FIELDS:
        assert field not in me, field


def test_model_picker_write_is_founder_only(env, monkeypatch):
    acc_id = env["store"].find_by_email(OWNER)["id"]
    client = _client_for(env["store"], OWNER)

    resp = client.post("/api/account/models", json={"models": [config.HAIKU]})
    assert resp.status_code == 403
    assert env["store"].get_by_id(acc_id)["models"] == []   # nothing written

    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    resp = client.post("/api/account/models", json={"models": [config.HAIKU]})
    assert resp.status_code == 200
    assert env["store"].get_by_id(acc_id)["models"] == [config.HAIKU]


def test_fusion_endpoint_is_founder_only(env, monkeypatch):
    # The verified A2 gap: /api/fusion is a /founder/* view whose response
    # carries machinery (per-call ¥ cost, account balance, per-answer token
    # counts), yet a customer session whose /api/me was correctly stripped
    # still got {"balance": ..., "cost": ...} straight from this endpoint.
    calls = []

    def fake_gateway_chat(model_id, messages, max_tokens=1024):
        calls.append(model_id)
        # Token counts big enough that the to-the-fen debit is non-zero.
        return {"text": "fused answer", "input_tokens": 100_000,
                "output_tokens": 20_000, "model": model_id,
                "provider": "anthropic"}

    monkeypatch.setattr(providers, "gateway_chat", fake_gateway_chat)
    client = _client_for(env["store"], OWNER)
    payload = {"prompt": "1+1?", "models": [config.HAIKU]}

    resp = client.post("/api/fusion", json=payload)
    assert resp.status_code == 403
    raw = resp.get_data(as_text=True)
    for needle in ("balance", "cost", "input_tokens", "output_tokens"):
        assert needle not in raw, needle
    assert calls == []   # refused before any model ran — nothing billed
    assert env["store"].find_by_email(OWNER)["balance"] == 1000.0

    # The founder keeps the working page, full billing view included.
    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    resp = client.post("/api/fusion", json=payload)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["fused"]["text"] == "fused answer"
    assert body["cost"] > 0
    assert body["balance"] < 1000.0   # the founder's own account was billed


# ===========================================================================
# Part A3 — the machinery DATA endpoints get the same server-side gate
# ===========================================================================
# Every founder-console endpoint that until now relied on the customer UI's
# isAdmin() alone: the /api/tokens family is the raw platform ¥ COGS (and its
# POSTs MUTATE the shared spend cap), /api/models + /api/providers are the
# catalog (raw model ids, provider names, per-1M-token ¥ pricing), and the
# /api/agents/model write reassigns the whole team. (method, path, body).
MACHINERY_ENDPOINTS = (
    ("GET", "/api/tokens", None),
    ("POST", "/api/tokens/total", {"limit": 9999}),
    ("POST", "/api/tokens/budget", {"name": "intruder", "limit": 1}),
    ("POST", "/api/tokens/budget/remove", {"name": "research"}),
    ("POST", "/api/tokens/reset", None),
    ("GET", "/api/models", None),
    ("GET", "/api/providers", None),
    ("POST", "/api/agents/model", {"agent_id": "ceo", "model": config.HAIKU}),
)


@pytest.fixture
def machinery(env, tmp_path, monkeypatch):
    """Hermetic machinery state: app.TOKENS is a module-level singleton that
    persists to memory_store/tokens.json (built before `env` redirects
    MEMORY_DIR), and the /api/agents/model write lands in config.MODEL_OVERRIDES
    + MODEL_OVERRIDES_PATH — swap all three for throwaways so neither a test
    mutation nor a regressed gate can touch the repo's real files."""
    tm = TokenManager(store_path=str(tmp_path / "tokens.json"),
                      total_budget=500.0, task_budgets={"research": 50.0})
    monkeypatch.setattr(app, "TOKENS", tm)
    monkeypatch.setattr(app, "MODEL_OVERRIDES_PATH",
                        str(tmp_path / "model_overrides.json"))
    monkeypatch.setattr(config, "MODEL_OVERRIDES", {})
    return tm


def _fire(client, method, path, body):
    return client.get(path) if method == "GET" else client.post(path, json=body)


def _assert_all_refused(client, tokens):
    tokens.record_cost("research", 7.5)            # so a leaked reset is visible
    for method, path, body in MACHINERY_ENDPOINTS:
        resp = _fire(client, method, path, body)
        assert resp.status_code == 403, path
        raw = resp.get_data(as_text=True)
        for needle in ("limit", "used", "remaining", "per_mtok", "price",
                       "anthropic", config.HAIKU):
            assert needle not in raw, (path, needle)
    # ...and none of the refused WRITES landed.
    st = tokens.status()
    assert st["total"]["limit"] == 500.0           # /total didn't raise the cap
    assert st["total"]["used"] == 7.5              # /reset didn't zero the spend
    assert [b["name"] for b in st["budgets"]] == ["research"]  # add/remove inert
    assert config.MODEL_OVERRIDES == {}            # the ceo reassignment refused


def test_machinery_endpoints_refuse_anonymous_callers(machinery):
    # The verified exposure: NONE of these had auth — an anonymous request
    # could read the platform's ¥ COGS and reset/raise its spend cap.
    _assert_all_refused(app.app.test_client(), machinery)


def test_machinery_endpoints_refuse_customer_sessions(env, machinery):
    _assert_all_refused(_client_for(env["store"], OWNER), machinery)


def test_machinery_endpoints_serve_the_founder(env, machinery, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    client = _client_for(env["store"], OWNER)

    st = client.get("/api/tokens").get_json()      # the raw ¥ COGS view
    assert st["total"]["limit"] == 500.0
    assert [b["name"] for b in st["budgets"]] == ["research"]

    models = client.get("/api/models").get_json()  # full catalog, ¥ pricing on
    haiku = next(m for m in models if m["id"] == config.HAIKU)
    assert haiku["provider"] == "anthropic"
    assert haiku["input_per_mtok_cny"] > 0

    providers_rows = client.get("/api/providers").get_json()
    assert "anthropic" in [p["id"] for p in providers_rows]

    # The budget writes work — and actually land in the manager.
    assert client.post("/api/tokens/total",
                       json={"limit": 800}).get_json()["total"]["limit"] == 800.0
    assert machinery.status()["total"]["limit"] == 800.0
    assert client.post("/api/tokens/budget",
                       json={"name": "video", "limit": 30}).status_code == 200
    assert client.post("/api/tokens/budget/remove",
                       json={"name": "video"}).status_code == 200
    machinery.record_cost("research", 7.5)
    assert client.post("/api/tokens/reset").get_json()["total"]["used"] == 0.0

    # The team write works and returns the founder view (models included).
    team = client.post("/api/agents/model",
                       json={"agent_id": "ceo", "model": config.HAIKU}).get_json()
    assert {a["id"]: a["model"] for a in team}["ceo"] == config.HAIKU
    assert config.MODEL_OVERRIDES == {"ceo": config.HAIKU}


def test_api_agents_keeps_the_feed_fields_but_strips_model(env):
    # /api/agents stays reachable — the report feed (report_new.js /
    # onboarding.js → loadAgents) needs the org chart to label the live feed
    # 报告撰写人 instead of raw agent ids — but the per-agent model ids are
    # machinery and are stripped for anonymous and customer sessions alike.
    for client in (app.app.test_client(), _client_for(env["store"], OWNER)):
        resp = client.get("/api/agents")
        assert resp.status_code == 200
        rows = resp.get_json()
        assert {r["id"] for r in rows} == {a["id"] for a in config.AGENTS}
        for row in rows:
            assert set(row) == {"id", "name", "title", "name_cn", "reports_to"}
        raw = resp.get_data(as_text=True)
        for a in config.AGENTS:                    # no effective model id leaks
            assert config.effective_model(a["id"]) not in raw


def test_api_agents_keeps_model_for_founder(env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    rows = _client_for(env["store"], OWNER).get("/api/agents").get_json()
    assert {r["id"]: r["model"] for r in rows} == {
        a["id"]: config.effective_model(a["id"]) for a in config.AGENTS}


# ===========================================================================
# Part B1 — six-step numbering: skip bounds, state shape, done condition
# ===========================================================================
def test_skip_bounds_and_done_requires_step_six(env):
    client = _client_for(env["store"], OWNER)
    assert client.post("/api/onboarding/skip",
                       json={"step": 7}).status_code == 400
    assert client.post("/api/onboarding/skip",
                       json={"step": 0}).status_code == 400

    resp = client.post("/api/onboarding/skip", json={"step": 6})
    assert resp.status_code == 200
    state = resp.get_json()
    assert set(state["steps"].keys()) == {"1", "2", "3", "4", "5", "6"}
    assert state["done"] is True                 # step 6 acted on → wizard over


def test_done_is_false_until_step_six_acts(env):
    acc_id = env["store"].find_by_email(OWNER)["id"]
    for n in range(1, 6):                        # 1..5 completed, 6 untouched
        metrics_store.record_onboarding_event(acc_id, n, "completed")
    st = metrics_store.onboarding_state(acc_id)
    assert st["done"] is False
    assert st["next_step"] == 6


# ===========================================================================
# Part B2 — the import step's funnel event (wizard-context, succeeded-only)
# ===========================================================================
def _import_job(name="历史数据.csv"):
    return {"job_id": "t-imp", "company_id": None, "status": "running",
            "total": 1, "done": 0, "cost": 0.0,
            "files": [{"name": name, "status": "pending", "metrics": 0}]}


def test_wizard_import_records_step4_completed_on_success(env):
    acc_id = env["store"].find_by_email(OWNER)["id"]
    app._run_import_job(_import_job(), acc_id, env["company_id"],
                        [("历史数据.csv", "月份,销售额\n2026-01,100\n")],
                        llm=_StaticLLM("[]"), onboarding_step=True)
    st = metrics_store.onboarding_state(acc_id)
    assert st["steps"]["4"] == "completed"
    assert st["next_step"] == 1                  # the others stay untouched


def test_brain_page_import_writes_no_funnel_rows(env):
    # A later 企业大脑 import must not pollute the funnel: an account that
    # never opened the wizard would otherwise land in the started denominator.
    acc_id = env["store"].find_by_email(OWNER)["id"]
    app._run_import_job(_import_job(), acc_id, env["company_id"],
                        [("历史数据.csv", "月份,销售额\n2026-01,100\n")],
                        llm=_StaticLLM("[]"))    # no onboarding flag
    assert metrics_store.list_onboarding_events(acc_id) == []
    assert metrics_store.onboarding_state(acc_id)["started"] is False


def test_budget_stopped_wizard_import_records_nothing(env):
    # Funnel honesty (the generate-step pattern): only a FINISHED import
    # counts; a budget-stopped job leaves step 4 open for a retry or a skip.
    acc_id = env["store"].find_by_email(OWNER)["id"]
    job = _import_job()
    app._run_import_job(job, acc_id, env["company_id"],
                        [("历史数据.csv", "月份,销售额\n2026-01,100\n")],
                        llm=_BudgetBlownLLM(), onboarding_step=True)
    assert job["status"] == "stopped_budget"
    assert metrics_store.onboarding_state(acc_id)["steps"]["4"] is None


def test_wizard_import_event_flows_through_the_http_endpoint(env, monkeypatch):
    # The whole plumbing: the wizard's start request carries onboarding=1 →
    # the worker thread finishes → the step-4 event is committed. Customers
    # see no ¥ in the progress payload (cost is founder-only).
    monkeypatch.setattr(app, "ModelRouter", lambda **kw: _StaticLLM("[]"))
    client = _client_for(env["store"], OWNER)
    resp = client.post(
        "/api/company-brain/import",
        data={"files": [(io.BytesIO("月份,销售额\n2026-01,100\n".encode()), "去年明细.csv")],
              "onboarding": "1"},
        content_type="multipart/form-data")
    assert resp.status_code == 200
    job_id = resp.get_json()["job_id"]

    acc_id = env["store"].find_by_email(OWNER)["id"]
    body = None
    for _ in range(100):                          # ≤ 10s; the fake job is instant
        body = client.get("/api/company-brain/import/%s" % job_id).get_json()
        if body.get("status") != "running":
            break
        time.sleep(0.1)
    assert body and body["status"] == "done"
    assert "cost" not in body                     # ¥ never reaches a customer
    for _ in range(50):                           # the event commits with the job
        if metrics_store.onboarding_state(acc_id)["steps"]["4"] == "completed":
            break
        time.sleep(0.1)
    assert metrics_store.onboarding_state(acc_id)["steps"]["4"] == "completed"


# ===========================================================================
# Machinery sweep — the metric-extraction bookkeeping lines stay internal
# ===========================================================================
def test_metric_extraction_lines_filtered_from_customer_feed():
    # Caught by the Phase 8 live sweep: with history imported, the extraction
    # reply routinely hits the output-token limit and its salvage line (plus
    # the rest of the "metric extraction …" bookkeeping family) reached the
    # customer feed. Machinery never leaves the server for customers; the
    # deliberately-LOUD ⚠️ 指标提取失败 warning is product signal and stays.
    feed = [
        {"agent": "data_analyst", "type": "info",
         "message": ("metric extraction reply hit the output-token limit — "
                     "salvaging complete rows from the truncated array.")},
        {"agent": "data_analyst", "type": "info",
         "message": "metric extraction LLM call failed on attempt 1 (boom)."},
        {"agent": "data_analyst", "type": "info",
         "message": "metric extraction stored 22 metric(s) to the company brain."},
        {"agent": "data_analyst", "type": "result",
         "message": "本周销售额 ¥164,100【1】，环比 +3.5%。"},
        {"agent": "data_analyst", "type": "info",
         "message": metrics_store.METRIC_EXTRACTION_FAILURE_WARNING},
    ]
    safe = app._customer_safe_activity(feed)
    msgs = [a["message"] for a in safe]
    assert not any(m.startswith("metric extraction") for m in msgs)
    assert any("¥164,100" in m for m in msgs)      # report CONTENT is untouched
    assert any("指标提取失败" in m for m in msgs)  # the loud warning survives


# ===========================================================================
# Part B3 — the funnel counts step-6 completions under the shifted labels
# ===========================================================================
def test_funnel_counts_step_six_completions(env):
    metrics_store.record_onboarding_event("cus_x", 0, "started")
    metrics_store.record_onboarding_event("cus_x", 6, "completed")
    funnel = queries.onboarding_funnel()
    by_step = {s["step"]: s for s in funnel["steps"]}
    assert by_step[6]["completed"] == 1           # BETWEEN 1 AND 6 — not lost
    assert by_step[6]["label"] == "每周定时"
    assert by_step[5]["completed"] == 0 and by_step[5]["label"] == "首份报告"
    assert by_step[4]["label"] == "导入历史数据"

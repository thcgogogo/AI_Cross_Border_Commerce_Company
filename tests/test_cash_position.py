"""
test_cash_position.py — 现金位置 / 资金跑道 (the 跨境财务 capstone).

Two layers, both hermetic (no network; the writer model is faked but still bills the real
per-account path):
  • the deterministic cash_position_engine — week roll, locked-vs-available split, multi-
    currency/FX, crunch detection, committed-inflow scheduling from the hub, data-completeness/
    staleness, graceful degradation, bridge self-audit;
  • the /api/cash/* HTTP surface — plan-gating (basic+pro, free blocked), a full threaded run
    that CONSUMES a seeded settlement report READ-ONLY, customer-safe payload, ownership, and the
    run folding OUT of the report quota (RUN_KIND_CASH_POSITION).
"""
import datetime
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402
from agent_team import (cash_position_engine as eng, cash_position_orchestrator,  # noqa: E402
                        cash_templates as tpl, config, db, metrics_store as ms)
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.models import (QUOTA_RUN_KINDS, RUN_KIND_CASH_POSITION,  # noqa: E402
                               RUN_KIND_RECON)

AS_OF = datetime.date(2026, 6, 22)


# ===========================================================================
# Engine — pure, no DB / no network
# ===========================================================================
def _hub(reserve=20000.0, orders=None, in_transit_agg=None, rebate=40000.0,
         created_at="2026-06-21T10:00:00", period="2026-W25"):
    # 在途金额 (settlement summary) folds reserve INTO 在途, so it = in-transit lag + reserve.
    # Default 66000 = 46000 in-transit + 20000 reserve (kept internally consistent). The cash
    # engine consumes this AGGREGATE (not the per-order list) to avoid double-counting arrived
    # proceeds against the seller's opening bank balance.
    summary = {"到账": 200000.0, "实收": 260000.0,
               "在途金额": (in_transit_agg if in_transit_agg is not None else 66000.0),
               "未解释差异": 1200.0, "period_label": period}
    settlement = {
        "summary": summary,
        "attributions": [{"kind": "reserve_held", "amount": reserve, "count": 2}],
        "orders": orders if orders is not None else [
            {"ref": "A1", "amount": 30000, "net": 28000, "date": "2026-06-20"},
            {"ref": "A3", "amount": 20000, "net": 18000, "date": "2026-06-25"},
        ],
        "bridge": {"ok": True}, "created_at": created_at, "platform": "amazon",
        "period_label": period,
    }
    return {"settlement": settlement,
            "rebate": ({"可退税额": rebate, "period_label": period} if rebate else None)}


def _manual(cny=80000, usd=5000, **extra):
    m = dict(tpl.default_manual_inputs())
    m["opening_balances"] = {"CNY": cny, "USD": usd}
    m.update(extra)
    return m


def test_available_and_locked_split_multicurrency():
    a = tpl.merged_assumptions("9810", {"buffer_cny": 60000, "fx_rate": 7.2})
    r = eng.project_cash(as_of=AS_OF, assumptions=a, hub=_hub(), manual=_manual())
    # available = 80000 CNY + 5000 USD * 7.2
    assert r["position"]["available_now"] == 80000 + 5000 * 7.2
    assert r["position"]["by_currency"] == {"CNY": 80000.0, "USD": 5000.0}
    lk = r["position"]["locked_now"]
    assert lk["reserve"] == 20000.0
    assert lk["in_transit"] == 46000.0      # A1(28000)+A3(18000); A2 already arrived
    assert lk["total"] == 66000.0
    assert r["position"]["expected"]["rebate"] == 40000.0


def test_committed_inflow_schedule_aggregate_and_lags():
    # The committed pipeline uses the AGGREGATE 在途 (bank-coverage-correct, no double-count vs the
    # opening bank balance), reserve release, and expected 退税 — each at its own lag.
    a = tpl.merged_assumptions("9810", {"buffer_cny": 0})
    r = eng.project_cash(as_of=AS_OF, assumptions=a, hub=_hub(), manual=_manual())
    by_src = {p["source"]: p for p in r["runway"]["pending_arrivals"]}
    assert by_src["settlement"]["amount"] == 46000.0          # 在途金额 66000 − reserve 20000
    assert by_src["settlement"]["date"] == "2026-06-29"       # as_of + settlement_lag 7
    assert by_src["reserve_release"]["date"] == "2026-07-06"  # as_of + 14
    assert by_src["rebate"]["amount"] == 40000.0 and by_src["rebate"]["date"] == "2026-08-21"


def test_crunch_detection_and_min_balance():
    a = tpl.merged_assumptions("9810", {"buffer_cny": 60000})
    manual = _manual(restock=[{"label": "夏季备货", "amount": 150000,
                               "currency": "CNY", "due_date": "2026-07-15"}],
                     fixed_costs=[{"label": "工资", "amount": 30000, "day_of_month": 5}])
    r = eng.project_cash(as_of=AS_OF, assumptions=a, hub=_hub(), manual=manual)
    assert r["crunch"] is not None
    assert r["crunch"]["date"] == "2026-07-13"        # week containing the 150k 备货
    assert r["crunch"]["shortfall"] > 0
    assert r["runway"]["min_balance"] < 0             # the big PO drives it negative later


def test_no_crunch_when_pipeline_covers_buffer():
    a = tpl.merged_assumptions("9810", {"buffer_cny": 1000})
    r = eng.project_cash(as_of=AS_OF, assumptions=a, hub=_hub(), manual=_manual())
    assert r["crunch"] is None


def test_bridge_balances():
    a = tpl.merged_assumptions("9810", {"buffer_cny": 60000})
    manual = _manual(restock=[{"label": "x", "amount": 50000, "currency": "CNY",
                               "due_date": "2026-07-15"}])
    r = eng.project_cash(as_of=AS_OF, assumptions=a, hub=_hub(), manual=manual)
    assert r["bridge"]["ok"] is True
    assert abs(r["bridge"]["imbalance"]) <= 0.01


def test_fx_conversion_on_outflow():
    a = tpl.merged_assumptions("9810", {"buffer_cny": 0, "fx_rate": 7.0})
    # a USD supplier payment converts at 7.0 → 7000 CNY out
    manual = _manual(cny=10000, usd=0,
                     supplier_payments=[{"label": "供应商", "amount": 1000,
                                         "currency": "USD", "due_date": "2026-06-25"}])
    r = eng.project_cash(as_of=AS_OF, assumptions=a, hub={"settlement": None, "rebate": None},
                         manual=manual)
    wk0 = r["runway"]["weekly"][0]
    assert wk0["out"] == 7000.0                       # 1000 USD * 7.0
    assert wk0["available"] == 10000 - 7000.0


def test_rebate_mode_gating_excludes_9610():
    # 9610 综试区零售 is 无票免税 territory — no 退税 inflow even if a rebate report exists.
    a = tpl.merged_assumptions("9610", None)
    assert a["include_rebate"] is False
    r = eng.project_cash(as_of=AS_OF, assumptions=a, hub=_hub(rebate=40000.0),
                         manual=_manual())
    assert r["position"]["expected"]["rebate"] == 0.0
    assert all(p["source"] != "rebate" for p in r["runway"]["pending_arrivals"])


def test_no_settlement_degrades_gracefully():
    a = tpl.merged_assumptions("9810", {"buffer_cny": 0})
    r = eng.project_cash(as_of=AS_OF, assumptions=a,
                         hub={"settlement": None, "rebate": None}, manual=_manual())
    assert r["position"]["available_now"] == 80000 + 5000 * 7.2
    assert r["position"]["locked_now"]["total"] == 0.0
    assert any("回款核对" in m for m in r["data_completeness"]["missing"])


def test_staleness_flag():
    a = tpl.merged_assumptions("9810", {"buffer_cny": 0, "stale_days": 14})
    r = eng.project_cash(as_of=AS_OF, assumptions=a,
                         hub=_hub(created_at="2026-05-01T00:00:00"), manual=_manual())
    assert r["data_completeness"]["stale"] is True


def test_facts_carry_anchors():
    a = tpl.merged_assumptions("9810", {"buffer_cny": 60000})
    r = eng.project_cash(as_of=AS_OF, assumptions=a, hub=_hub(), manual=_manual())
    assert any("【1】" in f for f in r["facts_block"])
    assert len(r["source_rows"]) == len(r["facts_block"])


def test_templates_merged_assumptions_overrides():
    base = tpl.default_assumptions("9810")
    assert base["_provisional"] is True
    merged = tpl.merged_assumptions("9810", {"buffer_cny": 99999, "lags": {"rebate_lag_days": 45}})
    assert merged["buffer_cny"] == 99999
    assert merged["lags"]["rebate_lag_days"] == 45
    assert merged["lags"]["settlement_lag_days"] == tpl.DEFAULT_LAGS["settlement_lag_days"]
    assert merged["_provisional"] is False


# ===========================================================================
# Routes + persistence — DB + HTTP
# ===========================================================================
@pytest.fixture
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "brain.db"))
    yield


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(ms, "REPORTS_DIR", str(tmp_path / "reports"))
    yield


def _store(tmp_path, **accounts):
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    for email, password in accounts.items():
        store.create_customer([], name=email.split("@")[0], starting_balance=100.0,
                              email=email, password=password)
    app.CUSTOMERS = store
    return store


def _login(store, email):
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return client


def _paid(monkeypatch, plan="pro"):
    monkeypatch.setattr(ms, "effective_plan", lambda *a, **k: (plan, None, False))


class _FakeCashRouter:
    """The cash writer returns one-page markdown prose; the engine owns the numbers."""
    REPLY = ("# 现金位置与资金跑道\n\n## 核心结论\n- 可动用现金与在途已分开列示。\n"
             "> 本报告为决策支持，非建议。\n")

    def __init__(self, token_manager=None, cost_listener=None, **k):
        self.cost_listener = cost_listener
        self.token_manager = token_manager

    def complete(self, **kw):
        if self.cost_listener:
            self.cost_listener(config.HAIKU, 1000, 200)
        return self.REPLY


def _seed_settlement(company_id, period="2026-06"):
    """Persist a 对账 report the cash capstone will CONSUME read-only."""
    run_id = ms.create_run(company_id, "对账", kind=RUN_KIND_RECON)
    result = {
        # 在途金额 = 48000 in-transit + 20000 reserve (consistent); the engine consumes this aggregate
        "summary": {"到账": 100000.0, "实收": 160000.0, "在途金额": 68000.0,
                    "未解释差异": 0.0, "period_label": period, "platform": "amazon"},
        "attributions": [{"kind": "reserve_held", "amount": 20000.0, "count": 2,
                          "human_cn": "账户准备金"}],
        "orders": [{"ref": "O1", "amount": 50000, "net": 48000, "date": "2026-06-25"}],
        "bridge": {"ok": True}, "exceptions": [], "source_rows": [],
    }
    ms.save_settlement_report(company_id, run_id, result, account_id="x",
                              platform="amazon", period_label=period)


def _wait_done(client, job_id, tries=80):
    snap = {}
    for _ in range(tries):
        snap = client.get(f"/api/status/{job_id}").get_json()
        if snap.get("status") in ("done", "error"):
            return snap
        time.sleep(0.05)
    return snap


def test_settings_get_requires_login(fresh_db, tmp_path):
    _store(tmp_path, **{"a@co.com": "pw"})
    assert app.app.test_client().get("/api/cash/settings").status_code == 401


def test_settings_save_requires_paid_plan(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"free@co.com": "pw"})   # free plan by default
    r = _login(store, "free@co.com").post("/api/cash/settings",
                                          json={"assumptions": {"buffer_cny": 1}})
    assert r.status_code == 402


def test_settings_roundtrip(fresh_db, tmp_storage, tmp_path, monkeypatch):
    _paid(monkeypatch, "basic")     # basic ALSO has the feature
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    c = _login(store, "boss@co.com")
    r = c.post("/api/cash/settings", json={
        "assumptions": {"buffer_cny": 70000, "fx_rate": 7.1, "lags": {"rebate_lag_days": 45}},
        "manual_inputs": {"opening_balances": {"CNY": 12345, "USD": 0}},
        "prefs": {"cadence": "weekly", "delivery_channels": ["email"]},
        "active": True})
    assert r.status_code == 200
    got = c.get("/api/cash/settings").get_json()
    assert got["assumptions"]["buffer_cny"] == 70000
    assert got["assumptions"]["lags"]["rebate_lag_days"] == 45
    assert got["manual_inputs"]["opening_balances"]["CNY"] == 12345
    assert got["active"] is True


def test_run_requires_paid_plan(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"free@co.com": "pw"})
    assert _login(store, "free@co.com").post("/api/cash/run").status_code == 402


def test_full_cash_run_consumes_hub(fresh_db, tmp_storage, tmp_path, monkeypatch):
    _paid(monkeypatch, "pro")
    monkeypatch.setattr(cash_position_orchestrator, "ModelRouter", _FakeCashRouter)
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    cid = ms.get_or_create_company("boss@co.com", company_name="boss")
    _seed_settlement(cid)
    c = _login(store, "boss@co.com")
    # set opening balances + buffer so the run has the seller-known inputs
    c.post("/api/cash/settings", json={
        "assumptions": {"buffer_cny": 50000},
        "manual_inputs": {"opening_balances": {"CNY": 30000, "USD": 0}}})

    r = c.post("/api/cash/run", json={})
    assert r.status_code == 200, r.get_data(as_text=True)
    job_id, run_id = r.get_json()["job_id"], r.get_json()["run_id"]
    snap = _wait_done(c, job_id)
    assert snap["status"] == "done", snap
    # no model names leak into the live feed
    assert all("claude" not in (a.get("message") or "").lower()
               for a in snap.get("activity", []))

    out = c.get(f"/api/cash/result/{run_id}").get_json()
    # consumed the seeded 对账: reserve 20000 + in-transit O1 net 48000 (06-25 + 7 = 07-02)
    assert out["position"]["locked_now"]["reserve"] == 20000.0
    assert out["position"]["locked_now"]["in_transit"] == 48000.0
    assert out["position"]["available_now"] == 30000.0
    assert out["runway"]["weekly"], "expected a week-by-week runway"
    assert "现金位置" in (out["final_report"] or "")

    # recorded as a cash_position run that does NOT consume report quota
    run = ms.get_run(run_id)
    assert run["kind"] == RUN_KIND_CASH_POSITION
    assert RUN_KIND_CASH_POSITION not in QUOTA_RUN_KINDS


def test_result_ownership(fresh_db, tmp_storage, tmp_path, monkeypatch):
    _paid(monkeypatch, "pro")
    monkeypatch.setattr(cash_position_orchestrator, "ModelRouter", _FakeCashRouter)
    store = _store(tmp_path, **{"a@co.com": "pw", "b@co.com": "pw"})
    a = _login(store, "a@co.com")
    body = a.post("/api/cash/run", json={}).get_json()
    _wait_done(a, body["job_id"])
    b = _login(store, "b@co.com")
    assert b.get(f"/api/cash/result/{body['run_id']}").status_code in (403, 404)


def test_payload_carries_no_machinery(fresh_db, tmp_storage, tmp_path, monkeypatch):
    _paid(monkeypatch, "pro")
    monkeypatch.setattr(cash_position_orchestrator, "ModelRouter", _FakeCashRouter)
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    c = _login(store, "boss@co.com")
    body = c.post("/api/cash/run", json={}).get_json()
    _wait_done(c, body["job_id"])
    text = c.get(f"/api/cash/result/{body['run_id']}").get_data(as_text=True).lower()
    for needle in ("model", "opus", "deepseek", "sk-", "per_mtok", "cost_yuan"):
        assert needle not in text


def test_active_settings_listed_for_cadence(fresh_db, tmp_storage, tmp_path):
    cid = ms.get_or_create_company("boss@co.com", company_name="boss")
    ms.save_cash_settings(cid, {"active": True, "prefs": {"cadence": "weekly"}})
    other = ms.get_or_create_company("idle@co.com", company_name="idle")
    ms.save_cash_settings(other, {"active": False})
    active = ms.list_active_cash_settings()
    assert [s["company_id"] for s in active] == [cid]


def test_cross_border_cash_spa_shell_served(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"a@co.com": "pw"})
    c = _login(store, "a@co.com")
    assert c.get("/cross-border/cash").status_code == 200
    assert c.get("/cross-border/cash/5").status_code == 200


# ===========================================================================
# Phase 3 — replenishment-plan 备货 model + inventory seam (engine, pure)
# ===========================================================================
def _restock_outflows(manual):
    a = tpl.merged_assumptions("9810", {"buffer_cny": 0})
    m = _manual(cny=1000000, usd=0, **manual)
    r = eng.project_cash(as_of=AS_OF, assumptions=a,
                         hub={"settlement": None, "rebate": None}, manual=m)
    return [(w["start"], w["out"]) for w in r["runway"]["weekly"] if w["out"] > 0]


def test_restock_plain_amount_back_compat():
    outs = _restock_outflows({"restock": [{"label": "A", "amount": 100000,
                                           "currency": "CNY", "due_date": "2026-07-01"}]})
    assert outs == [("2026-06-29", 100000.0)]   # single outflow, the week containing 07-01


def test_restock_deposit_balance_split_with_freight():
    # 30% 定金 on 06-25 (week of 06-22) + 70% 尾款 on 06-25+30=07-25 (week of 07-20)
    outs = dict(_restock_outflows({"restock": [{"label": "B", "amount": 100000,
        "currency": "CNY", "due_date": "2026-06-25", "deposit_pct": 30, "freight_days": 30}]}))
    assert outs["2026-06-22"] == 30000.0
    assert outs["2026-07-20"] == 70000.0
    assert round(sum(outs.values()), 2) == 100000.0   # deposit + balance == amount


def test_restock_qty_times_unit_cost():
    outs = _restock_outflows({"restock": [{"label": "C", "qty": 200, "unit_cost": 500,
                                           "currency": "CNY", "due_date": "2026-07-01"}]})
    assert outs == [("2026-06-29", 100000.0)]   # 200 × 500


def test_inventory_seam_returns_none_and_ccc_deferred():
    assert cash_position_orchestrator.inventory_restock_plan(1) is None
    r = eng.project_cash(as_of=AS_OF, assumptions=tpl.merged_assumptions("9810", None),
                         hub={"settlement": None, "rebate": None}, manual=_manual())
    assert any("CCC" in d for d in r["data_completeness"]["deferred"])


# ===========================================================================
# Phase 2 — interactive solver + scenarios (engine, pure)
# ===========================================================================
def _base_for_solver(buffer=50000):
    a = tpl.merged_assumptions("9810", {"buffer_cny": buffer})
    manual = _manual(restock=[{"label": "x", "amount": 40000, "currency": "CNY",
                               "due_date": "2026-07-15"}],
                     fixed_costs=[{"label": "工资", "amount": 30000, "day_of_month": 5}])
    return a, _hub(), manual


def test_max_safe_restock_closed_form_and_affordability_boundary():
    a, hub, manual = _base_for_solver()
    base = eng.project_cash(as_of=AS_OF, assumptions=a, hub=hub, manual=manual)
    ms = eng.max_safe_restock(base)
    # closed form: max_amount == min(weekly available) − buffer (when positive)
    mn = min(w["available"] for w in base["runway"]["weekly"])
    assert ms["max_amount"] == round(mn - base["summary"]["现金缓冲"], 2)
    # spending exactly max_safe today stays affordable; a bit more breaches the buffer
    at_max = eng.affordability(as_of=AS_OF, assumptions=a, hub=hub, manual=manual,
                               po={"amount": ms["max_amount"], "currency": "CNY",
                                   "date": AS_OF.isoformat()})
    over = eng.affordability(as_of=AS_OF, assumptions=a, hub=hub, manual=manual,
                             po={"amount": ms["max_amount"] + 5000, "currency": "CNY",
                                 "date": AS_OF.isoformat()})
    assert at_max["affordable"] is True
    assert over["affordable"] is False and over["weeks_below_buffer"] >= 1


def test_forecast_overlay_does_not_touch_committed_runway():
    a = tpl.merged_assumptions("9810", {"buffer_cny": 0})
    m0 = _manual()
    m1 = _manual(weekly_sales=8000)
    committed0 = eng.project_cash(as_of=AS_OF, assumptions=a, hub=_hub(), manual=m0)["runway"]["weekly"]
    r1 = eng.project_cash(as_of=AS_OF, assumptions=a, hub=_hub(), manual=m1)["runway"]
    committed1 = r1["weekly"]
    assert [w["available"] for w in committed0] == [w["available"] for w in committed1]
    assert r1["forecast"] is not None
    f = r1["forecast"]
    assert f["high"][-1] > f["base"][-1] > f["low"][-1]   # band ordering


def test_scenario_sales_delta_moves_forecast_not_committed():
    a = tpl.merged_assumptions("9810", {"buffer_cny": 0})
    manual = _manual(weekly_sales=10000)
    sc = eng.scenario(as_of=AS_OF, assumptions=a, hub=_hub(), manual=manual,
                      levers={"sales_delta_pct": -30})
    # committed min balance is sales-immune
    assert sc["baseline"]["min_balance"] == sc["scenario"]["min_balance"]
    # but the (low-confidence) forecast end-balance drops
    assert sc["forecast_end"]["scenario"] < sc["forecast_end"]["baseline"]


def test_scenario_rebate_delay_shifts_pipeline():
    a = tpl.merged_assumptions("9810", {"buffer_cny": 0, "horizon_weeks": 8})
    # rebate at +60d is ~week 8 (out of an 8-week horizon); delaying it pushes it past the window
    manual = _manual(restock=[{"label": "po", "amount": 120000, "currency": "CNY",
                               "due_date": "2026-06-25"}])
    sc = eng.scenario(as_of=AS_OF, assumptions=a, hub=_hub(rebate=40000.0), manual=manual,
                      levers={"rebate_delay_days": 40})
    # delaying an inflow can only lower (or equal) the min balance, never raise it
    assert sc["scenario"]["min_balance"] <= sc["baseline"]["min_balance"]


# ===========================================================================
# Phase 2 — solver/scenario routes (synchronous, no run row, gated)
# ===========================================================================
def _company_run_count(company_id):
    from agent_team.models import Run
    with db.session_scope() as s:
        return s.query(Run).filter_by(company_id=company_id).count()


def test_solve_requires_paid_plan(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"free@co.com": "pw"})
    assert _login(store, "free@co.com").post("/api/cash/solve", json={}).status_code == 402
    assert _login(store, "free@co.com").post("/api/cash/scenario", json={}).status_code == 402


def test_solve_is_synchronous_and_creates_no_run(fresh_db, tmp_storage, tmp_path, monkeypatch):
    _paid(monkeypatch, "pro")
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    cid = ms.get_or_create_company("boss@co.com", company_name="boss")
    _seed_settlement(cid)
    before = _company_run_count(cid)
    c = _login(store, "boss@co.com")
    c.post("/api/cash/settings", json={"assumptions": {"buffer_cny": 40000},
                                       "manual_inputs": {"opening_balances": {"CNY": 60000}}})
    r = c.post("/api/cash/solve", json={"po": {"amount": 10000, "currency": "CNY",
                                               "date": "2026-07-01"}})
    assert r.status_code == 200
    body = r.get_json()
    assert "max_safe" in body and "max_amount" in body["max_safe"]
    assert "affordability" in body and "affordable" in body["affordability"]
    # synchronous compute path must NOT open a run row (only the seeded 对账 run exists)
    assert _company_run_count(cid) == before


def test_scenario_route_returns_baseline_vs_scenario(fresh_db, tmp_storage, tmp_path, monkeypatch):
    _paid(monkeypatch, "basic")
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    cid = ms.get_or_create_company("boss@co.com", company_name="boss")
    _seed_settlement(cid)
    c = _login(store, "boss@co.com")
    c.post("/api/cash/settings", json={"manual_inputs": {"opening_balances": {"CNY": 50000},
                                                         "weekly_sales": 9000}})
    before = _company_run_count(cid)
    r = c.post("/api/cash/scenario", json={"levers": {"sales_delta_pct": -25}})
    assert r.status_code == 200
    body = r.get_json()
    assert "baseline" in body and "scenario" in body
    assert body["forecast_end"]["scenario"] < body["forecast_end"]["baseline"]
    assert _company_run_count(cid) == before

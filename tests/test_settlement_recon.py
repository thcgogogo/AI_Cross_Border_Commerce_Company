"""对账 / 平台结算核对 — settlement-reconciliation acceptance tests.

Mirrors the plan's Phase-0 acceptance cases, hermetic (no network) and billed
through the real per-account path (the model call is faked but still meters ¥):

  Engine (pure, deterministic — the reliability moat):
    1. 支付手续费 explains 应收−实收 exactly; residual 0.
    2. cross-period partial refund attributed; residual 0.
    3. many-to-one batch settlement (N orders → 1 bank到账) matches.
    4. an injected gap surfaces as 未解释差异 ¥X (N 笔).
    5. a tampered statement fails the pure-Python bridge-balance self-audit.
    6. a shared platform template + a per-account 费率 override.
    7. a learned classification reclassifies the same line on the next run.

  Store + web (customer-safe, ownership-enforced, quota-folded):
    8. one-tap classify writes a learned rule; the next run drops the residual.
    9. a faked-but-billed orchestrator run persists the result + bills the account.
   10. /api/recon/* auth, validation, ownership, classify, ledger, platforms.
   11. 对账 folds into the report quota (RUN_KIND_RECON counts).
"""
import csv
import io
import os
import sys
import time
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot is one-time & idempotent
from agent_team import (  # noqa: E402
    config, db, metrics_store, settlement_engine as eng, settlement_orchestrator,
    settlement_templates as templates)
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.memory import SharedMemory  # noqa: E402
from agent_team.models import (  # noqa: E402
    RUN_KIND_RECON, SETTLE_EXC_OPEN, SettlementReport)

PACK = templates.load_pack("微信支付")


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "brain.db"))
    yield


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    """Keep every on-disk write (uploads / memory / reports) inside tmp."""
    monkeypatch.setattr(app, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(app, "MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(metrics_store, "UPLOADS_DIR", str(tmp_path / "u2"))
    monkeypatch.setattr(metrics_store, "REPORTS_DIR", str(tmp_path / "reports"))
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


def _run(plat, bank, ledger, learned=None, pack=PACK):
    return eng.reconcile_records(
        {"platform": plat, "bank": bank, "ledger": ledger}, pack, learned_rules=learned)


def _csv_bytes(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


# A fixed Chinese 对账报告 the fake router returns (carries a 【N】 anchor + 核心结论).
_CN = ("# 对账报告\n\n## 核心结论\n- 应收 ¥300.00【1】，到账 ¥298.20【6】，"
       "未解释差异 ¥0.00【7】。\n")


class _FakeRouter:
    """Network-free ModelRouter stand-in; bills through the real per-account path."""

    def __init__(self, token_manager=None, cost_listener=None, **kwargs):
        self.cost_listener = cost_listener
        self.token_manager = token_manager

    def complete(self, **kwargs):
        if self.cost_listener:
            self.cost_listener(config.HAIKU, 80_000, 12_000)
        return _CN

    def complete_with_tools(self, **kwargs):
        return self.complete(**kwargs)


# Two clean wechat orders (gross=net, fee 0.6%) settling to one bank credit.
_PLAT_CLEAN = [
    {"交易时间": "2026-06-01", "微信订单号": "WX1", "商户订单号": "M1", "交易状态": "SUCCESS",
     "订单金额": "100.00", "应结订单金额": "100.00", "手续费": "0.60"},
    {"交易时间": "2026-06-01", "微信订单号": "WX2", "商户订单号": "M2", "交易状态": "SUCCESS",
     "订单金额": "200.00", "应结订单金额": "200.00", "手续费": "1.20"},
]
_BANK_CLEAN = [{"交易日期": "2026-06-03", "收入金额": "298.20"}]
_LEDGER_CLEAN = [{"商户订单号": "M1", "下单时间": "2026-06-01", "订单金额": "100.00"},
                 {"商户订单号": "M2", "下单时间": "2026-06-01", "订单金额": "200.00"}]


# ---------------------------------------------------------------------------
# (1) Payment fee attribution
# ---------------------------------------------------------------------------
def test_payment_fee_attribution():
    r = _run(_PLAT_CLEAN, _BANK_CLEAN, _LEDGER_CLEAN)
    s = r["summary"]
    assert s["应收"] == 300.0 and s["实收"] == 298.2
    fee = [a for a in r["attributions"] if a["kind"] == "fee"][0]
    assert fee["amount"] == 1.8                      # 手续费 == 应收 − 实收
    assert s["未解释差异"] == 0.0 and r["bridge"]["ok"] is True


# ---------------------------------------------------------------------------
# (2) Cross-period partial refund
# ---------------------------------------------------------------------------
def test_cross_period_partial_refund():
    plat = [
        {"交易时间": "2026-05-20", "商户订单号": "R1", "交易状态": "SUCCESS",
         "订单金额": "500", "应结订单金额": "500", "手续费": "3"},
        {"交易时间": "2026-06-10", "商户订单号": "R1", "交易状态": "REFUND",
         "退款金额": "200", "应结订单金额": "0", "手续费": "0"},
    ]
    bank = [{"交易日期": "2026-06-10", "收入金额": "497"},
            {"交易日期": "2026-06-10", "收入金额": "-200"}]
    r = _run(plat, bank, [{"商户订单号": "R1", "下单时间": "2026-05-20", "订单金额": "500"}])
    refund = [a for a in r["attributions"] if a["kind"] == "refund"][0]
    assert refund["amount"] == 200.0
    assert r["summary"]["未解释差异"] == 0.0 and r["summary"]["未解释笔数"] == 0


# ---------------------------------------------------------------------------
# (3) Many-to-one batch settlement
# ---------------------------------------------------------------------------
def test_many_to_one_batch_settlement():
    r = _run(_PLAT_CLEAN, _BANK_CLEAN, _LEDGER_CLEAN)
    assert r["buckets"]["L3"]["已匹配"] == 1            # 2 orders → 1 bank credit
    assert r["buckets"]["L1"]["已匹配"] == 2
    assert r["summary"]["未解释差异"] == 0.0


# ---------------------------------------------------------------------------
# (4) Residual 未解释差异
# ---------------------------------------------------------------------------
def test_residual_unexplained_surfaces():
    plat = [{"交易时间": "2026-06-01", "商户订单号": "U1", "交易状态": "SUCCESS",
             "订单金额": "1000", "应结订单金额": "1000", "手续费": "0"}]
    bank = [{"交易日期": "2026-06-03", "收入金额": "900"}]
    r = _run(plat, bank, [{"商户订单号": "U1", "下单时间": "2026-06-01", "订单金额": "1000"}])
    assert r["summary"]["未解释差异"] == 100.0 and r["summary"]["未解释笔数"] == 1
    l3 = [e for e in r["exceptions"] if e["layer"] == "L3"]
    assert len(l3) == 1 and l3[0]["amount"] == 100.0
    assert r["bridge"]["ok"] is True                  # statement itself is consistent


# ---------------------------------------------------------------------------
# (5) Bridge-balance self-audit
# ---------------------------------------------------------------------------
def test_bridge_balance_self_audit_flags_inconsistent_statement():
    # net != gross − coupon (a 技术服务费 the schema doesn't model) → bridge must FLAG.
    plat = [{"交易时间": "2026-06-01", "商户订单号": "B1", "交易状态": "SUCCESS",
             "订单金额": "1000", "应结订单金额": "950", "手续费": "0", "代金券金额": "0"}]
    r = _run(plat, [{"交易日期": "2026-06-03", "收入金额": "950"}], [])
    assert r["bridge"]["ok"] is False and r["bridge"]["imbalance"] == 50.0


# ---------------------------------------------------------------------------
# (6) Shared template + per-account override
# ---------------------------------------------------------------------------
def test_shared_template_and_per_account_override():
    # No 手续费 column → fee is estimated from the (overridable) 费率.
    plat = [{"交易时间": "2026-06-01", "商户订单号": "O1", "交易状态": "SUCCESS",
             "订单金额": "1000", "应结订单金额": "1000"}]
    base = eng.reconcile_records({"platform": plat, "bank": [], "ledger": []}, PACK)
    over = eng.reconcile_records({"platform": plat, "bank": [], "ledger": []},
                                 templates.apply_overrides(PACK, {"fee_rate": 0.01}))
    assert base["summary"]["总手续费"] == 6.0          # default 0.6%
    assert over["summary"]["总手续费"] == 10.0          # account override 1.0%


# ---------------------------------------------------------------------------
# (7) Learned-classification reuse (engine level)
# ---------------------------------------------------------------------------
def test_learned_classification_reclassifies_next_run():
    plat = [{"交易时间": "2026-06-01", "商户订单号": "K1", "交易状态": "SUCCESS",
             "订单金额": "300", "应结订单金额": "300", "手续费": "0"}]
    bank = [{"交易日期": "2026-06-03", "收入金额": "300"}]
    ledger = [{"商户订单号": "K1", "下单时间": "2026-06-01", "订单金额": "300"},
              {"商户订单号": "K9", "下单时间": "2026-06-01", "订单金额": "50"}]
    first = _run(plat, bank, ledger)
    k9 = [e for e in first["exceptions"] if e.get("ledger_ref") == "K9"][0]
    assert k9["cause"] == eng.CAUSE_UNEXPLAINED and first["summary"]["未解释笔数"] == 1

    rule = {"match": {"ledger_ref": "K9"}, "cause": "退款/退货", "label": "refund"}
    second = _run(plat, bank, ledger, learned=[rule])
    k9b = [e for e in second["exceptions"] if e.get("ledger_ref") == "K9"][0]
    assert k9b["cause"] == "退款/退货" and second["summary"]["未解释笔数"] == 0


# ---------------------------------------------------------------------------
# (8) The learning loop through the store: classify → learned rule → next run
# ---------------------------------------------------------------------------
def test_classify_writes_learned_rule_and_persists(fresh_db, tmp_path):
    cid = metrics_store.get_or_create_company("boss@corp.com", company_name="Acme")
    run_id = metrics_store.create_run(cid, "对账 2026-06", kind=RUN_KIND_RECON)
    plat = [{"交易时间": "2026-06-01", "商户订单号": "K1", "交易状态": "SUCCESS",
             "订单金额": "300", "应结订单金额": "300", "手续费": "0"}]
    bank = [{"交易日期": "2026-06-03", "收入金额": "300"}]
    ledger = [{"商户订单号": "K1", "下单时间": "2026-06-01", "订单金额": "300"},
              {"商户订单号": "K9", "下单时间": "2026-06-01", "订单金额": "50"}]
    result = _run(plat, bank, ledger)
    metrics_store.save_settlement_report(cid, run_id, result, platform="wechat_pay")

    excs = metrics_store.list_settlement_exceptions(cid, run_id, status=SETTLE_EXC_OPEN)
    k9 = [e for e in excs if e["ledger_ref"] == "K9"][0]
    updated = metrics_store.classify_settlement_exception(cid, k9["id"], "refund")
    assert updated["cause"] == "退款/退货" and updated["status"] != SETTLE_EXC_OPEN

    rules = metrics_store.learned_recon_rules(cid)
    assert any(r.get("match", {}).get("ledger_ref") == "K9" for r in rules)
    # The learned rule, fed to a fresh run, drops the residual count.
    second = _run(plat, bank, ledger, learned=rules)
    assert second["summary"]["未解释笔数"] == 0


# ---------------------------------------------------------------------------
# Regression: a duplicate 订单号 must match on the AMOUNT, not just the first row.
# ---------------------------------------------------------------------------
def test_duplicate_order_number_matches_on_amount():
    plat = [  # same 商户订单号, different amounts — the first is NOT the ledger's match
        {"交易时间": "2026-06-01", "商户订单号": "D1", "交易状态": "SUCCESS",
         "订单金额": "200", "应结订单金额": "200", "手续费": "0"},
        {"交易时间": "2026-06-01", "商户订单号": "D1", "交易状态": "SUCCESS",
         "订单金额": "100", "应结订单金额": "100", "手续费": "0"},
    ]
    r = _run(plat, [{"交易日期": "2026-06-03", "收入金额": "300"}],
             [{"商户订单号": "D1", "下单时间": "2026-06-01", "订单金额": "100"}])
    # The ¥100 candidate (cands[1]) is the real match → 已匹配, not 部分匹配.
    assert r["buckets"]["L1"]["已匹配"] == 1 and r["buckets"]["L1"]["部分匹配"] == 0


# ---------------------------------------------------------------------------
# Regression: re-saving a settlement report must PRESERVE the original created_at.
# ---------------------------------------------------------------------------
def test_save_settlement_report_preserves_created_at(fresh_db):
    cid = metrics_store.get_or_create_company("boss@corp.com")
    run_id = metrics_store.create_run(cid, "对账", kind=RUN_KIND_RECON)
    result = _run(_PLAT_CLEAN, _BANK_CLEAN, _LEDGER_CLEAN)
    metrics_store.save_settlement_report(cid, run_id, result, platform="wechat_pay")
    # Backdate the row, then re-save — created_at must NOT jump to "now".
    with db.session_scope() as s:
        s.query(SettlementReport).filter_by(company_id=cid, run_id=run_id).one() \
            .created_at = datetime(2020, 1, 1)
    old = metrics_store.get_settlement_report(cid, run_id)["created_at"]
    metrics_store.save_settlement_report(cid, run_id, result, platform="wechat_pay")
    assert metrics_store.get_settlement_report(cid, run_id)["created_at"] == old


# ---------------------------------------------------------------------------
# (9) A faked-but-billed orchestrator run persists + bills
# ---------------------------------------------------------------------------
def test_orchestrator_run_persists_and_bills(fresh_db, tmp_storage, tmp_path, monkeypatch):
    store = _store(tmp_path, **{"boss@corp.com": "pw1"})
    acct_id = store.find_by_email("boss@corp.com")["id"]
    cid = metrics_store.get_or_create_company("boss@corp.com", company_name="Acme")
    run_id = metrics_store.create_run(cid, "对账 2026-06", kind=RUN_KIND_RECON)

    files = {
        "platform": (str(_write(tmp_path, "wx.csv",
                     ["交易时间", "微信订单号", "商户订单号", "交易状态", "订单金额", "应结订单金额", "手续费"],
                     [["2026-06-01", "WX1", "M1", "SUCCESS", "100.00", "100.00", "0.60"],
                      ["2026-06-01", "WX2", "M2", "SUCCESS", "200.00", "200.00", "1.20"]])), "wx.csv"),
        "bank": (str(_write(tmp_path, "bank.csv", ["交易日期", "收入金额"],
                     [["2026-06-03", "298.20"]])), "bank.csv"),
        "ledger": (str(_write(tmp_path, "erp.csv", ["商户订单号", "下单时间", "订单金额"],
                     [["M1", "2026-06-01", "100.00"], ["M2", "2026-06-01", "200.00"]])), "erp.csv"),
    }
    mem = SharedMemory("jobrecon", store_dir=str(tmp_path / "mem"), goal="对账 2026-06")
    settlement_orchestrator.SettlementOrchestrator(
        mem, files, platform_key="微信支付", period_label="2026-06",
        token_manager=app.TOKENS,
        cost_listener=app._make_biller(acct_id, run_kind=RUN_KIND_RECON),
        llm=_FakeRouter(), company_id=cid, run_id=run_id, account_id=acct_id).run()

    snap = mem.snapshot()
    assert snap["status"] == "done" and "对账报告" in snap["final_report"]
    rep = metrics_store.get_settlement_report(cid, run_id)
    assert rep["summary"]["应收"] == 300.0 and rep["bridge_ok"] is True
    assert metrics_store.get_run(run_id)["status"] == "succeeded"
    assert metrics_store.get_run(run_id)["total_cost_yuan"] > 0      # billed
    # FREE account (balance-gated): the fake call decremented the demo balance.
    assert app.CUSTOMERS.find_by_email("boss@corp.com")["balance"] < 100.0


def _write(tmp_path, name, header, rows):
    p = tmp_path / name
    p.write_bytes(_csv_bytes(header, rows))
    return p


# ---------------------------------------------------------------------------
# (10) HTTP surface — auth, validation, ownership, classify, ledger, platforms
# ---------------------------------------------------------------------------
def test_recon_run_requires_login(fresh_db, tmp_path):
    _store(tmp_path)
    client = app.app.test_client()
    r = client.post("/api/recon/run", data={"platform_key": "微信支付"})
    assert r.status_code == 401


def test_recon_run_rejects_missing_files(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"a@corp.com": "pw"})
    client = _login(store, "a@corp.com")
    r = client.post("/api/recon/run", data={"platform_key": "微信支付"},
                    content_type="multipart/form-data")
    assert r.status_code == 400


def test_recon_platforms_lists_wechat(fresh_db, tmp_path):
    store = _store(tmp_path, **{"a@corp.com": "pw"})
    client = _login(store, "a@corp.com")
    keys = [p["key"] for p in client.get("/api/recon/platforms").get_json()["platforms"]]
    assert "wechat_pay" in keys
    assert app.app.test_client().get("/api/recon/platforms").status_code == 401


def _seed_report(cid, account_id=None):
    run_id = metrics_store.create_run(cid, "对账", kind=RUN_KIND_RECON)
    result = _run(
        [{"交易时间": "2026-06-01", "商户订单号": "U1", "交易状态": "SUCCESS",
          "订单金额": "1000", "应结订单金额": "1000", "手续费": "0"}],
        [{"交易日期": "2026-06-03", "收入金额": "900"}],
        [{"商户订单号": "U1", "下单时间": "2026-06-01", "订单金额": "1000"},
         {"商户订单号": "U9", "下单时间": "2026-06-01", "订单金额": "40"}])
    metrics_store.save_settlement_report(cid, run_id, result, account_id=account_id,
                                         platform="wechat_pay")
    return run_id


def test_recon_result_ownership_and_customer_safe(fresh_db, tmp_path):
    store = _store(tmp_path, **{"a@corp.com": "pw", "b@corp.com": "pw"})
    cid_a = metrics_store.get_or_create_company("a@corp.com")
    run_id = _seed_report(cid_a)

    ra = _login(store, "a@corp.com").get(f"/api/recon/result/{run_id}")
    assert ra.status_code == 200
    body = ra.get_json()
    assert body["summary"]["未解释差异"] == 100.0
    assert "total_cost_yuan" not in body and "report_path" not in body  # customer-safe

    rb = _login(store, "b@corp.com").get(f"/api/recon/result/{run_id}")
    assert rb.status_code == 403                                          # cross-account


def test_recon_worklist_classify_and_ledger(fresh_db, tmp_path):
    store = _store(tmp_path, **{"a@corp.com": "pw", "b@corp.com": "pw"})
    cid_a = metrics_store.get_or_create_company("a@corp.com")
    run_id = _seed_report(cid_a, account_id=store.find_by_email("a@corp.com")["id"])
    ca = _login(store, "a@corp.com")

    excs = ca.get(f"/api/recon/exceptions?run_id={run_id}").get_json()["exceptions"]
    u9 = [e for e in excs if e["ledger_ref"] == "U9"][0]

    # B cannot classify A's exception.
    rb = _login(store, "b@corp.com").post(
        f"/api/recon/exceptions/{u9['id']}/classify", json={"label": "refund"})
    assert rb.status_code == 404

    ok = ca.post(f"/api/recon/exceptions/{u9['id']}/classify", json={"label": "refund"})
    assert ok.status_code == 200 and ok.get_json()["exception"]["cause"] == "退款/退货"

    # The audit-ready ledger downloads as a real .xlsx.
    led = ca.get(f"/api/recon/ledger/{run_id}")
    assert led.status_code == 200
    assert "spreadsheetml" in led.headers["Content-Type"]
    assert led.data[:2] == b"PK"                          # a zip/xlsx magic


# ---------------------------------------------------------------------------
# (11) 对账 folds into the report quota
# ---------------------------------------------------------------------------
def test_recon_folds_into_report_quota(fresh_db, monkeypatch):
    monkeypatch.setenv("REPORT_QUOTA_ENABLED", "1")
    cid = metrics_store.get_or_create_company("boss@corp.com")
    metrics_store.create_run(cid, "经营周报")                       # a report run
    metrics_store.create_run(cid, "对账", kind=RUN_KIND_RECON)      # a recon run
    # Both count against the shared monthly quota (the QUOTA_RUN_KINDS fold).
    assert metrics_store.count_runs_this_month(cid) == 2
    assert metrics_store.count_runs_total(cid) == 2


# ---------------------------------------------------------------------------
# (12) Full threaded run via the HTTP endpoint (faked router), then result
# ---------------------------------------------------------------------------
def test_full_recon_run_via_client(fresh_db, tmp_storage, tmp_path, monkeypatch):
    monkeypatch.setattr(settlement_orchestrator, "ModelRouter", _FakeRouter)
    store = _store(tmp_path, **{"boss@corp.com": "pw"})
    client = _login(store, "boss@corp.com")

    data = {
        "platform_key": "微信支付", "period": "2026-06",
        "platform_file": (io.BytesIO(_csv_bytes(
            ["交易时间", "微信订单号", "商户订单号", "交易状态", "订单金额", "应结订单金额", "手续费"],
            [["2026-06-01", "WX1", "M1", "SUCCESS", "100.00", "100.00", "0.60"],
             ["2026-06-01", "WX2", "M2", "SUCCESS", "200.00", "200.00", "1.20"]])), "wx.csv"),
        "bank_file": (io.BytesIO(_csv_bytes(["交易日期", "收入金额"],
            [["2026-06-03", "298.20"]])), "bank.csv"),
        "ledger_file": (io.BytesIO(_csv_bytes(["商户订单号", "下单时间", "订单金额"],
            [["M1", "2026-06-01", "100.00"], ["M2", "2026-06-01", "200.00"]])), "erp.csv"),
    }
    r = client.post("/api/recon/run", data=data, content_type="multipart/form-data")
    assert r.status_code == 200
    body = r.get_json()
    job_id, run_id = body["job_id"], body["run_id"]

    snap = _wait_done(client, job_id)
    assert snap["status"] == "done"
    # Non-admin status feed is customer-safe (no raw machinery / model names).
    assert all("claude" not in (a.get("message") or "").lower()
               for a in snap.get("activity", []))

    res = client.get(f"/api/recon/result/{run_id}").get_json()
    assert res["summary"]["应收"] == 300.0 and res["summary"]["未解释差异"] == 0.0
    assert "对账报告" in (res.get("final_report") or "")


def _wait_done(client, job_id, tries=80):
    snap = {}
    for _ in range(tries):
        snap = client.get(f"/api/status/{job_id}").get_json()
        if snap.get("status") in ("done", "error"):
            return snap
        time.sleep(0.05)
    return snap

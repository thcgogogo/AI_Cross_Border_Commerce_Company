"""跨境财务 cross-sub-tab data flow (§6) — 退税 borrows from 发票审核 + 对账.

退税核算 builds on earlier sub-tabs so the seller needn't re-upload: the input-VAT pool
comes from 发票审核 (有效进项税额) when 进项专票 aren't uploaded, and 收汇 is corroborated by
对账 (到账) when 收汇/结汇 aren't uploaded. Uploaded data always wins; the read is an
aggregate (graceful None when upstream hasn't run). Hermetic — engines are pure, no LLM.
"""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402
from agent_team import (crossborder_orchestrator as cbo, db,  # noqa: E402
                        metrics_store as ms, rebate_engine, threeflow_engine)
from agent_team.customers import CustomerStore  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "brain.db"))
    yield


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "UPLOAD_DIR", str(tmp_path / "uploads"))
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


# --- engine: borrow the input-VAT pool when 进项专票 absent --------------------
def test_borrows_input_tax_pool_when_no_input_invoices():
    exports = [{"报关单号": "BG1", "订单号": "O1", "出口额": "1000", "退税率": "13%",
                "报关日期": "2026-06-12"}]
    out = rebate_engine.check_records({"exports": exports}, period_label="2026-06",
                                      context={"input_tax_pool": 130})
    assert out["summary"]["进项税额合计"] == 130.0
    assert "发票审核" in out["summary"]["上游带入"]
    assert any("由发票审核带入" in (r["source_detail"] or "") for r in out["source_rows"])


def test_uploaded_invoices_win_over_context_pool():
    inputs = [{"发票号": "FP1", "销售方": "X", "价税合计": "1130", "税额": "130",
               "税率": "13%", "开票日期": "2026-06-01"}]
    exports = [{"报关单号": "BG1", "订单号": "O1", "出口额": "1000", "退税率": "13%"}]
    out = rebate_engine.check_records({"input_invoices": inputs, "exports": exports},
                                      context={"input_tax_pool": 9999})
    assert out["summary"]["进项税额合计"] == 130.0          # uploaded wins; 9999 ignored
    assert "发票审核" not in (out["summary"]["上游带入"] or "")


# --- engine: corroborate 收汇 from 对账's 到账 (magnitude-aware) ----------------
def test_recon_fully_corroborates_when_covers_export():
    exports = [{"报关单号": "BG1", "订单号": "O1", "出口额": "1000", "退税率": "13%"}]
    out = rebate_engine.check_records({"exports": exports},
                                      context={"recon_received": 5000})
    # full coverage (5000 ≥ 1000) → caveat suppressed, coverage shown
    assert out["summary"]["收汇状态"] == "对账佐证 ¥5,000.00/出口额 ¥1,000.00"
    assert not any(e["cause"] in ("收汇未提供", "收汇部分佐证") for e in out["exceptions"])


def test_recon_partial_coverage_keeps_caveat():
    # a tiny 到账 must NOT mark a big export batch as collection-corroborated
    exports = [{"报关单号": "BG1", "订单号": "O1", "出口额": "1000000", "退税率": "13%"}]
    out = rebate_engine.check_records({"exports": exports},
                                      context={"recon_received": 1})
    assert any(e["cause"] == "收汇部分佐证" for e in out["exceptions"])
    assert "出口额 ¥1,000,000.00" in out["summary"]["收汇状态"]


def test_no_upstream_keeps_collection_note():
    exports = [{"报关单号": "BG1", "订单号": "O1", "出口额": "1000", "退税率": "13%"}]
    out = rebate_engine.check_records({"exports": exports})   # no context
    assert out["summary"]["收汇状态"] == "未提供"
    assert any(e["cause"] == "收汇未提供" for e in out["exceptions"])


# --- the context builder + brain reader -------------------------------------
def test_context_builder_reads_upstream(fresh_db):
    cid = ms.get_or_create_company("flow@co.com", company_name="Flow")
    # seed an 对账 到账 + a 发票审核 有效进项税额 for 2026-06
    r1 = ms.create_run(cid, "对账", kind="recon")
    ms.save_settlement_report(cid, r1, {"summary": {"到账": 5000, "实收": 5000}},
                              platform="amazon", period_label="2026-06")
    r2 = ms.create_run(cid, "发票审核", kind="crossborder")
    ms.save_crossborder_report(cid, r2, "invoice",
                               {"summary": {"有效进项税额": 130, "period_label": "2026-06"}},
                               period_label="2026-06")
    ctx = cbo._rebate_context(cid, "2026-06")
    assert ctx["recon_received"] == 5000
    assert ctx["input_tax_pool"] == 130
    # latest_crossborder_summary reader
    assert ms.latest_crossborder_summary(cid, "invoice", "2026-06")["有效进项税额"] == 130
    assert ms.latest_crossborder_summary(cid, "invoice", "1999-01") is None  # period miss
    assert cbo._rebate_context(cid, "1999-01") is None                       # nothing for period
    # a period-LESS run must NOT borrow (else it'd grab the newest of ANY period)
    assert cbo._rebate_context(cid, "") is None
    assert cbo._rebate_context(cid, None) is None


# --- 三流合一 imports the 订单 leg from 对账 -----------------------------------
def test_threeflow_imports_orders_from_recon():
    # no 订单 uploaded; the orchestrator passes recon's per-order list as context →
    # used as the 订单 flow so the four-flow set is complete (no 缺流).
    base = {
        "receipts": [{"订单号": "O1", "收款主体": "甲公司", "收款金额": "1000",
                      "币种": "USD", "收款日期": "2026-06-20"}],
        "customs": [{"报关单号": "BG1", "订单号": "O1", "经营单位": "甲公司",
                     "成交金额": "1000", "数量": "10", "报关日期": "2026-06-12"}],
        "invoices": [{"发票号": "FP1", "订单号": "O1", "销售方": "甲公司",
                      "价税合计": "1000", "数量": "10", "开票日期": "2026-06-11"}],
    }
    # without import → 缺流 (no 订单)
    bare = threeflow_engine.check_records(base)
    assert bare["summary"]["缺流"] == 1
    # with import → 订单 present, four-flow complete
    out = threeflow_engine.check_records(
        base, context={"orders_from_recon": [{"ref": "O1", "amount": 1000,
                                              "net": 980, "date": "2026-06-10"}]})
    assert out["summary"]["缺流"] == 0
    assert "订单(对账) 1 笔" in out["summary"]["上游带入"]


def test_uploaded_orders_win_over_recon_import():
    base = {
        "orders": [{"订单号": "O1", "卖方": "甲公司", "金额": "1000", "数量": "10"}],
        "receipts": [{"订单号": "O1", "收款主体": "甲公司", "收款金额": "1000"}],
        "customs": [{"报关单号": "BG1", "订单号": "O1", "经营单位": "甲公司",
                     "成交金额": "1000", "数量": "10"}],
        "invoices": [{"发票号": "FP1", "订单号": "O1", "销售方": "甲公司",
                      "价税合计": "1000", "数量": "10"}],
    }
    out = threeflow_engine.check_records(
        base, context={"orders_from_recon": [{"ref": "O1", "amount": 99999}]})
    assert out["summary"]["上游带入"] == ""          # upload present → no import
    assert out["summary"]["金额不一致"] == 0          # the 99999 import was ignored


def test_threeflow_context_builder_period_scoped(fresh_db):
    cid = ms.get_or_create_company("tf@co.com", company_name="TF")
    r1 = ms.create_run(cid, "对账", kind="recon")
    ms.save_settlement_report(
        cid, r1, {"summary": {}, "orders": [{"ref": "O1", "amount": 1000, "net": 980,
                                            "date": "2026-06-10"}]},
        platform="amazon", period_label="2026-06")
    ctx = cbo._threeflow_context(cid, "2026-06")
    assert ctx["orders_from_recon"][0]["ref"] == "O1"
    assert cbo._threeflow_context(cid, "") is None          # period-less → no import
    assert cbo._threeflow_context(cid, "1999-01") is None   # nothing for that period


# --- end-to-end through the route: rebate with ONLY exports borrows upstream --
def test_route_rebate_borrows_upstream(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    client = _login(store, "boss@co.com")
    cid = ms.get_or_create_company("boss@co.com",
                                   company_name=store.find_by_email("boss@co.com").get("company")
                                   or "boss")
    r1 = ms.create_run(cid, "对账", kind="recon")
    ms.save_settlement_report(cid, r1, {"summary": {"到账": 8000, "实收": 8000}},
                              platform="amazon", period_label="2026-06")
    r2 = ms.create_run(cid, "发票审核", kind="crossborder")
    ms.save_crossborder_report(cid, r2, "invoice",
                               {"summary": {"有效进项税额": 260, "period_label": "2026-06"}},
                               period_label="2026-06")

    import io
    exports = io.BytesIO("报关单号,订单号,出口额,退税率,报关日期\nBG1,O1,2000,13%,2026-06-12\n"
                         .encode("utf-8"))
    res = client.post("/api/crossborder/rebate/run",
                      data={"period": "2026-06", "exports_file": (exports, "exports.csv")},
                      content_type="multipart/form-data")
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    for _ in range(60):
        snap = client.get(f"/api/status/{body['job_id']}").get_json()
        if snap.get("status") in ("done", "error"):
            break
        time.sleep(0.1)
    assert snap["status"] == "done"

    out = client.get(f"/api/crossborder/rebate/result/{body['run_id']}").get_json()
    s = out["summary"]
    assert s["进项税额合计"] == 260.0                              # borrowed from 发票审核
    assert s["收汇状态"] == "对账佐证 ¥8,000.00/出口额 ¥2,000.00"  # corroborated by 对账
    assert "发票审核" in s["上游带入"] and "对账" in s["上游带入"]

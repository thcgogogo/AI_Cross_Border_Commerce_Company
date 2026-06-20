"""跨境财务 sub-tab 3 — 三流合一 / 四单合一 deterministic engine.

Cross-checks 订单 ↔ 收款/结汇 ↔ 报关 ↔ 发票 (+ 物流) per shared 订单号 and flags every
inconsistency. Evidence-assembler only: it decides no tax and files nothing. Pure,
deterministic, hermetic. PROVISIONAL columns/tolerances (the documented model).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import threeflow_engine as tf  # noqa: E402


def _order(ref, entity="甲公司", amount="1000", qty="10", buyer="海外客户"):
    return {"订单号": ref, "卖方": entity, "买方": buyer, "金额": amount,
            "币种": "USD", "数量": qty, "日期": "2026-06-10"}


def _receipt(ref, payee="甲公司", amount="1000"):
    return {"订单号": ref, "收款主体": payee, "收款金额": amount,
            "币种": "USD", "收款日期": "2026-06-20"}


def _customs(ref, exporter="甲公司", amount="1000", qty="10", no=None):
    return {"报关单号": no or ("BG" + ref), "订单号": ref, "经营单位": exporter,
            "成交金额": amount, "数量": qty, "报关日期": "2026-06-12"}


def _invoice(ref, seller="甲公司", amount="1000", qty="10", no=None):
    return {"发票号": no or ("FP" + ref), "订单号": ref, "销售方": seller,
            "价税合计": amount, "数量": qty, "开票日期": "2026-06-11"}


def _run(orders=None, receipts=None, customs=None, invoices=None, logistics=None):
    return tf.check_records({
        tf.ROLE_ORDERS: orders or [], tf.ROLE_RECEIPTS: receipts or [],
        tf.ROLE_CUSTOMS: customs or [], tf.ROLE_INVOICES: invoices or [],
        tf.ROLE_LOGISTICS: logistics or [],
    }, period_label="2026-06")


# --- the happy path: four flows agree → 一致, no exceptions ----------------------
def test_fully_consistent_transaction():
    out = _run([_order("O1")], [_receipt("O1")], [_customs("O1")], [_invoice("O1")])
    assert out["summary"]["交易数"] == 1
    assert out["summary"]["一致"] == 1 and out["summary"]["不一致"] == 0
    assert out["exceptions"] == []
    assert out["bridge"]["ok"] is True


# --- 主体不一致: 代理出口/实际货主 (payee ≠ exporter) ----------------------------
def test_entity_mismatch_flags_agency_export():
    out = _run([_order("O1", entity="甲公司")],
               [_receipt("O1", payee="香港离岸公司")],     # 收款 to a different entity
               [_customs("O1", exporter="甲公司")],
               [_invoice("O1", seller="甲公司")])
    causes = {e["cause"] for e in out["exceptions"]}
    assert "代理出口/实际货主存疑" in causes
    assert out["summary"]["主体不一致"] == 1
    assert out["summary"]["不一致"] == 1


def test_entity_mismatch_generic_when_no_agency():
    # seller on the invoice differs but payee == exporter → not the agency red flag
    out = _run([_order("O1", entity="甲公司")], [_receipt("O1", payee="甲公司")],
               [_customs("O1", exporter="甲公司")], [_invoice("O1", seller="乙公司")])
    causes = {e["cause"] for e in out["exceptions"]}
    assert "主体不一致(三流不合一)" in causes


# --- 金额不一致 -----------------------------------------------------------------
def test_amount_mismatch_flagged():
    out = _run([_order("O1", amount="1000")], [_receipt("O1", amount="1000")],
               [_customs("O1", amount="1000")], [_invoice("O1", amount="1200")])
    ex = [e for e in out["exceptions"] if e["cause"] == "金额不一致"]
    assert ex and ex[0]["amount"] == 200.0
    assert out["summary"]["金额不一致"] == 1


def test_amount_within_tolerance_is_consistent():
    out = _run([_order("O1", amount="1000.00")], [_receipt("O1", amount="1000.005")],
               [_customs("O1", amount="1000")], [_invoice("O1", amount="1000")])
    assert out["summary"]["金额不一致"] == 0


# --- 缺流: 有报关无收汇 → 收汇/退税风险 -----------------------------------------
def test_missing_flow_flags_receipt_risk():
    out = _run([_order("O1")], [], [_customs("O1")], [_invoice("O1")])  # no 收款
    ex = [e for e in out["exceptions"] if e["cause"] == "缺流"]
    assert ex and "收汇" in ex[0]["detail"]
    assert out["summary"]["缺流"] == 1


# --- 数量不一致 -----------------------------------------------------------------
def test_qty_mismatch_flagged():
    out = _run([_order("O1", qty="10")], [_receipt("O1")],
               [_customs("O1", qty="8")], [_invoice("O1", qty="10")])
    assert any(e["cause"] == "数量不一致" for e in out["exceptions"])


# --- bridge self-audit: Σ订单 vs Σ报关 ------------------------------------------
def test_bridge_imbalance_when_order_customs_totals_differ():
    out = _run(
        [_order("O1", amount="1000"), _order("O2", amount="500")],
        [_receipt("O1", amount="1000"), _receipt("O2", amount="500")],
        [_customs("O1", amount="1000"), _customs("O2", amount="400")],   # O2 under-declared
        [_invoice("O1", amount="1000"), _invoice("O2", amount="500")])
    assert out["bridge"]["ok"] is False
    assert out["bridge"]["imbalance"] == 100.0      # 1500 订单 − 1400 报关


# --- provenance + determinism ---------------------------------------------------
def test_provenance_anchored_and_engine_computed():
    out = _run([_order("O1")], [_receipt("O1")], [_customs("O1")], [_invoice("O1")])
    assert all(r["computed_by"] == "threeflow_engine" for r in out["source_rows"])
    assert any("【" in f for f in out["facts_block"])


def test_input_order_independent():
    a = _run([_order("O1"), _order("O2")], [_receipt("O1"), _receipt("O2")],
             [_customs("O1"), _customs("O2")], [_invoice("O1"), _invoice("O2")])
    b = _run([_order("O2"), _order("O1")], [_receipt("O2"), _receipt("O1")],
             [_customs("O2"), _customs("O1")], [_invoice("O2"), _invoice("O1")])
    assert a["summary"] == b["summary"]

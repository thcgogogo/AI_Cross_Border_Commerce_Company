"""出口退税核算 deterministic engine tests.

Estimates the 可退税额 = min(Σ出口额×退税率, Σ进项税额) and flags 进项不足 / 收汇缺失 /
退税率缺失. Evidence-assembler only: it estimates and flags, decides no tax, files
nothing. Pure, deterministic, hermetic. PROVISIONAL rates (退税率 varies by HS-code).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import rebate_engine as rb  # noqa: E402


def _input(no="FP1", seller="甲公司", amount="1130", tax="130", rate="13%",
           product="货物"):
    return {"发票号": no, "销售方": seller, "价税合计": amount, "税额": tax,
            "税率": rate, "开票日期": "2026-06-05", "货物名称": product}


def _export(ref="O1", fob="1000", rate="13%", no=None, product="货物"):
    row = {"报关单号": no or ("BG" + ref), "订单号": ref, "出口额": fob,
           "货物名称": product, "报关日期": "2026-06-10"}
    if rate is not None:
        row["退税率"] = rate
    return row


def _receipt(ref="O1", amount="1000", settled="1000"):
    return {"订单号": ref, "收汇金额": amount, "已结汇金额": settled,
            "结汇日期": "2026-06-20"}


def _run(inputs=None, exports=None, receipts=None):
    return rb.check_records({
        rb.ROLE_INPUT_INVOICES: inputs or [],
        rb.ROLE_EXPORTS: exports or [],
        rb.ROLE_RECEIPTS: receipts or [],
    }, period_label="2026-06")


# --- the clean path: 退税额 = fob×rate, input pool ample, 收汇 present ---------------
def test_clean_rebate_capped_by_inputs():
    # fob 1000 × 13% = 130 理论; input tax 200 → ample → 可退税额 == 130
    out = _run([_input(tax="200")], [_export("O1", fob="1000", rate="13%")],
               [_receipt("O1")])
    assert out["summary"]["出口额合计"] == 1000.0
    assert out["summary"]["进项税额合计"] == 200.0
    assert out["summary"]["理论退税额"] == 130.0
    assert out["summary"]["可退税额"] == 130.0
    assert out["summary"]["收汇状态"] == "已收汇1/总1"
    assert out["bridge"]["ok"] is True
    causes = {e["cause"] for e in out["exceptions"]}
    assert "进项不足" not in causes and "收汇缺失" not in causes


# --- 进项不足: 理论退税额 > 进项税额 → 可退税额 capped down, flagged -----------------
def test_input_shortfall_caps_refundable():
    # fob 1000 × 13% = 130 理论, but only 50 input tax → 可退税额 == 50
    out = _run([_input(tax="50")], [_export("O1", fob="1000", rate="13%")],
               [_receipt("O1")])
    ex = [e for e in out["exceptions"] if e["cause"] == "进项不足"]
    assert ex and ex[0]["severity"] == "高"
    assert ex[0]["amount"] == 80.0          # 130 理论 − 50 进项
    assert out["summary"]["可退税额"] == 50.0
    assert out["summary"]["理论退税额"] == 130.0
    assert out["bridge"]["ok"] is False


# --- 收汇缺失: an export with no settled receipt → 高 flag ---------------------------
def test_missing_settlement_flagged():
    out = _run([_input(tax="200")],
               [_export("O1", fob="1000"), _export("O2", fob="500")],
               [_receipt("O1")])              # only O1 settled
    ex = [e for e in out["exceptions"] if e["cause"] == "收汇缺失"]
    assert ex and ex[0]["ref"] == "O2" and ex[0]["severity"] == "高"
    assert "收汇" in ex[0]["detail"]
    assert out["summary"]["收汇状态"] == "已收汇1/总2"


def test_unsettled_receipt_does_not_count():
    # receipt exists but 已结汇金额 == 0 → not settled → still 收汇缺失
    out = _run([_input(tax="200")], [_export("O1", fob="1000")],
               [_receipt("O1", settled="0")])
    causes = {e["cause"] for e in out["exceptions"]}
    assert "收汇缺失" in causes
    assert out["summary"]["收汇状态"] == "已收汇0/总1"


# --- 收汇未提供: no receipts at all → ONE summary note, not per-export spam ----------
def test_no_receipts_emits_single_summary_note():
    out = _run([_input(tax="500")],
               [_export("O1"), _export("O2"), _export("O3")], [])
    notes = [e for e in out["exceptions"] if e["cause"] == "收汇未提供"]
    assert len(notes) == 1 and notes[0]["severity"] == "中"
    assert not any(e["cause"] == "收汇缺失" for e in out["exceptions"])
    assert out["summary"]["收汇状态"] == "未提供"


# --- 退税率缺失: an export with no 退税率 → default rate used + 中 flag ---------------
def test_default_rate_flag_when_rebate_rate_missing():
    out = _run([_input(tax="500")],
               [_export("O1", fob="1000", rate=None)], [_receipt("O1")])
    ex = [e for e in out["exceptions"] if e["cause"] == "退税率缺失(用默认)"]
    assert ex and ex[0]["severity"] == "中"
    # default 0.13 applied → 1000 × 0.13 == 130
    assert out["summary"]["理论退税额"] == 130.0


# --- 进项税额为0: input invoice has a 税率 but 税额 == 0 → data-quality 中 flag -------
def test_zero_tax_input_invoice_flagged():
    out = _run([_input(no="FPZ", tax="0", rate="13%")],
               [_export("O1", fob="1000")], [_receipt("O1")])
    ex = [e for e in out["exceptions"] if e["cause"] == "进项税额为0"]
    assert ex and ex[0]["ref"] == "FPZ" and ex[0]["severity"] == "中"


# --- bridge self-audit: ok when not input-constrained, lhs/rhs sane -----------------
def test_bridge_balanced_when_inputs_ample():
    out = _run([_input(tax="1000")],
               [_export("O1", fob="1000", rate="13%")], [_receipt("O1")])
    assert out["bridge"]["ok"] is True
    assert out["bridge"]["lhs"] == 130.0          # 理论退税额
    assert out["bridge"]["rhs"] == 130.0          # 可退税额 (not capped)
    assert out["bridge"]["imbalance"] == 0.0


def test_bridge_imbalance_when_input_constrained():
    out = _run([_input(tax="40")],
               [_export("O1", fob="1000", rate="13%")], [_receipt("O1")])
    assert out["bridge"]["ok"] is False
    assert out["bridge"]["lhs"] == 130.0          # 理论
    assert out["bridge"]["rhs"] == 40.0           # 可退 (capped to 进项)
    assert out["bridge"]["imbalance"] == 90.0


# --- provenance + determinism -------------------------------------------------------
def test_provenance_anchored_and_engine_computed():
    out = _run([_input(tax="200")], [_export("O1")], [_receipt("O1")])
    assert out["source_rows"]
    assert all(r["computed_by"] == "rebate_engine" for r in out["source_rows"])
    assert all(r["anchor"] == i + 1 for i, r in enumerate(out["source_rows"]))
    assert any("【" in f for f in out["facts_block"])


def test_input_order_independent():
    a = _run([_input(no="F1", tax="100"), _input(no="F2", tax="100")],
             [_export("O1", fob="500"), _export("O2", fob="500")],
             [_receipt("O1"), _receipt("O2")])
    b = _run([_input(no="F2", tax="100"), _input(no="F1", tax="100")],
             [_export("O2", fob="500"), _export("O1", fob="500")],
             [_receipt("O2"), _receipt("O1")])
    assert a["summary"] == b["summary"]
    assert a["bridge"] == b["bridge"]

"""进项发票审核 deterministic engine — hermetic tests.

Audits a batch of input-VAT 进项发票 for 退税/成本/合规: 抬头/税号 == 本公司, 作废/红冲,
普票不可抵扣, 税率异常, 重复发票, 超认证时限, 第三方查验, 税额自洽. Rolls up 有效进项税额
and (if 销项 given) 进销差. Evidence-assembler only: certifies nothing, files nothing.
The 真伪查验 is UPSTREAM — `verify_result` is plain data here, never a network call. Pure,
deterministic, hermetic. PROVISIONAL税率集/窗口/columns (the documented model).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import invoice_audit_engine as ia  # noqa: E402

CO_NAME = "甲公司"
CO_TAXNO = "91110000AAAAAAAA1X"


def _invoice(no, *, code="ABC", type="专票", buyer_name=CO_NAME, buyer_taxno=CO_TAXNO,
             seller="供应商A", amount="1130", tax="130", rate="13%",
             date="2026-06-10", status="正常", verify="一致"):
    return {"发票号": no, "发票代码": code, "发票类型": type, "购方名称": buyer_name,
            "购方税号": buyer_taxno, "销售方": seller, "价税合计": amount, "税额": tax,
            "税率": rate, "开票日期": date, "票面状态": status, "查验结果": verify}


def _sale(ref, *, tax="100", amount="1000", date="2026-06-15"):
    return {"凭证号": ref, "销项税额": tax, "金额": amount, "日期": date}


def _pack(**over):
    p = dict(ia.DEFAULT_PACK)
    p["company"] = {"name": CO_NAME, "taxno": CO_TAXNO}
    p.update(over)
    return p


def _run(invoices=None, sales=None, pack=None):
    return ia.check_records(
        {ia.ROLE_INVOICES: invoices or [], ia.ROLE_SALES: sales or []},
        pack or _pack(), period_label="2026-06")


# --- the happy path: a clean valid 专票 → no exceptions, counted in 有效进项税额 -------
def test_clean_special_invoice_no_exceptions():
    out = _run([_invoice("F1")])
    assert out["summary"]["发票总数"] == 1
    assert out["summary"]["有效进项专票数"] == 1
    assert out["summary"]["有效进项税额"] == 130.0
    assert out["summary"]["异常数"] == 0
    assert out["exceptions"] == []
    assert out["bridge"]["ok"] is True


# --- 1. 抬头/税号不符 ------------------------------------------------------------
def test_buyer_name_mismatch_flagged():
    out = _run([_invoice("F1", buyer_name="别家公司")])
    causes = {e["cause"] for e in out["exceptions"]}
    assert "抬头/税号不符" in causes
    assert out["summary"]["有效进项专票数"] == 1   # 抬头不符不剔除 有效计数 (仍是专票/正常)


def test_buyer_taxno_mismatch_flagged():
    out = _run([_invoice("F1", buyer_taxno="91110000ZZZZZZZZ9Y")])
    ex = [e for e in out["exceptions"] if e["cause"] == "抬头/税号不符"]
    assert ex and ex[0]["ref"] == "F1" and ex[0]["severity"] == "高"


# --- 2. 作废/红冲 ----------------------------------------------------------------
def test_void_invoice_flagged_and_excluded():
    out = _run([_invoice("F1", status="作废")])
    causes = {e["cause"] for e in out["exceptions"]}
    assert "作废/红冲发票" in causes
    assert out["summary"]["作废红冲数"] == 1
    assert out["summary"]["有效进项专票数"] == 0      # excluded from 有效进项
    assert out["summary"]["有效进项税额"] == 0.0


# --- 3. 普票不可抵扣 -------------------------------------------------------------
def test_general_invoice_excluded_from_deductible():
    out = _run([_invoice("F1", type="普票")])
    causes = {e["cause"] for e in out["exceptions"]}
    assert "普票不可抵扣进项" in causes
    assert out["summary"]["普票数"] == 1
    assert out["summary"]["有效进项专票数"] == 0
    assert out["summary"]["有效进项税额"] == 0.0


# --- 4. 税率异常 -----------------------------------------------------------------
def test_abnormal_rate_flagged():
    # 17% is not in the legal set; recompute tax to keep 税额自洽 quiet.
    out = _run([_invoice("F1", rate="17%", amount="1170", tax="170")])
    assert any(e["cause"] == "税率异常" for e in out["exceptions"])


# --- 5. 重复发票号 ---------------------------------------------------------------
def test_duplicate_invoice_number_flagged():
    out = _run([_invoice("F1"), _invoice("F1")])
    dups = [e for e in out["exceptions"] if e["cause"] == "重复发票号"]
    assert len(dups) == 1                         # only the 2nd occurrence fires
    assert out["summary"]["重复数"] == 1
    assert out["summary"]["有效进项专票数"] == 1   # the duplicate is dropped from 有效


# --- 6. 超查验时限(5年) — the 认证/抵扣 deadline is abolished; this is the 查验 platform limit -
def test_stale_invoice_beyond_5yr_verify_window_flagged():
    # one fresh anchor + one >5 years older (查验平台 can't verify online) → 低-severity info.
    out = _run([_invoice("F1", date="2026-06-10"),
                _invoice("F2", date="2020-01-01")], pack=_pack())
    stale = [e for e in out["exceptions"] if e["cause"].startswith("超查验时限")]
    assert stale and stale[0]["ref"] == "F2" and stale[0]["severity"] == "低"


def test_within_5yr_not_flagged():
    # ~2.5 years old → within the 5-year 查验 window → NOT flagged (no认证 deadline anymore).
    out = _run([_invoice("F1", date="2026-06-10"),
                _invoice("F2", date="2024-01-01")], pack=_pack())
    assert not any(e["cause"].startswith("超查验时限") for e in out["exceptions"])


def test_verify_window_override_respected():
    out = _run([_invoice("F1", date="2026-06-10"),
                _invoice("F2", date="2026-03-01")],
               pack=_pack(verify_window_days=30))
    assert any(e["cause"].startswith("超查验时限") for e in out["exceptions"])


# --- 7. 查验未通过 ---------------------------------------------------------------
def test_verify_failed_flagged_and_excluded():
    out = _run([_invoice("F1", verify="不一致")])
    causes = {e["cause"] for e in out["exceptions"]}
    assert "查验未通过(请核实)" in causes
    assert out["summary"]["查验未通过数"] == 1
    assert out["summary"]["有效进项专票数"] == 0   # not 一致 → not 有效


def test_unverified_blank_is_still_valid():
    out = _run([_invoice("F1", verify="")])        # 未查验 (空) 不视为 查验未通过
    assert out["summary"]["查验未通过数"] == 0
    assert out["summary"]["有效进项专票数"] == 1


# --- 8. 税额与价税不符 -----------------------------------------------------------
def test_tax_amount_self_consistency_flagged():
    # 价税合计 1130 @13% implies 税额≈130; declare 200 → mismatch.
    out = _run([_invoice("F1", amount="1130", tax="200", rate="13%")])
    assert any(e["cause"] == "税额与价税不符" for e in out["exceptions"])
    assert out["bridge"]["ok"] is False            # Σ票面 ≠ Σ推算


# --- 进销差 when 销项 supplied ---------------------------------------------------
def test_input_output_gap_when_sales_given():
    out = _run([_invoice("F1", tax="130")], sales=[_sale("S1", tax="100")])
    assert "进销差" in out["summary"]
    assert out["summary"]["进销差"] == 30.0         # 130 进项 − 100 销项


def test_no_sales_omits_gap():
    out = _run([_invoice("F1")])
    assert "进销差" not in out["summary"]


# --- provenance + determinism ---------------------------------------------------
def test_provenance_anchored_and_engine_computed():
    out = _run([_invoice("F1")])
    assert all(r["computed_by"] == "invoice_audit_engine" for r in out["source_rows"])
    assert any("【" in f for f in out["facts_block"])


def test_input_order_independent():
    a = _run([_invoice("F1"), _invoice("F2", date="2026-06-05")])
    b = _run([_invoice("F2", date="2026-06-05"), _invoice("F1")])
    assert a["summary"] == b["summary"]

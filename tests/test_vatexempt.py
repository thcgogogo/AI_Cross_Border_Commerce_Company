"""9610 综试区零售出口 — 无票免税 + 核定征收 deterministic engine.

Judges each 出口 row for 无票免税 eligibility (综服平台登记 + 资格 + 适用窗口), sums the符合
出口额, estimates核定征收企业所得税 (PROVISIONAL率), and gauges成本票据 coverage for the所得税
扣除. Evidence-assembler only: it decides no tax, files nothing. Pure, deterministic,
hermetic. PROVISIONAL columns/tolerances/rates/windows (the documented model). The
load-bearing纳税口径: 无票免税仅免增值税，不替代企业所得税的成本扣除.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import vatexempt_engine as ve  # noqa: E402


def _export(ref, amount="1000", registered="是", date="2026-06-10"):
    return {"出口单号": ref, "出口额": amount, "综服平台登记": registered,
            "出口日期": date}


def _cost(no, amount="1000", date="2026-06-08"):
    return {"发票号": no, "金额": amount, "开票日期": date}


def _run(exports=None, costs=None, pack=None):
    body = {ve.ROLE_EXPORTS: exports or [], ve.ROLE_COSTS: costs or []}
    return ve.check_records(body, pack=pack, period_label="2026-06")


# --- the happy path: a登记 + 资格 + 在窗口 export → 符合, no exception ----------------
def test_fully_eligible_export_counts_no_exception():
    out = _run([_export("E1", amount="1000")])
    assert out["summary"]["出口额合计"] == 1000.0
    assert out["summary"]["符合无票免税出口额"] == 1000.0
    assert out["summary"]["不符合数"] == 0
    assert out["summary"]["交易数"] == 1
    # no per-export exception (成本票据未提供 note may exist; assert no eligibility ones)
    causes = {e["cause"] for e in out["exceptions"]}
    assert "未登记综服平台" not in causes
    assert "超无票免税适用窗口(请核实)" not in causes
    assert "综试区资格存疑" not in causes


# --- 未登记综服平台 → 高 exception, excluded from符合出口额 -----------------------------
def test_unregistered_export_flagged_and_excluded():
    out = _run([_export("E1", amount="1000", registered="否")])
    ex = [e for e in out["exceptions"] if e["cause"] == "未登记综服平台"]
    assert ex and ex[0]["severity"] == "高"
    assert ex[0]["amount"] == 1000.0
    assert "综服平台" in ex[0]["detail"]
    assert out["summary"]["符合无票免税出口额"] == 0.0
    assert out["summary"]["不符合数"] == 1


def test_empty_registration_flag_is_unregistered():
    out = _run([_export("E1", registered="")])
    assert any(e["cause"] == "未登记综服平台" for e in out["exceptions"])


# --- 财税[2018]103号 sets NO time window → an old registered export is still eligible ---
def test_no_time_window_old_export_still_eligible():
    out = _run([
        _export("E1", amount="500", date="2026-06-10"),     # recent
        _export("E2", amount="700", date="2020-01-01"),     # years old — STILL eligible
    ])
    causes = {e["cause"] for e in out["exceptions"]}
    assert "超无票免税适用窗口(请核实)" not in causes        # the 36-month window was removed
    assert out["summary"]["符合无票免税出口额"] == 1200.0   # both count (both registered)
    assert out["summary"]["不符合数"] == 0


# --- 综试区资格存疑: pack qualified False → one summary-level高 exception --------------
def test_unqualified_company_flags_summary_exception():
    pack = dict(ve.DEFAULT_PACK)
    pack["qualified"] = False
    out = _run([_export("E1", amount="1000"), _export("E2", amount="500")], pack=pack)
    ex = [e for e in out["exceptions"] if e["cause"] == "综试区资格存疑"]
    assert len(ex) == 1 and ex[0]["severity"] == "高"
    assert ex[0]["ref"] is None
    assert out["summary"]["符合无票免税出口额"] == 0.0
    assert out["summary"]["不符合数"] == 2          # all exports ineligible


# --- 成本票据不足(影响所得税) ----------------------------------------------------------
def test_insufficient_cost_invoices_flagged():
    # 符合出口额 1000 × cost_target 0.6 = 600 needed; only 100 provided.
    out = _run([_export("E1", amount="1000")], [_cost("C1", amount="100")])
    ex = [e for e in out["exceptions"] if e["cause"] == "成本票据不足(影响所得税)"]
    assert ex and ex[0]["severity"] == "中"
    assert ex[0]["amount"] == 500.0                # 600 - 100
    assert "所得税" in ex[0]["detail"]


def test_sufficient_cost_invoices_no_flag():
    out = _run([_export("E1", amount="1000")], [_cost("C1", amount="800")])
    causes = {e["cause"] for e in out["exceptions"]}
    assert "成本票据不足(影响所得税)" not in causes
    assert out["summary"]["成本票据合计"] == 800.0


# --- 成本票据未提供: note when no cost invoices given -------------------------------
def test_missing_cost_invoices_noted():
    out = _run([_export("E1", amount="1000")])
    ex = [e for e in out["exceptions"] if e["cause"] == "成本票据未提供"]
    assert ex and ex[0]["severity"] == "中"
    assert "无票免税仅免增值税" in ex[0]["detail"]
    assert out["summary"]["成本票据合计"] == 0.0


# --- 核定征收 estimate math: 符合出口额 × 0.04, × 0.25 -------------------------------
def test_assessed_estimate_math():
    out = _run([_export("E1", amount="1000")], [_cost("C1", amount="1000")])
    assert out["summary"]["符合无票免税出口额"] == 1000.0
    assert out["summary"]["核定应税所得"] == 40.0       # 1000 × 0.04
    assert out["summary"]["估算企业所得税"] == 10.0      # 40 × 0.25


def test_assessed_estimate_respects_pack_rates():
    pack = dict(ve.DEFAULT_PACK)
    pack["assessed_income_rate"] = 0.05
    pack["cit_rate"] = 0.20
    out = _run([_export("E1", amount="2000")], [_cost("C1", amount="2000")], pack=pack)
    assert out["summary"]["核定应税所得"] == 100.0      # 2000 × 0.05
    assert out["summary"]["估算企业所得税"] == 20.0     # 100 × 0.20


# --- bridge self-audit: 符合出口额 ≤ 出口额合计 -------------------------------------
def test_bridge_subset_invariant_holds():
    out = _run([
        _export("E1", amount="1000", registered="是"),
        _export("E2", amount="500", registered="否"),     # excluded
    ])
    assert out["bridge"]["ok"] is True
    assert out["bridge"]["lhs"] == 1000.0               # 符合
    assert out["bridge"]["rhs"] == 1500.0               # 合计
    assert out["bridge"]["imbalance"] == -500.0


# --- provenance + determinism + the VAT-only NUANCE in facts -----------------------
def test_provenance_anchored_and_engine_computed():
    out = _run([_export("E1")])
    assert all(r["computed_by"] == "vatexempt_engine" for r in out["source_rows"])
    assert any("【" in f for f in out["facts_block"])


def test_vat_only_nuance_present_in_facts():
    out = _run([_export("E1")])
    blob = "".join(out["facts_block"])
    assert "无票免税仅免增值税，不替代企业所得税的成本扣除" in blob


def test_input_order_independent():
    a = _run([_export("E1", amount="1000"), _export("E2", amount="500")],
             [_cost("C1", amount="300"), _cost("C2", amount="200")])
    b = _run([_export("E2", amount="500"), _export("E1", amount="1000")],
             [_cost("C2", amount="200"), _cost("C1", amount="300")])
    assert a["summary"] == b["summary"]
    assert a["bridge"] == b["bridge"]

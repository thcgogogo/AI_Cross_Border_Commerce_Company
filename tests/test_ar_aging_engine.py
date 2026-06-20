"""应收账款 (AR aging) — deterministic-engine acceptance tests.

Hermetic, no network, no LLM (the engine is pure Python — the reliability moat):

  Payment application (the crux):
    - FIFO oldest-first; explicit invoice_ref precedence; ref_fallback (bad ref);
      partial pay; overpayment + orphan/unapplied cash surfaced (never netted).
  Aging:  bucket boundaries (current/0-30/31-60/61-90/90+ on due_date), dpd; DSO.
  Self-audit:  identities A (应收==已核销+未结), B (回款==已核销+未核销), C (per-customer)
               hold on valid data; D (recon cross-check) flips 疑似 / is None w/o recon.
  Grading:  R1–R10 -> 确定 / 疑似 with the right reason; clean overdue -> 确定.
  Learned rule: a prior one-tap classify reclassifies the same line next run.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import ar_aging_engine as eng  # noqa: E402
from agent_team import ar_templates as templates  # noqa: E402

PACK = templates.load_pack("通用")


def _run(invoices, payments, customers=None, *, as_of="2026-06-30",
         recon=None, learned=None, pack=PACK):
    return eng.aging_from_records(
        {"invoices": invoices, "payments": payments, "customers": customers or []},
        pack, as_of=as_of, recon_totals=recon, learned_rules=learned)


def _inv(no, cust, gross, *, issue=None, due=None, terms=None, note=""):
    return {"发票号": no, "客户编号": cust, "客户名称": cust, "开票日期": issue or "",
            "到期日": due or "", "账期天数": terms or "", "金额": str(gross), "备注": note}


def _pay(no, cust, amount, *, date="2026-06-05", ref=""):
    return {"流水号": no, "客户编号": cust, "收款日期": date,
            "收款金额": str(amount), "对应发票号": ref}


def _cust(cust, *, terms=None, limit=None):
    return {"客户编号": cust, "客户名称": cust, "账期天数": terms or "",
            "信用额度": "" if limit is None else str(limit), "联系方式": "boss@x.com"}


def _open_by_no(r):
    return {i["invoice_no"]: i["open_balance"] for i in r["invoices"]}


# ---------------------------------------------------------------------------
# Payment application
# ---------------------------------------------------------------------------
def test_fifo_oldest_first():
    r = _run(
        [_inv("INV1", "C1", 100, due="2026-05-01"),
         _inv("INV2", "C1", 100, due="2026-06-01")],
        [_pay("P1", "C1", 100)])
    apps = r["applications"]
    assert len(apps) == 1 and apps[0]["invoice_no"] == "INV1"  # oldest first
    assert apps[0]["applied_via"] == "fifo"
    opens = _open_by_no(r)
    assert "INV1" not in opens                       # settled -> excluded
    assert opens["INV2"] == 100.0
    assert r["summary"]["应收账款总额"] == 100.0


def test_by_reference_precedence():
    r = _run(
        [_inv("INV1", "C1", 100, due="2026-05-01"),
         _inv("INV2", "C1", 100, due="2026-06-01")],
        [_pay("P1", "C1", 100, ref="INV2")])             # ref the NEWER, not the oldest
    apps = r["applications"]
    assert apps[0]["invoice_no"] == "INV2" and apps[0]["applied_via"] == "ref"
    opens = _open_by_no(r)
    assert opens["INV1"] == 100.0 and "INV2" not in opens


def test_ref_fallback_flags_suspect():
    r = _run([_inv("INV1", "C1", 100, due="2026-05-01")],
             [_pay("P1", "C1", 60, ref="NOPE")])          # bad ref -> FIFO + 疑似
    row = r["invoices"][0]
    assert row["open_balance"] == 40.0 and row["grade"] == "疑似"
    assert eng.SUSPECT_ORPHAN_PAYMENT in row["suspect_reasons"]
    assert eng.SUSPECT_PARTIAL_PAY in row["suspect_reasons"]
    assert "ref_fallback" in row["applied_via"]


def test_partial_payment():
    r = _run([_inv("INV1", "C1", 100, due="2026-05-01")], [_pay("P1", "C1", 30)])
    row = r["invoices"][0]
    assert row["open_balance"] == 70.0
    assert row["grade"] == "疑似" and eng.SUSPECT_PARTIAL_PAY in row["suspect_reasons"]


def test_overpayment_and_orphan_unapplied_surface():
    r = _run(
        [_inv("INV1", "C1", 100, due="2026-05-01")],
        [_pay("P1", "C1", 150), _pay("P2", "C2", 80)])    # C1 overpays; C2 has no invoice
    kinds = {u["customer_id"]: u for u in r["unapplied"]}
    assert kinds["C1"]["amount"] == 50.0 and kinds["C1"]["kind"] == "overpaid"
    assert kinds["C2"]["amount"] == 80.0 and kinds["C2"]["kind"] == "unapplied"
    assert r["summary"]["应收账款总额"] == 0.0           # nothing open
    cust = {c["customer_id"]: c for c in r["customers"]}
    assert cust["C1"]["credit"] == 50.0 and cust["C1"]["grade"] == "疑似"
    assert eng.SUSPECT_OVERPAID in cust["C1"]["suspect_reasons"]
    assert eng.SUSPECT_ORPHAN_PAYMENT in cust["C2"]["suspect_reasons"]
    assert r["bridge"]["payments_ok"] is True            # 回款 conserved


# ---------------------------------------------------------------------------
# Aging buckets + DSO
# ---------------------------------------------------------------------------
def test_aging_buckets():
    r = _run(
        [_inv("A", "C1", 100, due="2026-06-20"),   # dpd 10  -> 0-30
         _inv("B", "C1", 100, due="2026-05-20"),   # dpd 41  -> 31-60
         _inv("C", "C1", 100, due="2026-04-15"),   # dpd 76  -> 61-90
         _inv("D", "C1", 100, due="2026-01-01"),   # dpd 180 -> 90+
         _inv("E", "C1", 100, due="2026-07-10")],  # not overdue -> current
        [], as_of="2026-06-30")
    s = r["summary"]
    assert (s["0-30"], s["31-60"], s["61-90"], s["90+"], s["current"]) == \
        (100.0, 100.0, 100.0, 100.0, 100.0)
    assert s["逾期总额"] == 400.0 and s["应收账款总额"] == 500.0
    assert s["逾期发票数"] == 4


def test_dso_formula_and_insufficient_data():
    r = _run([_inv("A", "C1", 100, issue="2026-06-01", due="2026-07-31"),
              _inv("B", "C1", 100, issue="2026-06-30", due="2026-07-31")],
             [], as_of="2026-07-15")
    # open 200 / sales 200 * window(30 days incl.) = 30.0
    assert r["summary"]["DSO"] == 30.0
    assert _run([], [], as_of="2026-06-30")["summary"]["DSO"] is None


# ---------------------------------------------------------------------------
# Self-audit identities
# ---------------------------------------------------------------------------
def test_self_audit_identities_hold():
    r = _run([_inv("A", "C1", 100, due="2026-05-01"),
              _inv("B", "C2", 200, due="2026-05-01")],
             [_pay("P1", "C1", 60), _pay("P2", "C2", 200)])
    b = r["bridge"]
    assert b["ok"] is True and b["payments_ok"] is True and b["per_customer_ok"] is True
    assert b["customer_imbalances"] == []


def test_recon_crosscheck_flips_suspect_and_none_without_recon():
    inv = [_inv("A", "C1", 100, due="2026-05-01")]
    pays = [_pay("P1", "C1", 60)]
    ok = _run(inv, pays, recon={"到账": 60})
    assert ok["bridge"]["recon_crosscheck_ok"] is True
    bad = _run(inv, pays, recon={"到账": 999})
    assert bad["bridge"]["recon_crosscheck_ok"] is False
    c1 = [c for c in bad["customers"] if c["customer_id"] == "C1"][0]
    assert eng.SUSPECT_PENDING_RECON in c1["suspect_reasons"] and c1["grade"] == "疑似"
    none = _run(inv, pays)
    assert none["bridge"]["recon_crosscheck_ok"] is None   # graceful fallback


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------
def test_clean_overdue_is_confirmed():
    r = _run([_inv("A", "C1", 100, due="2026-05-01")], [], as_of="2026-06-30")
    row = r["invoices"][0]
    assert row["grade"] == "确定" and row["suspect_reasons"] == []
    exc = [e for e in r["exceptions"] if e["invoice_ref"] == "A"][0]
    assert exc["cause"] == eng.CAUSE_OVERDUE and exc["grade"] == "确定"


def test_derived_due_date_is_suspect():
    # no due_date, no row/customer terms -> default 30 -> due 2026-05-31 -> overdue
    r = _run([_inv("A", "C1", 100, issue="2026-05-01")], [], as_of="2026-06-30")
    row = r["invoices"][0]
    assert row["grade"] == "疑似" and eng.SUSPECT_NO_DUE_DATE in row["suspect_reasons"]


def test_dispute_note_is_suspect():
    r = _run([_inv("A", "C1", 100, due="2026-05-01", note="客户对账中存在争议")], [],
             as_of="2026-06-30")
    row = r["invoices"][0]
    assert row["grade"] == "疑似" and eng.SUSPECT_DISPUTED in row["suspect_reasons"]


def test_credit_limit_anomaly_is_suspect():
    r = _run([_inv("A", "C1", 100, due="2026-05-01")], [],
             customers=[_cust("C1", limit=50)], as_of="2026-06-30")
    c1 = [c for c in r["customers"] if c["customer_id"] == "C1"][0]
    assert eng.SUSPECT_CREDIT_ANOMALY in c1["suspect_reasons"] and c1["grade"] == "疑似"


def test_learned_rule_reclassifies_next_run():
    inv = [_inv("A", "C1", 100, due="2026-05-01")]
    first = _run(inv, [], as_of="2026-06-30")
    exc = [e for e in first["exceptions"] if e["invoice_ref"] == "A"][0]
    assert exc["cause"] == eng.CAUSE_OVERDUE
    rule = {"match": {"invoice_ref": "A"}, "cause": "已确认", "label": "已确认"}
    second = _run(inv, [], as_of="2026-06-30", learned=[rule])
    exc2 = [e for e in second["exceptions"] if e["invoice_ref"] == "A"][0]
    assert exc2["cause"] == "已确认" and exc2.get("user_label") == "已确认"


# ---------------------------------------------------------------------------
# Provenance / facts (the writer-verbatim contract)
# ---------------------------------------------------------------------------
def test_provenance_anchors_and_computed_by():
    r = _run([_inv("A", "C1", 100, due="2026-05-01")], [], as_of="2026-06-30")
    assert r["source_rows"] and all(s["computed_by"] == "ar_aging_engine"
                                    for s in r["source_rows"])
    # every fact carries a 【N】 anchor matching a source row
    anchors = {s["anchor"] for s in r["source_rows"]}
    for fact in r["facts_block"]:
        assert "【" in fact and "】" in fact
    assert len(anchors) == len(r["source_rows"])

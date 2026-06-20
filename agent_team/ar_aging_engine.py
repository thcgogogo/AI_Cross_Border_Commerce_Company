"""
ar_aging_engine.py — The DETERMINISTIC 应收账款 (AR aging) engine.

The 应收 sibling of settlement_engine.py: a pure-Python, no-LLM engine that does ALL
the arithmetic and ALL the payment-to-invoice application, then hands the report/催收
writer finished numbers it must quote verbatim. The LLM never computes a figure, decides
which accounts are overdue, or alters a number — that is the entire reliability story.

What it does, given three normalized source lists (invoices 应收 / payments 回款 /
customer master) and an AR pack (ar_templates):

  1. NORMALIZE   raw rows -> canonical dicts (column map + cleaning), per role.
  2. APPLY       received payments to specific OPEN invoices, deterministically:
                   - honor an explicit invoice_ref first (else FIFO oldest-first),
                   - partial payments reduce that invoice's open balance,
                   - overpayments / unapplied cash are SURFACED, never silently netted.
                 (You cannot age a net invoiced−paid balance — you must know WHICH
                 invoices are open.)
  3. AGE         per-invoice open balance -> overdue buckets (0-30/31-60/61-90/90+),
                 per-customer outstanding, DSO, overdue flags.
  4. GRADE       每笔/每户 -> 确定 (clearly overdue, clean) vs 疑似 (disputed / partial /
                 unapplied / pending-reconciliation / over-limit).
  5. SELF-AUDIT  pure-Python conservation identities (Σ应收 == Σ已核销 + Σ未结;
                 Σ回款 == Σ已核销 + Σ未核销). If one fails, FLAG it (bridge.ok=False).

Everything returns plain dicts/lists (never ORM objects), money as float rounded to the
fen, so it's thread-safe across the background JOBS workers and JSON-serializable. The
trivial numeric/date helpers are copied 1:1 from settlement_engine (a future shared
chassis will dedupe them); AR is otherwise independent of the 对账 module by design.
"""

import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from . import ar_templates as templates

AMOUNT_TOLERANCE = Decimal("0.01")
_ZERO = Decimal("0")

# computed_by tag for every source row (mirrors settlement_engine's recon_engine).
COMPUTED_BY = "ar_aging_engine"

# 异常分级 (NEW — 对账 has no grader).
GRADE_CONFIRMED = "确定"
GRADE_SUSPECTED = "疑似"

# Suspect-reason vocabulary (中文; shared with store + tests, like 对账's CAUSE_*).
SUSPECT_PARTIAL_PAY = "部分回款"
SUSPECT_NO_DUE_DATE = "账期缺失(默认推算)"
SUSPECT_DISPUTED = "争议/备注"
SUSPECT_ORPHAN_PAYMENT = "回款无对应发票"
SUSPECT_UNAPPLIED_CASH = "未核销余款"
SUSPECT_OVERPAID = "超额回款"
SUSPECT_CREDIT_ANOMALY = "超信用额度"
SUSPECT_PENDING_RECON = "待对账确认"

# The cause stamped on a clean (确定) overdue worklist item.
CAUSE_OVERDUE = "逾期应收"


# ---------------------------------------------------------------------------
# Numeric / date helpers (pure) — copied 1:1 from settlement_engine (CHASSIS-CANDIDATE)
# ---------------------------------------------------------------------------
def _money(x):
    """Parse a cell into a Decimal yuan amount. Tolerates backtick prefix, ¥/￥ signs,
    thousands separators, parentheses-for-negative, blanks -> 0."""
    if x is None:
        return _ZERO
    if isinstance(x, Decimal):
        return x
    if isinstance(x, (int, float)):
        try:
            return Decimal(str(x))
        except InvalidOperation:
            return _ZERO
    s = str(x).strip().lstrip("`").strip()
    if not s:
        return _ZERO
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("¥", "").replace("￥", "").replace(",", "").replace(" ", "")
    if s in ("", "-", "--", "N/A", "无"):
        return _ZERO
    try:
        d = Decimal(s)
    except InvalidOperation:
        return _ZERO
    return -d if neg else d


def _money_opt(x):
    """Like _money but returns None for a blank/absent cell (used for credit_limit,
    where None means 'no limit' rather than zero)."""
    if x is None:
        return None
    s = str(x).strip().lstrip("`").strip()
    if not s or s in ("-", "--", "N/A", "无"):
        return None
    return _money(x)


def _f2(d):
    """Decimal/number -> float rounded to the fen (for JSON output)."""
    if d is None:
        return 0.0
    if not isinstance(d, Decimal):
        d = _money(d)
    return float(d.quantize(Decimal("0.01")))


def _clean(x):
    if x is None:
        return ""
    return str(x).strip().lstrip("`").strip()


def _parse_date(x):
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    s = _clean(x)
    if not s:
        return None
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _int_opt(x):
    s = _clean(x)
    if not s:
        return None
    m = re.search(r"-?\d+", s)
    return int(m.group(0)) if m else None


def _amt_match(a, b, tol=AMOUNT_TOLERANCE):
    return abs(a - b) <= tol


def _add_days(d, days):
    return d + timedelta(days=int(days))


def _fmt(d):
    """Format a Decimal/number as a thousands-separated 2dp string (for human_cn)."""
    return f"{_f2(d):,.2f}"


# ---------------------------------------------------------------------------
# Normalization: raw record dicts -> canonical AR row dicts
# ---------------------------------------------------------------------------
_TEXT_FIELDS = ("invoice_no", "customer_id", "customer_name", "currency", "note",
                "payment_no", "invoice_ref", "contact")
_MONEY_FIELDS = ("gross_amount", "amount")
_DATE_FIELDS = ("issue_date", "due_date", "pay_date")
_INT_FIELDS = ("terms_days",)


def normalize_rows(records, role, pack, source_file):
    """Map a list of raw row dicts (keyed by the source's own column headers) to
    canonical AR dicts using the pack's column_map for `role`. Pure; no file IO."""
    col = templates.column_map_for(pack, role)
    out = []
    for i, rec in enumerate(records or []):
        if not isinstance(rec, dict):
            continue
        row = {
            "source": role,
            "source_file": source_file,
            "row_id": i + 1,
            "raw": {str(k): _clean(v) for k, v in rec.items()},
        }
        for f in _TEXT_FIELDS:
            row[f] = _clean(rec.get(col.get(f))) if col.get(f) else ""
        for f in _MONEY_FIELDS:
            row[f] = _money(rec.get(col.get(f))) if col.get(f) else _ZERO
        for f in _DATE_FIELDS:
            row[f] = _parse_date(rec.get(col.get(f))) if col.get(f) else None
        for f in _INT_FIELDS:
            row[f] = _int_opt(rec.get(col.get(f))) if col.get(f) else None
        # credit_limit: None means "no limit" (so a blank cell never over-flags R9).
        row["credit_limit"] = _money_opt(rec.get(col.get("credit_limit"))) \
            if col.get("credit_limit") else None
        out.append(row)
    return out


def _cust_key(row):
    """Stable per-customer grouping key (id preferred, else name)."""
    return (row.get("customer_id") or "").strip() or (row.get("customer_name") or "").strip()


# ---------------------------------------------------------------------------
# Step 1 — due-date derivation
# ---------------------------------------------------------------------------
def _derive_due_dates(invoices, cust_master, default_terms):
    """Fill due_date where absent: issue_date + terms (row terms -> customer terms ->
    default). Stamps inv['_due_derived'] when the date was not given explicitly."""
    for inv in invoices:
        inv["_due_derived"] = False
        if inv.get("due_date"):
            continue
        issue = inv.get("issue_date")
        if not issue:
            continue  # no basis to derive; stays None (graded 疑似 via R3 below)
        terms = inv.get("terms_days")
        if terms is None:
            cm = cust_master.get(_cust_key(inv))
            terms = cm.get("terms_days") if cm else None
        if terms is None:
            terms = default_terms
        inv["due_date"] = _add_days(issue, terms)
        inv["_due_derived"] = True


# ---------------------------------------------------------------------------
# Step 2 — payment-to-invoice application (the crux; deterministic, never nets)
# ---------------------------------------------------------------------------
def _fifo_key(inv):
    due = inv.get("due_date")
    issue = inv.get("issue_date")
    return (due is None, due or date.min, issue is None, issue or date.min,
            inv.get("invoice_no") or "")


def apply_payments(invoices, payments, *, tol):
    """Apply payments to invoices. Mutates each invoice's working `_open` (and records
    `_via`), and returns (applications, open_invoices, unapplied). Deterministic:
    by-reference first, then FIFO oldest-first; partials reduce open; leftover cash is
    surfaced in `unapplied` (never subtracted from any invoice)."""
    ftol = float(tol)
    for inv in invoices:
        inv["_open"] = inv.get("gross_amount") or _ZERO
        inv["_via"] = []

    inv_by_no = {}
    inv_by_customer = {}
    for inv in invoices:
        no = (inv.get("invoice_no") or "").strip()
        if no:
            inv_by_no.setdefault(no, inv)  # first wins on duplicate invoice_no
        inv_by_customer.setdefault(_cust_key(inv), []).append(inv)
    for lst in inv_by_customer.values():
        lst.sort(key=_fifo_key)
    invoice_customers = set(inv_by_customer.keys())

    applications, unapplied = [], []

    def _apply(p, inv, amount, via):
        inv["_open"] -= amount
        if via not in inv["_via"]:
            inv["_via"].append(via)
        applications.append({
            "payment_no": p.get("payment_no") or "",
            "invoice_no": inv.get("invoice_no") or "",
            "customer_id": _cust_key(inv),
            "amount": _f2(amount),
            "applied_via": via,
            "pay_date": p["pay_date"].isoformat() if p.get("pay_date") else "",
        })

    def _pay_sort_key(p):
        d = p.get("pay_date")
        return (d is None, d or date.min, p.get("payment_no") or "")

    for p in sorted(payments, key=_pay_sort_key):
        remaining = p.get("amount") or _ZERO
        cust = _cust_key(p)
        ref = (p.get("invoice_ref") or "").strip()
        ref_bad = False

        # (3) by-reference first
        if ref:
            target = inv_by_no.get(ref)
            same_cust = target is not None and (not cust or _cust_key(target) == cust)
            if target is not None and same_cust and target["_open"] > tol:
                applied = min(remaining, target["_open"])
                if applied > tol:
                    _apply(p, target, applied, "ref")
                    remaining -= applied
            elif target is None or not same_cust:
                ref_bad = True  # ref pointed nowhere -> fall through to FIFO + flag

        # (4) FIFO residual against the payment's customer, oldest-first
        if remaining > tol:
            for inv in inv_by_customer.get(cust, []):
                if remaining <= tol:
                    break
                if inv["_open"] <= tol:
                    continue
                applied = min(remaining, inv["_open"])
                if applied > tol:
                    _apply(p, inv, applied, "ref_fallback" if ref_bad else "fifo")
                    remaining -= applied

        # (7) leftover cash — surfaced, NEVER netted into an invoice
        if remaining > tol:
            had_invoices = cust in invoice_customers
            kind = "overpaid" if had_invoices else "unapplied"
            unapplied.append({
                "payment_no": p.get("payment_no") or "",
                "customer_id": cust,
                "customer_name": p.get("customer_name") or "",
                "amount": _f2(remaining),
                "kind": kind,
                "invoice_ref": ref,
            })

    return applications, invoices, unapplied


# ---------------------------------------------------------------------------
# Step 3 — aging buckets
# ---------------------------------------------------------------------------
def _bucket_labels(boundaries):
    b0, b1, b2 = boundaries
    return [f"0-{b0}", f"{b0 + 1}-{b1}", f"{b1 + 1}-{b2}", f"{b2}+"]


OVERDUE_BUCKETS = None  # set per-run from the pack boundaries


def _bucket_for_dpd(dpd, boundaries):
    if dpd <= 0:
        return "current"
    b0, b1, b2 = boundaries
    labels = _bucket_labels(boundaries)
    if dpd <= b0:
        return labels[0]
    if dpd <= b1:
        return labels[1]
    if dpd <= b2:
        return labels[2]
    return labels[3]


def _infer_as_of(invoices, payments):
    dates = []
    for inv in invoices:
        for k in ("issue_date", "due_date"):
            if inv.get(k):
                dates.append(inv[k])
    for p in payments:
        if p.get("pay_date"):
            dates.append(p["pay_date"])
    return max(dates) if dates else date.today()


# ---------------------------------------------------------------------------
# The aging core (pure — operates on normalized lists)
# ---------------------------------------------------------------------------
def compute_aging(invoices, payments, customers, *, pack, recon_totals=None,
                  learned_rules=None, as_of=None, period_label=None):
    """Compute the AR aging result. Pure: no DB, no LLM, no file IO. Returns the engine
    output dict (see module docstring + the keys assembled at the end)."""
    invoices = list(invoices or [])
    payments = list(payments or [])
    customers = list(customers or [])

    tol = Decimal(str(templates.amount_tolerance(pack)))
    boundaries = templates.aging_boundaries(pack)
    labels = _bucket_labels(boundaries)
    default_terms = templates.default_terms_days(pack)
    dispute_kw = templates.dispute_keywords(pack)

    cust_master = {}
    for c in customers:
        k = _cust_key(c)
        if k:
            cust_master[k] = c

    _derive_due_dates(invoices, cust_master, default_terms)
    as_of = _parse_date(as_of) if isinstance(as_of, str) else as_of
    as_of = as_of or _infer_as_of(invoices, payments)

    applications, _open_invoices, unapplied = apply_payments(invoices, payments, tol=tol)

    # --- per-invoice aging + per-invoice grade ---
    inv_rows = []
    bucket_counts = {"current": 0, **{lab: 0 for lab in labels}}
    grade_counts = {GRADE_CONFIRMED: 0, GRADE_SUSPECTED: 0}
    for inv in invoices:
        open_bal = inv["_open"]
        if open_bal <= tol:
            continue  # fully settled — excluded from worklist/outstanding (still in identity A)
        paid = (inv.get("gross_amount") or _ZERO) - open_bal
        due = inv.get("due_date")
        dpd = (as_of - due).days if due and as_of > due else 0
        overdue = bool(due and as_of > due)
        bucket = _bucket_for_dpd(dpd, boundaries) if overdue else "current"

        reasons = []
        if paid > tol:
            reasons.append(SUSPECT_PARTIAL_PAY)
        if inv.get("_due_derived") or due is None:
            reasons.append(SUSPECT_NO_DUE_DATE)
        note = inv.get("note") or ""
        if note and any(kw in note for kw in dispute_kw):
            reasons.append(SUSPECT_DISPUTED)
        if "ref_fallback" in (inv.get("_via") or []):
            reasons.append(SUSPECT_ORPHAN_PAYMENT)
        grade = GRADE_SUSPECTED if reasons else GRADE_CONFIRMED

        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
        grade_counts[grade] += 1
        inv_rows.append({
            "invoice_no": inv.get("invoice_no") or "",
            "customer_id": _cust_key(inv),
            "customer_name": inv.get("customer_name") or "",
            "issue_date": inv["issue_date"].isoformat() if inv.get("issue_date") else "",
            "due_date": due.isoformat() if due else "",
            "gross_amount": _f2(inv.get("gross_amount") or _ZERO),
            "open_balance": _f2(open_bal),
            "dpd": dpd,
            "bucket": bucket,
            "overdue": overdue,
            "grade": grade,
            "suspect_reasons": reasons,
            "applied_via": list(inv.get("_via") or []),
        })

    # --- per-customer roll-up + customer-level grade ---
    customers_out, cust_index = [], {}
    for r in inv_rows:
        k = r["customer_id"]
        c = cust_index.get(k)
        if c is None:
            c = cust_index[k] = {
                "customer_id": k, "customer_name": r["customer_name"],
                "outstanding": _ZERO, "credit": _ZERO,
                "current": _ZERO, **{lab: _ZERO for lab in labels},
                "overdue": False, "grade": GRADE_CONFIRMED, "suspect_reasons": [],
                "_invoice_grades": [],
            }
            customers_out.append(c)
        ob = Decimal(str(r["open_balance"]))
        c["outstanding"] += ob
        c[r["bucket"]] += ob
        if r["overdue"]:
            c["overdue"] = True
        c["_invoice_grades"].append(r["grade"])
        for reason in r["suspect_reasons"]:
            if reason not in c["suspect_reasons"]:
                c["suspect_reasons"].append(reason)

    # leftover cash (credit) + customer-level suspect reasons (R6/R7/R8)
    cust_level_flags = []  # customer-level exceptions (not tied to an overdue invoice)
    for u in unapplied:
        k = u["customer_id"]
        c = cust_index.get(k)
        if c is None:
            c = cust_index[k] = {
                "customer_id": k, "customer_name": u.get("customer_name") or "",
                "outstanding": _ZERO, "credit": _ZERO,
                "current": _ZERO, **{lab: _ZERO for lab in labels},
                "overdue": False, "grade": GRADE_CONFIRMED, "suspect_reasons": [],
                "_invoice_grades": [],
            }
            customers_out.append(c)
        c["credit"] += Decimal(str(u["amount"]))
        if u["kind"] == "overpaid":
            reason = SUSPECT_OVERPAID
        elif k in {r["customer_id"] for r in inv_rows} or k in cust_master:
            reason = SUSPECT_UNAPPLIED_CASH
        else:
            reason = SUSPECT_ORPHAN_PAYMENT
        if reason not in c["suspect_reasons"]:
            c["suspect_reasons"].append(reason)
        cust_level_flags.append((k, reason, Decimal(str(u["amount"])),
                                 f"回款 {u['payment_no']} {u['kind']} ¥{u['amount']:,.2f}"))

    # recon cross-check (identity D) — soft 疑似 nudge for all customers when it fails
    recon_ok = None
    recon_arrived = None
    applied_total = sum((Decimal(str(a["amount"])) for a in applications), _ZERO)
    unapplied_total = sum((Decimal(str(u["amount"])) for u in unapplied), _ZERO)
    if recon_totals and (recon_totals.get("到账") is not None or recon_totals.get("实收") is not None):
        recon_arrived = _money(recon_totals.get("到账") if recon_totals.get("到账") is not None
                               else recon_totals.get("实收"))
        cross_tol = max(tol, (recon_arrived.copy_abs() * Decimal("0.01")))
        recon_ok = _amt_match(applied_total + unapplied_total, recon_arrived, cross_tol)

    # finalize customer grade + credit-limit anomaly (R9) + recon nudge (R10)
    for c in customers_out:
        cm = cust_master.get(c["customer_id"])
        climit = cm.get("credit_limit") if cm else None
        if climit is not None and c["outstanding"] > (climit + tol):
            if SUSPECT_CREDIT_ANOMALY not in c["suspect_reasons"]:
                c["suspect_reasons"].append(SUSPECT_CREDIT_ANOMALY)
            cust_level_flags.append((c["customer_id"], SUSPECT_CREDIT_ANOMALY,
                                     c["outstanding"],
                                     f"未结 ¥{_fmt(c['outstanding'])} 超过信用额度 ¥{_fmt(climit)}"))
        if recon_ok is False and SUSPECT_PENDING_RECON not in c["suspect_reasons"]:
            c["suspect_reasons"].append(SUSPECT_PENDING_RECON)
        any_inv_suspect = GRADE_SUSPECTED in c["_invoice_grades"]
        c["grade"] = (GRADE_SUSPECTED if (any_inv_suspect or c["suspect_reasons"])
                      else GRADE_CONFIRMED)

    # --- headline aggregates ---
    gross_all = sum((inv.get("gross_amount") or _ZERO for inv in invoices), _ZERO)
    open_all = sum((Decimal(str(r["open_balance"])) for r in inv_rows), _ZERO)
    overdue_total = sum((Decimal(str(r["open_balance"])) for r in inv_rows
                         if r["overdue"]), _ZERO)
    bucket_amounts = {"current": _ZERO, **{lab: _ZERO for lab in labels}}
    for r in inv_rows:
        bucket_amounts[r["bucket"]] += Decimal(str(r["open_balance"]))
    overdue_count = sum(1 for r in inv_rows if r["overdue"])

    # DSO (PROVISIONAL): ending open AR / Σ credit sales in window × window_days.
    dso = _dso(open_all, gross_all, invoices, tol)

    # --- self-audit identities (mirror 对账's bridge) ---
    open_total_all = sum((inv["_open"] for inv in invoices), _ZERO)  # incl. <=tol residue
    payments_total = sum((p.get("amount") or _ZERO for p in payments), _ZERO)
    a_lhs, a_rhs = gross_all, applied_total + open_total_all
    bridge_ok = _amt_match(a_lhs, a_rhs, tol)
    payments_ok = _amt_match(payments_total, applied_total + unapplied_total, tol)
    cust_imbalances = _per_customer_audit(invoices, applications, unapplied, tol)
    bridge = {
        "ok": bool(bridge_ok),
        "lhs": _f2(a_lhs), "rhs": _f2(a_rhs), "imbalance": _f2(a_lhs - a_rhs),
        "应收": _f2(gross_all), "已核销": _f2(applied_total),
        "未结": _f2(open_total_all), "未核销": _f2(unapplied_total),
        "payments_ok": bool(payments_ok),
        "per_customer_ok": not cust_imbalances,
        "customer_imbalances": cust_imbalances,
        "recon_crosscheck_ok": recon_ok,
        "recon_到账": _f2(recon_arrived) if recon_arrived is not None else None,
    }

    # --- exceptions worklist (overdue invoices + customer-level flags) ---
    exceptions = _build_exceptions(inv_rows, cust_level_flags)
    exceptions = _apply_learned_rules(exceptions, learned_rules)
    suspect_count = sum(1 for e in exceptions if e["grade"] == GRADE_SUSPECTED)

    # --- attributions (aging-bucket roll-ups, for the report) ---
    attributions = _build_attributions(labels, bucket_amounts, bucket_counts)

    summary = {
        "应收账款总额": _f2(open_all), "逾期总额": _f2(overdue_total),
        "未结发票数": len(inv_rows), "逾期发票数": overdue_count,
        "current": _f2(bucket_amounts["current"]),
        **{lab: _f2(bucket_amounts[lab]) for lab in labels},
        "DSO": dso, "未核销超额回款": _f2(unapplied_total),
        "疑似条目数": suspect_count,
        "period_label": period_label,
        "as_of": as_of.isoformat() if as_of else "",
        "platform": pack.get("_key"),
    }

    name = (pack.get("display_name_cn") or pack.get("_key") or "应收台账")
    source_rows, facts = _build_provenance(
        name, open_all, len(inv_rows), bucket_amounts, labels, overdue_total,
        overdue_count, dso, unapplied_total, len(unapplied), customers_out,
        suspect_count, invoice_file=_first_file(invoices),
        payment_file=_first_file(payments))

    # strip engine-private keys from customers before returning
    for c in customers_out:
        c.pop("_invoice_grades", None)
        for lab in ("current", *labels, "outstanding", "credit"):
            c[lab] = _f2(c[lab])

    return {
        "summary": summary,
        "buckets": {"aging": bucket_counts, "grade": grade_counts},
        "customers": sorted(customers_out, key=lambda c: c["outstanding"], reverse=True),
        "invoices": inv_rows,
        "applications": applications,
        "unapplied": unapplied,
        "attributions": attributions,
        "exceptions": exceptions,
        "bridge": bridge,
        "source_rows": source_rows,
        "facts_block": facts,
    }


# ---------------------------------------------------------------------------
# DSO + per-customer audit
# ---------------------------------------------------------------------------
def _dso(open_total, sales_total, invoices, tol):
    """DSO = ending open AR / Σ credit sales × window_days. window = issue_date span.
    None (数据不足) when sales <= tol or no datable window. PROVISIONAL."""
    if sales_total is None or sales_total <= tol:
        return None
    issues = [inv["issue_date"] for inv in invoices if inv.get("issue_date")]
    if not issues:
        return None
    window_days = (max(issues) - min(issues)).days + 1
    if window_days <= 0:
        return None
    return round(float(open_total) / float(sales_total) * window_days, 1)


def _per_customer_audit(invoices, applications, unapplied, tol):
    """Identity C: per customer, Σ gross == Σ applied + Σ open + Σ unapplied. Returns
    the list of customers that fail (should be empty unless an engine bug)."""
    gross, applied, openb, unapp = {}, {}, {}, {}
    for inv in invoices:
        k = _cust_key(inv)
        gross[k] = gross.get(k, _ZERO) + (inv.get("gross_amount") or _ZERO)
        openb[k] = openb.get(k, _ZERO) + inv["_open"]
    for a in applications:
        k = a["customer_id"]
        applied[k] = applied.get(k, _ZERO) + Decimal(str(a["amount"]))
    for u in unapplied:
        k = u["customer_id"]
        unapp[k] = unapp.get(k, _ZERO) + Decimal(str(u["amount"]))
    out = []
    for k in set(gross) | set(applied) | set(openb) | set(unapp):
        lhs = gross.get(k, _ZERO)
        rhs = applied.get(k, _ZERO) + openb.get(k, _ZERO)
        # unapplied is payment-side leftover for this customer; gross conservation is
        # gross == applied + open (the customer's invoices), so unapp is NOT added here.
        if not _amt_match(lhs, rhs, tol):
            out.append({"customer_id": k, "imbalance": _f2(lhs - rhs)})
    return out


# ---------------------------------------------------------------------------
# Exceptions (worklist) + learned rules + attributions
# ---------------------------------------------------------------------------
def _build_exceptions(inv_rows, cust_level_flags):
    """Every OVERDUE invoice becomes a worklist item (确定 = the collection list; 疑似 =
    verify first). Plus customer-level 疑似 flags not tied to a single overdue invoice."""
    exc = []
    for r in inv_rows:
        if not r["overdue"]:
            continue
        cause = (r["suspect_reasons"][0] if r["grade"] == GRADE_SUSPECTED
                 and r["suspect_reasons"] else CAUSE_OVERDUE)
        exc.append({
            "layer": "AR", "bucket": r["bucket"], "amount": r["open_balance"],
            "cause": cause, "grade": r["grade"],
            "suspect_reasons": list(r["suspect_reasons"]),
            "customer_ref": r["customer_id"], "invoice_ref": r["invoice_no"],
            "detail": (f"{r['customer_name']} 发票 {r['invoice_no']} 逾期 {r['dpd']} 天，"
                       f"未结 ¥{r['open_balance']:,.2f}"),
            "status": "open",
        })
    for cust_ref, reason, amount, detail in cust_level_flags:
        exc.append({
            "layer": "AR", "bucket": "客户", "amount": _f2(amount),
            "cause": reason, "grade": GRADE_SUSPECTED, "suspect_reasons": [reason],
            "customer_ref": cust_ref, "invoice_ref": "",
            "detail": detail, "status": "open",
        })
    return exc


def _apply_learned_rules(exceptions, learned_rules):
    """Re-classify exceptions whose identifying fields match a learned rule (written by
    a prior one-tap classify). Rule = {match: {field: value}, cause, label}. Copied 1:1
    from settlement_engine (CHASSIS-CANDIDATE)."""
    rules = learned_rules or []
    if not rules:
        return exceptions
    for e in exceptions:
        for rule in rules:
            crit = (rule or {}).get("match") or {}
            if crit and all(str(e.get(f, "")) == str(v) and v != ""
                            for f, v in crit.items()):
                e["cause"] = rule.get("cause") or e["cause"]
                e["user_label"] = rule.get("label") or rule.get("cause")
                break
    return exceptions


def _build_attributions(labels, bucket_amounts, bucket_counts):
    """Aging-bucket roll-ups for the report (the AR analog of 对账's attributions)."""
    out = []
    for lab in labels:
        out.append({
            "cause": f"逾期 {lab} 天", "kind": lab,
            "amount": _f2(bucket_amounts[lab]), "count": bucket_counts.get(lab, 0),
            "human_cn": f"逾期 {lab} 天：¥{_fmt(bucket_amounts[lab])}"
                        f"（{bucket_counts.get(lab, 0)} 张）",
        })
    return out


# ---------------------------------------------------------------------------
# Provenance (5-key source rows) + writer facts block
# ---------------------------------------------------------------------------
def _build_provenance(name, open_all, n_open, bucket_amounts, labels, overdue_total,
                      overdue_count, dso, unapplied_total, n_unapplied, customers_out,
                      suspect_count, invoice_file, payment_file):
    """Return (source_rows, facts). source_rows are verification.py-shaped 5-key dicts;
    facts are the value-with-【N】 lines the writer must reproduce verbatim."""
    rows, facts = [], []
    anchor = [0]

    def add(claim_cn, value_str, detail, *, source=invoice_file):
        anchor[0] += 1
        n = anchor[0]
        rows.append({
            "anchor": n, "claim_cn": claim_cn, "source_file": source,
            "source_detail": detail, "computed_by": COMPUTED_BY,
        })
        facts.append(f"{claim_cn} {value_str}【{n}】（{detail}）")
        return n

    add("应收账款总额（未结）", f"¥{_fmt(open_all)}", f"{n_open} 张未结发票求和")
    for lab in labels:
        add(f"逾期 {lab} 天", f"¥{_fmt(bucket_amounts[lab])}", "按到期日账龄归集")
    add("逾期总额", f"¥{_fmt(overdue_total)}", f"{overdue_count} 张逾期发票求和")
    add("DSO（应收账款周转天数，PROVISIONAL）",
        (f"{dso} 天" if dso is not None else "数据不足"),
        "未结应收 ÷ 区间销售额 × 区间天数")
    if n_unapplied:
        add("未核销/超额回款", f"¥{_fmt(unapplied_total)}", f"{n_unapplied} 笔",
            source=payment_file)
    top = [c for c in sorted(customers_out, key=lambda c: c["outstanding"], reverse=True)
           if c["outstanding"] > 0][:3]
    for c in top:
        add(f"重点欠款客户 {c['customer_name'] or c['customer_id']}",
            f"¥{_fmt(c['outstanding'])}", "该客户未结应收求和")
    add("待人工核查（疑似）", f"{suspect_count} 项",
        "部分回款/争议/未核销/超额/超信用额度/待对账 等")
    return rows, facts


# ---------------------------------------------------------------------------
# Record + file tiers (mirror settlement_engine's seams)
# ---------------------------------------------------------------------------
def aging_from_records(records_by_role, pack, *, recon_totals=None, learned_rules=None,
                       as_of=None, period_label=None, file_names=None):
    """Normalize raw record dicts for each role, then compute. The testable seam:
    records_by_role = {"invoices": [...], "payments": [...], "customers": [...]}."""
    names = file_names or {}
    inv = normalize_rows(records_by_role.get("invoices"), "invoices", pack,
                         names.get("invoices", "应收台账"))
    pay = normalize_rows(records_by_role.get("payments"), "payments", pack,
                         names.get("payments", "回款记录"))
    cust = normalize_rows(records_by_role.get("customers"), "customers", pack,
                          names.get("customers", "客户主数据"))
    return compute_aging(inv, pay, cust, pack=pack, recon_totals=recon_totals,
                         learned_rules=learned_rules, as_of=as_of,
                         period_label=period_label)


def run_ar_aging(files_by_role, pack, *, recon_totals=None, learned_rules=None,
                 as_of=None, period_label=None):
    """Read each uploaded file (decrypting in memory) into raw records, then compute.
    files_by_role = {role: (path, display_name)}. Raises ValueError with a
    customer-facing Chinese message when a source can't be read."""
    records, names = {}, {}
    for role, spec in (files_by_role or {}).items():
        path, display = spec if isinstance(spec, (tuple, list)) else (spec, role)
        names[role] = display
        records[role] = _read_table(path)
    return aging_from_records(records, pack, recon_totals=recon_totals,
                              learned_rules=learned_rules, as_of=as_of,
                              period_label=period_label, file_names=names)


def _read_table(path):
    """Read an .xlsx/.xls/.csv into a list of raw row dicts (str values), decrypting at
    rest via crypto. CHASSIS-CANDIDATE: identical to settlement_engine._read_table."""
    import pandas as pd

    from . import crypto
    ext = (path.rsplit(".", 1)[-1] or "").lower() if "." in path else ""
    with crypto.materialize_plaintext(path) as plain:
        try:
            if ext in ("xlsx", "xls"):
                df = pd.read_excel(plain, dtype=str)
            else:
                df = pd.read_csv(plain, dtype=str)
        except Exception as e:  # noqa: BLE001 — surface a clean Chinese error
            raise ValueError(f"无法识别上传文件「{path}」：{type(e).__name__}") from e
    df = df.where(df.notna(), None)
    return df.to_dict("records")


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------
def _first_file(rows):
    for r in (rows or []):
        if r.get("source_file"):
            return r["source_file"]
    return ""


def to_json(result):
    return json.dumps(result, ensure_ascii=False)

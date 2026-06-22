"""
settlement_engine.py — The DETERMINISTIC 对账 / 平台结算核对 engine.

This is to 对账 what trends.py is to the weekly report: a pure-Python, no-LLM
engine that does ALL the arithmetic and ALL the matching, then hands the report
writer finished numbers it must quote verbatim. The LLM never computes a figure,
decides a match, or alters a number — that is the entire reliability story.

What it does, given three normalized source lists (platform settlement statement /
bank 流水 / 账本-ERP) and a platform pack (settlement_templates):

  1. NORMALIZE   raw rows -> a canonical NormalizedTxn shape (column map + cleaning).
  2. MATCH       multi-layer, never 1:1 on 订单号:
       L1 订单匹配   账本订单 ↔ 平台订单明细   (order-level cross-check)
       L3 银行匹配   平台结算批次 ↔ 银行到账     (aggregate + batch, many-to-one)
     each line lands in a bucket: 已匹配 / 单边挂账(本方) / 单边挂账(对方) / 部分匹配.
  3. EXPLAIN     attribute every gap to a cause (支付手续费 / 退款 / 在途 / 提现差额),
                 isolating 未解释差异 ¥X (N 笔) = the residual no rule explained.
  4. SELF-AUDIT  a pure-Python bridge-balance check: does the platform statement's
                 own net (Σ应结金额) equal 应收 − 优惠 − 手续费 − 退款? If not, FLAG it.

Everything returns plain dicts/lists (never ORM objects), money as float rounded to
the fen, so it's thread-safe across the background JOBS workers and JSON-serializable.
Mirrors trends.py discipline: _round/_pct helpers, module-level tolerance constants,
a `human_cn` one-liner per figure the writer quotes verbatim.
"""

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from . import crossborder_adapters
from . import settlement_templates as templates

# Amount equality tolerance (the fen). Two amounts match within ±1 分 by default.
AMOUNT_TOLERANCE = Decimal("0.01")
# Default settlement lag if a pack doesn't set one (bank lands T+N).
DEFAULT_LAG_DAYS = 1
_ZERO = Decimal("0")

# Canonical cause labels (中文) the engine assigns. Kept here so the store + tests
# share one vocabulary; the active set per platform comes from the pack.
CAUSE_FEE = "支付手续费"
CAUSE_REFUND = "退款/退货"
CAUSE_IN_TRANSIT = "在途/未结算"
CAUSE_WITHDRAW_DIFF = "提现差额"
CAUSE_UNEXPLAINED = "未解释"
CAUSE_CHECKED = "已核对"

# computed_by tag for every source row (mirrors verification.py's trend_engine).
COMPUTED_BY = "recon_engine"


# ---------------------------------------------------------------------------
# Numeric / date helpers (pure)
# ---------------------------------------------------------------------------
def _money(x):
    """Parse a cell into a Decimal yuan amount. Tolerates WeChat's backtick prefix,
    ¥/￥ signs, thousands separators, parentheses-for-negative, and blanks -> 0."""
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


def _f2(d):
    """Decimal/number -> float rounded to the fen (for JSON output)."""
    if d is None:
        return 0.0
    if not isinstance(d, Decimal):
        d = _money(d)
    return float(d.quantize(Decimal("0.01")))


def _clean(x):
    """Strip WeChat's leading backtick and surrounding whitespace from a text cell."""
    if x is None:
        return ""
    return str(x).strip().lstrip("`").strip()


def _parse_date(x):
    """Best-effort parse of a cell into a date. Returns None when unparseable."""
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


def _amt_match(a, b, tol=AMOUNT_TOLERANCE):
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# Normalization: raw record dicts -> canonical NormalizedTxn dicts
# ---------------------------------------------------------------------------
_MONEY_FIELDS = ("gross_amount", "net_amount", "fee", "commission",
                 "refund_amount", "coupon_amount")


def normalize_rows(records, role, pack, source_file):
    """Map a list of raw row dicts (keyed by the source's own column headers) to
    canonical NormalizedTxn dicts using the pack's column_map for `role`. Pure; no
    file IO. Unmapped money fields default to 0, text fields to ""."""
    col = templates.column_map_for(pack, role)
    smap = templates.status_map_for(pack)
    out = []
    for i, rec in enumerate(records or []):
        if not isinstance(rec, dict):
            continue
        txn = {
            "source": role,
            "source_file": source_file,
            "row_id": i + 1,
            "raw": {str(k): _clean(v) for k, v in rec.items()},
        }
        for field in ("platform_txn_no", "merchant_order_no", "batch_no",
                      "counterparty", "currency"):
            txn[field] = _clean(rec.get(col.get(field))) if col.get(field) else ""
        for field in _MONEY_FIELDS:
            txn[field] = _money(rec.get(col.get(field))) if col.get(field) else _ZERO
        # Whether the statement actually carried a 手续费 column for this row — so a
        # genuine 手续费=0 is respected and the fee_rate estimate only fills a gap.
        fee_col = col.get("fee")
        txn["_fee_known"] = bool(fee_col and fee_col in rec)
        txn["event_time"] = _parse_date(rec.get(col.get("event_time"))) \
            if col.get("event_time") else None
        raw_status = _clean(rec.get(col.get("status"))) if col.get("status") else ""
        txn["status"] = smap.get(raw_status, raw_status)
        out.append(txn)
    return out


def _is_success(txn):
    s = (txn.get("status") or "").upper()
    return s in ("SUCCESS", "") or s == "成功" or txn.get("status") == "交易成功"


def _is_refund(txn):
    s = (txn.get("status") or "").upper()
    return s == "REFUND" or txn.get("status") in ("退款", "已退款") \
        or txn.get("refund_amount", _ZERO) > _ZERO


# ---------------------------------------------------------------------------
# The reconciliation core (pure — operates on NormalizedTxn lists)
# ---------------------------------------------------------------------------
def reconcile(platform, bank, ledger, *, pack, learned_rules=None, period_label=None,
              cross_border=None):
    """Reconcile the three normalized source lists. Returns the engine output dict
    (see module docstring). Pure: no DB, no LLM, no file IO.

    `cross_border` (optional) carries a platform adapter's extras — reserve (held →
    在途) and the FX 点差. It is None for every domestic pack, so the arithmetic below
    is identical to the original in that case."""
    platform = platform or []
    bank = bank or []
    ledger = ledger or []
    rules = templates.attribution_rules(pack)
    lag = templates.settlement_lag_days(pack, DEFAULT_LAG_DAYS)
    tol = _tolerance(pack)
    eff_fee_rate = templates.fee_rate(pack)

    success = [p for p in platform if _is_success(p) and not _is_refund(p)]
    refunds = [p for p in platform if _is_refund(p)]

    # Per-order economics. fee comes from the statement column; if absent (0) and the
    # pack carries a 费率, estimate it (this is the one place fee_rate affects numbers).
    for p in success:
        fee = p.get("fee") or _ZERO
        if not p.get("_fee_known") and eff_fee_rate:
            fee = (p.get("net_amount") or _ZERO) * Decimal(str(eff_fee_rate))
        p["_fee"] = fee
        # what actually reaches the bank — net of fee AND commission, consistent with the
        # headline `settled` below. (Domestic packs carry commission=0, so they are
        # unchanged; cross-border packs, e.g. Amazon, emit a separate commission, and
        # omitting it here would over-state L3 batch totals + 在途 and push 未解释差异
        # negative by exactly the commission.)
        p["_net_settle"] = ((p.get("net_amount") or _ZERO) - fee
                            - (p.get("commission") or _ZERO))

    # --- Headline aggregates (every figure deterministic) ---
    gross = sum((p.get("gross_amount") or _ZERO for p in success), _ZERO)        # 应收
    coupon = sum((p.get("coupon_amount") or _ZERO for p in success), _ZERO)      # 优惠
    fee_total = sum((p["_fee"] for p in success), _ZERO)                          # 手续费
    commission = sum((p.get("commission") or _ZERO for p in success), _ZERO)     # 佣金
    n_commission = sum(1 for p in success if (p.get("commission") or _ZERO))      # 计佣笔数
    refund_total = sum((p.get("refund_amount") or _ZERO for p in refunds), _ZERO)  # 退款
    net_stated = sum((p.get("net_amount") or _ZERO for p in success), _ZERO)     # Σ应结金额
    settled = net_stated - fee_total - refund_total - commission                 # 实收(应结净额)
    arrived = sum((b.get("net_amount") or _ZERO for b in bank), _ZERO)           # 到账

    # In-transit: success orders whose settle date falls AFTER the bank statement's
    # coverage end (legitimately not yet arrived). Independent of the residual.
    bank_end = max((b["event_time"] for b in bank if b.get("event_time")), default=None)
    in_transit_orders = []
    if bank_end is not None:
        for p in success:
            et = p.get("event_time")
            if et is not None and _add_days(et, lag) > bank_end:
                in_transit_orders.append(p)
    in_transit_lag = sum((p["_net_settle"] for p in in_transit_orders), _ZERO)
    withdraw_diff = _ZERO  # MVP: no 提现 row type yet (later phase)

    # Cross-border extras (from a platform adapter, e.g. Amazon FF V2). 准备金/reserve is
    # money the platform is HOLDING — legitimately not yet arrived → folded into 在途 so
    # it never inflates 未解释差异. The FX 点差 is a surfaced cost (attribution only). Both
    # are 0 for every domestic pack (no adapter), so the residual below is unchanged.
    cb = cross_border or {}
    reserve_amount = _money(cb.get("reserve"))
    reserve_count = int(cb.get("reserve_count") or 0)
    fx_spread = _money(cb.get("fx_spread"))
    in_transit = in_transit_lag + reserve_amount

    # Residual the boss cares about: platform-settled money that did NOT arrive and
    # isn't legitimately in transit (lag) or held in reserve. Independent (NOT a plug).
    unexplained_amount = settled - arrived - in_transit - withdraw_diff

    # --- L1 订单匹配 (账本 ↔ 平台) ---
    l1 = _match_orders(ledger, success + refunds, tol)
    # --- L3 银行匹配 (平台结算批次 ↔ 银行到账, many-to-one) ---
    l3 = _match_batches(success, in_transit_orders, bank, lag, tol)

    # --- Exception worklist (the non-blocking actionable items) ---
    exceptions = _build_exceptions(l1, l3, in_transit_orders, unexplained_amount, tol)
    exceptions = _apply_learned_rules(exceptions, learned_rules)
    unexplained_count = sum(1 for e in exceptions if e["cause"] == CAUSE_UNEXPLAINED)

    # --- Attributions (explained reductions), driven by the pack's rule order ---
    # The 在途 line is the lag-based in-transit; 准备金 is its own line. commission /
    # reserve_held / fx_spread only render if the pack declares those kinds (cross-border
    # packs do; domestic packs don't → identical output).
    attributions = _build_attributions(
        rules, fee_total, len(success), refund_total, len(refunds),
        in_transit_lag, len(in_transit_orders), withdraw_diff,
        commission=commission, n_commission=n_commission,
        reserve=reserve_amount, n_reserve=reserve_count, fx_spread=fx_spread)

    # --- Bridge-balance self-audit (pure Python; independent of the residual) ---
    # Checks the platform statement is internally consistent: Σ应结金额 == 应收 − 优惠.
    lhs = gross - coupon
    rhs = net_stated
    bridge_ok = _amt_match(lhs, rhs, tol)
    bridge = {
        "ok": bool(bridge_ok),
        "lhs": _f2(lhs), "rhs": _f2(rhs), "imbalance": _f2(lhs - rhs),
        "应收": _f2(gross), "优惠": _f2(coupon), "手续费": _f2(fee_total),
        "退款": _f2(refund_total), "佣金": _f2(commission), "实收": _f2(settled),
        "到账": _f2(arrived), "在途": _f2(in_transit),
        "未解释差异": _f2(unexplained_amount),
    }

    summary = {
        "应收": _f2(gross), "实收": _f2(settled), "到账": _f2(arrived),
        "在途金额": _f2(in_transit), "总手续费": _f2(fee_total),
        "总佣金": _f2(commission), "总退款": _f2(refund_total),
        "未解释差异": _f2(unexplained_amount), "未解释笔数": unexplained_count,
        "period_label": period_label,
        "platform": pack.get("_key"),
    }

    # --- Provenance source rows (5-key, computed_by=recon_engine) + writer facts ---
    name = (pack.get("display_name_cn") or pack.get("_key") or "结算平台")
    source_rows, facts = _build_provenance(
        name, gross, len(success), settled, fee_total, refund_total, len(refunds),
        in_transit_lag, len(in_transit_orders), arrived, len(bank),
        unexplained_amount, unexplained_count, lag,
        platform_file=_first_file(platform), bank_file=_first_file(bank),
        reserve=reserve_amount, n_reserve=reserve_count, fx_spread=fx_spread,
        currency_in=cb.get("currency_in"), fx_rate=cb.get("fx_rate"))

    # A compact per-order list (success orders only), persisted in the result blob for
    # DOWNSTREAM 跨境财务 sub-tabs to read (三流合一 imports the 订单 leg from here so the
    # seller needn't re-upload). NOT exposed in the customer-safe recon payload
    # (_settlement_report_to_dict selects keys), and capped so a huge run can't bloat it.
    orders_out = [{
        "ref": (p.get("merchant_order_no") or p.get("platform_txn_no") or ""),
        "amount": _f2(p.get("gross_amount") or _ZERO),
        "net": _f2(p.get("_net_settle") or _ZERO),
        "date": (p["event_time"].isoformat() if p.get("event_time") else ""),
    } for p in success[:5000] if (p.get("merchant_order_no") or p.get("platform_txn_no"))]

    return {
        "summary": summary,
        "buckets": {"L1": l1["counts"], "L3": l3["counts"]},
        "attributions": attributions,
        "exceptions": exceptions,
        "bridge": bridge,
        "source_rows": source_rows,
        "facts_block": facts,
        "orders": orders_out,
    }


# ---------------------------------------------------------------------------
# Matching layers
# ---------------------------------------------------------------------------
def _match_orders(ledger, platform_orders, tol):
    """L1: 账本订单 ↔ 平台订单明细 on merchant_order_no (then amount). Returns buckets
    + counts. Order amount compared on gross_amount."""
    by_key = {}
    for p in platform_orders:
        k = p.get("merchant_order_no") or p.get("platform_txn_no")
        if k:
            by_key.setdefault(k, []).append(p)
    matched, partial, unmatched_ours, unmatched_theirs = [], [], [], []
    ledger_keys = set()  # built in this single pass; used by the theirs-only check below
    consumed = set()     # ids of platform orders already claimed — L1 is 1:1, so a repeated
                         # 订单号 (split shipment / re-export) can't double-count one order
    for lg in ledger:
        k = lg.get("merchant_order_no") or lg.get("platform_txn_no")
        if k:
            ledger_keys.add(k)
        cands = by_key.get(k) if k else None
        if not cands:
            unmatched_ours.append(lg)
            continue
        # Each platform order matches at most ONE ledger row. Among the not-yet-claimed
        # candidates, prefer one whose amount matches (a 订单号 can repeat — a refund row
        # alongside the success row, or a corrected amount) before falling back to the
        # first, so a duplicate key neither mislabels a real match as 部分匹配 nor inflates
        # 已匹配 by claiming the same platform order twice.
        gl = lg.get("gross_amount") or _ZERO
        avail = [c for c in cands if id(c) not in consumed]
        if not avail:
            unmatched_ours.append(lg)  # all platform orders for this key already claimed
            continue
        p = next((c for c in avail if _amt_match(gl, c.get("gross_amount") or _ZERO, tol)),
                 avail[0])
        consumed.add(id(p))
        if _amt_match(gl, p.get("gross_amount") or _ZERO, tol):
            matched.append((lg, p))
        else:
            partial.append((lg, p))
    if ledger:  # only flag platform-only orders when we actually have a ledger to compare
        for p in platform_orders:
            k = p.get("merchant_order_no") or p.get("platform_txn_no")
            if k and k not in ledger_keys:
                unmatched_theirs.append(p)
    return {
        "matched": matched, "partial": partial,
        "unmatched_ours": unmatched_ours, "unmatched_theirs": unmatched_theirs,
        "counts": {
            "已匹配": len(matched), "部分匹配": len(partial),
            "单边挂账本方": len(unmatched_ours), "单边挂账对方": len(unmatched_theirs),
        },
    }


def _match_batches(success, in_transit_orders, bank, lag, tol):
    """L3: settle non-in-transit success orders into batches (by batch_no else settle
    date) and match each batch to ONE bank credit by amount (many-to-one). Returns
    matched/unmatched batches + unmatched bank credits + counts."""
    transit_ids = {id(p) for p in in_transit_orders}
    settleable = [p for p in success if id(p) not in transit_ids]
    batches = {}
    for p in settleable:
        key = p.get("batch_no") or (_add_days(p["event_time"], lag).isoformat()
                                    if p.get("event_time") else "_nobatch")
        batches.setdefault(key, {"key": key, "amount": _ZERO, "orders": []})
        batches[key]["amount"] += p["_net_settle"]
        batches[key]["orders"].append(p)

    bank_lines = list(bank)
    used = set()
    matched, unmatched_batches = [], []
    for b in batches.values():
        hit = None
        for j, bk in enumerate(bank_lines):
            if j in used:
                continue
            if _amt_match(b["amount"], bk.get("net_amount") or _ZERO, tol):
                hit = j
                break
        if hit is not None:
            used.add(hit)
            matched.append((b, bank_lines[hit]))
        else:
            unmatched_batches.append(b)
    unmatched_bank = [bk for j, bk in enumerate(bank_lines) if j not in used]
    return {
        "matched": matched, "unmatched_batches": unmatched_batches,
        "unmatched_bank": unmatched_bank,
        "counts": {
            "已匹配": len(matched), "单边挂账本方": len(unmatched_batches),
            "单边挂账对方": len(unmatched_bank),
        },
    }


# ---------------------------------------------------------------------------
# Exceptions (worklist) + attribution
# ---------------------------------------------------------------------------
def _build_exceptions(l1, l3, in_transit_orders, unexplained_amount, tol):
    """Turn the unmatched/partial lines into worklist items, each with an initial
    cause. In-transit orders are flagged 在途; everything else unmatched is 未解释
    until a learned rule (or the customer) reclassifies it. The L3 bank gap is a
    SINGLE residual line equal to the headline 未解释差异 (the per-batch detail is
    summarized) so the worklist isn't noisy and the headline ties to one line."""
    exc = []

    def _ex(layer, bucket, amount, cause, *, platform_ref="", bank_ref="",
            ledger_ref="", detail=""):
        exc.append({
            "layer": layer, "bucket": bucket, "amount": _f2(amount), "cause": cause,
            "platform_ref": platform_ref, "bank_ref": bank_ref, "ledger_ref": ledger_ref,
            "detail": detail,
        })

    transit_ids = {id(p) for p in in_transit_orders}

    for lg in l1["unmatched_ours"]:
        _ex("L1", "单边挂账(本方)", lg.get("gross_amount") or _ZERO, CAUSE_UNEXPLAINED,
            ledger_ref=lg.get("merchant_order_no") or "",
            detail="账本有此订单，平台结算单中未找到")
    for p in l1["unmatched_theirs"]:
        cause = CAUSE_IN_TRANSIT if id(p) in transit_ids else CAUSE_UNEXPLAINED
        _ex("L1", "单边挂账(对方)", p.get("net_amount") or _ZERO, cause,
            platform_ref=p.get("merchant_order_no") or p.get("platform_txn_no") or "",
            detail="平台结算单有此订单，账本中未找到")
    for lg, p in l1["partial"]:
        diff = (lg.get("gross_amount") or _ZERO) - (p.get("gross_amount") or _ZERO)
        _ex("L1", "部分匹配", diff, CAUSE_UNEXPLAINED,
            ledger_ref=lg.get("merchant_order_no") or "",
            platform_ref=p.get("merchant_order_no") or "",
            detail="订单号匹配但金额不一致")

    # L3: one residual line equal to the headline 未解释差异 (= Σ未匹配批次 − Σ未匹配到账).
    if abs(unexplained_amount) > tol:
        n_batches = len(l3["unmatched_batches"])
        n_bank = len(l3["unmatched_bank"])
        bucket = "单边挂账(本方)" if unexplained_amount > _ZERO else "单边挂账(对方)"
        _ex("L3", bucket, unexplained_amount, CAUSE_UNEXPLAINED,
            detail=(f"平台应结与银行到账差异：未匹配结算批次 {n_batches} 笔 / "
                    f"银行到账 {n_bank} 笔"))

    return exc


def _apply_learned_rules(exceptions, learned_rules):
    """Re-classify exceptions whose identifying fields match a learned rule
    (written by a prior one-tap classify). A rule is
    {match: {field: value, ...}, cause: "...", label: "..."}; a matched exception
    takes the rule's cause (so it leaves 未解释) and records the label."""
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


def _build_attributions(rules, fee_total, n_fee, refund_total, n_refund,
                        in_transit, n_transit, withdraw_diff,
                        *, commission=_ZERO, n_commission=0,
                        reserve=_ZERO, n_reserve=0, fx_spread=_ZERO):
    """Build the explained-reduction list in the pack's declared rule order. Each
    entry is {cause, kind, amount, count, human_cn}. Zero-amount rules are kept
    (so the report can say 本期无退款) but flagged. The `commission` / `reserve_held` /
    `fx_spread` kinds are cross-border additions — only the packs that declare them
    (e.g. Amazon) emit them; domestic packs never reference these kinds."""
    handlers = {
        "fee": (fee_total, n_fee, lambda a, n: f"支付手续费合计 ¥{_fmt(a)}（{n} 笔）"),
        "commission": (commission, n_commission,
                       lambda a, n: f"平台佣金合计 ¥{_fmt(a)}（{n} 笔）"),
        "refund": (refund_total, n_refund, lambda a, n: f"退款/退货合计 ¥{_fmt(a)}（{n} 笔）"),
        "in_transit": (in_transit, n_transit,
                       lambda a, n: f"在途/未结算 ¥{_fmt(a)}（{n} 笔）"),
        "reserve_held": (reserve, n_reserve,
                         lambda a, n: f"账户准备金(reserve) ¥{_fmt(a)}（{n} 笔）"),
        "fx_spread": (fx_spread, 0, lambda a, n: f"汇兑点差(FX) ¥{_fmt(a)}"),
        "withdraw_diff": (withdraw_diff, 0, lambda a, n: f"提现差额 ¥{_fmt(a)}"),
    }
    out = []
    for r in rules:
        kind = r["kind"]
        if kind not in handlers:
            continue
        amount, count, human = handlers[kind]
        out.append({
            "cause": r["cause"], "kind": kind,
            "amount": _f2(amount), "count": count,
            "human_cn": human(amount, count),
        })
    return out


# ---------------------------------------------------------------------------
# Provenance (5-key source rows) + writer facts block
# ---------------------------------------------------------------------------
def _build_provenance(platform_name, gross, n_success, settled, fee_total,
                      refund_total, n_refund, in_transit, n_transit, arrived, n_bank,
                      unexplained_amount, unexplained_count, lag,
                      platform_file, bank_file,
                      *, reserve=_ZERO, n_reserve=0, fx_spread=_ZERO,
                      currency_in=None, fx_rate=None):
    """Return (source_rows, facts). source_rows are verification.py-shaped 5-key
    dicts the orchestrator renders into the 数据来源与核验 table; facts are the
    `human_cn`-with-【N】 lines the writer must reproduce verbatim. The reserve / FX
    facts are appended only for a cross-border run (both 0 otherwise)."""
    rows, facts = [], []
    anchor = [0]

    def add(claim_cn, value, detail, *, source=platform_file):
        anchor[0] += 1
        n = anchor[0]
        rows.append({
            "anchor": n, "claim_cn": claim_cn, "source_file": source,
            "source_detail": detail, "computed_by": COMPUTED_BY,
        })
        facts.append(f"{claim_cn} ¥{_fmt(value)}【{n}】（{detail}）")
        return n

    cur = (currency_in or "").strip()
    fx_note = f"，原币 {cur}，汇率 {fx_rate}" if (cur and fx_rate is not None) else ""
    add("应收（订单总额）", gross, f"{platform_name}结算单 {n_success} 笔成功订单求和{fx_note}")
    add("实收（平台应结净额）", settled, "已扣手续费/退款/佣金")
    add("总手续费", fee_total, f"{n_success} 笔成功订单手续费求和")
    if n_refund or refund_total:
        add("总退款", refund_total, f"{n_refund} 笔退款求和")
    add("在途/未结算", in_transit, f"{n_transit} 笔，预计 T+{lag} 到账")
    if reserve or n_reserve:
        add("账户准备金(reserve)", reserve, f"{n_reserve} 笔，平台保留未结算（计入在途）")
    if fx_spread:
        add("汇兑点差(FX)", fx_spread, f"按汇率 {fx_rate} 折算的汇兑点差成本")
    add("银行实际到账", arrived, f"{n_bank} 笔银行到账求和", source=bank_file)
    # The amount is the bank-arrival gap (实收 − 到账 − 在途); the worklist count is a
    # SEPARATE figure (it also includes order-level 单边/部分匹配 items), so the fact must
    # NOT imply the count composes the amount.
    add("未解释差异", unexplained_amount, "平台应结与银行到账的差额（在途已扣除）")
    return rows, facts


# ---------------------------------------------------------------------------
# File reading (the only impure entry; lazily imports pandas + crypto)
# ---------------------------------------------------------------------------
def reconcile_records(records_by_role, pack, *, learned_rules=None, period_label=None,
                      file_names=None):
    """Normalize raw record dicts for each role, then reconcile. The testable seam:
    `records_by_role` = {"platform": [...], "bank": [...], "ledger": [...]} with raw
    rows keyed by each source's own column headers."""
    names = file_names or {}
    # Cross-border seam: a pack may name a pre-normalization adapter (e.g. Amazon FF V2)
    # that aggregates a multi-row-per-order export into one synthetic per-order record
    # (in CNY) and returns the reserve/FX extras reconcile folds in. None for domestic
    # packs (no `adapter` key) → the path below is byte-for-byte the original.
    raw_platform = records_by_role.get("platform")
    cross_border = None
    adapter = crossborder_adapters.get_adapter(pack)
    if adapter:
        raw_platform, cross_border = adapter(raw_platform, pack)
    plat = normalize_rows(raw_platform, "platform", pack,
                          names.get("platform", "平台结算单"))
    bank = normalize_rows(records_by_role.get("bank"), "bank", pack,
                          names.get("bank", "银行流水"))
    ledger = normalize_rows(records_by_role.get("ledger"), "ledger", pack,
                            names.get("ledger", "账本"))
    return reconcile(plat, bank, ledger, pack=pack, learned_rules=learned_rules,
                     period_label=period_label, cross_border=cross_border)


def run_settlement(files_by_role, pack, *, learned_rules=None, period_label=None):
    """Read each uploaded file (decrypting in memory) into raw records, then
    reconcile. `files_by_role` = {role: (path, display_name)}. Raises ValueError with
    a customer-facing Chinese message when a required source can't be read/recognized."""
    records, names = {}, {}
    for role, spec in (files_by_role or {}).items():
        path, display = spec if isinstance(spec, (tuple, list)) else (spec, role)
        names[role] = display
        records[role] = _read_table(path)
    return reconcile_records(records, pack, learned_rules=learned_rules,
                             period_label=period_label, file_names=names)


def _read_table(path):
    """Read an .xlsx/.xls/.csv into a list of raw row dicts (str values), decrypting
    at rest via crypto. Full-fidelity (NOT file_tools.parse_file's 30-row preview)."""
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
def _tolerance(pack):
    try:
        return Decimal(str(pack.get("amount_tolerance"))) if pack.get("amount_tolerance") \
            is not None else AMOUNT_TOLERANCE
    except (InvalidOperation, TypeError):
        return AMOUNT_TOLERANCE


def _add_days(d, days):
    from datetime import timedelta
    return d + timedelta(days=int(days))


def _fmt(d):
    """Format a Decimal/number as a thousands-separated 2dp string (for human_cn)."""
    return f"{_f2(d):,.2f}"


def _first_file(rows):
    for r in (rows or []):
        if r.get("source_file"):
            return r["source_file"]
    return ""


# Convenience for callers that persist the result blob.
def to_json(result):
    return json.dumps(result, ensure_ascii=False)

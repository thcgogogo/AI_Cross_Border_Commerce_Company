"""
cash_position_engine.py — DETERMINISTIC 现金位置 / 资金跑道 simulation (the capstone core).

A forward-looking cash projection built ON TOP of the 跨境财务 hub's reconciled outputs. The
hub is backward-looking (it reconciles what happened); this engine answers the seller's daily
question — "where is my money right now, and how much can I actually spend?" — by separating
AVAILABLE cash from LOCKED / 在途 (platform reserves + in-transit settlements + un-结汇 外币) and
rolling a near-term AVAILABLE-cash runway forward from money already committed / in-flight.

Reliability story (identical to trends.py / settlement_engine): this engine owns EVERY number;
the LLM writer only words them. Pure Python — no LLM, no DB, no network, no file IO. Money as
Decimal internally, floats-via-_f2 out, JSON-serializable. Returns the SAME envelope as the hub
engines (summary / bridge / source_rows / facts_block) PLUS cash-specific blocks (position /
runway / crunch / data_completeness / assumptions), so it rides the existing writer +
verification + persistence + PDF rails.

What it is NOT (hard product invariants — see the plan):
  * NOT a precise far-horizon forecast. v1 projects ONLY the COMMITTED pipeline (deterministic,
    high-confidence). The sales-driven far horizon (ranges/scenarios) is Phase 2 and is never
    a single confident number here.
  * NOT advice and NOT an action. It surfaces position + the next crunch + the assumptions; the
    seller and their 会计 decide. It places no PO, settles no FX, moves no money.
  * NOT a generic estimator. The position is the seller's REAL reconciled one (hub-sourced); the
    only inputs the hub cannot know (current bank balance, planned 备货, terms, FX, buffer) come
    in as explicit, editable inputs — never silent defaults (cash_templates marks them PROVISIONAL).

Forward TIMING (reserve release, 退税 receipt, 结汇) is platform/seller-specific and NOT in the
hub; it comes from the editable `assumptions` lags (history-derived later). Every projected flow
traces to its source via a 【N】 anchor.
"""
import calendar
from datetime import date, datetime
from decimal import Decimal

from .settlement_engine import _add_days, _f2, _fmt, _money, _parse_date

_ZERO = Decimal("0")
TOLERANCE = Decimal("0.01")
COMPUTED_BY = "cash_position_engine"

# Currencies treated as already-RMB (no FX conversion). USD converts at the editable fx_rate;
# any other currency is conservatively treated as RMB and noted (v1 supports CNY + USD).
_CNY_ALIASES = {"", "CNY", "RMB", "¥", "￥", "人民币"}
_USD_ALIASES = {"USD", "US$", "$", "美元", "美金"}


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------
def _as_date(x, default=None):
    """Coerce a date / datetime / ISO-ish string into a date (or `default`)."""
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return _parse_date(x) or default


def _to_cny(amount, currency, fx_rate):
    """Convert an amount in its source currency to RMB at the editable fx_rate. USD only in v1;
    unknown currencies are treated as already-RMB (the caller surfaces a note)."""
    cur = (currency or "").strip().upper()
    amt = _money(amount)
    if cur in _USD_ALIASES:
        return amt * _money(fx_rate)
    return amt  # CNY (or unknown → conservative passthrough)


def _known_currency(currency):
    cur = (currency or "").strip().upper()
    return cur in _CNY_ALIASES or cur in _USD_ALIASES


def _monthly_dates(start, end, dom):
    """Dates on day-of-month `dom` within [start, end), for recurring fixed costs (工资/房租/税).
    A `dom` past a month's length lands on that month's ACTUAL last day (so month-end payroll on
    the 31st isn't silently pulled to the 28th). Iteration-capped; the horizon is ~a quarter."""
    dom = max(1, int(dom or 1))
    out = []
    y, m = start.year, start.month
    for _ in range(60):
        d = date(y, m, min(dom, calendar.monthrange(y, m)[1]))
        if d >= end:
            break
        if d >= start:
            out.append(d)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


# ---------------------------------------------------------------------------
# the projection
# ---------------------------------------------------------------------------
def project_cash(*, as_of=None, assumptions=None, hub=None, manual=None, period_label=None):
    """Project the forward cash position + runway. Pure.

    Inputs (all assembled by the orchestrator from the brain — the engine reads no DB):
      as_of       projection start date (date / ISO string); defaults to today.
      assumptions merged editable assumptions: {fx_rate, buffer_cny, horizon_weeks, stale_days,
                  lags:{settlement_lag_days, reserve_release_days, rebate_lag_days, ...},
                  include_rebate, _provisional}.
      hub         read-only hub outputs: {"settlement": <latest_settlement_summary or None>,
                  "rebate": <latest_crossborder_summary('rebate') or None>}.
      manual      what the hub cannot know: {opening_balances:{CNY,USD}, restock[],
                  supplier_payments[], ad_budget[], fixed_costs[], duties[], financing[]}.

    Returns the engine envelope (summary / position / runway / crunch / data_completeness /
    assumptions / bridge / source_rows / facts_block)."""
    a = assumptions or {}
    hub = hub or {}
    manual = manual or {}
    as_of = _as_date(as_of) or date.today()

    fx = _money(a.get("fx_rate") or 7.2)
    buffer = _money(a.get("buffer_cny") or 0)
    horizon_weeks = max(1, int(a.get("horizon_weeks") or 13))
    stale_days = int(a.get("stale_days") or 14)
    lags = a.get("lags") or {}
    settle_lag = int(lags.get("settlement_lag_days", 7))
    reserve_lag = int(lags.get("reserve_release_days", 14))
    rebate_lag = int(lags.get("rebate_lag_days", 60))
    include_rebate = bool(a.get("include_rebate", True))
    horizon_end = _add_days(as_of, 7 * horizon_weeks)

    # ---- 【N】 provenance --------------------------------------------------
    rows, facts = [], []
    anchor = [0]

    def add_fact(claim_cn, value_str, detail, source_file="现金引擎"):
        anchor[0] += 1
        n = anchor[0]
        rows.append({"anchor": n, "claim_cn": claim_cn, "source_file": source_file,
                     "source_detail": detail, "computed_by": COMPUTED_BY})
        facts.append(f"{claim_cn} {value_str}【{n}】（{detail}）")
        return n

    # ---- opening AVAILABLE cash (seller-known; the hub can't see the bank balance) ----
    opening = manual.get("opening_balances") or {}
    open_cny = _money(opening.get("CNY"))
    open_usd = _money(opening.get("USD"))
    available_now = open_cny + open_usd * fx
    by_currency = {"CNY": _f2(open_cny), "USD": _f2(open_usd)}

    # ---- LOCKED / 在途 + committed inflow SCHEDULE (hub-sourced) ----------
    settlement = hub.get("settlement")
    rebate = hub.get("rebate")
    reserve = _ZERO
    in_transit_agg = _ZERO
    committed_inflows = []   # [{date(obj), amount(Decimal CNY), source, ref}] — locked converting in

    if settlement:
        s = settlement.get("summary") or {}
        for at in settlement.get("attributions") or []:
            if at.get("kind") == "reserve_held":
                reserve += _money(at.get("amount"))
        # In-transit (not-yet-arrived) committed inflow = the settlement's AGGREGATE 在途金额 minus
        # the reserve portion (the settlement engine folds reserve INTO 在途). We deliberately use
        # the aggregate, NOT a per-order re-derivation: the persisted per-order list carries no
        # arrival flag, and the seller's opening bank balance already includes the proceeds of
        # orders that HAVE arrived — so scheduling each order by `order_date + lag > as_of` would
        # double-count arrived money against the opening balance (the settlement engine classifies
        # in-transit against its bank-coverage end, a different reference than our as_of). The
        # aggregate 在途 is the bank-coverage-correct not-yet-arrived total, so it never
        # double-counts. (A future hub enhancement could emit a per-order arrival flag for a dated
        # schedule; until then this single bucket arrives at as_of + settlement_lag_days.)
        in_transit_agg = _money(s.get("在途金额")) - reserve
        if in_transit_agg < _ZERO:
            in_transit_agg = _ZERO
        if in_transit_agg > _ZERO:
            committed_inflows.append({"date": _add_days(as_of, settle_lag),
                                      "amount": in_transit_agg, "source": "settlement",
                                      "ref": "在途回款合计"})
        # reserve releases as a single bucket at as_of + reserve_release_days (PROVISIONAL lag)
        if reserve > _ZERO:
            committed_inflows.append({"date": _add_days(as_of, reserve_lag),
                                      "amount": reserve, "source": "reserve_release",
                                      "ref": "平台准备金"})

    # ---- expected 退税 (a committed-but-not-yet-realized claim) ------------
    rebate_amt = _ZERO
    if include_rebate and rebate:
        rebate_amt = _money(rebate.get("可退税额"))
        if rebate_amt > _ZERO:
            committed_inflows.append({"date": _add_days(as_of, rebate_lag),
                                      "amount": rebate_amt, "source": "rebate",
                                      "ref": rebate.get("period_label") or "退税核算"})

    unsettled_fx = _ZERO   # v1: un-结汇 USD held by platform not yet modeled separately
    locked_total = reserve + in_transit_agg + unsettled_fx

    # ---- committed OUTFLOWS + financing advances (manual; hub can't know) -
    outflows = []        # [{date(obj), amount(Decimal CNY), currency, label, kind}]
    advances = []        # [{date(obj), amount(Decimal CNY), source, ref}] — external money in

    def _add_out(d, amount, currency, label, kind):
        d = _as_date(d)
        amt = _to_cny(amount, currency, fx)
        if d is None or amt <= _ZERO:
            return
        outflows.append({"date": d, "amount": amt, "currency": currency or "CNY",
                         "label": label, "kind": kind})

    for grp, kind in (("supplier_payments", "supplier"), ("duties", "duty")):
        for it in manual.get(grp) or []:
            _add_out(it.get("due_date"), it.get("amount"), it.get("currency"),
                     it.get("label") or kind, kind)

    # 备货 (restock) — a replenishment-plan row (Phase 3, closing the planning→cash loop):
    #   amount, OR qty × unit_cost when amount is absent;
    #   an optional 定金/尾款 split (deposit_pct < 100) where the 尾款 lands order_date + 头程
    #     (freight_days, defaulting to the assumption) — modeling how 备货 cash really leaves.
    # A plain {amount, due_date} row (deposit_pct defaulting to 100) behaves EXACTLY as before, so
    # the solver's injected PO + every existing caller are unchanged.
    freight_default = int(lags.get("freight_days", 21))
    for it in manual.get("restock") or []:
        amt = _money(it.get("amount"))
        if amt <= _ZERO:
            amt = _money(it.get("qty")) * _money(it.get("unit_cost"))
        amt = _to_cny(amt, it.get("currency"), fx)
        order_date = _as_date(it.get("due_date") or it.get("order_date"))
        if amt <= _ZERO or order_date is None:
            continue
        try:
            dep_pct = (Decimal(str(it.get("deposit_pct")))
                       if it.get("deposit_pct") not in (None, "") else Decimal("100"))
        except (ValueError, ArithmeticError):
            dep_pct = Decimal("100")
        dep_pct = max(_ZERO, min(Decimal("100"), dep_pct))
        cur = it.get("currency") or "CNY"
        label = it.get("label") or "备货"
        if dep_pct >= Decimal("100"):
            outflows.append({"date": order_date, "amount": amt, "currency": cur,
                             "label": label, "kind": "restock"})
        else:
            deposit = amt * dep_pct / Decimal("100")
            try:
                fdays = int(it.get("freight_days")) if it.get("freight_days") not in (None, "") \
                    else freight_default
            except (TypeError, ValueError):
                fdays = freight_default
            outflows.append({"date": order_date, "amount": deposit, "currency": cur,
                             "label": label + "(定金)", "kind": "restock"})
            outflows.append({"date": _add_days(order_date, fdays), "amount": amt - deposit,
                             "currency": cur, "label": label + "(尾款)", "kind": "restock"})

    for it in manual.get("financing") or []:
        d = _as_date(it.get("due_date"))
        amt = _to_cny(it.get("amount"), it.get("currency"), fx)
        if d is None or amt <= _ZERO:
            continue
        if (it.get("kind") or "repayment") == "advance":
            advances.append({"date": d, "amount": amt, "source": "financing",
                             "ref": it.get("label") or "融资到账"})
        else:
            outflows.append({"date": d, "amount": amt, "currency": it.get("currency") or "CNY",
                             "label": it.get("label") or "融资还款", "kind": "financing"})

    for it in manual.get("ad_budget") or []:
        if it.get("weekly"):
            wk = _to_cny(it.get("amount"), it.get("currency"), fx)
            if wk > _ZERO:
                for w in range(horizon_weeks):
                    outflows.append({"date": _add_days(as_of, 7 * w), "amount": wk,
                                     "currency": it.get("currency") or "CNY",
                                     "label": it.get("label") or "广告投放", "kind": "ad"})
        else:
            _add_out(it.get("due_date"), it.get("amount"), it.get("currency"),
                     it.get("label") or "广告投放", "ad")

    for it in manual.get("fixed_costs") or []:
        amt = _to_cny(it.get("amount"), it.get("currency"), fx)
        if amt <= _ZERO:
            continue
        for d in _monthly_dates(as_of, horizon_end, it.get("day_of_month")):
            outflows.append({"date": d, "amount": amt, "currency": it.get("currency") or "CNY",
                             "label": it.get("label") or "固定成本", "kind": "fixed"})

    inflows = committed_inflows + advances

    # ---- week-by-week roll (the runway) ----------------------------------
    weekly = []
    bal = available_now
    locked_remaining = locked_total + rebate_amt   # all committed inflows convert in over time
    min_balance = bal
    crunch = None
    for w in range(horizon_weeks):
        ws = _add_days(as_of, 7 * w)
        we = _add_days(ws, 7)
        win = sum((f["amount"] for f in inflows if ws <= f["date"] < we), _ZERO)
        wout = sum((o["amount"] for o in outflows if ws <= o["date"] < we), _ZERO)
        committed_in = sum((f["amount"] for f in committed_inflows if ws <= f["date"] < we),
                           _ZERO)
        bal = bal + win - wout
        locked_remaining = max(_ZERO, locked_remaining - committed_in)
        if bal < min_balance:
            min_balance = bal
        # committed_in is the 已承诺/在途 figure the UI column claims (回款/准备金/退税); `win` also
        # includes financing advances (external money-in), so it would mislabel those as committed.
        weekly.append({"week": w, "start": ws.isoformat(), "available": _f2(bal),
                       "locked": _f2(locked_remaining), "committed_in": _f2(committed_in),
                       "total_in": _f2(win), "out": _f2(wout)})
        if crunch is None and bal < buffer:
            crunch = {"week": w, "date": ws.isoformat(),
                      "shortfall": _f2(buffer - bal), "available": _f2(bal)}

    # ---- far-horizon FORECAST overlay (Phase 2, LOW-CONFIDENCE) ----------
    # Sales the seller estimates will keep coming convert to cash after the settlement lag. This
    # is the ONLY sales-driven projection and it NEVER touches the committed runway / crunch /
    # solver above (the invariant) — it is a clearly-separate band the UI renders as 预测.
    weekly_sales = _money(manual.get("weekly_sales"))
    forecast = None
    if weekly_sales > _ZERO:
        band = Decimal(str(a.get("forecast_band", 0.3)))
        lag_weeks = max(0, round(settle_lag / 7.0))

        def _track(mult):
            b = available_now
            row = []
            for w in range(horizon_weeks):
                ws = _add_days(as_of, 7 * w)
                we = _add_days(ws, 7)
                win = sum((f["amount"] for f in inflows if ws <= f["date"] < we), _ZERO)
                wout = sum((o["amount"] for o in outflows if ws <= o["date"] < we), _ZERO)
                fin = (weekly_sales * mult) if w >= lag_weeks else _ZERO
                b = b + win + fin - wout
                row.append(_f2(b))
            return row

        forecast = {
            "base": _track(Decimal("1")), "low": _track(Decimal("1") - band),
            "high": _track(Decimal("1") + band), "weekly_sales": _f2(weekly_sales),
            "band": float(band),
            "note": "预测基于销售估算，低置信度；不影响上面的『已承诺』资金跑道与现金紧张判断。",
        }

    # ---- bridge self-audit: 期初 + Σ流入 − Σ流出 == 期末 (within tolerance) ----
    tot_in = sum((f["amount"] for f in inflows if as_of <= f["date"] < horizon_end), _ZERO)
    tot_out = sum((o["amount"] for o in outflows if as_of <= o["date"] < horizon_end), _ZERO)
    closing = available_now + tot_in - tot_out
    bridge = {
        "ok": bool(abs(closing - bal) <= TOLERANCE),
        "lhs": _f2(closing), "rhs": _f2(bal), "imbalance": _f2(closing - bal),
        "期初可动用": _f2(available_now), "区间流入": _f2(tot_in),
        "区间流出": _f2(tot_out), "期末可动用": _f2(bal),
    }

    # ---- data completeness / staleness (never trust a stale number) ------
    missing = []
    stale = False
    settlements_through = ""
    pending_unreconciled = 0.0
    if settlement:
        s = settlement.get("summary") or {}
        settlements_through = (settlement.get("period_label") or s.get("period_label")
                               or (settlement.get("created_at") or "")[:10] or "")
        pending_unreconciled = _f2(_money(s.get("未解释差异")))
        created = _as_date((settlement.get("created_at") or "")[:10])
        if created is not None and (as_of - created).days > stale_days:
            stale = True
    else:
        missing.append("回款核对（对账）尚未运行：现金位置仅基于期初余额，未含在途回款与平台准备金")
    if include_rebate and not rebate:
        missing.append("退税核算尚未运行：未含预计退税到账")
    # Unknown/unsupported currencies are converted 1:1 to RMB (only CNY + USD are supported in v1);
    # surface that rather than silently risk under-stating a flow. Covers both outflow rows and any
    # opening-balance bucket outside {CNY, USD} (which the engine otherwise ignores).
    unknown_curr = {(o.get("currency") or "").strip().upper() for o in outflows
                    if (o.get("currency") or "").strip() and not _known_currency(o.get("currency"))}
    unknown_curr |= {str(k).strip().upper() for k in (opening or {})
                     if str(k).strip().upper() not in ("CNY", "USD") and _money(opening.get(k)) != 0}
    if unknown_curr:
        missing.append("检测到未支持币种 " + "、".join(sorted(unknown_curr))
                       + "：已按人民币原值处理（v1 仅支持 CNY/USD），金额可能不准。")
    data_completeness = {
        "as_of": as_of.isoformat(),
        "settlements_through": settlements_through,
        "pending_unreconciled": pending_unreconciled,
        "stale": bool(stale), "missing": missing,
        # Honest deferral (plan Phase 3 / NOT-DO #7): no inventory data source is integrated, so
        # we do NOT fabricate a 现金循环周期(CCC) or 库存周转 — they are out of scope until 库存补货 ships.
        "deferred": ["现金循环周期(CCC) / 库存周转：需库存数据，待『库存补货』接入后提供"],
    }

    # ---- facts (the writer must quote these verbatim with their 【N】) ----
    add_fact("可动用现金", f"¥{_fmt(available_now)}", "期初银行 + 收款服务商余额（卖家填写）")
    add_fact("锁定/在途合计", f"¥{_fmt(locked_total)}",
             f"平台准备金 ¥{_fmt(reserve)} + 在途回款 ¥{_fmt(in_transit_agg)}（由对账带入）")
    if rebate_amt > _ZERO:
        add_fact("预计退税", f"¥{_fmt(rebate_amt)}",
                 f"退税核算可退税额，按假设时滞预计 T+{rebate_lag} 天到账（时滞可在设置中编辑）")
    if crunch:
        add_fact("最早现金紧张", crunch["date"],
                 f"该周可动用 ¥{_fmt(_money(crunch['available']))} 低于现金缓冲 "
                 f"¥{_fmt(buffer)}，缺口 ¥{_fmt(_money(crunch['shortfall']))}")
    else:
        add_fact("资金跑道", f"未来 {horizon_weeks} 周",
                 f"按已承诺资金推演，期间未跌破现金缓冲 ¥{_fmt(buffer)}")
    add_fact("最低可动用", f"¥{_fmt(min_balance)}", f"未来 {horizon_weeks} 周内最低周末余额")

    # ---- assemble the envelope -------------------------------------------
    pending_arrivals = [{"date": f["date"].isoformat(), "amount": _f2(f["amount"]),
                         "currency": "CNY", "source": f["source"], "ref": f["ref"]}
                        for f in sorted(committed_inflows, key=lambda x: x["date"])]

    summary = {
        "可动用现金": _f2(available_now),
        "锁定在途合计": _f2(locked_total),
        "预计退税": _f2(rebate_amt),
        "现金缓冲": _f2(buffer),
        "最低可动用": _f2(min_balance),
        "现金紧张日期": (crunch or {}).get("date") or "",
        "现金紧张缺口": (crunch or {}).get("shortfall") or 0.0,
        "fx_rate": _f2(fx),
        "horizon_weeks": horizon_weeks,
        "period_label": period_label or settlements_through,
    }
    position = {
        "as_of": as_of.isoformat(),
        "available_now": _f2(available_now),
        "by_currency": by_currency,
        "locked_now": {"reserve": _f2(reserve), "in_transit": _f2(in_transit_agg),
                       "unsettled_fx": _f2(unsettled_fx), "total": _f2(locked_total)},
        "expected": {"rebate": _f2(rebate_amt), "total": _f2(rebate_amt)},
    }
    runway = {
        "weekly": weekly, "min_balance": _f2(min_balance),
        "horizon_weeks": horizon_weeks, "pending_arrivals": pending_arrivals,
        "forecast": forecast,
    }
    assumptions_echo = {
        "fx_rate": _f2(fx), "buffer_cny": _f2(buffer), "horizon_weeks": horizon_weeks,
        "stale_days": stale_days, "include_rebate": include_rebate,
        "lags": {"settlement_lag_days": settle_lag, "reserve_release_days": reserve_lag,
                 "rebate_lag_days": rebate_lag},
        "provisional": bool(a.get("_provisional", True)),
    }
    return {
        "summary": summary, "position": position, "runway": runway, "crunch": crunch,
        "data_completeness": data_completeness, "assumptions": assumptions_echo,
        "bridge": bridge, "source_rows": rows, "facts_block": facts,
    }


# ===========================================================================
# Phase 2 — the interactive affordability solver + scenarios (deterministic, no LLM).
# These power the SYNCHRONOUS /api/cash/solve + /api/cash/scenario surfaces: compute-on-request,
# no Run row, no model call. They reuse project_cash so every number stays the engine's.
# ===========================================================================
def _shift_iso(iso, days):
    d = _as_date(iso)
    return _add_days(d, int(days)).isoformat() if d else iso


def max_safe_restock(result, from_week=0):
    """The largest one-time 备货/PO outflow (placed at `from_week`) that keeps EVERY later week
    at or above the cash buffer — closed-form, exact: X_max = max(0, min(available[w≥from_week]) −
    buffer). Pure; reads a project_cash result (its committed runway). The binding week is the one
    that would first drop below buffer once X is spent."""
    weekly = (result.get("runway") or {}).get("weekly") or []
    buffer = _money((result.get("summary") or {}).get("现金缓冲"))
    tail = [w for w in weekly if w.get("week", 0) >= int(from_week)]
    if not tail:
        return {"max_amount": 0.0, "binding_week": None, "binding_date": None,
                "buffer": _f2(buffer), "from_week": int(from_week)}
    binding = min(tail, key=lambda w: _money(w["available"]))
    x = _money(binding["available"]) - buffer
    if x < _ZERO:
        x = _ZERO
    return {"max_amount": _f2(x), "binding_week": binding["week"],
            "binding_date": binding["start"], "buffer": _f2(buffer),
            "from_week": int(from_week)}


def affordability(*, as_of=None, assumptions=None, hub=None, manual=None, po=None,
                  period_label=None):
    """Can the seller afford a SPECIFIC 备货/PO? Injects it as a dated outflow, re-rolls, and
    reports whether any week drops below buffer (+ the min balance before/after). Pure."""
    po = po or {}
    base = project_cash(as_of=as_of, assumptions=assumptions, hub=hub, manual=manual,
                        period_label=period_label)
    m2 = dict(manual or {})
    m2["restock"] = list(m2.get("restock") or []) + [{
        "label": po.get("label") or "拟备货", "amount": po.get("amount"),
        "currency": po.get("currency") or "CNY", "due_date": po.get("date")}]
    after = project_cash(as_of=as_of, assumptions=assumptions, hub=hub, manual=m2,
                         period_label=period_label)
    buffer = _money((after.get("summary") or {}).get("现金缓冲"))
    weekly = (after.get("runway") or {}).get("weekly") or []
    breaches = [w for w in weekly if _money(w["available"]) < buffer]
    return {
        "affordable": not breaches,
        "min_balance_before": base["runway"]["min_balance"],
        "min_balance_after": after["runway"]["min_balance"],
        "weeks_below_buffer": len(breaches),
        "first_breach_date": (breaches[0]["start"] if breaches else None),
        "buffer": _f2(buffer),
        "po": {"amount": _f2(_money(po.get("amount"))),
               "currency": po.get("currency") or "CNY", "date": po.get("date")},
    }


def scenario(*, as_of=None, assumptions=None, hub=None, manual=None, levers=None,
             period_label=None):
    """Re-roll under what-if levers vs the baseline. Levers:
      • rebate_delay_days — 退税 arrives later (adds to the rebate lag)
      • po_delay_weeks    — push every planned 备货 out N weeks
      • sales_delta_pct   — scale the (low-confidence) weekly-sales forecast ±%
    Returns baseline-vs-scenario min balance + crunch. Pure."""
    levers = levers or {}
    base = project_cash(as_of=as_of, assumptions=assumptions, hub=hub, manual=manual,
                        period_label=period_label)
    a2 = dict(assumptions or {})
    a2["lags"] = dict((assumptions or {}).get("lags") or {})
    rd = int(levers.get("rebate_delay_days") or 0)
    if rd:
        a2["lags"]["rebate_lag_days"] = int(a2["lags"].get("rebate_lag_days", 60)) + rd
    m2 = dict(manual or {})
    pw = int(levers.get("po_delay_weeks") or 0)
    if pw:
        # shift whichever date key the row actually carries (due_date OR the seam's order_date),
        # so the lever isn't a silent no-op for order_date-keyed replenishment-plan rows.
        def _delay(r):
            key = "due_date" if (r.get("due_date") not in (None, "")) else "order_date"
            return {**r, key: _shift_iso(r.get(key), pw * 7)}
        m2["restock"] = [_delay(r) for r in (m2.get("restock") or [])]
    sd = levers.get("sales_delta_pct")
    if sd is not None and m2.get("weekly_sales"):
        m2["weekly_sales"] = _f2(_money(m2["weekly_sales"])
                                 * (Decimal("1") + Decimal(str(sd)) / Decimal("100")))
    scen = project_cash(as_of=as_of, assumptions=a2, hub=hub, manual=m2,
                        period_label=period_label)

    def _fc_end(r):
        fc = (r.get("runway") or {}).get("forecast")
        return fc["base"][-1] if fc and fc.get("base") else None

    out = {
        "baseline": {"min_balance": base["runway"]["min_balance"], "crunch": base.get("crunch")},
        "scenario": {"min_balance": scen["runway"]["min_balance"], "crunch": scen.get("crunch")},
        "min_balance_delta": _f2(_money(scen["runway"]["min_balance"])
                                 - _money(base["runway"]["min_balance"])),
        "levers": {"sales_delta_pct": sd, "rebate_delay_days": rd, "po_delay_weeks": pw},
    }
    # The committed min_balance is sales-immune (the invariant); sales_delta_pct moves only the
    # LOW-CONFIDENCE forecast, so surface the forecast end-balance too or that lever looks inert.
    fb, fs = _fc_end(base), _fc_end(scen)
    if fb is not None or fs is not None:
        out["forecast_end"] = {"baseline": fb, "scenario": fs}
    return out


def forecast_weekly_sales(history, horizon_weeks, band=0.3):
    """PURE helper for later history-driven velocity (plan §4): a moving-average run-rate from a
    list of recent period sales values → a flat weekly base with a ±band. Returns
    {weekly_sales, band} or None. NOT yet wired to a specific brain metric (PROVISIONAL — v1 uses
    the seller's manual weekly_sales estimate); kept here so the wiring is a one-line change."""
    vals = [float(v) for v in (history or []) if isinstance(v, (int, float))]
    if not vals:
        return None
    avg_period = sum(vals[-6:]) / len(vals[-6:])     # last ≤6 periods
    # treat each historical period as ~a month → /4.345 to a weekly run-rate
    weekly = avg_period / 4.345
    return {"weekly_sales": round(weekly, 2), "band": band}

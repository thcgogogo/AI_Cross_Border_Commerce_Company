"""
dunning.py — DETERMINISTIC 催收 (dunning) tier logic + the no-double-dun re-run classifier.

The genuinely NEW part of the AR workflow (对账 has no analog). Pure Python, no LLM:
the escalation TIER is decided here from days-overdue + amount + the prior-sent tier +
the 确定/疑似 grade; only the message WORDING is the LLM's job (see config.dunning_writer
+ ar_orchestrator). Every amount/date/invoice-no in a draft is INJECTED from the engine
via build_account_facts; the writer may never produce one.

Two pure functions own the two decisions, kept separate so each is unit-testable:
  - assign_tier(...)      -> what tier does the DATA warrant (ignores timing).
  - classify_outcome(...) -> given the prior SENT message + the cadence lock, is acting
                             allowed RIGHT NOW: NEW / ESCALATED / LOCKED / AWAITING /
                             HANDOFF. Only NEW and ESCALATED produce a draft.
"""

from datetime import datetime, timedelta

from . import ar_templates as templates

GRADE_SUSPECTED = "疑似"

TIER_REMINDER = "reminder"
TIER_URGE = "urge"
TIER_FINAL = "final_notice"
TIER_HANDOFF = "handoff"

_RANK = {TIER_REMINDER: 1, TIER_URGE: 2, TIER_FINAL: 3, TIER_HANDOFF: 4}
_BY_RANK = {1: TIER_REMINDER, 2: TIER_URGE, 3: TIER_FINAL, 4: TIER_HANDOFF}

TIER_CN = {
    TIER_REMINDER: "提醒", TIER_URGE: "催促",
    TIER_FINAL: "最终通知", TIER_HANDOFF: "转人工/法务",
}

# Re-run outcomes (per overdue account).
OUTCOME_NEW = "new"            # never dunned -> first touch (draft)
OUTCOME_ESCALATED = "escalated"  # warrants a higher tier than last sent + cadence elapsed (draft)
OUTCOME_LOCKED = "locked"      # dunned within the cadence window -> surface read-only, NO draft
OUTCOME_AWAITING = "awaiting"  # overdue, gap elapsed, band not advanced -> NO re-send
OUTCOME_HANDOFF = "handoff"    # internal 转人工/法务 -> NO customer message


# ---------------------------------------------------------------------------
# Tier logic (pure; ignores timing — that is classify_outcome's job)
# ---------------------------------------------------------------------------
def _eligible_tier(dpd, amount, cfg):
    """The tier the data alone warrants, from days-overdue + amount (no history)."""
    if dpd > cfg["final_max_dpd"] or amount >= cfg["amount_legal"]:
        return TIER_HANDOFF
    if amount >= cfg["amount_large"]:          # large balances escalate sooner
        if dpd >= 30:
            return TIER_FINAL
        if dpd >= 8:
            return TIER_URGE
        return TIER_REMINDER
    if dpd <= cfg["reminder_max_dpd"]:
        return TIER_REMINDER
    if dpd <= cfg["urge_max_dpd"]:
        return TIER_URGE
    return TIER_FINAL


def assign_tier(dpd, amount, last_sent_tier, grade, pack):
    """Decide what 催收 tier an overdue account warrants. Pure: no timing, no DB, no LLM.

    Returns {eligible_tier, target_tier, route, is_escalation, capped_by_grade,
    reason_cn}. route ∈ {"draft","handoff","skip"}; target_tier is None for skip and
    handoff produces no customer draft. 疑似 caps the tier (never auto-draft an
    aggressive message against unverified data); escalation climbs ONE rung per run and
    never drops a tier."""
    cfg = templates.dunning_config(pack)
    amount = float(amount or 0)
    dpd = int(dpd or 0)
    eligible = _eligible_tier(dpd, amount, cfg)
    capped = False

    # 确定/疑似 gate — the invariant: never dun aggressively off unverified data.
    if grade == GRADE_SUSPECTED:
        if _RANK[eligible] >= _RANK[TIER_FINAL]:
            return {"eligible_tier": eligible, "target_tier": TIER_HANDOFF,
                    "route": "handoff", "is_escalation": False, "capped_by_grade": True,
                    "reason_cn": "疑似数据，转人工核实后再催收"}
        eligible = TIER_REMINDER                # cap urge -> reminder
        capped = True

    if eligible == TIER_HANDOFF:
        return {"eligible_tier": eligible, "target_tier": TIER_HANDOFF,
                "route": "handoff", "is_escalation": False, "capped_by_grade": capped,
                "reason_cn": "逾期严重或金额过大，转人工/法务"}

    last_rank = _RANK.get(last_sent_tier or "", 0)
    elig_rank = _RANK[eligible]
    if last_rank == 0:
        target_rank = elig_rank                       # first touch reflects severity
    elif elig_rank > last_rank:
        target_rank = min(elig_rank, last_rank + 1)   # escalate ONE rung per run
    else:
        # the data warrants a tier already sent (or lower) — nothing new to draft.
        return {"eligible_tier": eligible, "target_tier": None, "route": "skip",
                "is_escalation": False, "capped_by_grade": capped,
                "reason_cn": "尚未需要升级，等待回款或账龄推进"}
    target = _BY_RANK[target_rank]
    return {"eligible_tier": eligible, "target_tier": target, "route": "draft",
            "is_escalation": last_rank > 0, "capped_by_grade": capped,
            "reason_cn": f"账龄/金额符合『{TIER_CN[target]}』级别"}


# ---------------------------------------------------------------------------
# Re-run classifier (the no-double-dun gate; owns the cadence lock)
# ---------------------------------------------------------------------------
def _parse_dt(x):
    if isinstance(x, datetime):
        return x
    if not x:
        return None
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00").split("+")[0])
    except ValueError:
        return None


def classify_outcome(decision, last_dunning, pack, *, now):
    """Given the tier `decision` (from assign_tier), the most-recent SENT message
    `last_dunning` (a dict with `tier` + `sent_at`, or None), and the cadence lock,
    decide the re-run outcome RIGHT NOW. Returns
    {outcome, should_draft, tier, locked_until, reason_cn}. The literal no-double-dun
    guarantee: a SENT message within `cadence_lock_days` -> LOCKED, never re-drafted."""
    route = decision.get("route")
    if route == "handoff":
        return {"outcome": OUTCOME_HANDOFF, "should_draft": False,
                "tier": TIER_HANDOFF, "locked_until": None,
                "reason_cn": decision.get("reason_cn") or "转人工/法务"}
    target = decision.get("target_tier")
    if route == "skip" or not target:
        return {"outcome": OUTCOME_AWAITING, "should_draft": False, "tier": None,
                "locked_until": None, "reason_cn": decision.get("reason_cn") or "等待"}

    if not last_dunning:
        return {"outcome": OUTCOME_NEW, "should_draft": True, "tier": target,
                "locked_until": None, "reason_cn": "首次催收"}

    last_tier = last_dunning.get("tier")
    sent_at = _parse_dt(last_dunning.get("sent_at"))
    if sent_at is not None:
        lock_days = templates.cadence_lock_days(pack, last_tier)
        unlock = sent_at + timedelta(days=int(lock_days))
        if now < unlock:
            return {"outcome": OUTCOME_LOCKED, "should_draft": False, "tier": target,
                    "locked_until": unlock.isoformat(),
                    "reason_cn": f"{lock_days} 天内已催收过（{TIER_CN.get(last_tier, last_tier)}），"
                                 f"暂不重复催收"}

    if _RANK.get(target, 0) > _RANK.get(last_tier or "", 0):
        return {"outcome": OUTCOME_ESCALATED, "should_draft": True, "tier": target,
                "locked_until": None,
                "reason_cn": f"已升级至『{TIER_CN[target]}』"}
    # same tier already sent, cadence elapsed, no escalation — re-sending would double-dun.
    return {"outcome": OUTCOME_AWAITING, "should_draft": False, "tier": target,
            "locked_until": None, "reason_cn": "级别未提升，等待回款"}


# ---------------------------------------------------------------------------
# Per-customer summary + the engine-number facts block injected into a draft
# ---------------------------------------------------------------------------
def summarize_customer(invoices):
    """From a customer's OVERDUE invoice rows (engine inv_rows), return
    (max_dpd, overdue_total, count) — the scalars assign_tier needs."""
    overdue = [i for i in invoices if i.get("overdue")]
    max_dpd = max((int(i.get("dpd") or 0) for i in overdue), default=0)
    total = sum((float(i.get("open_balance") or 0) for i in overdue), 0.0)
    return max_dpd, round(total, 2), len(overdue)


def build_account_facts(customer, invoices, tier):
    """Build the per-account 催收事实数据 the draft writer must quote VERBATIM. Every
    number comes from the engine's invoice rows — the writer never computes one. Returns
    {customer_name, tier, tier_cn, facts_block, source_rows, known_numbers}. The
    orchestrator wraps facts_block under DUNNING_HEADER and audits the draft against
    known_numbers."""
    overdue = [i for i in invoices if i.get("overdue")]
    rows, facts, known = [], [], []
    anchor = [0]

    def add(claim, value_str, detail):
        anchor[0] += 1
        n = anchor[0]
        rows.append({"anchor": n, "claim_cn": claim, "source_file": "应收台账",
                     "source_detail": detail, "computed_by": "ar_aging_engine"})
        facts.append(f"{claim} {value_str}【{n}】（{detail}）")
        return n

    total = round(sum(float(i.get("open_balance") or 0) for i in overdue), 2)
    for inv in sorted(overdue, key=lambda i: i.get("dpd") or 0, reverse=True):
        amt = float(inv.get("open_balance") or 0)
        add(f"逾期发票 {inv.get('invoice_no')}", f"¥{amt:,.2f}",
            f"到期日 {inv.get('due_date')}，已逾期 {inv.get('dpd')} 天")
        known += [f"{amt:.2f}", f"{amt:,.2f}", str(int(inv.get('dpd') or 0))]
    add("应收合计", f"¥{total:,.2f}", f"{len(overdue)} 张逾期发票")
    known += [f"{total:.2f}", f"{total:,.2f}"]

    return {
        "customer_name": customer.get("customer_name") or customer.get("customer_id") or "客户",
        "tier": tier, "tier_cn": TIER_CN.get(tier, tier),
        "facts_block": facts, "source_rows": rows, "known_numbers": known,
    }

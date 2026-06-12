"""
admin/queries.py — the ops dashboard's SQL (Phase 10).

Every number on /admin comes from ONE function here, each a small amount of SQL
(via SQLAlchemy text()) plus minimal Python shaping — so the dashboard's
definitions live in one reviewable file and the unit tests assert each function
against hand-computed values on a seeded dataset (tests/test_phase10_onboarding).

The metrics, and their exact definitions:

  signups_per_day        accounts created per calendar day, last N days
                         (zero-filled so the sparkline has every day).
  activation_rate        % of the window's signups that produced >= 1 SUCCEEDED
                         run within 7 days of signing up.
  retention_cohorts      signup-week x weeks-active matrix: for each cohort
                         (week of signup, Monday-start), how many accounts had
                         >= 1 run in week k after signing up.
  mrr                    sum of ACTIVE subscriptions' plan prices. Plan prices
                         live in config.PLANS (not the DB), so SQL counts per
                         plan_code and Python multiplies.
  churn_this_month       subscriptions canceled this calendar month / active at
                         month start. `updated_at` stamps the cancel transition,
                         so "canceled this month" = status=canceled AND
                         updated_at >= month_start (documented approximation —
                         there is no status-history table).
  account_economics      per account, 90d revenue (paid orders net of refunds)
                         vs 90d upstream model cost (runs.total_cost_yuan — the
                         existing ¥ cost tracking), flagging cost > 60% of
                         revenue.
  onboarding_funnel      % of wizard-starting accounts that completed each step.

Dialect: the same SQL must run on SQLite (dev/tests) and Postgres (prod). The
only divergent pieces — day bucketing and "timestamp + N days" — go through the
two tiny helpers below, switched on the live engine's dialect. The SQLite
branches carry literal '%' (strftime); that is safe because SQLite's qmark
paramstyle never interprets '%', and the Postgres branches use to_char/
make_interval with no '%' at all.

Every function takes `now` so tests pin time deterministically.
"""

from datetime import datetime, timedelta

from sqlalchemy import text

from agent_team import config, db

# How many wizard steps the funnel reports on (steps 1–6; step 0 = wizard open).
# Phase 8 inserted ④ 导入历史数据 — the Moat-1 front-loading step — shifting
# 首份报告/每周定时 to 5/6 (plan §3.10; numbering authority: models.ONBOARDING_STEPS).
FUNNEL_STEPS = (1, 2, 3, 4, 5, 6)
FUNNEL_STEP_LABELS = {
    1: "公司信息",
    2: "上传/示例数据",
    3: "KPI 确认",
    4: "导入历史数据",
    5: "首份报告",
    6: "每周定时",
}
# Economics flag threshold, per the spec: cost > 60% of revenue.
ECONOMICS_FLAG_RATIO = 0.6


def _dialect():
    try:
        return db.engine.dialect.name
    except Exception:  # pragma: no cover — engine not initialised yet
        return "sqlite"


def _day_expr(col):
    """SQL rendering `col` (a timestamp) as its 'YYYY-MM-DD' day string."""
    if _dialect() == "postgresql":
        return "to_char(" + col + ", 'YYYY-MM-DD')"
    return "strftime('%Y-%m-%d', " + col + ")"


def _plus_days_expr(col, days_param):
    """SQL rendering `col` + :days_param days (days_param is a bind name)."""
    if _dialect() == "postgresql":
        return "%s + make_interval(days => CAST(:%s AS int))" % (col, days_param)
    return "datetime(%s, '+' || CAST(:%s AS text) || ' days')" % (col, days_param)


# ---------------------------------------------------------------------------
# Signups & activation
# ---------------------------------------------------------------------------
def signups_per_day(days=30, now=None):
    """[{"day": "YYYY-MM-DD", "count": int}] for the last `days` calendar days
    (inclusive of today), zero-filled so charts get every day."""
    now = now or datetime.utcnow()
    start_day = (now - timedelta(days=int(days) - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    sql = text(
        "SELECT " + _day_expr("created_at") + " AS d, COUNT(*) AS n "
        "FROM accounts WHERE created_at >= :start GROUP BY d")
    with db.session_scope() as s:
        rows = dict(s.execute(sql, {"start": start_day}).fetchall())
    out = []
    for i in range(int(days)):
        day = (start_day + timedelta(days=i)).strftime("%Y-%m-%d")
        out.append({"day": day, "count": int(rows.get(day, 0))})
    return out


def activation_rate(days=30, window_days=7, now=None):
    """Of accounts created in the last `days`, the share that produced at least
    one SUCCEEDED run within `window_days` of signing up.

    Returns {"signups", "activated", "rate"} — rate in [0, 1], 0.0 when there
    are no signups. Accounts join runs through companies on the (lowercased)
    login email, the same identity the rest of the brain uses."""
    now = now or datetime.utcnow()
    start = now - timedelta(days=int(days))
    with db.session_scope() as s:
        signups = int(s.execute(
            text("SELECT COUNT(*) FROM accounts WHERE created_at >= :start"),
            {"start": start}).scalar() or 0)
        activated = int(s.execute(text(
            "SELECT COUNT(DISTINCT a.id) FROM accounts a "
            "JOIN companies c ON c.account_email = LOWER(a.email) "
            "JOIN runs r ON r.company_id = c.id "
            " AND r.status = 'succeeded' "
            " AND r.started_at >= a.created_at "
            " AND r.started_at <= " + _plus_days_expr("a.created_at", "wd") + " "
            "WHERE a.created_at >= :start"),
            {"start": start, "wd": int(window_days)}).scalar() or 0)
    rate = (activated / signups) if signups else 0.0
    return {"signups": signups, "activated": activated, "rate": round(rate, 4)}


# ---------------------------------------------------------------------------
# Weekly retention cohorts
# ---------------------------------------------------------------------------
def _week_start(dt):
    """The Monday 00:00 of `dt`'s week (cohorts and activity buckets align on it)."""
    monday = dt - timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def retention_cohorts(weeks=8, now=None):
    """The signup-week x weeks-active matrix.

    Cohort = accounts whose created_at falls in one Monday-start week (the last
    `weeks` weeks). Cell k = how many of them had >= 1 run (any status — a failed
    run is still activity) in [signup_week_start + 7k, +7) days. Cells in the
    future (cohort week + k beyond now) are None so the UI greys them out.

    One SQL pull (signups + their runs via the email join); the week bucketing is
    plain Python on that result set. Returns
    {"week_starts": [...], "cohorts": [{"week": "YYYY-MM-DD", "signups": n,
    "active": [n0, n1, …]}]} — n0 includes the signup week itself."""
    now = now or datetime.utcnow()
    this_week = _week_start(now)
    first_week = this_week - timedelta(weeks=int(weeks) - 1)

    with db.session_scope() as s:
        accounts = s.execute(text(
            "SELECT a.id, a.created_at FROM accounts a "
            "WHERE a.created_at >= :start"), {"start": first_week}).fetchall()
        activity = s.execute(text(
            "SELECT a.id, r.started_at FROM accounts a "
            "JOIN companies c ON c.account_email = LOWER(a.email) "
            "JOIN runs r ON r.company_id = c.id "
            "WHERE a.created_at >= :start"), {"start": first_week}).fetchall()

    def _dt(v):
        return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))

    runs_by_account = {}
    for acc_id, started_at in activity:
        runs_by_account.setdefault(acc_id, []).append(_dt(started_at))

    week_starts = [first_week + timedelta(weeks=i) for i in range(int(weeks))]
    cohorts = []
    for ws in week_starts:
        we = ws + timedelta(weeks=1)
        members = [(acc_id, _dt(created)) for acc_id, created in accounts
                   if ws <= _dt(created) < we]
        active = []
        for k in range(int(weeks)):
            cell_start = ws + timedelta(weeks=k)
            if cell_start > now:
                active.append(None)        # the future — not measurable yet
                continue
            cell_end = cell_start + timedelta(weeks=1)
            n = sum(1 for acc_id, _created in members
                    if any(cell_start <= r < cell_end
                           for r in runs_by_account.get(acc_id, ())))
            active.append(n)
        cohorts.append({"week": ws.strftime("%Y-%m-%d"),
                        "signups": len(members), "active": active})
    return {"week_starts": [w.strftime("%Y-%m-%d") for w in week_starts],
            "cohorts": cohorts}


# ---------------------------------------------------------------------------
# MRR & churn
# ---------------------------------------------------------------------------
def mrr(now=None):
    """Monthly recurring revenue = Σ over ACTIVE subscriptions of their plan's
    monthly price. SQL counts per plan_code; prices come from config.PLANS (the
    single price authority — the DB stores no prices). past_due / canceled rows
    don't count. Returns {"total_yuan", "by_plan": {code: {count, price_yuan,
    mrr_yuan}}, "active_subscriptions"}."""
    with db.session_scope() as s:
        rows = s.execute(text(
            "SELECT plan_code, COUNT(*) FROM subscriptions "
            "WHERE status = 'active' GROUP BY plan_code")).fetchall()
    by_plan, total, count_all = {}, 0.0, 0
    for plan_code, count in rows:
        price = float(config.plan(plan_code).get("monthly_price_yuan") or 0.0)
        by_plan[plan_code] = {"count": int(count), "price_yuan": price,
                              "mrr_yuan": round(price * int(count), 2)}
        total += price * int(count)
        count_all += int(count)
    return {"total_yuan": round(total, 2), "by_plan": by_plan,
            "active_subscriptions": count_all}


def churn_this_month(now=None):
    """Churn = subscriptions canceled THIS calendar month / subscriptions active
    at month start.

    "Canceled this month": status=canceled AND updated_at >= month_start (the
    tick/cancel path stamps updated_at at the transition — no history table, so
    this is the documented approximation). "Active at month start": created
    before the month AND not already canceled before it. Returns {"canceled",
    "active_at_month_start", "rate"} — rate 0.0 when the denominator is 0."""
    now = now or datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with db.session_scope() as s:
        canceled = int(s.execute(text(
            "SELECT COUNT(*) FROM subscriptions "
            "WHERE status = 'canceled' AND updated_at >= :ms"),
            {"ms": month_start}).scalar() or 0)
        active_at_start = int(s.execute(text(
            "SELECT COUNT(*) FROM subscriptions "
            "WHERE created_at < :ms "
            "  AND NOT (status = 'canceled' AND updated_at < :ms)"),
            {"ms": month_start}).scalar() or 0)
    rate = (canceled / active_at_start) if active_at_start else 0.0
    return {"canceled": canceled, "active_at_month_start": active_at_start,
            "rate": round(rate, 4)}


# ---------------------------------------------------------------------------
# Per-account economics
# ---------------------------------------------------------------------------
def account_economics(days=90, flag_ratio=ECONOMICS_FLAG_RATIO, now=None):
    """Per-account revenue vs upstream model cost over the last `days`.

    revenue  = Σ (amount_yuan - refunded_yuan) of the account's orders PAID in
               the window (status paid or refunded — a refunded order's net is
               what we kept).
    cost     = Σ runs.total_cost_yuan of the account's runs STARTED in the
               window — the existing ¥ upstream cost tracking, joined through
               companies on the login email.
    flagged  = cost > flag_ratio × revenue (so any cost with ¥0 revenue flags).

    Returns rows sorted by revenue desc then cost desc:
    [{"account_id", "email", "revenue_yuan", "cost_yuan", "ratio", "flagged"}];
    ratio is None when revenue is 0."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=int(days))
    with db.session_scope() as s:
        revenue_rows = s.execute(text(
            "SELECT account_id, SUM(amount_yuan - refunded_yuan) "
            "FROM orders "
            "WHERE paid_at IS NOT NULL AND paid_at >= :cutoff "
            "  AND status IN ('paid', 'refunded') "
            "GROUP BY account_id"), {"cutoff": cutoff}).fetchall()
        cost_rows = s.execute(text(
            "SELECT a.id, COALESCE(SUM(r.total_cost_yuan), 0) "
            "FROM accounts a "
            "JOIN companies c ON c.account_email = LOWER(a.email) "
            "JOIN runs r ON r.company_id = c.id "
            "WHERE r.started_at >= :cutoff "
            "GROUP BY a.id"), {"cutoff": cutoff}).fetchall()
        emails = dict(s.execute(text("SELECT id, email FROM accounts")).fetchall())

    revenue = {acc: float(v or 0) for acc, v in revenue_rows}
    cost = {acc: float(v or 0) for acc, v in cost_rows}
    out = []
    for acc in sorted(set(revenue) | set(cost)):
        rev = round(revenue.get(acc, 0.0), 2)
        c = round(cost.get(acc, 0.0), 4)
        flagged = c > float(flag_ratio) * rev
        out.append({
            "account_id": acc,
            "email": emails.get(acc) or "",
            "revenue_yuan": rev,
            "cost_yuan": c,
            "ratio": (round(c / rev, 4) if rev else None),
            "flagged": bool(flagged),
        })
    out.sort(key=lambda r: (-r["revenue_yuan"], -r["cost_yuan"]))
    return out


# ---------------------------------------------------------------------------
# Onboarding funnel
# ---------------------------------------------------------------------------
def onboarding_funnel():
    """Step completion percentages for the wizard.

    Denominator = DISTINCT accounts with ANY onboarding event (they opened the
    wizard at least once). Per step 1–6, numerator = DISTINCT accounts with a
    `completed` event at that step; skips don't count as completion. Returns
    {"accounts_started": n, "steps": [{"step", "label", "completed", "pct"}]}
    with pct as a 0–100 float (one decimal), 0.0 when nobody has started."""
    with db.session_scope() as s:
        started = int(s.execute(text(
            "SELECT COUNT(DISTINCT account_id) FROM onboarding_events"
        )).scalar() or 0)
        rows = dict(s.execute(text(
            "SELECT step, COUNT(DISTINCT account_id) FROM onboarding_events "
            "WHERE event = 'completed' AND step BETWEEN 1 AND 6 "
            "GROUP BY step")).fetchall())
    steps = []
    for n in FUNNEL_STEPS:
        completed = int(rows.get(n, 0))
        pct = round(completed * 100.0 / started, 1) if started else 0.0
        steps.append({"step": n, "label": FUNNEL_STEP_LABELS[n],
                      "completed": completed, "pct": pct})
    return {"accounts_started": started, "steps": steps}

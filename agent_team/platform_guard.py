"""
platform_guard.py — the platform spend circuit breaker (Phase 8, CRITICAL).

Abuse of OUR upstream provider keys is the top financial risk in a China launch: a
stolen account or a runaway loop could burn real money on Anthropic / OpenAI / …
keys far faster than per-account ¥ balances would suggest. This module is the
backstop — it meters every ¥ we spend upstream and HARD-STOPS new runs
platform-wide once a rolling cap is exceeded, until a human resets it.

  record_spend(provider, yuan, account_id)  — called from the universal billing
        chokepoint (customers.record_usage) for every billed upstream call.
  check(account_id, account_daily_cap)       — gate before starting a run / gateway
        / fusion call. Returns (allowed, reason). If a platform cap is already
        exceeded it TRIPS the breaker (latch + admin alert) and refuses. A
        per-account daily cap refuses just that account WITHOUT tripping the latch.
  status()                                   — dict for /admin/circuit-breaker
        (spend vs. caps + latch state).
  trip(scope, reason) / reset()              — latch control; reset() is the admin's
        "resume service" button and also resets the rolling window.

DESIGN — the breaker is a PERSISTENT LATCH (metrics_store.PlatformBreaker). Once
tripped it STAYS tripped, even if spend later drops below the cap, until an admin
resets it: an automatic un-trip would let a runaway flap on/off and keep spending.
reset() stamps cleared_at; rolling sums then count only spend recorded AFTER the
reset, so service genuinely resumes and the breaker re-trips only on NEW over-cap
spend.

ZERO-CONFIG — caps come from config (env). With every cap 0 (the default) the
breaker is INERT, so dev/tests never trip and `python app.py` needs no setup.
"""

import logging
from datetime import datetime

from . import alerts, config, metrics_store

log = logging.getLogger("agent_team.platform_guard")

# VERBATIM user-facing refusal from the Phase 8 spec — do not reword.
BUSY_MESSAGE = "系统繁忙，请稍后再试"

_SCOPE_CN = {"hourly": "小时", "daily": "单日"}


def record_spend(provider, yuan, account_id=None):
    """Record one upstream spend (best-effort; never raises — billing must not fail
    because the meter hiccuped). Returns the row id or None."""
    return metrics_store.record_platform_spend(provider, yuan, account_id=account_id)


def _window_starts(now=None):
    """(plain_hour_start, plain_day_start, platform_hour_start, platform_day_start).
    The platform_* variants are lower-bounded by the breaker's last reset so a reset
    zeroes the rolling platform counter; the plain_* variants (true clock windows)
    drive the per-account daily cap, which a platform reset must NOT zero."""
    now = now or datetime.utcnow()
    hour = now.replace(minute=0, second=0, microsecond=0)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    cleared = metrics_store.breaker_cleared_at()
    p_hour = max(hour, cleared) if cleared else hour
    p_day = max(day, cleared) if cleared else day
    return hour, day, p_hour, p_day


def _trip_and_alert(scope, spent, cap):
    """Latch the breaker and fire a one-shot admin alert on the OFF→ON transition."""
    reason = "平台%s上游消耗 ¥%.2f 触及上限 ¥%.2f" % (
        _SCOPE_CN.get(scope, scope), spent, cap)
    already = metrics_store.get_breaker().get("tripped")
    metrics_store.trip_breaker(scope, reason)
    log.error("platform spend breaker TRIPPED (%s): %s", scope, reason)
    if not already:
        try:
            alerts.send_admin_alert("平台消费断路器已触发", reason)
        except Exception:  # noqa: BLE001 — alerting must never mask the trip itself
            log.exception("admin alert failed after breaker trip")


def check(account_id=None, account_daily_cap=None):
    """Gate a new run / gateway / fusion call. Returns (allowed: bool, reason: str|None).

    Order: if already latched-tripped → refuse. Else if the hourly or daily PLATFORM
    cap is exceeded → trip (latch + alert) and refuse. Else if this account is over
    its per-account daily cap → refuse just this account (no platform trip). A cap of
    0/None means "no cap" for that tier."""
    if metrics_store.get_breaker().get("tripped"):
        return False, BUSY_MESSAGE

    _, _, p_hour, p_day = _window_starts()

    hourly_cap = config.platform_hourly_cap_yuan()
    if hourly_cap and hourly_cap > 0:
        spent = metrics_store.sum_platform_spend(p_hour)
        if spent >= hourly_cap:
            _trip_and_alert("hourly", spent, hourly_cap)
            return False, BUSY_MESSAGE

    daily_cap = config.platform_daily_cap_yuan()
    if daily_cap and daily_cap > 0:
        spent = metrics_store.sum_platform_spend(p_day)
        if spent >= daily_cap:
            _trip_and_alert("daily", spent, daily_cap)
            return False, BUSY_MESSAGE

    if account_id:
        cap = (account_daily_cap if account_daily_cap is not None
               else config.account_daily_cap_yuan())
        if cap and cap > 0:
            _, day, _, _ = _window_starts()
            spent = metrics_store.sum_platform_spend(day, account_id=account_id)
            if spent >= cap:
                return False, BUSY_MESSAGE

    return True, None


def status():
    """A snapshot for the admin page: latch state, caps, and current rolling spend."""
    _, _, p_hour, p_day = _window_starts()
    return {
        "breaker": metrics_store.get_breaker(),
        "hourly_cap_yuan": config.platform_hourly_cap_yuan(),
        "daily_cap_yuan": config.platform_daily_cap_yuan(),
        "account_daily_cap_yuan": config.account_daily_cap_yuan(),
        "hour_spend_yuan": round(metrics_store.sum_platform_spend(p_hour), 4),
        "day_spend_yuan": round(metrics_store.sum_platform_spend(p_day), 4),
    }


def trip(scope="manual", reason="手动触发"):
    """Manually latch the breaker (admin / tests). Returns the breaker dict."""
    return metrics_store.trip_breaker(scope, reason)


def reset():
    """Admin "resume service": clear the latch and reset the rolling window."""
    log.warning("platform spend breaker RESET by admin")
    return metrics_store.reset_breaker()

"""
alerts.py — operator alerts for the platform circuit breaker (Phase 8).

When the platform-wide spend circuit breaker trips, an operator has to know
immediately and reset it manually (see platform_guard + /admin/circuit-breaker).
This module is the one place that fans an alert out to the configured channels:

  - EMAIL   to every address in ADMIN_EMAILS (via delivery.send_email / SMTP).
  - WEBHOOK a POST to ADMIN_ALERT_WEBHOOK (a 企业微信群机器人 or any markdown sink).

send_admin_alert(subject, body) is BEST-EFFORT: a channel that is unconfigured or
fails is recorded in the returned dict but never raises — an alert delivery problem
must not mask the breaker event that triggered it. Both channels reuse delivery.py,
so credentials/URLs are never logged (delivery returns credential-free errors).

`_EMAIL_SENDER` / `_WEBHOOK_SENDER` are module-level so tests can swap them for a
spy without configuring real SMTP or a webhook.
"""

import logging

from . import config, delivery

log = logging.getLogger("agent_team.alerts")

# Swappable in tests (monkeypatch these names). Each mirrors the delivery.py
# signature and returns (ok: bool, error: str|None).
_EMAIL_SENDER = delivery.send_email
_WEBHOOK_SENDER = delivery.send_wechat_webhook


def send_admin_alert(subject, body):
    """Fan `subject`/`body` out to the admin email list + alert webhook.

    Returns a result dict::

        {"email": [{"to": addr, "ok": bool, "error": str|None}, ...],
         "webhook": {"ok": bool, "error": str|None} | None,
         "any_sent": bool}

    Never raises — every channel is attempted independently and its outcome
    recorded. `any_sent` is True if at least one channel delivered."""
    result = {"email": [], "webhook": None, "any_sent": False}

    for addr in config.admin_emails():
        try:
            ok, err = _EMAIL_SENDER(addr, subject, body)
        except Exception as e:  # noqa: BLE001 — type only; delivery already hides creds
            ok, err = False, type(e).__name__
        result["email"].append({"to": addr, "ok": bool(ok), "error": err})
        result["any_sent"] = result["any_sent"] or bool(ok)

    webhook = config.admin_alert_webhook()
    if webhook:
        text = "### ⚠️ %s\n\n%s" % (subject, body)
        try:
            ok, err = _WEBHOOK_SENDER(webhook, text)
        except Exception as e:  # noqa: BLE001
            ok, err = False, type(e).__name__
        result["webhook"] = {"ok": bool(ok), "error": err}
        result["any_sent"] = result["any_sent"] or bool(ok)

    if not result["any_sent"]:
        # No channel delivered (or none configured). Log it so the event is at
        # least visible in stdout — but NEVER include credentials or the URL.
        log.warning("admin alert not delivered (no channel configured/succeeded): %s",
                    subject)
    return result

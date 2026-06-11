"""
lifecycle.py — the Phase 10 lifecycle emails: welcome, day-3 nudge, receipts.

Three touchpoints, all in plain, short Chinese, all sent through the existing
delivery layer (so SMTP credentials never appear here or in logs):

  send_welcome_email        right after sign-up: a 3-step quickstart.
  daily_lifecycle_tick      the day-3 "还没生成第一份报告？" nudge with a
                            use-demo-data link, for accounts that still have no
                            finished report. Runs as a scheduler system job.
  send_renewal_receipt      a payment receipt when a subscription order is paid
                            (first purchase and renewals alike).

AT-MOST-ONCE: every send first claims its dedup mark via
metrics_store.record_lifecycle_event_once (welcome/nudge keyed per account,
receipts per order_no) and only then talks to SMTP — so a re-run of the daily
tick, a double signup submit, or a replayed payment notify can never spam the
customer. The trade-off (a crash between mark and send loses that one email) is
deliberate: for lifecycle mail, silence beats duplicates. Sends are best-effort —
a mail failure is logged by type only and never raises into signup/payment paths.
"""

import logging

from . import config, delivery, metrics_store
from .models import (
    LIFECYCLE_NUDGE_DAY3,
    LIFECYCLE_RECEIPT,
    LIFECYCLE_WELCOME,
    ORDER_SUBSCRIPTION,
)

log = logging.getLogger("agent_team.lifecycle")

# How old an account must be before the nudge, and how old is TOO old to bother
# (so enabling this feature on an existing deploy doesn't blast every veteran).
NUDGE_MIN_AGE_DAYS = 3
NUDGE_MAX_AGE_DAYS = 14


def _demo_link():
    """Deep link that opens the onboarding wizard on the demo-data step."""
    return config.app_base_url() + "/?onboarding=demo"


def _send(email, subject, body, kind):
    """One best-effort email; failures are logged by exception/reason TYPE only."""
    try:
        ok, err = delivery.send_email(email, subject, body)
        if not ok:
            log.info("lifecycle %s email not sent (%s)", kind, err)
        return bool(ok)
    except Exception as e:  # noqa: BLE001 — lifecycle mail must never raise upward
        log.info("lifecycle %s email not sent (%s)", kind, type(e).__name__)
        return False


# ---------------------------------------------------------------------------
# Welcome (on sign-up)
# ---------------------------------------------------------------------------
def send_welcome_email(account_id, email, name=None):
    """The 3-step quickstart, sent once per account right after sign-up.
    Returns True only when the mark was newly claimed AND the mail went out."""
    if not email:
        return False
    if not metrics_store.record_lifecycle_event_once(account_id, LIFECYCLE_WELCOME):
        return False
    body = (
        "%s 您好：\n\n"
        "欢迎使用统一智能体中转站！三步生成你的第一份经营周报：\n\n"
        "1. 登录后按「新手引导」上传一份近期经营数据（Excel/CSV），"
        "没有数据就点「用示例数据体验」；\n"
        "2. 点「生成第一份报告」，AI 团队几分钟内完成分析并出报告；\n"
        "3. 一键设置每周一早 8 点自动生成，报告自动送达邮箱/企业微信。\n\n"
        "开始体验：%s\n\n"
        "有任何问题，直接回复本邮件即可。\n— 统一智能体中转站"
        % ((name or "").strip() or "客户", config.app_base_url())
    )
    return _send(email, "欢迎使用 · 三步生成你的第一份经营周报", body, "welcome")


# ---------------------------------------------------------------------------
# Day-3 nudge (scheduler system job)
# ---------------------------------------------------------------------------
def daily_lifecycle_tick(now=None):
    """The daily nudge pass (idempotent — the dedup mark makes re-runs no-ops).

    Targets accounts aged NUDGE_MIN_AGE_DAYS–NUDGE_MAX_AGE_DAYS whose company
    still has NO succeeded run, and sends "还没生成第一份报告？" with the
    demo-data deep link. Returns {"nudged": [account_ids]} for logs/tests."""
    summary = {"nudged": []}
    for acct in metrics_store.list_accounts_for_nudge(
            NUDGE_MIN_AGE_DAYS, NUDGE_MAX_AGE_DAYS, now=now):
        try:
            company_id = metrics_store.company_id_for_email(acct["email"])
            if company_id and metrics_store.company_has_successful_run(company_id):
                continue  # they already reached the first report — no nudge
            if not metrics_store.record_lifecycle_event_once(
                    acct["id"], LIFECYCLE_NUDGE_DAY3):
                continue  # already nudged on a previous tick
            body = (
                "%s 您好：\n\n"
                "注册已经 3 天了，还没生成第一份报告？不用准备数据也能体验：\n\n"
                "点这里用示例数据一键生成：%s\n"
                "（登录后会自动打开新手引导，选「用示例数据体验」即可，"
                "AI 团队几分钟内就能交出一份完整的经营周报。）\n\n"
                "有任何问题，直接回复本邮件即可。\n— 统一智能体中转站"
                % (acct["name"], _demo_link())
            )
            if _send(acct["email"], "还没生成第一份报告？用示例数据一键体验",
                     body, "nudge_day3"):
                summary["nudged"].append(acct["id"])
        except Exception:  # noqa: BLE001 — one bad account must not stop the pass
            log.exception("lifecycle nudge failed for account %s", acct.get("id"))
    return summary


# ---------------------------------------------------------------------------
# Subscription payment receipts
# ---------------------------------------------------------------------------
def send_renewal_receipt(order):
    """Email a short receipt for a PAID subscription order (first purchase and
    renewals alike). `order` is the dict mark_order_paid returned. Deduped per
    order_no, so the replayed-notify path can call this freely."""
    if not order or order.get("type") != ORDER_SUBSCRIPTION:
        return False
    account_id = order.get("account_id")
    contact = metrics_store.account_contact(account_id) or {}
    if not contact.get("email"):
        return False
    if not metrics_store.record_lifecycle_event_once(
            account_id, LIFECYCLE_RECEIPT, detail=order.get("order_no")):
        return False
    plan = config.plan(order.get("plan_code"))
    sub = metrics_store.get_subscription(account_id) or {}
    paid_through = (sub.get("current_period_end") or "")[:10]
    body = (
        "%s 您好：\n\n"
        "我们已收到您的订阅付款，服务已生效。\n\n"
        "套餐：%s\n金额：¥%.2f\n订单号：%s\n"
        "服务有效期至：%s\n\n"
        "如需发票，请在控制台「订单与发票」中对该订单点击「申请发票」。\n"
        "— 统一智能体中转站"
        % (contact.get("name") or "客户", plan["label"],
           float(order.get("amount_yuan") or 0), order.get("order_no") or "",
           paid_through or "见控制台")
    )
    return _send(contact["email"], "订阅付款成功 · 收据", body, "receipt")

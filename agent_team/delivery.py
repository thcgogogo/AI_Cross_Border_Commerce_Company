"""
delivery.py — Push a finished report out to the customer's channels.

Two live channels:
  - email   SMTP, creds from .env (SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS /
            SMTP_FROM, plus optional SMTP_STARTTLS / SMTP_SSL). Sends the branded
            PDF as an attachment with a 3-line Chinese summary in the body.
  - wechat  企业微信群机器人 webhook (per-company URL, stored in the company brain).
            POSTs a markdown card: the 核心结论 + a login-protected download link.

A 服务号 (service-account template message) channel is stubbed below with a clear
TODO and a stable interface for a later phase.

SECURITY: SMTP credentials and the webhook URL are NEVER written to logs or to the
deliveries table. Failures are recorded as the exception *type* + a Chinese reason
only — never the host, password, or URL. Download links point at a login-protected
route that re-checks report ownership server-side (see app.py).
"""

import json
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage

from . import config, crypto, metrics_store, pdf_report
from .models import DELIVERY_FAILED, DELIVERY_SENT, DELIVERY_SKIPPED

INSUFFICIENT_BALANCE_MSG = "余额不足，本次周报未生成"


# ---------------------------------------------------------------------------
# Low-level senders (each returns (ok: bool, error: str|None); never leak creds)
# ---------------------------------------------------------------------------
def email_settings_from_env():
    """Read SMTP settings from the environment, or None if SMTP_HOST is unset.
    The raw values are returned ONLY to the sender below — never logged."""
    host = (os.environ.get("SMTP_HOST") or "").strip()
    if not host:
        return None

    def _flag(name):
        return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")

    try:
        port = int((os.environ.get("SMTP_PORT") or "25").strip() or "25")
    except ValueError:
        port = 25
    return {
        "host": host,
        "port": port,
        "user": (os.environ.get("SMTP_USER") or "").strip(),
        "password": os.environ.get("SMTP_PASS") or "",
        "from_addr": (os.environ.get("SMTP_FROM")
                      or os.environ.get("SMTP_USER") or "no-reply@localhost").strip(),
        "starttls": _flag("SMTP_STARTTLS"),
        "ssl": _flag("SMTP_SSL"),
    }


def send_email(to_addr, subject, body_text, attachment_path=None,
               settings=None, timeout=20):
    """Send one email (optionally with a PDF attachment). Returns (ok, error).
    The error is deliberately credential-free."""
    settings = settings or email_settings_from_env()
    if not settings:
        return False, "SMTP 未配置"
    if not to_addr:
        return False, "缺少收件人邮箱"

    msg = EmailMessage()
    msg["From"] = settings["from_addr"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body_text)
    if attachment_path and os.path.isfile(attachment_path):
        try:
            with open(attachment_path, "rb") as f:
                data = f.read()
            msg.add_attachment(data, maintype="application", subtype="pdf",
                               filename=os.path.basename(attachment_path))
        except OSError:
            pass  # a missing/unreadable PDF must not block the text email

    try:
        if settings.get("ssl"):
            server = smtplib.SMTP_SSL(settings["host"], settings["port"],
                                      timeout=timeout, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(settings["host"], settings["port"], timeout=timeout)
        with server:
            if settings.get("starttls"):
                server.starttls(context=ssl.create_default_context())
            if settings.get("user"):
                server.login(settings["user"], settings["password"])
            server.send_message(msg)
        return True, None
    except Exception as e:  # noqa: BLE001 — report type only, never the creds/host
        return False, "SMTP 发送失败：%s" % type(e).__name__


def send_wechat_webhook(webhook_url, markdown_text, timeout=15):
    """POST a markdown message to a 企业微信群机器人 webhook. Returns (ok, error).
    The webhook URL is NEVER included in the error string."""
    if not webhook_url:
        return False, "未配置企业微信群机器人 webhook"
    payload = json.dumps(
        {"msgtype": "markdown", "markdown": {"content": markdown_text}}
    ).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — config'd URL
            raw = resp.read().decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        if int(data.get("errcode", 0) or 0) == 0:
            return True, None
        return False, "企业微信返回 errcode=%s" % data.get("errcode")
    except Exception as e:  # noqa: BLE001 — type only, never the URL
        return False, "企业微信发送失败：%s" % type(e).__name__


def send_service_account_template(company_id, template_payload):
    """TODO(Phase 5+): 微信服务号模板消息.

    Stable interface for a later phase: given a company_id and a payload shaped
    like {"template_id": str, "url": str, "data": {field: {"value": str}}}, this
    will look up the company's 服务号 config (appid/secret + an openid mapping),
    fetch an access_token, and POST to
        https://api.weixin.qq.com/cgi-bin/message/template/send

    NOT yet implemented — returns a clear (False, reason) so callers and the
    deliveries log surface it explicitly instead of silently doing nothing.
    """
    return False, "服务号模板消息尚未实现（TODO）"


# ---------------------------------------------------------------------------
# High-level orchestration (records every attempt in the deliveries table)
# ---------------------------------------------------------------------------
def report_download_url(run_id):
    """The login-protected download URL for a run's PDF. Base from APP_BASE_URL
    (default the local dev server). The route itself re-checks ownership."""
    return "%s/api/reports/%s/download" % (config.app_base_url(), run_id)


def report_view_url(run_id):
    """The login-protected web report view (redesign B7) — the mobile-readable
    page the 企业微信/email message deep-links, next to the PDF download."""
    return "%s/reports/%s" % (config.app_base_url(), run_id)


def _summary_blurb(report_markdown):
    blurb = pdf_report.extract_section(report_markdown or "", "核心结论", max_lines=3)
    return blurb or "本期经营周报已生成，详见附件。"


def _deliver_email(company_name, blurb, pdf_path, recipient_email, download_url,
                   view_url=None):
    if not recipient_email:
        return False, "缺少账户邮箱"
    subject = "%s · 经营周报" % company_name
    body = ("%s 您好：\n\n本期经营周报已生成。\n\n本周核心结论：\n%s\n"
            % (company_name, blurb))
    if view_url:
        body += "\n在线查看报告（需登录）：%s\n" % view_url
    if download_url:
        body += "\n下载 PDF：%s\n" % download_url
    body += "\n（完整报告见附件 PDF）\n— %s AI 团队" % config.product_name()
    # The PDF is encrypted at rest; hand SMTP a real plaintext temp file (no-op
    # passthrough when FERNET is unset), cleaned up on context exit.
    with crypto.materialize_plaintext(pdf_path) as plain:
        return send_email(recipient_email, subject, body, attachment_path=plain)


def _deliver_wechat(company_id, company_name, blurb, download_url, view_url=None):
    webhook = metrics_store.get_branding(company_id).get("wechat_webhook")
    if not webhook:
        return False, "未配置企业微信群机器人 webhook"
    lines = ["### %s · 经营周报" % company_name, "", "**本周核心结论：**", blurb]
    if view_url:
        lines += ["", "[在线查看报告（需登录）](%s)" % view_url]
    if download_url:
        lines += ["", "[下载 PDF](%s)" % download_url]
    return send_wechat_webhook(webhook, "\n".join(lines))


def deliver_report(*, company_id, run_id, channels, pdf_path, report_markdown,
                   company_name, recipient_email=None, job_id=None,
                   download_url=None, log=None):
    """Deliver a finished report over each requested channel, logging one
    deliveries row per channel. Returns a list of per-channel result dicts.
    Best-effort: a channel failure is recorded, never raised."""
    company_name = (company_name or "本公司").strip() or "本公司"
    if download_url is None:
        download_url = report_download_url(run_id)
    # The B7 deep link: the mobile-readable web report at /reports/<run_id>.
    # Only meaningful when the run row exists (history-less fires send PDF only).
    view_url = report_view_url(run_id) if run_id else None
    blurb = _summary_blurb(report_markdown)

    results = []
    for raw_ch in (channels or []):
        ch = (raw_ch or "").strip().lower()
        if ch == "email":
            ok, err = _deliver_email(company_name, blurb, pdf_path,
                                     recipient_email, download_url,
                                     view_url=view_url)
        elif ch == "wechat":
            ok, err = _deliver_wechat(company_id, company_name, blurb,
                                      download_url, view_url=view_url)
        elif ch in ("service_account", "service-account", "fwh"):
            ok, err = send_service_account_template(company_id, {})
        else:
            ok, err = False, "未知交付渠道：%s" % (ch or "(空)")

        status = DELIVERY_SENT if ok else DELIVERY_FAILED
        metrics_store.record_delivery(ch or "system", status, job_id=job_id,
                                      run_id=run_id, error=(None if ok else err))
        if log:
            try:
                log("交付[%s]：%s" % (ch or "system",
                                     "成功" if ok else "失败 - %s" % (err or "")))
            except Exception:
                pass
        results.append({"channel": ch, "ok": ok, "status": status, "error": err})
    return results


def notify_insufficient_balance(*, company_id, job_id, channels,
                                recipient_email=None, log=None):
    """A scheduled fire produced nothing because the ¥ balance was too low. Send
    the "余额不足" notice over the configured channels (best-effort) and ALWAYS
    record a skipped delivery row per channel so the miss is auditable.
    Returns the list of recorded delivery ids."""
    msg = INSUFFICIENT_BALANCE_MSG
    chans = [c for c in (channels or []) if (c or "").strip()] or ["system"]
    recorded = []
    for raw_ch in chans:
        ch = (raw_ch or "").strip().lower()
        try:
            if ch == "email" and recipient_email:
                settings = email_settings_from_env()
                if settings:
                    send_email(recipient_email, "经营周报未生成：余额不足", msg,
                               settings=settings)
            elif ch == "wechat":
                webhook = metrics_store.get_branding(company_id).get("wechat_webhook")
                if webhook:
                    send_wechat_webhook(webhook, "⚠️ %s" % msg)
        except Exception:
            pass  # the notice is best-effort; the audit row below is what matters
        recorded.append(metrics_store.record_delivery(
            ch or "system", DELIVERY_SKIPPED, job_id=job_id, error=msg))
    if log:
        try:
            log(msg)
        except Exception:
            pass
    return recorded

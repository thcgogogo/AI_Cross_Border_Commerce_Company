"""
payments.py — payment provider abstraction + subscription billing (Phase 9).

Real Alipay / WeChat Pay replace the demo top-up. The rest of the app never cares
HOW money moves — it talks to one small interface:

    PaymentProvider.create_order(amount_yuan, subject, order_no) -> {pay_url/qr}
    PaymentProvider.verify_notify(request) -> (order_no, paid_amount, txn_id) | None
    PaymentProvider.refund(order_no, amount_yuan, ...) -> (ok, error)

Three implementations:
  - AlipayProvider     alipay.trade.page.pay + async-notify RSA2 (SHA256withRSA)
                       verification, implemented directly on the documented
                       open-api wire protocol with the `cryptography` package
                       (already a Phase 8 dependency) — the same no-SDK pattern
                       as the Aliyun SMS provider. The RSA2 crypto is the real
                       thing: requests are signed with the merchant application
                       private key, notifies verified against the Alipay public
                       key. Signature verification ALWAYS comes first.
  - WechatPayProvider  WeChat Pay APIv3 native (QR) pay. Requests carry the
                       WECHATPAY2-SHA256-RSA2048 Authorization signature built
                       from the merchant private key; notifies are verified
                       against the PLATFORM certificate and their resource
                       decrypted with the APIv3 key (AES-256-GCM).
  - MockProvider       the ZERO-CONFIG dev/test fallback: orders "pay" at a
                       local fake cashier, and notifies are HMAC-SHA256-signed
                       so a tampered payload fails verification exactly like a
                       bad RSA signature would. The moment a real provider is
                       configured, mock drops out of the available list and its
                       notify endpoint goes dead (see config).

This module also owns the SUBSCRIPTION BILLING POLICY (daily_subscription_tick):
renewal orders at T-3d with a user-confirmed pay link (China flows favor that
over silent auto-charge), a short grace window after expiry, then past_due
(features locked except data export), then canceled.

SECURITY: merchant keys live in env only and are never logged or persisted; every
error string is credential-free (exception TYPE only). The paid amount is taken
exclusively from the VERIFIED notify payload — never from anything a client sent.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from . import config, delivery, metrics_store
from .models import ORDER_CREATED, ORDER_SUBSCRIPTION, SUB_CANCELED, SUB_PAST_DUE

log = logging.getLogger("agent_team.payments")


# ---------------------------------------------------------------------------
# PEM/key helpers (Alipay's console hands out bare base64 bodies; env values
# often carry literal \n escapes — accept all of it).
# ---------------------------------------------------------------------------
def _ensure_pem(text, header):
    """Wrap a bare base64 key body as PEM (or pass a real PEM through)."""
    txt = (text or "").strip().replace("\\n", "\n")
    if not txt:
        return ""
    if "-----BEGIN" in txt:
        return txt
    body = "".join(txt.split())
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (header, "\n".join(lines), header)


def _load_private_key(text):
    """An RSA private key from PEM or a bare PKCS1/PKCS8 base64 body, or None."""
    from cryptography.hazmat.primitives import serialization
    for header in ("RSA PRIVATE KEY", "PRIVATE KEY"):
        pem = _ensure_pem(text, header)
        if not pem:
            return None
        try:
            return serialization.load_pem_private_key(pem.encode(), password=None)
        except Exception:  # noqa: BLE001 — try the other PKCS framing
            continue
    return None


def _load_public_key(text):
    """An RSA public key from PEM or a bare base64 body, or None."""
    from cryptography.hazmat.primitives import serialization
    try:
        return serialization.load_pem_public_key(_ensure_pem(text, "PUBLIC KEY").encode())
    except Exception:  # noqa: BLE001
        return None


def _rsa_sign_b64(private_key, message_bytes):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    sig = private_key.sign(message_bytes, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode("ascii")


def _rsa_verify(public_key, signature_b64, message_bytes):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    try:
        public_key.verify(base64.b64decode(signature_b64), message_bytes,
                          padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:  # noqa: BLE001 — any failure means "not verified"
        return False


def _amount2(x):
    """A fen-quantized Decimal, or None on junk."""
    try:
        return Decimal(str(x)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------
class PaymentProvider:
    """One payment channel. All three methods are credential-free on error."""

    name = "base"

    def create_order(self, amount_yuan, subject, order_no):  # pragma: no cover
        """Open the provider-side trade. Returns
        {"ok": bool, "pay_url": str|None, "qr_text": str|None, "error": str|None}."""
        raise NotImplementedError

    def verify_notify(self, request):  # pragma: no cover
        """SIGNATURE-FIRST check of an async notify. Returns
        (order_no, paid_amount, provider_txn_id) for a VERIFIED successful
        payment, or None for anything unverifiable (treat as hostile)."""
        raise NotImplementedError

    def refund(self, order_no, amount_yuan, total_yuan=None, reason=""):  # pragma: no cover
        """Refund `amount_yuan` of an order (full or partial). Returns (ok, error).
        `total_yuan` is the order's original amount (WeChat requires it)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Mock — dev/test. Signed notifies, local fake cashier, zero real money.
# ---------------------------------------------------------------------------
class MockProvider(PaymentProvider):
    """Dev/test provider. Its notifies carry an HMAC-SHA256 signature over the
    sorted fields, so the acceptance tests exercise the REAL handler logic:
    a tampered amount or signature fails verification exactly like a bad RSA2
    signature on a production notify."""

    name = "mock"

    def __init__(self, secret=None):
        self._secret = (secret or config.payment_mock_secret()).encode("utf-8")

    def sign(self, fields):
        canon = "&".join("%s=%s" % (k, fields[k])
                         for k in sorted(fields) if k != "sign")
        return hmac.new(self._secret, canon.encode("utf-8"), hashlib.sha256).hexdigest()

    def notify_fields(self, order_no, paid_amount, txn_id=None):
        """The signed form body of a 'payment succeeded' notify — what the fake
        cashier (and the tests) POST to /pay/notify/mock."""
        fields = {
            "order_no": str(order_no),
            "paid_amount": "%.2f" % float(paid_amount),
            "txn_id": txn_id or ("MOCK" + uuid.uuid4().hex[:20]),
        }
        fields["sign"] = self.sign(fields)
        return fields

    def create_order(self, amount_yuan, subject, order_no):
        return {"ok": True, "pay_url": "/pay/mock/%s" % order_no,
                "qr_text": None, "error": None}

    def verify_notify(self, request):
        fields = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
        sign = str(fields.get("sign") or "")
        expect = self.sign({k: str(v) for k, v in fields.items() if k != "sign"})
        if not (sign and hmac.compare_digest(sign, expect)):
            return None
        amount = _amount2(fields.get("paid_amount"))
        order_no = str(fields.get("order_no") or "").strip()
        if amount is None or not order_no:
            return None
        return order_no, float(amount), str(fields.get("txn_id") or "")[:128]

    def refund(self, order_no, amount_yuan, total_yuan=None, reason=""):
        return True, None


# ---------------------------------------------------------------------------
# Alipay — alipay.trade.page.pay + RSA2-verified async notify
# ---------------------------------------------------------------------------
class AlipayProvider(PaymentProvider):
    name = "alipay"

    def __init__(self, settings=None, timeout=15):
        self.settings = settings or config.alipay_settings() or {}
        self.timeout = timeout

    # -- request signing ------------------------------------------------------
    def _public_params(self, method, biz_content):
        return {
            "app_id": self.settings.get("app_id") or "",
            "method": method,
            "format": "JSON",
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": "1.0",
            "notify_url": config.app_base_url() + "/pay/notify/alipay",
            "biz_content": biz_content,
        }

    def _sign_params(self, params):
        key = _load_private_key(self.settings.get("app_private_key"))
        if key is None:
            raise ValueError("ALIPAY_APP_PRIVATE_KEY missing/unparseable")
        canon = "&".join("%s=%s" % (k, params[k])
                         for k in sorted(params) if params[k] != "" and k != "sign")
        params["sign"] = _rsa_sign_b64(key, canon.encode("utf-8"))
        return params

    def create_order(self, amount_yuan, subject, order_no):
        if not self.settings.get("app_id"):
            return {"ok": False, "pay_url": None, "qr_text": None,
                    "error": "支付宝支付未配置"}
        biz = json.dumps({
            "out_trade_no": str(order_no),
            "total_amount": "%.2f" % float(amount_yuan),
            "subject": str(subject or "充值")[:255],
            "product_code": "FAST_INSTANT_TRADE_PAY",
        }, ensure_ascii=False, separators=(",", ":"))
        params = self._public_params("alipay.trade.page.pay", biz)
        params["return_url"] = config.app_base_url() + "/"
        try:
            self._sign_params(params)
        except Exception as e:  # noqa: BLE001 — type only, never the key
            return {"ok": False, "pay_url": None, "qr_text": None,
                    "error": "支付宝下单失败：%s" % type(e).__name__}
        pay_url = (self.settings.get("gateway") or "https://openapi.alipay.com/gateway.do"
                   ) + "?" + urllib.parse.urlencode(params)
        return {"ok": True, "pay_url": pay_url, "qr_text": None, "error": None}

    def verify_notify(self, request):
        fields = request.form.to_dict()
        sign = fields.pop("sign", "")
        fields.pop("sign_type", None)
        if not sign or not fields:
            return None
        pub = _load_public_key(self.settings.get("alipay_public_key"))
        if pub is None:
            return None
        canon = "&".join("%s=%s" % (k, fields[k]) for k in sorted(fields))
        if not _rsa_verify(pub, sign, canon.encode("utf-8")):
            return None
        # Verified — now apply the documented business checks.
        if fields.get("app_id") != self.settings.get("app_id"):
            return None
        if fields.get("trade_status") not in ("TRADE_SUCCESS", "TRADE_FINISHED"):
            return None
        amount = _amount2(fields.get("total_amount"))
        order_no = str(fields.get("out_trade_no") or "").strip()
        if amount is None or not order_no:
            return None
        return order_no, float(amount), str(fields.get("trade_no") or "")[:128]

    def refund(self, order_no, amount_yuan, total_yuan=None, reason=""):
        biz = json.dumps({
            "out_trade_no": str(order_no),
            "refund_amount": "%.2f" % float(amount_yuan),
            "refund_reason": str(reason or "管理员退款")[:200],
        }, ensure_ascii=False, separators=(",", ":"))
        params = self._public_params("alipay.trade.refund", biz)
        try:
            self._sign_params(params)
            data = urllib.parse.urlencode(params).encode("utf-8")
            req = urllib.request.Request(
                self.settings.get("gateway") or "https://openapi.alipay.com/gateway.do",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8", "replace")
            body = (json.loads(raw) or {}).get("alipay_trade_refund_response") or {}
        except Exception as e:  # noqa: BLE001 — type only
            return False, "支付宝退款失败：%s" % type(e).__name__
        if str(body.get("code")) == "10000":
            return True, None
        return False, "支付宝退款失败：%s" % (body.get("sub_msg") or body.get("msg") or "未知错误")


# ---------------------------------------------------------------------------
# WeChat Pay — APIv3 native (QR), platform-cert-verified notify
# ---------------------------------------------------------------------------
class WechatPayProvider(PaymentProvider):
    name = "wechat"

    def __init__(self, settings=None, timeout=15):
        self.settings = settings or config.wechatpay_settings() or {}
        self.timeout = timeout

    def _auth_header(self, method, url_path, body=""):
        key = _load_private_key(self.settings.get("private_key"))
        if key is None:
            raise ValueError("WECHATPAY_PRIVATE_KEY missing/unparseable")
        ts = str(int(time.time()))
        nonce = uuid.uuid4().hex
        msg = "%s\n%s\n%s\n%s\n%s\n" % (method, url_path, ts, nonce, body)
        sig = _rsa_sign_b64(key, msg.encode("utf-8"))
        return ('WECHATPAY2-SHA256-RSA2048 mchid="%s",nonce_str="%s",timestamp="%s",'
                'serial_no="%s",signature="%s"'
                % (self.settings.get("mchid"), nonce, ts,
                   self.settings.get("serial_no"), sig))

    def _post_json(self, url_path, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        req = urllib.request.Request(
            (self.settings.get("api_base") or "https://api.mch.weixin.qq.com") + url_path,
            data=body.encode("utf-8"), method="POST",
            headers={
                "Authorization": self._auth_header("POST", url_path, body),
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "company-agent-demo/1.0",
            })
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8", "replace") or "{}")

    def create_order(self, amount_yuan, subject, order_no):
        s = self.settings
        if not (s.get("mchid") and s.get("appid")):
            return {"ok": False, "pay_url": None, "qr_text": None,
                    "error": "微信支付未配置"}
        payload = {
            "mchid": s["mchid"],
            "appid": s["appid"],
            "description": str(subject or "充值")[:127],
            "out_trade_no": str(order_no),
            "notify_url": config.app_base_url() + "/pay/notify/wechat",
            "amount": {"total": int(round(float(amount_yuan) * 100)), "currency": "CNY"},
        }
        try:
            data = self._post_json("/v3/pay/transactions/native", payload)
        except Exception as e:  # noqa: BLE001 — type only
            return {"ok": False, "pay_url": None, "qr_text": None,
                    "error": "微信支付下单失败：%s" % type(e).__name__}
        code_url = data.get("code_url")
        if not code_url:
            return {"ok": False, "pay_url": None, "qr_text": None,
                    "error": "微信支付下单失败：%s" % (data.get("message") or "未知错误")}
        return {"ok": True, "pay_url": None, "qr_text": code_url, "error": None}

    def _platform_public_key(self):
        from cryptography import x509
        pem = _ensure_pem(self.settings.get("platform_cert"), "CERTIFICATE")
        if not pem:
            return None
        try:
            return x509.load_pem_x509_certificate(pem.encode()).public_key()
        except Exception:  # noqa: BLE001
            return None

    def verify_notify(self, request):
        ts = request.headers.get("Wechatpay-Timestamp") or ""
        nonce = request.headers.get("Wechatpay-Nonce") or ""
        sign = request.headers.get("Wechatpay-Signature") or ""
        body = request.get_data(as_text=True) or ""
        if not (ts and nonce and sign and body):
            return None
        pub = self._platform_public_key()
        if pub is None:
            return None
        if not _rsa_verify(pub, sign, ("%s\n%s\n%s\n" % (ts, nonce, body)).encode("utf-8")):
            return None
        # Verified — decrypt the resource with the APIv3 key (AES-256-GCM).
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            data = json.loads(body)
            res = data.get("resource") or {}
            aes = AESGCM((self.settings.get("api_v3_key") or "").encode("utf-8"))
            plain = aes.decrypt(
                (res.get("nonce") or "").encode("utf-8"),
                base64.b64decode(res.get("ciphertext") or ""),
                (res.get("associated_data") or "").encode("utf-8") or None)
            obj = json.loads(plain.decode("utf-8"))
        except Exception:  # noqa: BLE001 — undecryptable == unverified
            return None
        if obj.get("trade_state") != "SUCCESS":
            return None
        order_no = str(obj.get("out_trade_no") or "").strip()
        fen = ((obj.get("amount") or {}).get("payer_total")
               or (obj.get("amount") or {}).get("total"))
        if not order_no or fen is None:
            return None
        amount = _amount2(Decimal(int(fen)) / 100)
        if amount is None:
            return None
        return order_no, float(amount), str(obj.get("transaction_id") or "")[:128]

    def refund(self, order_no, amount_yuan, total_yuan=None, reason=""):
        payload = {
            "out_trade_no": str(order_no),
            "out_refund_no": "R%s%s" % (order_no, uuid.uuid4().hex[:6]),
            "reason": str(reason or "管理员退款")[:80],
            "amount": {
                "refund": int(round(float(amount_yuan) * 100)),
                "total": int(round(float(total_yuan if total_yuan is not None
                                         else amount_yuan) * 100)),
                "currency": "CNY",
            },
        }
        try:
            data = self._post_json("/v3/refund/domestic/refunds", payload)
        except Exception as e:  # noqa: BLE001 — type only
            return False, "微信退款失败：%s" % type(e).__name__
        if data.get("status") in ("SUCCESS", "PROCESSING"):
            return True, None
        return False, "微信退款失败：%s" % (data.get("message") or data.get("status") or "未知错误")


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------
_PROVIDERS = {
    "alipay": AlipayProvider,
    "wechat": WechatPayProvider,
    "mock": MockProvider,
}


def available_providers():
    """The configured provider names (["mock"] only when nothing real is set)."""
    return config.payment_providers_available()


def get_provider(name=None):
    """The PaymentProvider for `name` (default: the first available). Returns
    None for an unknown or UNAVAILABLE name — notably "mock" the moment a real
    provider is configured, which kills the mock notify endpoint + cashier."""
    avail = available_providers()
    name = (name or "").strip().lower() or avail[0]
    if name not in avail or name not in _PROVIDERS:
        return None
    return _PROVIDERS[name]()


# ---------------------------------------------------------------------------
# Subscription billing — the daily tick (user-confirmed renewal, never auto-charge)
# ---------------------------------------------------------------------------
def daily_subscription_tick(now=None):
    """The daily billing pass (idempotent — safe to fire more than once a day).

    Per not-canceled subscription, by timestamps:
      1. inside `lead` days of period end (and not user-canceled): make sure ONE
         renewal order exists; on first creation, notify the user via email +
         WeChat webhook with a pay link.
      2. past period end + `grace` days: → past_due (features locked except
         data export — enforced by the plan gate in app.py).
      3. past_due for `cancel` more days, or user-canceled & period over:
         → canceled (back to the free plan).

    Returns a summary dict (lists of account_ids) for logs/tests."""
    now = now or datetime.utcnow()
    lead = timedelta(days=config.subscription_renewal_lead_days())
    grace = timedelta(days=config.subscription_grace_days())
    cancel_after = timedelta(days=config.subscription_past_due_cancel_days())
    summary = {"renewal_orders": [], "past_due": [], "canceled": []}

    for sub in metrics_store.list_subscriptions_for_tick():
        account_id = sub["account_id"]
        end = metrics_store._parse_dt(sub["current_period_end"])
        if end is None:
            continue
        try:
            if sub["cancel_at_period_end"]:
                if now >= end:
                    metrics_store.set_subscription_status(account_id, SUB_CANCELED)
                    summary["canceled"].append(account_id)
                continue
            if now >= end + grace + cancel_after:
                metrics_store.set_subscription_status(account_id, SUB_CANCELED)
                summary["canceled"].append(account_id)
                continue
            if now >= end + grace:
                if sub["status"] != SUB_PAST_DUE:
                    metrics_store.set_subscription_status(account_id, SUB_PAST_DUE)
                    summary["past_due"].append(account_id)
                # fall through: keep a payable renewal order alive while locked
            if now >= end - lead:
                order = _ensure_renewal_order(sub, now)
                if order:
                    summary["renewal_orders"].append(order["order_no"])
        except Exception:  # noqa: BLE001 — one bad subscription must not stop the pass
            log.exception("subscription tick failed for account %s", account_id)
    return summary


def _ensure_renewal_order(sub, now):
    """Create the period's renewal order ONCE (+ notify the user). Re-ticks see
    the still-open order via subscriptions.renewal_order_no and do nothing.
    Returns the new order dict, or None when one already exists / on failure."""
    existing_no = sub.get("renewal_order_no")
    if existing_no:
        existing = metrics_store.get_order(existing_no)
        if existing and existing["status"] == ORDER_CREATED:
            return None  # already issued + notified for this period
    plan = config.plan(sub["plan_code"])
    if plan["code"] not in config.PURCHASABLE_PLANS:
        return None
    order = metrics_store.create_order(
        sub["account_id"], ORDER_SUBSCRIPTION, plan["monthly_price_yuan"],
        provider=available_providers()[0], plan_code=plan["code"],
        subject="订阅续费 · %s（%d天）" % (plan["label"], config.SUBSCRIPTION_PERIOD_DAYS))
    metrics_store.set_renewal_order(sub["account_id"], order["order_no"])

    # Open the provider-side trade now so the notice carries a LIVE pay link;
    # if that fails, the dashboard's billing page can still re-open it.
    pay_link = config.app_base_url() + "/#unified"
    provider = get_provider()
    if provider:
        res = provider.create_order(order["amount_yuan"], order["subject"],
                                    order["order_no"])
        if res.get("ok") and res.get("pay_url"):
            url = res["pay_url"]
            pay_link = url if url.startswith("http") else config.app_base_url() + url

    _notify_renewal(sub, plan, order, pay_link)
    return order


def _notify_renewal(sub, plan, order, pay_link):
    """Email + WeChat-webhook the renewal notice (best-effort, never raises)."""
    contact = metrics_store.account_contact(sub["account_id"]) or {}
    body = (
        "%s 您好：\n\n您的「%s」订阅将于 %s 到期。\n"
        "请确认续费（¥%.2f / %d天）：\n%s\n\n"
        "到期后有 %d 天宽限期；逾期未续费将锁定功能（仅保留数据导出），"
        "续费成功后立即恢复。\n— %s"
        % (contact.get("name") or "客户", plan["label"],
           (sub.get("current_period_end") or "")[:10], order["amount_yuan"],
           config.SUBSCRIPTION_PERIOD_DAYS, pay_link,
           config.subscription_grace_days(), config.product_name()))
    try:
        if contact.get("email"):
            delivery.send_email(contact["email"], "订阅续费提醒", body)
    except Exception:  # noqa: BLE001
        pass
    try:
        company_id = metrics_store.company_id_for_email(contact.get("email"))
        webhook = (metrics_store.get_branding(company_id) or {}).get("wechat_webhook") \
            if company_id else None
        if webhook:
            delivery.send_wechat_webhook(
                webhook, "### 订阅续费提醒\n\n「%s」将于 **%s** 到期。\n"
                         "[点击续费（¥%.2f）](%s)"
                         % (plan["label"], (sub.get("current_period_end") or "")[:10],
                            order["amount_yuan"], pay_link))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Fapiao e-invoice stub (issuance is manual via 代记账 at first)
# ---------------------------------------------------------------------------
def issue_electronic_fapiao(fapiao_request):
    """TODO(Phase 10+): 电子发票 API integration.

    Stable interface for a later phase: given a fapiao_requests dict (title 抬头,
    tax_no 税号, amount, email), this will call an e-fapiao provider (百望/诺诺
    /航信), get back the PDF/OFD download URL, store it, and auto-mark the row
    issued. NOT yet implemented — issuance is manual via the bookkeeper working
    from /admin/fapiao + its CSV export; this returns a clear (False, reason) so
    any premature caller surfaces it instead of silently doing nothing."""
    return False, "电子发票 API 尚未接入（当前为人工开票流程）"

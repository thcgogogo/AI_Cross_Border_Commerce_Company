"""
sms.py — phone (SMS) verification code delivery (Phase 8).

China launch makes phone verification the norm for signup + password reset. We
deliver a 6-digit code through a small provider abstraction so the rest of the app
never cares HOW the code is sent:

    SmsProvider.send(phone, code) -> (ok: bool, error: str|None)

Two implementations:
  - AliyunSmsProvider — real delivery via Aliyun 短信服务 (Dysmsapi). Credentials
    come from the environment (see config.aliyun_sms_settings). The request is
    signed with the documented RPC HMAC-SHA1 scheme using only the stdlib, so no
    extra SDK dependency is required.
  - ConsoleSmsProvider — prints/logs the code instead of sending it. This is the
    ZERO-CONFIG dev/test default, so local `python app.py` and the test-suite can
    complete the signup flow without a real SMS account.

get_provider() picks Aliyun when its keys are present, else Console — matching
config.sms_provider_name().

SECURITY: the verification code is a short-lived secret. It is NEVER written to the
database in plaintext (callers store only a hash) and the Aliyun secret is never
logged. The Console provider does print the code — that is intentional and only
ever happens when no real provider is configured (i.e. dev/test).
"""

import base64
import hashlib
import hmac
import json
import logging
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from . import config

log = logging.getLogger("agent_team.sms")


class SmsProvider:
    """Interface: send one verification `code` to `phone`. Returns (ok, error)
    where error is a short, credential-free Chinese reason on failure."""

    name = "base"

    def send(self, phone, code):  # pragma: no cover - abstract
        raise NotImplementedError


class ConsoleSmsProvider(SmsProvider):
    """Dev/test provider: log the code instead of sending an SMS. Used whenever no
    real provider is configured, so the signup/reset flow is fully exercisable
    locally and in CI without an SMS account."""

    name = "console"

    def send(self, phone, code):
        # This print is the WHOLE point of the console provider (dev/test only).
        log.info("[SMS:console] code for %s is %s (no provider configured)", phone, code)
        print("[SMS] 验证码 for %s: %s" % (phone, code), flush=True)
        return True, None


class AliyunSmsProvider(SmsProvider):
    """Real delivery via Aliyun Dysmsapi (SendSms, API version 2017-05-25).

    Signs the request with the documented RPC scheme (HMAC-SHA1 over the sorted,
    percent-encoded params) using only the stdlib. The template is expected to take
    a single ``${code}`` parameter."""

    name = "aliyun"
    ENDPOINT = "https://dysmsapi.aliyuncs.com/"
    API_VERSION = "2017-05-25"

    def __init__(self, settings=None, timeout=10):
        self.settings = settings or config.aliyun_sms_settings()
        self.timeout = timeout

    @staticmethod
    def _percent_encode(value):
        """Aliyun's RPC percent-encoding: RFC 3986 but with +→%20, *→%2A, %7E→~."""
        enc = urllib.parse.quote(str(value), safe="")
        return enc.replace("+", "%20").replace("*", "%2A").replace("%7E", "~")

    def _signed_url(self, phone, code):
        s = self.settings
        params = {
            "AccessKeyId": s["access_key_id"],
            "Action": "SendSms",
            "Format": "JSON",
            "PhoneNumbers": phone,
            "RegionId": s.get("region") or "cn-hangzhou",
            "SignName": s["sign_name"],
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": uuid.uuid4().hex,
            "SignatureVersion": "1.0",
            "TemplateCode": s["template_code"],
            "TemplateParam": json.dumps({"code": code}, separators=(",", ":")),
            "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Version": self.API_VERSION,
        }
        canonical = "&".join(
            "%s=%s" % (self._percent_encode(k), self._percent_encode(params[k]))
            for k in sorted(params)
        )
        string_to_sign = "GET&%s&%s" % (
            self._percent_encode("/"), self._percent_encode(canonical))
        signing_key = (s["access_key_secret"] + "&").encode("utf-8")
        digest = hmac.new(signing_key, string_to_sign.encode("utf-8"),
                          hashlib.sha1).digest()
        params["Signature"] = base64.b64encode(digest).decode("ascii")
        return self.ENDPOINT + "?" + urllib.parse.urlencode(params)

    def send(self, phone, code):
        s = self.settings
        if not (s.get("access_key_id") and s.get("access_key_secret")
                and s.get("sign_name") and s.get("template_code")):
            return False, "阿里云短信未配置完整"
        try:
            url = self._signed_url(phone, code)
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8", "replace")
            data = json.loads(raw) if raw else {}
        except Exception as e:  # noqa: BLE001 — report type only, never creds/code
            return False, "短信发送失败：%s" % type(e).__name__
        if (data.get("Code") or "").upper() == "OK":
            return True, None
        # The Aliyun error code (e.g. "isv.BUSINESS_LIMIT_CONTROL") is safe to
        # surface; it contains no secret and helps ops diagnose delivery issues.
        return False, "短信发送失败：%s" % (data.get("Code") or "未知错误")


def get_provider():
    """Return the configured SmsProvider: Aliyun when its keys are present, else
    the Console provider (the dev/test default)."""
    if config.sms_provider_name() == "aliyun":
        return AliyunSmsProvider()
    return ConsoleSmsProvider()

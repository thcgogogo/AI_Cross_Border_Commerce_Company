"""
devices.py — per-device token accounting for the desktop "Tangent Agent" daemon.

Mirrors customers.py's secret discipline EXACTLY: a device pairs to an account via a
one-time pairing code, exchanges it for a per-device token (tng-dev-…) shown ONCE,
and from then on authenticates the daemon's cloud /v1 calls. We store ONLY the
SHA-256 hash of the token (and of the pairing code) — never the plaintext — and look
up by hash. Revoking a device flips its status to 'revoked': verify then fails closed
forever, an instant kill switch that needs no key rotation.

LIVE RESOLUTION, NEVER FROZEN: verify_token returns the device identity + account_id
ONLY. It deliberately does NOT embed a snapshot of the account's purchased models or
any ¥ figure — the downstream gateway (a later workstream) intersects can_use against
the account's LIVE model subset at request time, so a model bought/sold or a cap
changed after pairing takes effect immediately, without re-pairing.

Pure accounting: no LLM, no provider calls, no effective_model/provider_for — it
imports cleanly with no Anthropic key and on a fully-domestic cloud. Thread-safe (an
RLock around every state-changing mutation; the hot verify path stays lock-free like
customers.verify_key) and DB-backed (one short transaction per call). Every method
returns plain dict snapshots (never live ORM rows), so results are safe to hand across
the background job threads; NO secret hash is ever exposed in a record.
"""
import hashlib
import secrets
import threading
from datetime import datetime, timedelta

from . import db
from .models import DEVICE_ACTIVE, DEVICE_PENDING, DEVICE_REVOKED, Device

# Device tokens look like:  tng-dev-<43 url-safe chars>
DEVICE_TOKEN_PREFIX = "tng-dev-"
# A pairing code is short-lived: minted in the web app, exchanged by the daemon.
_PAIRING_TTL_SECS = 600   # 10 minutes


def _hash_key(raw):
    """SHA-256 hex digest — what we store instead of a plaintext token/code."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _utcnow():
    return datetime.utcnow()


def _iso(dt):
    return dt.isoformat() if dt else None


def _device_to_record(dev):
    """The verify-path snapshot: device identity + account_id, NO secrets and NO
    model/¥ snapshot (live resolution happens downstream). OMITS token_hash and
    pairing_code_hash exactly as customers._account_to_record omits key_hash."""
    if dev is None:
        return None
    return {
        "id": dev.id,
        "account_id": dev.account_id,
        "name": dev.name,
        "platform": dev.platform,
        "status": dev.status,
        "last_seen_at": _iso(dev.last_seen_at),
        "created_at": _iso(dev.created_at),
    }


def _device_public_view(dev):
    """The list/revoke view for the device-management UI. Strictly field-stripped
    (mirrors app._account_view): id/name/platform/status + timestamps only — never a
    token, hash, ¥, or model id."""
    if dev is None:
        return None
    return {
        "id": dev.id,
        "name": dev.name,
        "platform": dev.platform,
        "status": dev.status,
        "last_seen_at": _iso(dev.last_seen_at),
        "created_at": _iso(dev.created_at),
    }


class DeviceTokenStore:
    """DB-backed per-device token store. Thread-safe (RLock around mutations + one DB
    transaction per call); no LLM, no provider calls."""

    def __init__(self):
        self._lock = threading.RLock()

    # ---- pairing: mint a one-time code, then exchange it for a token --------
    def create_pairing_code(self, account_id, name=None, platform=None):
        """Mint a one-time pairing code + a pending device row. The raw code is a
        url-safe token (token_urlsafe(6) → ~8 chars), shown ONCE in the web app; only
        its hash is stored. Returns {pairing_code, device_id, account_id, expires_at}."""
        code = secrets.token_urlsafe(6)
        device_id = "dev_" + secrets.token_hex(6)
        expires_at = _utcnow() + timedelta(seconds=_PAIRING_TTL_SECS)
        with self._lock:
            with db.session_scope() as s:
                s.add(Device(
                    id=device_id, account_id=account_id, name=name, platform=platform,
                    pairing_code_hash=_hash_key(code), pairing_expires_at=expires_at,
                    status=DEVICE_PENDING, created_at=_utcnow()))
        return {
            "pairing_code": code,            # shown ONCE — never persisted in plaintext
            "device_id": device_id,
            "account_id": account_id,
            "expires_at": _iso(expires_at),
        }

    def exchange_pairing_code(self, raw_code, device_name=None, platform=None):
        """Exchange a valid, unexpired, un-used pairing code for a device token.
        Returns {device_token, device_id, account_id} ONCE, or None if the code is
        unknown / expired / already exchanged (all fail identically). Burns the
        pairing material under a row lock so a replayed code can never mint a SECOND
        token, even under concurrency."""
        if not raw_code:
            return None
        code_hash = _hash_key(raw_code.strip())
        with self._lock:
            with db.session_scope() as s:
                dev = (s.query(Device)
                       .filter(Device.pairing_code_hash == code_hash,
                               Device.status == DEVICE_PENDING)
                       .with_for_update().one_or_none())
                if dev is None:
                    return None
                if not dev.pairing_expires_at or dev.pairing_expires_at <= _utcnow():
                    return None
                raw_token = DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)
                dev.token_hash = _hash_key(raw_token)
                dev.status = DEVICE_ACTIVE
                dev.pairing_code_hash = None       # BURN — a replayed code can't re-mint
                dev.pairing_expires_at = None
                if device_name:
                    dev.name = device_name
                if platform:
                    dev.platform = platform
                return {
                    "device_token": raw_token,     # shown ONCE
                    "device_id": dev.id,
                    "account_id": dev.account_id,
                }

    # ---- the hot path: authenticate a device token -------------------------
    def verify_token(self, raw_token):
        """Return the device record for a raw token, or None. Looks up by hash; only
        an ACTIVE device verifies (pending/revoked fail closed). Bumps last_seen_at
        (best-effort). The record carries device identity + account_id — NEVER a
        snapshot of the account's models/¥; the gateway resolves can_use against the
        LIVE account downstream at request time. Lock-free, like customers.verify_key."""
        if not raw_token:
            return None
        token_hash = _hash_key(raw_token.strip())
        with db.session_scope() as s:
            dev = (s.query(Device)
                   .filter(Device.token_hash == token_hash,
                           Device.status == DEVICE_ACTIVE).one_or_none())
            if dev is None:
                return None
            dev.last_seen_at = _utcnow()           # best-effort; harmless if it races
            return _device_to_record(dev)

    # ---- management views --------------------------------------------------
    def list_for_account(self, account_id):
        """Field-stripped public views of this account's devices, newest first."""
        if not account_id:
            return []
        with db.session_scope() as s:
            rows = (s.query(Device)
                    .filter(Device.account_id == account_id)
                    .order_by(Device.created_at.desc()).all())
            return [_device_public_view(d) for d in rows]

    def revoke(self, device_id, account_id=None):
        """Flip a device to 'revoked' (instant kill switch). When account_id is given,
        the update is scoped to that account so one account can never revoke another's
        device. Returns the updated public view, or None if no scoped row matched.
        After this, verify_token fails closed for the device forever."""
        if not device_id:
            return None
        with self._lock:
            with db.session_scope() as s:
                q = s.query(Device).filter(Device.id == device_id)
                if account_id is not None:
                    q = q.filter(Device.account_id == account_id)
                dev = q.with_for_update().one_or_none()
                if dev is None:
                    return None
                dev.status = DEVICE_REVOKED
                return _device_public_view(dev)

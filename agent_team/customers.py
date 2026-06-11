"""
customers.py — The unified-API "storefront" + gateway accounting.

This powers the OpenRouter-style business: a customer picks the models they want
in the store, gets ONE gateway API key, and can then call any of those models
through our /v1/chat/completions endpoint. This module is the deterministic
book-keeper behind that — the three layers from the design:

  LAYER 1  AUTH        verify_key(raw_key) -> the customer record, or None.
                       (The gateway checks the request's Bearer token here.)
  LAYER 2  BILLING     record_usage(...) charges the customer in ¥ (each model's
                       per-token US$ price converted at config.USD_TO_CNY),
                       decrements their balance, and keeps a usage log.
                       has_balance() gates calls; top_up() adds credit.
  LAYER 3  ACCESS      can_use(customer, model) -> only the models they bought.

All money — balances, spend, per-call cost — is in Chinese Yuan (¥).

STORAGE (Phase 7): accounts live in the `accounts` SQL table (db.py / models.py),
NOT a JSON file. That makes balance changes safe under concurrency: every mutation
runs inside a transaction that takes a row lock (SELECT … FOR UPDATE on Postgres)
AND an in-process lock, then clamps the balance so it can NEVER go negative even if
two requests spend at the same instant. An existing memory_store/customers.json from
an older release is imported once (import_legacy_json) — see scripts/migrate_json_accounts.py.

Like the Token Manager, this is plain accounting (no LLM) and thread-safe. Every
public method returns plain dict snapshots (never live ORM rows), so results are
safe to hand across the background job threads.

SECURITY: a customer's API key is NEVER stored in plaintext and NEVER returned
after creation. We keep only a SHA-256 hash plus a short display prefix and the
last 4 characters (enough to recognize a key, useless for impersonation). The raw
key is returned exactly once, by create_customer(), for the user to copy. The
password is stored ONLY as a salted hash. Neither hash is exposed in any snapshot.
"""

import hashlib
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy.exc import IntegrityError

from . import config, db, metrics_store, security
from .models import ACCOUNT_ACTIVE, Account

# Customer keys look like:  tng-sk-<43 url-safe chars>
KEY_PREFIX = "tng-sk-"
_USAGE_LOG_CAP = 50  # keep only the most recent N calls per customer
# ¥ of demo credit a new account starts with (config is the single source of truth).
_DEFAULT_START_BALANCE = config.START_BALANCE_CNY
CURRENCY = "CNY"

# Balances/spend are stored as NUMERIC(12,2): money is rounded to the fen (2 dp),
# which is exactly how every ¥ figure is shown to customers, so this is purely an
# internal storage choice — no user-visible change.
_CENT = Decimal("0.01")
_ZERO = Decimal("0.00")


class CustomerStore:
    """DB-backed account store. `store_path` is retained for compatibility: it is
    the path of an older JSON store that import_legacy_json() can absorb once."""

    def __init__(self, store_path=None):
        # In-process guard. Combined with the per-row DB lock in record_usage/top_up
        # this serialises balance changes both within one process (SQLite/dev, and
        # multiple threads in one gunicorn worker) and across processes (Postgres).
        self._lock = threading.RLock()
        self.store_path = store_path

    # ---- LAYER 1: issue + authenticate keys ------------------------------
    def create_customer(self, models, name=None, starting_balance=_DEFAULT_START_BALANCE,
                        email=None, password=None, company=None, phone=None):
        """Create a customer account with a unique gateway key.

        `models` is a list of model ids (unknown ids are dropped) and MAY be
        empty — accounts are created first and pick their models afterwards on
        the dashboard (use set_models). Returns {"api_key": <raw key, shown
        ONCE>, "customer": <public view>}.

        Accounts are created from the sign-up page with `email` + `password`:
        the email is normalized + must be unique, and the password is stored
        ONLY as a bcrypt hash (never plaintext). `phone` is the SMS-verified
        number captured at signup (Phase 8). Raises ValueError if the email
        is already taken."""
        picked = _clean_models(models)

        email_norm = (email or "").strip().lower() or None
        phone_norm = (phone or "").strip() or None
        # Hash OUTSIDE the lock — key-stretching is deliberately slow.
        password_hash = security.hash_password(password) if password else None

        raw_key = KEY_PREFIX + secrets.token_urlsafe(32)
        balance = _money(starting_balance, default=_DEFAULT_START_BALANCE)
        acc_id = "cus_" + secrets.token_hex(6)

        try:
            with self._lock:
                with db.session_scope() as s:
                    if email_norm and s.query(Account.id).filter(
                            Account.email == email_norm).first():
                        raise ValueError("An account with that email already exists.")
                    s.add(Account(
                        id=acc_id,
                        name=(name or "").strip() or "Customer",
                        email=email_norm,
                        phone=phone_norm,
                        company=(company or "").strip() or None,
                        password_hash=password_hash,  # bcrypt hash only — never plaintext
                        key_hash=_hash_key(raw_key),
                        key_prefix=raw_key[:len(KEY_PREFIX) + 4],  # e.g. "tng-sk-Ab3D"
                        key_last4=raw_key[-4:],
                        models_json=json.dumps(picked),
                        balance_yuan=balance,
                        spent_yuan=_ZERO,
                        requests=0,
                        usage_json="[]",
                        status=ACCOUNT_ACTIVE,
                    ))
        except IntegrityError:
            # A concurrent signup won the unique-email race between our check and commit.
            raise ValueError("An account with that email already exists.")

        record = self.get_by_id(acc_id)
        return {"api_key": raw_key, "customer": self.public_view(record)}

    def verify_key(self, raw_key):
        """LAYER 1 — return the customer record for a raw key, or None.

        We store only the SHA-256 hash, so the plaintext key is never on disk; the
        lookup is by hash (the 256-bit digest of a 32-byte random key leaks nothing)."""
        if not raw_key:
            return None
        candidate = _hash_key(raw_key.strip())
        with db.session_scope() as s:
            acc = s.query(Account).filter(Account.key_hash == candidate).one_or_none()
            return _account_to_record(acc)

    def find_by_email(self, email):
        """Return the customer record for an email (case-insensitive), or None."""
        norm = (email or "").strip().lower()
        if not norm:
            return None
        with db.session_scope() as s:
            acc = s.query(Account).filter(Account.email == norm).one_or_none()
            return _account_to_record(acc)

    def find_by_phone(self, phone):
        """Return the customer record for a phone number, or None. Used by the
        SMS password-reset flow to resolve a verified phone to its account."""
        norm = (phone or "").strip()
        if not norm:
            return None
        with db.session_scope() as s:
            acc = s.query(Account).filter(Account.phone == norm).one_or_none()
            return _account_to_record(acc)

    def verify_login(self, email, password):
        """Website login — return the customer for a valid email+password, else None.

        The plaintext password is compared only against the stored hash (never
        persisted). verify_password accepts both bcrypt and legacy werkzeug
        hashes; on a successful login against a legacy hash we transparently
        re-hash with bcrypt (a silent, no-friction migration — Phase 8)."""
        if not email or not password:
            return None
        norm = email.strip().lower()
        with db.session_scope() as s:
            acc = s.query(Account).filter(Account.email == norm).one_or_none()
            if not (acc and acc.password_hash
                    and security.verify_password(password, acc.password_hash)):
                return None
            if security.needs_rehash(acc.password_hash):
                acc.password_hash = security.hash_password(password)
            return _account_to_record(acc)

    def set_password(self, customer_id_or_email, new_password):
        """Set a new password (bcrypt-hashed) for an account, resolved by id first
        then by email. Used by the SMS/email password-reset flow AFTER the code has
        been verified. Returns the updated record, or None if no such account.
        Raises ValueError on an empty password."""
        if not new_password:
            raise ValueError("Password must not be empty.")
        ident = (customer_id_or_email or "").strip()
        if not ident:
            return None
        # Hash OUTSIDE the lock — key-stretching is deliberately slow.
        new_hash = security.hash_password(new_password)
        with self._lock:
            with db.session_scope() as s:
                acc = s.get(Account, ident)
                if acc is None:
                    acc = s.query(Account).filter(
                        Account.email == ident.lower()).one_or_none()
                if acc is None:
                    return None
                acc.password_hash = new_hash
                cid = acc.id
        return self.get_by_id(cid)

    def current_session_token(self, customer_id):
        """Return the account's session token, minting+persisting one if it has
        none yet (legacy rows). Stamped into the signed cookie on login so a later
        rotate_session_token() can invalidate every outstanding cookie. None if the
        account is unknown."""
        with self._lock:
            with db.session_scope() as s:
                acc = s.get(Account, customer_id)
                if not acc:
                    return None
                if not acc.session_token:
                    acc.session_token = secrets.token_hex(16)
                return acc.session_token

    def rotate_session_token(self, customer_id):
        """"Log out everywhere": set a fresh session token, invalidating every
        existing signed cookie for this account. Returns the new token, or None if
        the account is unknown."""
        with self._lock:
            with db.session_scope() as s:
                acc = s.get(Account, customer_id)
                if not acc:
                    return None
                acc.session_token = secrets.token_hex(16)
                return acc.session_token

    def get_by_id(self, customer_id):
        """Return the customer record for an id (used to rebuild a logged-in session)."""
        if not customer_id:
            return None
        with db.session_scope() as s:
            return _account_to_record(s.get(Account, customer_id))

    def set_models(self, customer_id, models):
        """Replace the set of models a customer's key may call (the dashboard's
        model picker). Unknown ids are dropped; order/de-dupe preserved. The key
        itself is unchanged — it simply unlocks the new selection. Returns the
        updated record, or None if the customer id is unknown."""
        picked = _clean_models(models)
        with self._lock:
            with db.session_scope() as s:
                acc = s.get(Account, customer_id)
                if not acc:
                    return None
                acc.models_json = json.dumps(picked)
        return self.get_by_id(customer_id)

    # ---- LAYER 3: access control -----------------------------------------
    def can_use(self, customer, model):
        """LAYER 3 — True only if this customer purchased access to `model`."""
        return bool(customer) and model in (customer.get("models") or [])

    # ---- LAYER 2: billing + balance --------------------------------------
    def has_balance(self, customer):
        """LAYER 2 — True if the account still has credit (None balance = unlimited)."""
        if not customer:
            return False
        bal = customer.get("balance")
        return bal is None or bal > 0

    def record_usage(self, customer_id, model, input_tokens, output_tokens,
                     should_charge_balance=True):
        """LAYER 2 — charge a call to the customer and log it.

        Cost comes from the catalog's per-million pricing. Decrements balance,
        bumps lifetime spend + request count, and appends a capped usage entry.
        Returns {"cost", "balance", "input_tokens", "output_tokens"}.

        B8 (redesign Phase 7): `should_charge_balance=False` is the flat-plan
        subscriber path — the ¥ balance is left UNTOUCHED (a ¥199/月 customer
        is gated by plan quota, never by the invisible demo credit), while
        everything that meters COGS still happens exactly as before:
        spent_yuan, the request count, the usage log, and platform_spend.
        The returned "balance" is then the current (undecremented) value.

        CONCURRENCY: the whole read-modify-write runs under the in-process lock AND
        a row lock (with_for_update → real SELECT … FOR UPDATE on Postgres), and the
        balance is clamped at 0. So two parallel calls can never drive it negative:
        the second sees the first's committed balance and stops at zero."""
        in_tok = int(input_tokens or 0)
        out_tok = int(output_tokens or 0)
        cost = _cost_for(model, in_tok, out_tok)       # precise ¥ (what callers report)
        cost_dec = _money(cost, default=0.0)           # what we actually debit, to the fen

        # Phase 8: meter what this upstream call cost us (¥) for the platform-spend
        # circuit breaker. Best-effort, on its own transaction — billing must never
        # fail because the meter hiccuped.
        provider = (config.get_model(model) or {}).get("provider") or "unknown"
        metrics_store.record_platform_spend(provider, float(cost_dec),
                                            account_id=customer_id)

        with self._lock:
            with db.session_scope() as s:
                acc = (s.query(Account).filter(Account.id == customer_id)
                       .with_for_update().one_or_none())
                if not acc:
                    return {"cost": cost, "balance": None,
                            "input_tokens": in_tok, "output_tokens": out_tok}

                acc.spent_yuan = _q((acc.spent_yuan or _ZERO) + cost_dec)
                acc.requests = int(acc.requests or 0) + 1

                new_balance = None
                if acc.balance_yuan is not None:
                    if should_charge_balance:
                        nb = _q(acc.balance_yuan - cost_dec)
                        if nb < _ZERO:
                            nb = _ZERO                 # NEVER negative
                        acc.balance_yuan = nb
                        new_balance = float(nb)
                    else:
                        new_balance = float(_q(acc.balance_yuan))

                log = _json_list(acc.usage_json)
                log.append({"at": _now(), "model": model, "input_tokens": in_tok,
                            "output_tokens": out_tok, "cost": float(cost_dec)})
                if len(log) > _USAGE_LOG_CAP:
                    log = log[-_USAGE_LOG_CAP:]        # keep only the most recent N
                acc.usage_json = json.dumps(log, ensure_ascii=False)

        return {"cost": cost, "balance": new_balance,
                "input_tokens": in_tok, "output_tokens": out_tok}

    def top_up(self, customer_id, amount):
        """Add ¥ credit to an account's balance. Returns the updated record, or
        None if the customer id is unknown. Raises ValueError on a bad amount.

        Phase 9: customer-facing top-ups now go through a PAID ORDER (the credit
        lands inside metrics_store.mark_order_paid's transaction). This method
        remains the low-level wallet primitive for tests/ops adjustments."""
        try:
            amt = _to_decimal(amount)
        except (TypeError, ValueError, InvalidOperation):
            raise ValueError("Top-up amount must be a number.")
        amt = _q(amt)
        if amt <= _ZERO:
            raise ValueError("Top-up amount must be greater than zero.")
        with self._lock:
            with db.session_scope() as s:
                acc = (s.query(Account).filter(Account.id == customer_id)
                       .with_for_update().one_or_none())
                if not acc:
                    return None
                base = acc.balance_yuan if acc.balance_yuan is not None else _ZERO
                acc.balance_yuan = _q(base + amt)
        return self.get_by_id(customer_id)

    def deduct_balance(self, customer_id, amount):
        """Remove ¥ from an account's balance — the admin-refund path (Phase 9).

        Same locking discipline as top_up/record_usage. Raises ValueError with a
        clear Chinese message when the remaining balance is below `amount` (i.e.
        the credit being refunded has already been spent) so the caller REFUSES
        the refund instead of driving the balance negative. An unlimited (None)
        balance is a no-op. Returns the updated record, or None if unknown."""
        try:
            amt = _q(_to_decimal(amount))
        except (TypeError, ValueError, InvalidOperation):
            raise ValueError("Deduct amount must be a number.")
        if amt <= _ZERO:
            raise ValueError("Deduct amount must be greater than zero.")
        with self._lock:
            with db.session_scope() as s:
                acc = (s.query(Account).filter(Account.id == customer_id)
                       .with_for_update().one_or_none())
                if not acc:
                    return None
                if acc.balance_yuan is not None:
                    bal = _q(acc.balance_yuan)
                    if bal < amt:
                        raise ValueError(
                            "余额不足，无法退款：该笔充值已被消费（当前余额 ¥%s，"
                            "申请退款 ¥%s）。" % (bal, amt))
                    acc.balance_yuan = _q(bal - amt)
        return self.get_by_id(customer_id)

    # ---- reads ------------------------------------------------------------
    def public_view(self, customer):
        """A safe snapshot for the dashboard — NEVER includes the key hash.

        Enriches each chosen model with its catalog details (label/pricing/etc.)
        so the dashboard can render the customer's 'shelf' of models directly."""
        if not customer:
            return None
        models = []
        for mid in customer.get("models") or []:
            row = config.get_model(mid)
            if row:
                kind = row.get("kind", "text")
                models.append({
                    "id": row["id"], "label": row["label"], "provider": row["provider"],
                    "tier": row.get("tier"), "context": row.get("context"),
                    "kind": kind, "kind_label": config.kind_label(kind),
                    "input_per_mtok": row.get("input_per_mtok"),
                    "output_per_mtok": row.get("output_per_mtok"),
                    "price": row.get("price"), "price_unit": row.get("price_unit"),
                    "approx": bool(row.get("approx")),
                })
            else:
                models.append({"id": mid, "label": mid, "provider": "unknown"})
        return {
            "id": customer.get("id"),
            "name": customer.get("name"),
            "email": customer.get("email"),
            "company": customer.get("company"),
            "key_prefix": customer.get("key_prefix"),
            "key_last4": customer.get("key_last4"),
            "models": models,
            "balance": customer.get("balance"),
            "spent": round(float(customer.get("spent", 0.0)), 6),
            "requests": int(customer.get("requests", 0)),
            "report_credits": int(customer.get("report_credits", 0) or 0),
            "created_at": customer.get("created_at"),
            "usage": list(reversed(customer.get("usage") or []))[:_USAGE_LOG_CAP],
        }

    # ---- one-time legacy JSON → DB import --------------------------------
    def import_legacy_json(self, store_path=None):
        """Absorb accounts from an older memory_store/customers.json into the DB.

        Idempotent: an account already present (by id, else by email) is skipped, so
        this is safe to run on every boot and explicitly via the migration script.
        Returns the number of NEW accounts created. Best-effort: a missing/garbled
        file just yields 0."""
        path = store_path or self.store_path
        if not path or not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError):
            return 0

        accounts = (data or {}).get("customers") or []
        created = 0
        with self._lock:
            with db.session_scope() as s:
                for rec in accounts:
                    cid = (rec.get("id") or "").strip() or None
                    email = (rec.get("email") or "").strip().lower() or None
                    if cid and s.get(Account, cid):
                        continue
                    if email and s.query(Account.id).filter(
                            Account.email == email).first():
                        continue
                    bal = rec.get("balance")
                    s.add(Account(
                        id=cid or ("cus_" + secrets.token_hex(6)),
                        name=(rec.get("name") or "").strip() or "Customer",
                        email=email,
                        company=(rec.get("company") or "").strip() or None,
                        password_hash=rec.get("password_hash"),
                        key_hash=rec.get("key_hash"),
                        key_prefix=rec.get("key_prefix"),
                        key_last4=rec.get("key_last4"),
                        models_json=json.dumps(rec.get("models") or []),
                        balance_yuan=(_money(bal) if bal is not None else None),
                        spent_yuan=_money(rec.get("spent") or 0, default=0.0),
                        requests=int(rec.get("requests") or 0),
                        usage_json=json.dumps((rec.get("usage") or [])[-_USAGE_LOG_CAP:],
                                              ensure_ascii=False),
                        status=ACCOUNT_ACTIVE,
                        created_at=_parse_iso(rec.get("created_at")) or datetime.utcnow(),
                    ))
                    created += 1
        return created


# ---------------------------------------------------------------------------
def _account_to_record(acc):
    """A plain dict snapshot of an Account row, in the exact shape the rest of the
    app expects. Deliberately OMITS password_hash and key_hash so no secret ever
    leaves this module. Decimal money is returned as float (jsonify-safe)."""
    if acc is None:
        return None
    bal = acc.balance_yuan
    return {
        "id": acc.id,
        "name": acc.name or "Customer",
        "email": acc.email,
        "company": acc.company,
        "phone": acc.phone,
        "key_prefix": acc.key_prefix,
        "key_last4": acc.key_last4,
        "models": _json_list(acc.models_json),
        "balance": float(bal) if bal is not None else None,
        "spent": float(acc.spent_yuan or 0),
        "requests": int(acc.requests or 0),
        # Unconsumed 单次报告 ¥29 purchases (redesign B4) — a quota concept,
        # customer-visible, unlike the ¥ figures above.
        "report_credits": int(acc.report_credits or 0),
        "status": acc.status,
        "rpm_limit": int(acc.rpm_limit) if acc.rpm_limit is not None else None,
        "daily_cap_yuan": float(acc.daily_cap_yuan) if acc.daily_cap_yuan is not None else None,
        # Internal session validator (never surfaced by public_view → never sent to
        # a client); _current_customer compares it to the cookie's "sid".
        "session_token": acc.session_token,
        "created_at": acc.created_at.isoformat() if acc.created_at else None,
        "usage": _json_list(acc.usage_json),
    }


def _clean_models(models):
    """Keep only ids in the catalog, de-duped, preserving the order picked."""
    seen, picked = set(), []
    for m in models or []:
        if m not in seen and config.get_model(m):
            seen.add(m)
            picked.append(m)
    return picked


def _json_list(raw):
    """Parse a JSON list column into a Python list (empty list on anything odd)."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return val if isinstance(val, list) else []


def _hash_key(raw_key):
    """SHA-256 hex digest of a key — what we store instead of the plaintext."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _cost_for(model_id, input_tokens, output_tokens):
    """¥ cost of a call — the catalog's per-million-token US$ price, in Yuan."""
    return config.cost_cny(model_id, input_tokens, output_tokens)


def _to_decimal(x):
    """A Decimal from a number/str. Raises (ValueError/InvalidOperation/TypeError)
    on junk — callers that want a fallback use _money()."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def _q(d):
    """Quantize a Decimal to the fen (2 dp), rounding half-up."""
    return d.quantize(_CENT, rounding=ROUND_HALF_UP)


def _money(x, default=None):
    """A fen-quantized Decimal for `x`; on bad input fall back to `default`
    (also quantized), or re-raise if no default was given."""
    try:
        return _q(_to_decimal(x))
    except (TypeError, ValueError, InvalidOperation):
        if default is None:
            raise
        return _q(_to_decimal(default))


def _parse_iso(s):
    """A datetime from an ISO string (naive, UTC-ish), or None if unparseable."""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    # Store naive UTC to match the other models' datetime.utcnow() columns.
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _now():
    return datetime.now(timezone.utc).isoformat()

"""
security.py — password hashing, upload magic-byte sniffing, secret scanning (Phase 8).

Three independent helpers:

PASSWORDS — hash_password / verify_password / needs_rehash.
    New passwords are hashed with bcrypt. Legacy accounts created before Phase 8
    carry a werkzeug hash (``pbkdf2:sha256:…`` / ``scrypt:…``); verify_password
    transparently accepts BOTH, and needs_rehash() returns True for a legacy hash
    so customers.verify_login can re-hash it with bcrypt on the next successful
    login (a silent, no-friction migration). bcrypt caps the input at 72 bytes, so
    we pre-hash with SHA-256+base64 first (the standard BCryptSHA256 technique) —
    long passphrases are fully mixed in instead of being truncated.

UPLOADS — looks_executable(head_bytes).
    A cheap magic-byte sniff to reject executable uploads (ELF / Mach-O / PE / Java
    class / shebang scripts). The run upload path reads the first few bytes and
    refuses anything that looks like a program, regardless of filename/extension.

SECRETS — scan_files_for_secrets(paths, secrets).
    A boot-time guard: assert that no real secret VALUE (API key, SECRET_KEY,
    SMTP password, webhook URL) has leaked into a shipped template/static file. It
    returns the offending (path, label) pairs WITHOUT ever echoing the secret value
    itself, so the failure is actionable but the log stays clean.
"""

import base64
import hashlib
import logging

import bcrypt
from werkzeug.security import check_password_hash

log = logging.getLogger("agent_team.security")

# A bcrypt hash always starts with one of these version tags.
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")

# Leading bytes that mark an executable / loadable binary or a script. Filenames
# lie, magic bytes don't — so we sniff content, not the extension.
_EXECUTABLE_MAGIC = (
    b"\x7fELF",          # Linux/Unix ELF executable or shared object
    b"MZ",               # DOS/Windows PE (.exe/.dll)
    b"\xfe\xed\xfa\xce",  # Mach-O 32-bit (big/little variants below)
    b"\xfe\xed\xfa\xcf",  # Mach-O 64-bit
    b"\xce\xfa\xed\xfe",  # Mach-O 32-bit byte-swapped
    b"\xcf\xfa\xed\xfe",  # Mach-O 64-bit byte-swapped
    b"\xca\xfe\xba\xbe",  # Mach-O universal ("fat") binary AND Java .class
    b"\xca\xfe\xd0\x0d",  # Java pack200
    b"dex\n",            # Android Dalvik executable
    b"#!",               # shebang script (#!/bin/sh, #!/usr/bin/env python, …)
)

_SECRET_SCAN_MIN_LEN = 8          # ignore very short values (noise / placeholders)
_SECRET_SCAN_MAX_BYTES = 4 * 1024 * 1024  # cap per-file read while scanning


# ---- passwords ------------------------------------------------------------
def _prehash(plain):
    """SHA-256 then base64 → a fixed 44-byte token, so bcrypt's 72-byte limit can
    never silently truncate a long passphrase. Hash and verify must agree on this."""
    digest = hashlib.sha256((plain or "").encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(plain):
    """Return a bcrypt hash string for `plain` (suitable for accounts.password_hash)."""
    return bcrypt.hashpw(_prehash(plain), bcrypt.gensalt()).decode("ascii")


def _is_bcrypt(stored_hash):
    return bool(stored_hash) and stored_hash.startswith(_BCRYPT_PREFIXES)


def verify_password(plain, stored_hash):
    """True if `plain` matches `stored_hash`. Accepts a bcrypt hash (Phase 8+) OR a
    legacy werkzeug hash, so existing accounts keep working through the migration."""
    if not stored_hash:
        return False
    if _is_bcrypt(stored_hash):
        try:
            return bcrypt.checkpw(_prehash(plain), stored_hash.encode("ascii"))
        except (ValueError, TypeError):
            return False
    # Legacy werkzeug hash (pbkdf2/scrypt). check_password_hash is exception-safe.
    try:
        return check_password_hash(stored_hash, plain or "")
    except Exception:  # noqa: BLE001 — a malformed legacy hash must just fail closed
        return False


def needs_rehash(stored_hash):
    """True when `stored_hash` is NOT bcrypt — i.e. a legacy werkzeug hash that
    should be upgraded to bcrypt the next time the password is verified."""
    return bool(stored_hash) and not _is_bcrypt(stored_hash)


# ---- uploads --------------------------------------------------------------
def looks_executable(head_bytes):
    """True if these leading bytes look like an executable/loadable binary or a
    script with a shebang. Pass the first ~16 bytes of an uploaded file."""
    if not head_bytes:
        return False
    return any(head_bytes.startswith(magic) for magic in _EXECUTABLE_MAGIC)


# ---- secret scanning (boot check) -----------------------------------------
def _normalize_secrets(secrets):
    """Accept either a {label: value} mapping or an iterable of values; yield
    (label, value) pairs, skipping empties and values too short to scan safely."""
    items = secrets.items() if hasattr(secrets, "items") else enumerate(secrets)
    for label, value in items:
        if not value:
            continue
        sval = value if isinstance(value, str) else str(value)
        if len(sval) < _SECRET_SCAN_MIN_LEN:
            continue
        yield (str(label), sval)


def scan_files_for_secrets(paths, secrets):
    """Scan each file in `paths` for any of the secret VALUES in `secrets` (a
    {label: value} mapping or a list of values). Returns a list of hit dicts
    ``{"path": str, "label": str}`` — the secret value itself is deliberately
    NEVER included, so the result is safe to log. Empty list == clean."""
    wanted = list(_normalize_secrets(secrets))
    if not wanted:
        return []
    encoded = [(label, sval.encode("utf-8")) for label, sval in wanted]
    hits = []
    for path in paths:
        try:
            with open(path, "rb") as fh:
                blob = fh.read(_SECRET_SCAN_MAX_BYTES)
        except OSError:
            continue  # unreadable/missing file isn't a leak
        for label, needle in encoded:
            if needle in blob:
                hits.append({"path": path, "label": label})
    return hits

"""
crypto.py — encryption of sensitive files AT REST (Phase 8).

Uploaded files and generated report PDFs are encrypted on disk with Fernet
(AES-128-CBC + HMAC authentication) so a leaked disk / backup / stolen volume
never exposes a customer's raw spreadsheets or reports.

KEY ROTATION: ``FERNET_KEYS`` is a comma-separated list, NEWEST first. We build a
``MultiFernet``: new data is encrypted with the first key; existing data still
decrypts with any key in the list. To rotate, prepend a fresh key and keep the
old one around until everything has been re-written.

ZERO-CONFIG DEV: with no key set, encryption is OFF — every function is a
pass-through, so files stay plaintext and local `python app.py` needs no setup.
Turn it on by setting ``FERNET_KEYS`` (see .env.example). Generate a key with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

SECURITY: keys come from the environment only — never hard-coded, logged, or
written into the database. A decrypt of tampered/garbage bytes raises, so a
corrupted file can never be silently served as if it were the original.
"""

import contextlib
import os
import tempfile

from . import config

# A Fernet token always begins with version byte 0x80, which base64-url-encodes to
# a leading "gAAAAA". We use this only as a cheap hint for "is this already
# encrypted?"; the authoritative check is whether decrypt() succeeds.
_FERNET_MAGIC = b"gAAAAA"


def _multifernet():
    """Build a MultiFernet from FERNET_KEYS, or None when encryption is disabled.

    Rebuilt per call so rotating keys in .env applies without a restart; this is
    cheap (a few HMAC key schedules) relative to the file I/O around it."""
    keys = config.fernet_keys()
    if not keys:
        return None
    from cryptography.fernet import Fernet, MultiFernet
    return MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in keys])


def enabled():
    """True when at least one Fernet key is configured (encryption is ON)."""
    return bool(config.fernet_keys())


def encrypt_bytes(data):
    """Return the Fernet ciphertext for `data`, or `data` unchanged when disabled."""
    f = _multifernet()
    if f is None:
        return data
    return f.encrypt(data)


def decrypt_bytes(token):
    """Return the plaintext for a Fernet `token`. When encryption is disabled this
    is a pass-through. When enabled but the bytes aren't a valid token (e.g. a
    legacy plaintext file written before a key was set), fall back to returning
    them as-is so old files remain readable."""
    f = _multifernet()
    if f is None:
        return token
    from cryptography.fernet import InvalidToken
    try:
        return f.decrypt(token)
    except InvalidToken:
        return token  # pre-encryption legacy file, or already-plaintext input


def looks_encrypted(data):
    """Cheap heuristic: does this blob look like a Fernet token? Used to decide
    whether a download needs decrypting. Not a security boundary — decrypt_bytes
    is authoritative."""
    return bool(data) and data[:6] == _FERNET_MAGIC


# ---- string helpers (DB columns) ------------------------------------------
def encrypt_string(text):
    """Encrypt a unicode string for storage in a DB column, returning a unicode
    Fernet token (ascii). Pass-through (returns `text` unchanged) when encryption
    is disabled or `text` is None/empty — so a dev DB stays plaintext and readable,
    matching how files behave with no FERNET_KEYS set."""
    if text is None or text == "":
        return text
    f = _multifernet()
    if f is None:
        return text
    return f.encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_string(token):
    """Inverse of encrypt_string — return the plaintext unicode string. Pass-through
    when encryption is disabled; falls back to returning the input unchanged when it
    isn't a valid token (a legacy plaintext row written before a key was set)."""
    if token is None or token == "":
        return token
    f = _multifernet()
    if f is None:
        return token
    from cryptography.fernet import InvalidToken
    raw = token.encode("utf-8") if isinstance(token, str) else token
    try:
        return f.decrypt(raw).decode("utf-8")
    except InvalidToken:
        return token  # pre-encryption legacy row, or already-plaintext input


# ---- file helpers ---------------------------------------------------------
def encrypt_file_in_place(path):
    """Encrypt the file at `path`, overwriting it with ciphertext. No-op when
    encryption is disabled, or when the file is already a Fernet token (so calling
    this twice is safe / idempotent). The write is atomic (temp file + os.replace)
    so a crash mid-write can't leave a truncated, unrecoverable file."""
    if not enabled() or not os.path.isfile(path):
        return path
    with open(path, "rb") as fh:
        data = fh.read()
    if looks_encrypted(data):
        return path
    token = encrypt_bytes(data)
    tmp = path + ".enc.tmp"
    with open(tmp, "wb") as fh:
        fh.write(token)
    os.replace(tmp, path)
    return path


def read_plaintext(path):
    """Read a possibly-encrypted file and return its PLAINTEXT bytes."""
    with open(path, "rb") as fh:
        return decrypt_bytes(fh.read())


@contextlib.contextmanager
def materialize_plaintext(path):
    """Context manager yielding a filesystem path to the PLAINTEXT contents.

    If `path` is not encrypted (or encryption is off) it yields `path` itself with
    no copy. If it is encrypted, it decrypts to a private temp file, yields that
    path, and deletes it on exit. Use this where a real plaintext file is needed —
    e.g. emailing a report PDF as an attachment, or re-parsing a stored upload."""
    if not enabled() or not path or not os.path.isfile(path):
        yield path  # nothing to decrypt (disabled, or no/absent file) — pass through
        return
    with open(path, "rb") as fh:
        raw = fh.read()
    if not looks_encrypted(raw):
        yield path  # legacy plaintext on disk — no temp needed
        return
    plain = decrypt_bytes(raw)
    suffix = os.path.splitext(path)[1]
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="dec_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(plain)
        yield tmp
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)

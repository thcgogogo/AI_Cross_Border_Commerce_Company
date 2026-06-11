"""Phase 8 — security & abuse-hardening acceptance tests.

The five checks below mirror the Phase 8 spec's acceptance criteria, in order:

  1. Sign-up is impossible without a valid SMS code, and a code is invalidated after
     the max wrong attempts (the next correct guess is rejected).
  2. The 11th run-start within the hour is rejected with HTTP 429 + a Chinese message.
  3. Simulated upstream spend over the hourly cap trips the breaker (new runs refused
     with the verbatim 系统繁忙 message + a one-shot admin alert); an admin reset
     restores service.
  4. An uploaded file is ciphertext on disk (magic bytes differ from the plaintext);
     downloading it returns bytes with the ORIGINAL checksum.
  5. Audit rows are written for login + topup + download.

Isolation: each test gets a throwaway SQLite DB (the `fresh_db` fixture) and its own
CustomerStore wired into `app.CUSTOMERS`, and every env tweak goes through
`monkeypatch` so nothing leaks between tests. The suite-wide limiter stays OFF
(see conftest) except test 2, which re-enables it locally and resets it.
"""
import hashlib
import os
import sys

import pytest
from cryptography.fernet import Fernet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot is one-time & idempotent
from agent_team import config, crypto, db, metrics_store, platform_guard  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.models import (  # noqa: E402
    AUDIT_DOWNLOAD, AUDIT_LOGIN, AUDIT_TOPUP, CODE_SIGNUP)


@pytest.fixture
def fresh_db(tmp_path):
    """Point the company brain at a throwaway SQLite file for this one test, so the
    audit log, phone codes, and the persistent breaker latch all start empty."""
    db.init_db(str(tmp_path / "brain.db"))
    yield


def _store(tmp_path, **accounts):
    """A fresh CustomerStore wired into the app, pre-loaded with {email: password}."""
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    for email, password in accounts.items():
        store.create_customer([], name=email.split("@")[0], starting_balance=100.0,
                              email=email, password=password)
    app.CUSTOMERS = store
    return store


# ---------------------------------------------------------------------------
# (1) Sign-up requires a valid SMS code; the code burns after the attempt cap.
# ---------------------------------------------------------------------------
def test_signup_requires_sms_code_and_code_burns_after_max_attempts(
        fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("PHONE_VERIFICATION", "1")
    store = _store(tmp_path)
    client = app.app.test_client()
    phone = "13800000001"

    # No code at all → rejected, and NO account is created.
    r = client.post("/api/signup", json={
        "name": "Z", "email": "z@corp.com", "password": "password1", "phone": phone})
    assert r.status_code == 400
    assert store.find_by_email("z@corp.com") is None

    # Plant a known code (only its hash is persisted; plaintext never stored).
    metrics_store.issue_phone_code(phone, CODE_SIGNUP, "123456")

    # Wrong code → still rejected, still no account.
    r = client.post("/api/signup", json={
        "name": "Z", "email": "z@corp.com", "password": "password1",
        "phone": phone, "code": "000000"})
    assert r.status_code == 400
    assert store.find_by_email("z@corp.com") is None

    # Correct code → account created and the gateway key is returned exactly once.
    r = client.post("/api/signup", json={
        "name": "Z", "email": "z@corp.com", "password": "password1",
        "phone": phone, "code": "123456"})
    assert r.status_code == 200
    assert r.get_json().get("api_key")
    assert store.find_by_email("z@corp.com") is not None

    # A NEW code, then exhaust the attempt cap: each wrong guess fails, and the
    # cap-th wrong guess BURNS the code — so even the correct code is now rejected.
    phone2 = "13800000002"
    metrics_store.issue_phone_code(phone2, CODE_SIGNUP, "654321")
    for _ in range(config.SMS_MAX_VERIFY_ATTEMPTS):
        v = metrics_store.verify_phone_code(phone2, CODE_SIGNUP, "111111",
                                            config.SMS_MAX_VERIFY_ATTEMPTS)
        assert v["ok"] is False
    burned = metrics_store.verify_phone_code(phone2, CODE_SIGNUP, "654321",
                                             config.SMS_MAX_VERIFY_ATTEMPTS)
    assert burned["ok"] is False  # correct code, but the code was invalidated


# ---------------------------------------------------------------------------
# (2) The 11th run-start in an hour is rate-limited (429 + Chinese message).
# ---------------------------------------------------------------------------
def test_eleventh_run_start_per_hour_returns_429_chinese(
        fresh_db, tmp_path, monkeypatch):
    _store(tmp_path, **{"runner@corp.com": "password1"})

    # Re-enable the limiter (conftest builds it fully but flips it OFF suite-wide) and
    # clear its counters. monkeypatch reverts `enabled` to OFF for the later tests.
    monkeypatch.setattr(app.limiter, "enabled", True)
    app.limiter.reset()

    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = app.CUSTOMERS.find_by_email("runner@corp.com")["id"]

    # The first 10 starts pass the limiter (they 400 on the empty goal, but
    # flask-limiter counts the request regardless of the view's return).
    first_ten = [client.post("/api/run", data={"goal": ""}).status_code
                 for _ in range(10)]
    assert 429 not in first_ten

    r = client.post("/api/run", data={"goal": ""})
    assert r.status_code == 429
    assert r.get_json()["error"] == "请求过于频繁，请稍后再试"
    app.limiter.reset()


# ---------------------------------------------------------------------------
# (3) Platform spend over the hourly cap trips the breaker (+ alert); admin reset
#     restores service.
# ---------------------------------------------------------------------------
def test_platform_spend_breaker_trips_alerts_and_admin_reset_restores(
        fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_HOURLY_CAP_YUAN", "1")
    monkeypatch.setenv("PLATFORM_DAILY_CAP_YUAN", "0")
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")
    # Capture the admin alert instead of really sending email / hitting a webhook.
    fired = []
    monkeypatch.setattr(platform_guard.alerts, "send_admin_alert",
                        lambda subject, body: fired.append((subject, body)))

    store = _store(tmp_path, **{"spender@corp.com": "password1",
                                "founder@corp.com": "password1"})
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email("spender@corp.com")["id"]

    # Before any over-cap spend, a run-start passes the breaker (400 on empty goal).
    assert client.post("/api/run", data={"goal": ""}).status_code == 400

    # Simulate upstream spend OVER the ¥1 hourly cap.
    platform_guard.record_spend("anthropic", 5.0)

    # New runs are now refused with the verbatim Chinese busy message (503), the
    # breaker has latched tripped, and exactly one admin alert fired.
    r = client.post("/api/run", data={"goal": "做个经营周报"})
    assert r.status_code == 503
    assert r.get_json()["error"] == platform_guard.BUSY_MESSAGE
    assert platform_guard.status()["breaker"]["tripped"] is True
    assert len(fired) == 1

    # An admin resets the breaker via the founder-gated route → service resumes.
    admin = app.app.test_client()
    with admin.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email("founder@corp.com")["id"]
    assert admin.post("/admin/circuit-breaker",
                      data={"action": "reset"}).status_code == 200
    assert platform_guard.status()["breaker"]["tripped"] is False

    # Service genuinely resumes: the pre-reset spend no longer counts, so a
    # run-start passes the breaker again (400 on the empty goal).
    assert client.post("/api/run", data={"goal": ""}).status_code == 400


# ---------------------------------------------------------------------------
# (4) Uploads are encrypted at rest; downloads decrypt to the original bytes.
# ---------------------------------------------------------------------------
def test_upload_encrypted_at_rest_download_returns_original_checksum(
        fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("FERNET_KEYS", Fernet.generate_key().decode())
    assert crypto.enabled() is True

    out_root = tmp_path / "outputs"
    monkeypatch.setattr(app, "OUTPUT_DIR", str(out_root))

    original = ("月份,营收\n2026-01,123456\n2026-02,234567\n" * 30).encode("utf-8")
    checksum = hashlib.sha256(original).hexdigest()

    job = "phase8job"
    jdir = out_root / job
    jdir.mkdir(parents=True)
    fp = jdir / "data.csv"
    fp.write_bytes(original)

    crypto.encrypt_file_in_place(str(fp))
    on_disk = fp.read_bytes()
    assert on_disk != original                  # not stored as plaintext
    assert crypto.looks_encrypted(on_disk)      # leading magic bytes differ (Fernet)

    client = app.app.test_client()
    r = client.get(f"/api/files/{job}/data.csv")
    assert r.status_code == 200
    assert hashlib.sha256(r.data).hexdigest() == checksum  # original recovered


# ---------------------------------------------------------------------------
# (5) Audit rows are written for login + topup + download.
# ---------------------------------------------------------------------------
def test_audit_rows_written_for_login_topup_download(
        fresh_db, tmp_path, monkeypatch):
    store = _store(tmp_path, **{"audit@corp.com": "password1"})
    out_root = tmp_path / "outputs"
    monkeypatch.setattr(app, "OUTPUT_DIR", str(out_root))

    client = app.app.test_client()
    assert client.post("/api/login", json={
        "email": "audit@corp.com", "password": "password1"}).status_code == 200

    # Top-up now goes through a real payment order (Phase 9): create the order,
    # then deliver a SIGNED mock notify — the verified paid transition is what
    # writes the topup audit row.
    from agent_team import payments
    r = client.post("/api/pay/create", json={"type": "topup", "amount": 25})
    assert r.status_code == 200
    fields = payments.MockProvider().notify_fields(r.get_json()["order_no"], 25)
    assert client.post("/pay/notify/mock", data=fields).status_code == 200

    job = "auditdl"
    jdir = out_root / job
    jdir.mkdir(parents=True)
    (jdir / "f.svg").write_bytes(b"<svg/>")
    assert client.get(f"/api/files/{job}/f.svg").status_code == 200

    assert len(metrics_store.list_audit(action=AUDIT_LOGIN)) >= 1
    assert len(metrics_store.list_audit(action=AUDIT_TOPUP)) >= 1
    assert len(metrics_store.list_audit(action=AUDIT_DOWNLOAD)) >= 1

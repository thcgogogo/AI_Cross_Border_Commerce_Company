"""
test_phase11_device_routes.py — WS04: device-management routes + /v1 device auth.

Covers the four /api/devices/* routes (pair-code, exchange, list, revoke), the
device-token branch of the /v1 gateway (per-device RPM + ¥ sub-cap + model-subset
narrowing, billing the OWNING account), and field-stripping of the device list.

Hermetic, like test_phase8_security: a throwaway SQLite brain per test, a fresh
CustomerStore wired into app.CUSTOMERS, the module-global app.DEVICES running against
the same DB, providers.gateway_chat stubbed so no real upstream call is made.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402
from agent_team import config, db, devices  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402

MODEL = config.HAIKU          # a text model the test accounts purchase
OTHER_MODEL = config.OPUS     # a text model they do NOT purchase


@pytest.fixture
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "brain.db"))
    yield


def _store(tmp_path, models, **accounts):
    """A CustomerStore wired into app.CUSTOMERS; each account gets `models` + ¥100."""
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    keys = {}
    for email, password in accounts.items():
        res = store.create_customer(list(models), name=email.split("@")[0],
                                    starting_balance=100.0, email=email, password=password)
        keys[email] = res["api_key"]
    app.CUSTOMERS = store
    return store, keys


def _login(store, email):
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return client


def _stub_gateway(monkeypatch, in_tok=120, out_tok=40):
    monkeypatch.setattr(app.providers, "gateway_chat",
                        lambda model, messages, max_tokens=1024: {
                            "text": "ok", "input_tokens": in_tok, "output_tokens": out_tok,
                            "model": model, "provider": "test"})


def _pair_device(store, email, monkeypatch=None):
    """Mint + redeem a pairing code; return (device_token, device_id)."""
    client = _login(store, email)
    pc = client.post("/api/devices/pair-code").get_json()
    anon = app.app.test_client()      # the daemon has no session yet
    ex = anon.post("/api/devices/exchange",
                   json={"pairing_code": pc["pairing_code"], "device_name": "MBP",
                         "platform": "darwin"}).get_json()
    return ex["device_token"], ex["device_id"]


# ---------------------------------------------------------------------------
# Pairing lifecycle
# ---------------------------------------------------------------------------
def test_pairing_happy_path(fresh_db, tmp_path):
    store, _ = _store(tmp_path, [MODEL], **{"a@corp.com": "pw123456"})
    client = _login(store, "a@corp.com")
    pc = client.post("/api/devices/pair-code")
    assert pc.status_code == 200
    code = pc.get_json()["pairing_code"]
    assert code and pc.get_json()["expires_at"]

    anon = app.app.test_client()      # NO session — possession of the code is the proof
    ex = anon.post("/api/devices/exchange",
                   json={"pairing_code": code, "platform": "darwin"})
    assert ex.status_code == 200
    body = ex.get_json()
    assert body["device_token"].startswith("tng-dev-")
    assert body["account_id"] == store.find_by_email("a@corp.com")["id"]


def test_pairing_replay_is_burned(fresh_db, tmp_path):
    store, _ = _store(tmp_path, [MODEL], **{"a@corp.com": "pw123456"})
    client = _login(store, "a@corp.com")
    code = client.post("/api/devices/pair-code").get_json()["pairing_code"]
    anon = app.app.test_client()
    assert anon.post("/api/devices/exchange", json={"pairing_code": code}).status_code == 200
    # A replayed code mints nothing.
    assert anon.post("/api/devices/exchange", json={"pairing_code": code}).status_code == 400


def test_pairing_expired_code_rejected(fresh_db, tmp_path, monkeypatch):
    store, _ = _store(tmp_path, [MODEL], **{"a@corp.com": "pw123456"})
    monkeypatch.setattr(devices, "_PAIRING_TTL_SECS", -10)   # expires in the past
    client = _login(store, "a@corp.com")
    code = client.post("/api/devices/pair-code").get_json()["pairing_code"]
    anon = app.app.test_client()
    assert anon.post("/api/devices/exchange", json={"pairing_code": code}).status_code == 400


def test_pair_code_requires_login(fresh_db, tmp_path):
    _store(tmp_path, [MODEL])
    anon = app.app.test_client()
    assert anon.post("/api/devices/pair-code").status_code == 401


# ---------------------------------------------------------------------------
# Device list — field-stripped
# ---------------------------------------------------------------------------
def test_device_list_is_field_stripped(fresh_db, tmp_path):
    store, _ = _store(tmp_path, [MODEL], **{"a@corp.com": "pw123456"})
    token, device_id = _pair_device(store, "a@corp.com")
    client = _login(store, "a@corp.com")
    resp = client.get("/api/devices")
    assert resp.status_code == 200
    raw = resp.get_data(as_text=True)
    devs = resp.get_json()["devices"]
    assert len(devs) == 1 and devs[0]["id"] == device_id
    assert set(devs[0].keys()) == {"id", "name", "platform", "status",
                                   "last_seen_at", "created_at"}
    # No secret/machinery anywhere in the body, and the raw token never appears.
    for forbidden in ("token_hash", "pairing_code_hash", "device_token", "balance"):
        assert forbidden not in raw
    assert token not in raw


# ---------------------------------------------------------------------------
# Revoke — instant kill switch + cross-account isolation
# ---------------------------------------------------------------------------
def test_revoke_kills_the_token(fresh_db, tmp_path, monkeypatch):
    store, _ = _store(tmp_path, [MODEL], **{"a@corp.com": "pw123456"})
    _stub_gateway(monkeypatch)
    token, device_id = _pair_device(store, "a@corp.com")
    hdr = {"Authorization": f"Bearer {token}"}
    call = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}

    gw = app.app.test_client()
    assert gw.post("/v1/chat/completions", json=call, headers=hdr).status_code == 200

    client = _login(store, "a@corp.com")
    assert client.post(f"/api/devices/{device_id}/revoke").status_code == 200
    # The token now fails closed at the gateway.
    assert gw.post("/v1/chat/completions", json=call, headers=hdr).status_code == 401


def test_revoke_is_account_scoped(fresh_db, tmp_path):
    store, _ = _store(tmp_path, [MODEL],
                      **{"a@corp.com": "pw123456", "b@corp.com": "pw123456"})
    _token, device_id = _pair_device(store, "a@corp.com")
    # Account B cannot revoke A's device → 404 (no existence leak), and B sees none.
    b = _login(store, "b@corp.com")
    assert b.post(f"/api/devices/{device_id}/revoke").status_code == 404
    assert b.get("/api/devices").get_json()["devices"] == []


# ---------------------------------------------------------------------------
# /v1 device auth — billing, model-subset, per-device RPM
# ---------------------------------------------------------------------------
def test_v1_device_auth_bills_the_account(fresh_db, tmp_path, monkeypatch):
    store, _ = _store(tmp_path, [MODEL], **{"a@corp.com": "pw123456"})
    # Realistic token counts so the ¥ cost doesn't round to 0.00 at fen precision.
    _stub_gateway(monkeypatch, in_tok=500_000, out_tok=200_000)
    token, _ = _pair_device(store, "a@corp.com")
    acct_id = store.find_by_email("a@corp.com")["id"]

    gw = app.app.test_client()
    r = gw.post("/v1/chat/completions",
                json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    after = app.CUSTOMERS.get_by_id(acct_id)
    assert after["requests"] == 1              # the call was metered to the account…
    assert after["spent"] > 0                  # …and charged the account wallet
    assert after["balance"] < 100.0


def test_v1_device_model_subset_narrowing(fresh_db, tmp_path, monkeypatch):
    store, _ = _store(tmp_path, [MODEL], **{"a@corp.com": "pw123456"})   # owns MODEL, not OTHER_MODEL
    _stub_gateway(monkeypatch)
    token, _ = _pair_device(store, "a@corp.com")
    gw = app.app.test_client()
    r = gw.post("/v1/chat/completions",
                json={"model": OTHER_MODEL, "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert r.get_json()["error"]["code"] == "model_access_denied"


def test_v1_per_device_rpm_keys_on_device_id(fresh_db, tmp_path, monkeypatch):
    store, keys = _store(tmp_path, [MODEL], **{"a@corp.com": "pw123456"})
    _stub_gateway(monkeypatch)
    token, device_id = _pair_device(store, "a@corp.com")
    acct_id = store.find_by_email("a@corp.com")["id"]

    seen = []
    monkeypatch.setattr(app, "_rpm_check", lambda key, limit: seen.append(key) or True)
    gw = app.app.test_client()
    call = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}

    # Device-authenticated → RPM bucket keyed on the DEVICE id.
    gw.post("/v1/chat/completions", json=call, headers={"Authorization": f"Bearer {token}"})
    assert seen[-1] == device_id
    # Account-key authenticated → keyed on the account id (unchanged).
    gw.post("/v1/chat/completions", json=call,
            headers={"Authorization": f"Bearer {keys['a@corp.com']}"})
    assert seen[-1] == acct_id


def test_v1_account_key_path_unchanged(fresh_db, tmp_path, monkeypatch):
    """A plain account-key call still works (no device involved)."""
    store, keys = _store(tmp_path, [MODEL], **{"a@corp.com": "pw123456"})
    _stub_gateway(monkeypatch)
    gw = app.app.test_client()
    r = gw.post("/v1/chat/completions",
                json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {keys['a@corp.com']}"})
    assert r.status_code == 200

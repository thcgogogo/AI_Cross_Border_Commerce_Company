"""跨境财务 hub — /api/cross-border/settings (贸易方式 persistence).

The Phase-A consolidation re-homes 对账 + AR under one 跨境财务 section gated by a
贸易方式 selector. The only NEW backend surface is the customer-safe settings
endpoint that persists the seller's trade mode into the company brain
(profile_json["cross_border"]) — no schema change, no machinery. These tests pin
its auth, defaults, round-trip, validation, per-account isolation, and that the
existing 对账/AR engines + endpoints are untouched (those suites run unchanged).

Hermetic: no network, no model calls.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402
from agent_team import db, metrics_store as ms  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "brain.db"))
    yield


def _store(tmp_path, **accounts):
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    for email, password in accounts.items():
        store.create_customer([], name=email.split("@")[0], starting_balance=100.0,
                              email=email, password=password)
    app.CUSTOMERS = store
    return store


def _login(store, email):
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return client


# --- auth -------------------------------------------------------------------
def test_settings_requires_login(fresh_db, tmp_path):
    _store(tmp_path, **{"boss@co.com": "pw"})
    client = app.app.test_client()  # no session
    assert client.get("/api/cross-border/settings").status_code == 401
    assert client.post("/api/cross-border/settings", json={"mode": "9710"}).status_code == 401


# --- default + round-trip ---------------------------------------------------
def test_default_mode_is_9810(fresh_db, tmp_path):
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    client = _login(store, "boss@co.com")
    r = client.get("/api/cross-border/settings")
    assert r.status_code == 200
    s = r.get_json()["settings"]
    assert s["mode"] == "9810"            # 海外仓 / Amazon FBA — the first customer
    assert s["platforms"] == [] and s["payment_tools"] == []


def test_save_and_read_back_mode(fresh_db, tmp_path):
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    client = _login(store, "boss@co.com")
    saved = client.post("/api/cross-border/settings", json={"mode": "9710"}).get_json()
    assert saved["ok"] is True and saved["settings"]["mode"] == "9710"
    # durable across a fresh read
    assert client.get("/api/cross-border/settings").get_json()["settings"]["mode"] == "9710"


def test_save_platforms_and_tools(fresh_db, tmp_path):
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    client = _login(store, "boss@co.com")
    client.post("/api/cross-border/settings",
                json={"platforms": ["Amazon", " ", "TikTok"], "payment_tools": ["PingPong"]})
    s = client.get("/api/cross-border/settings").get_json()["settings"]
    assert s["platforms"] == ["Amazon", "TikTok"]   # blanks stripped
    assert s["payment_tools"] == ["PingPong"]
    assert s["mode"] == "9810"                       # untouched fields preserved


def test_invalid_mode_coerced_to_default(fresh_db, tmp_path):
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    client = _login(store, "boss@co.com")
    s = client.post("/api/cross-border/settings", json={"mode": "nonsense"}).get_json()["settings"]
    assert s["mode"] == "9810"


def test_partial_update_preserves_other_fields(fresh_db, tmp_path):
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    client = _login(store, "boss@co.com")
    client.post("/api/cross-border/settings", json={"mode": "9610", "platforms": ["独立站"]})
    # a mode-only update must not wipe the stored platforms
    client.post("/api/cross-border/settings", json={"mode": "1210"})
    s = client.get("/api/cross-border/settings").get_json()["settings"]
    assert s["mode"] == "1210" and s["platforms"] == ["独立站"]


# --- isolation + safety -----------------------------------------------------
def test_settings_are_per_account(fresh_db, tmp_path):
    store = _store(tmp_path, **{"a@co.com": "pw", "b@co.com": "pw"})
    a = _login(store, "a@co.com")
    a.post("/api/cross-border/settings", json={"mode": "9710"})
    b = _login(store, "b@co.com")
    # b never set a mode → still the default, not a's 9710
    assert b.get("/api/cross-border/settings").get_json()["settings"]["mode"] == "9810"


def test_payload_carries_no_machinery(fresh_db, tmp_path):
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    client = _login(store, "boss@co.com")
    body = client.get("/api/cross-border/settings").get_data(as_text=True).lower()
    for needle in ("model", "token", "opus", "deepseek", "sk-", "per_mtok", "¥"):
        assert needle not in body


# --- SPA shell routes (hard-load / refresh / shared deep links) -------------
def test_spa_shell_routes_not_404(fresh_db, tmp_path):
    """A hard load / refresh / shared link of a hub path must serve the SPA shell
    (302 → /login when logged out), NEVER 404. Regression guard: /cross-border and
    the legacy /ar were both missing from the Flask route allowlist (a browser smoke
    caught it — in-app SPA nav worked, but a refresh 404'd)."""
    _store(tmp_path, **{"boss@co.com": "pw"})
    client = app.app.test_client()  # logged out → spa_page redirects to /login
    for path in ("/cross-border", "/cross-border/recon", "/cross-border/ar/5",
                 "/ar", "/ar/5", "/recon", "/recon/5"):
        code = client.get(path).status_code
        assert code != 404, f"{path} 404s — missing SPA route"
        assert code in (302, 200), f"{path} -> {code}"


# --- the metrics_store helpers directly -------------------------------------
def test_store_helpers_roundtrip(fresh_db, tmp_path):
    cid = ms.get_or_create_company("helper@co.com", company_name="Helper")
    assert ms.get_cross_border_settings(cid)["mode"] == "9810"        # default
    ms.save_cross_border_settings(cid, mode="9610", payment_tools=["Payoneer"])
    got = ms.get_cross_border_settings(cid)
    assert got["mode"] == "9610" and got["payment_tools"] == ["Payoneer"]
    # cross_border namespace lives alongside other profile keys, doesn't clobber them
    ms.merge_profile(cid, {"company_name": "Helper Co"})
    assert ms.get_cross_border_settings(cid)["mode"] == "9610"
    assert ms.get_profile(cid)["company_name"] == "Helper Co"

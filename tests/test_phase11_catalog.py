"""
test_phase11_catalog.py — WS07: the agent catalog (publish / install / device pull).

The runner-visible manifest is a behavior-only whitelist — a device NEVER receives a
model id, provider, or ¥ rate. publish is founder-gated + content-hash-idempotent;
catalog is device-authed (a real paired device via the WS03/04 store).

Harness mirrors test_redesign_phase7: throwaway SQLite, a CustomerStore wired into
app.CUSTOMERS, sessions set directly; admin via $ADMIN_EMAILS.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402
from agent_team import config, db, metrics_store  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402

AGENT = "data_analyst"        # a real config.AGENTS id (carries a 'model' key we must strip)
_FOUNDER_HALF_KEYS = ("model", "provider", "input_per_mtok", "output_per_mtok",
                      "cost_budget_cny", "team_model_preset")


@pytest.fixture
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "phase11.db"))
    yield


def _store(tmp_path, **accounts):
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    for email, password in accounts.items():
        store.create_customer([], name=email.split("@")[0], starting_balance=100.0,
                              email=email, password=password)
    app.CUSTOMERS = store
    return store


def _client(store, email):
    c = app.app.test_client()
    with c.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return c


def _device_token(account_id):
    pc = app.DEVICES.create_pairing_code(account_id)
    return app.DEVICES.exchange_pairing_code(pc["pairing_code"])["device_token"]


# ---------------------------------------------------------------------------
# publish — gating, idempotency, whitelist
# ---------------------------------------------------------------------------
def test_publish_requires_admin(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")
    store = _store(tmp_path, **{"u@corp.com": "pw123456", "founder@corp.com": "pw123456"})
    assert _client(store, "u@corp.com").post(f"/api/agents/{AGENT}/publish").status_code == 403
    assert _client(store, "founder@corp.com").post("/api/agents/ghost/publish").status_code == 404


def test_publish_is_content_hash_idempotent_and_whitelisted(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")
    store = _store(tmp_path, **{"founder@corp.com": "pw123456"})
    admin = _client(store, "founder@corp.com")

    r1 = admin.post(f"/api/agents/{AGENT}/publish").get_json()
    assert r1["version"] == 1 and r1["changed"] is True
    assert r1["content_hash"].startswith("sha256:")
    # A no-op republish does NOT bump the version.
    r2 = admin.post(f"/api/agents/{AGENT}/publish").get_json()
    assert r2["version"] == 1 and r2["changed"] is False

    # The stored manifest is behavior-only — no model/provider/¥ anywhere.
    import json
    manifest = json.loads(metrics_store.latest_agent_catalog(AGENT)["manifest_json"])
    assert manifest["agent_id"] == AGENT and manifest["manifest_version"] == 1
    assert "system_prompt" in manifest and "tools" in manifest
    for k in _FOUNDER_HALF_KEYS:
        assert k not in manifest


def test_publish_does_not_resolve_a_model(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")
    store = _store(tmp_path, **{"founder@corp.com": "pw123456"})
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not resolve a model"))
    monkeypatch.setattr(config, "effective_model", boom)
    monkeypatch.setattr(config, "provider_for", boom)
    assert _client(store, "founder@corp.com").post(f"/api/agents/{AGENT}/publish").status_code == 200


# ---------------------------------------------------------------------------
# install — session-gated upsert
# ---------------------------------------------------------------------------
def test_install_requires_session_and_upserts(fresh_db, tmp_path):
    store = _store(tmp_path, **{"u@corp.com": "pw123456"})
    assert app.app.test_client().post(f"/api/agents/{AGENT}/install").status_code == 401
    c = _client(store, "u@corp.com")
    assert c.post(f"/api/agents/{AGENT}/install", json={"active": True}).status_code == 200
    # Idempotent on (account, agent): a second install updates, never duplicates.
    installs = c.post(f"/api/agents/{AGENT}/install", json={"active": False}).get_json()["installs"]
    rows = [i for i in installs if i["agent_id"] == AGENT]
    assert len(rows) == 1 and rows[0]["active"] is False     # soft uninstall, kept


# ---------------------------------------------------------------------------
# catalog — device-auth, resolves installed+published, excludes inactive
# ---------------------------------------------------------------------------
def test_catalog_requires_device_token(fresh_db, tmp_path):
    _store(tmp_path)
    assert app.app.test_client().get("/api/agents/catalog").status_code == 401


def test_catalog_returns_installed_published_manifests(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")
    store = _store(tmp_path, **{"founder@corp.com": "pw123456"})
    acct = store.find_by_email("founder@corp.com")["id"]
    admin = _client(store, "founder@corp.com")
    admin.post(f"/api/agents/{AGENT}/publish")
    admin.post(f"/api/agents/{AGENT}/install", json={"active": True})

    token = _device_token(acct)
    body = app.app.test_client().get(
        "/api/agents/catalog", headers={"Authorization": f"Bearer {token}"}).get_json()
    agents = body["agents"]
    assert len(agents) == 1 and agents[0]["agent_id"] == AGENT
    assert isinstance(agents[0]["manifest"], dict)         # parsed, not a string
    for k in _FOUNDER_HALF_KEYS:
        assert k not in agents[0]["manifest"]

    # Deactivate → the device sees nothing.
    admin.post(f"/api/agents/{AGENT}/install", json={"active": False})
    body2 = app.app.test_client().get(
        "/api/agents/catalog", headers={"Authorization": f"Bearer {token}"}).get_json()
    assert body2["agents"] == []


def test_catalog_pinned_vs_latest_resolution(fresh_db, tmp_path):
    """catalog_for_account: a pinned install returns that exact version; unpinned
    returns the latest published. (Two versions seeded directly via the store.)"""
    acct = "cus_pinme000000"
    metrics_store.publish_agent(AGENT, "数据分析师", {"agent_id": AGENT, "tools": ["a"]}, "sha256:v1hash")
    metrics_store.publish_agent(AGENT, "数据分析师", {"agent_id": AGENT, "tools": ["b"]}, "sha256:v2hash")

    metrics_store.set_agent_install(acct, AGENT, active=True, pinned_version=1)
    pinned = metrics_store.catalog_for_account(acct)
    assert pinned[0]["version"] == 1

    metrics_store.set_agent_install(acct, AGENT, active=True, pinned_version=None)
    latest = metrics_store.catalog_for_account(acct)
    assert latest[0]["version"] == 2


def test_catalog_does_not_resolve_a_model(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")
    store = _store(tmp_path, **{"founder@corp.com": "pw123456"})
    acct = store.find_by_email("founder@corp.com")["id"]
    admin = _client(store, "founder@corp.com")
    admin.post(f"/api/agents/{AGENT}/publish")
    admin.post(f"/api/agents/{AGENT}/install", json={"active": True})
    token = _device_token(acct)

    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("catalog must not resolve a model"))
    monkeypatch.setattr(config, "effective_model", boom)
    monkeypatch.setattr(config, "provider_for", boom)
    r = app.app.test_client().get("/api/agents/catalog", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and len(r.get_json()["agents"]) == 1

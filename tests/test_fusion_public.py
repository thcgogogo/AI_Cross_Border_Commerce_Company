"""
test_fusion_public.py — Fusion's PUBLIC surface: the freemium teaser.

An anonymous visitor gets a few FREE turns (server-paid, NOT persisted) and then must
log in to continue. The full authenticated, multi-turn, per-conversation-memory behavior
(persistence, the rail, ownership) lives in test_fusion_authenticated.py. This file pins:
  - the first N anonymous turns succeed and the free-turn counter decrements,
  - the (N+1)th anonymous turn is refused with 401 + login_required,
  - GET /api/fusion/quota reports the remaining free turns,
  - anonymous turns persist NOTHING (no conversation/message rows),
  - the conversation-memory routes still require login (401),
  - /api/fusion/models stays public with NO pricing,
  - metered spend still feeds the platform breaker (which refuses anonymous runs too).

Network-free: providers.gateway_chat is monkeypatched. Run from the project root:

    .venv/bin/python -m pytest tests/test_fusion_public.py -v
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — one-time, idempotent module boot
from agent_team import config, db, platform_guard  # noqa: E402


@pytest.fixture
def client():
    return app.app.test_client()


@pytest.fixture
def fresh_db(tmp_path):
    """Throwaway SQLite so spend/persistence assertions are isolated."""
    db.init_db(str(tmp_path / "brain.db"))
    yield


def _fake_gateway():
    def fake(model_id, messages, max_tokens=1024):
        return {"text": f"[{model_id}] reply", "input_tokens": 5, "output_tokens": 7,
                "model": model_id, "provider": config.provider_for(model_id)}
    return fake


def _anon_turn(client, text):
    return client.post("/api/fusion/chat", json={
        "messages": [{"role": "user", "content": text}], "models": ["deepseek-v4-flash"]})


# ---------------------------------------------------------------------------
# 1. The freemium teaser: N free anonymous turns, then a login gate
# ---------------------------------------------------------------------------
def test_anonymous_free_turns_then_gate(fresh_db, client, monkeypatch):
    monkeypatch.setenv("FUSION_FREE_ANON_TURNS", "3")
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    remaining = []
    for i in range(3):
        r = _anon_turn(client, f"问题 {i}")
        assert r.status_code == 200, r.get_data(as_text=True)
        body = r.get_json()
        assert body["fused"] and body["fused"]["text"]
        assert body.get("anonymous") is True
        remaining.append(body["free_remaining"])
    assert remaining == [2, 1, 0]
    # the 4th turn is gated
    r = _anon_turn(client, "问题 4")
    assert r.status_code == 401
    assert r.get_json().get("login_required") is True


# ---------------------------------------------------------------------------
# 2. The quota endpoint reports the remaining free turns
# ---------------------------------------------------------------------------
def test_fusion_quota_endpoint(fresh_db, client, monkeypatch):
    monkeypatch.setenv("FUSION_FREE_ANON_TURNS", "3")
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    q0 = client.get("/api/fusion/quota").get_json()
    assert q0["signed_in"] is False and q0["free_remaining"] == 3
    _anon_turn(client, "hi")
    q1 = client.get("/api/fusion/quota").get_json()
    assert q1["free_remaining"] == 2


# ---------------------------------------------------------------------------
# 3. Anonymous turns persist NOTHING
# ---------------------------------------------------------------------------
def test_anonymous_turns_are_not_persisted(fresh_db, client, monkeypatch):
    from agent_team import metrics_store
    from agent_team.models import FusionConversation, FusionMessage
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    _anon_turn(client, "hi")
    with db.session_scope() as s:
        assert s.query(FusionConversation).count() == 0
        assert s.query(FusionMessage).count() == 0


# ---------------------------------------------------------------------------
# 4. The conversation-memory routes still require login
# ---------------------------------------------------------------------------
def test_conversation_routes_require_login(client):
    assert client.get("/api/fusion/conversations").status_code == 401
    assert client.post("/api/fusion/conversations", json={}).status_code == 401
    assert client.get("/api/fusion/conversations/1").status_code == 401
    assert client.patch("/api/fusion/conversations/1", json={"title": "x"}).status_code == 401
    assert client.delete("/api/fusion/conversations/1").status_code == 401


# ---------------------------------------------------------------------------
# 5. Public model list carries NO pricing (the picker stays open)
# ---------------------------------------------------------------------------
def test_fusion_models_endpoint_has_no_pricing(client):
    r = client.get("/api/fusion/models")
    assert r.status_code == 200
    models = r.get_json()["models"]
    assert models and any(m["id"] == "deepseek-v4-flash" for m in models)
    for m in models:
        assert set(m) == {"id", "label", "provider", "provider_label", "tier", "available", "vision"}
        for priced in ("input_per_mtok", "output_per_mtok", "price"):
            assert priced not in m


# ---------------------------------------------------------------------------
# 6. Cost guard — the breaker refuses anonymous runs when tripped
# ---------------------------------------------------------------------------
def test_spend_feeds_breaker(fresh_db, monkeypatch):
    from agent_team import metrics_store
    from datetime import datetime, timezone
    monkeypatch.setenv("PLATFORM_HOURLY_CAP_YUAN", "0.01")
    platform_guard.record_spend("deepseek", 1.0, account_id=None)
    hour_start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    assert metrics_store.sum_platform_spend(hour_start) >= 1.0
    allowed, reason = platform_guard.check()
    assert allowed is False and reason


def test_breaker_refuses_anonymous_chat(fresh_db, client, monkeypatch):
    # Within the free quota, but the breaker is tripped → 503 (not a free answer).
    monkeypatch.setattr(app.platform_guard, "check",
                        lambda *a, **k: (False, "系统繁忙，请稍后再试"))
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    r = _anon_turn(client, "hi")
    assert r.status_code == 503
    assert "系统繁忙" in r.get_json()["error"]

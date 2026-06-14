"""
test_fusion_public.py — the PUBLIC Fusion chat (the new landing page).

Asserts the anonymous, multi-turn, server-paid contract and its cost guards:
  - works with NO login and returns a fused answer (no ¥ cost/balance in the body),
  - forwards the full conversation history to each model (multi-turn),
  - caps the model fan-out per run,
  - is rate-limited per IP (429),
  - /api/fusion/models lists models WITHOUT pricing,
  - anonymous (account_id=None) spend is metered and trips the platform breaker.

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
    """Throwaway SQLite for the breaker/spend test (isolated platform_spend table)."""
    db.init_db(str(tmp_path / "brain.db"))
    yield


def _fake_gateway(captured=None):
    def fake(model_id, messages, max_tokens=1024):
        if captured is not None:
            captured.setdefault("calls", []).append({"model": model_id, "messages": messages})
        return {"text": f"[{model_id}] reply", "input_tokens": 5, "output_tokens": 7,
                "model": model_id, "provider": config.provider_for(model_id)}
    return fake


# ---------------------------------------------------------------------------
# 1. Anonymous chat returns a fused answer, with no ¥ machinery in the body
# ---------------------------------------------------------------------------
def test_anonymous_fusion_chat_returns_fused_answer(client, monkeypatch):
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    r = client.post("/api/fusion/chat", json={
        "messages": [{"role": "user", "content": "你好"}],
        "models": ["deepseek-v4-flash", "qwen-max"], "fuser": "auto"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["fused"] and body["fused"]["text"]
    assert len(body["answers"]) == 2
    # No login was sent and no billing machinery may leak to the public.
    assert "cost" not in body and "balance" not in body
    for a in body["answers"]:
        assert "input_tokens" not in a and "output_tokens" not in a


# ---------------------------------------------------------------------------
# 2. Multi-turn: the full conversation reaches each model
# ---------------------------------------------------------------------------
def test_multi_turn_history_is_forwarded(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway(captured))
    convo = [{"role": "user", "content": "first"},
             {"role": "assistant", "content": "ok"},
             {"role": "user", "content": "second"}]
    r = client.post("/api/fusion/chat", json={"messages": convo, "models": ["deepseek-v4-flash"]})
    assert r.status_code == 200
    # One model → one candidate call, no separate fuser call; it saw the whole convo.
    sent = captured["calls"][0]["messages"]
    assert [m["content"] for m in sent] == ["first", "ok", "second"]


# ---------------------------------------------------------------------------
# 3. The per-run model fan-out is capped
# ---------------------------------------------------------------------------
def test_model_cap_enforced(client, monkeypatch):
    monkeypatch.setenv("FUSION_ANON_MAX_MODELS", "2")
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    r = client.post("/api/fusion/chat", json={
        "messages": [{"role": "user", "content": "hi"}],
        "models": ["deepseek-v4-flash", "qwen-max", "qwen-plus", config.HAIKU]})
    assert r.status_code == 200
    assert len(r.get_json()["answers"]) == 2  # capped from 4


# ---------------------------------------------------------------------------
# 4. Per-IP rate limit
# ---------------------------------------------------------------------------
def test_per_ip_rate_limit_429(client, monkeypatch):
    monkeypatch.setenv("FUSION_RATE_LIMIT", "2 per hour")
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    monkeypatch.setattr(app.limiter, "enabled", True)  # conftest disables it suite-wide
    app.limiter.reset()
    payload = {"messages": [{"role": "user", "content": "hi"}], "models": ["deepseek-v4-flash"]}
    codes = [client.post("/api/fusion/chat", json=payload).status_code for _ in range(3)]
    app.limiter.reset()
    assert codes[0] == 200 and codes[1] == 200
    assert codes[2] == 429


# ---------------------------------------------------------------------------
# 5. Public model list carries NO pricing
# ---------------------------------------------------------------------------
def test_fusion_models_endpoint_has_no_pricing(client):
    r = client.get("/api/fusion/models")
    assert r.status_code == 200
    models = r.get_json()["models"]
    assert models and any(m["id"] == "deepseek-v4-flash" for m in models)
    for m in models:
        assert set(m) == {"id", "label", "provider", "provider_label", "tier", "available"}
        for priced in ("input_per_mtok", "output_per_mtok", "price"):
            assert priced not in m


# ---------------------------------------------------------------------------
# 6. Cost guard — anonymous spend is metered and trips the breaker
# ---------------------------------------------------------------------------
def test_anonymous_spend_feeds_breaker(fresh_db, monkeypatch):
    from agent_team import metrics_store
    from datetime import datetime, timezone
    monkeypatch.setenv("PLATFORM_HOURLY_CAP_YUAN", "0.01")
    # An anonymous (account_id=None) fusion call records platform spend...
    platform_guard.record_spend("deepseek", 1.0, account_id=None)
    hour_start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    assert metrics_store.sum_platform_spend(hour_start) >= 1.0
    # ...which pushes rolling spend over the cap, so the breaker refuses the next run.
    allowed, reason = platform_guard.check()
    assert allowed is False and reason


def test_route_refuses_when_breaker_tripped(client, monkeypatch):
    monkeypatch.setattr(app.platform_guard, "check", lambda *a, **k: (False, "系统繁忙，请稍后再试"))
    r = client.post("/api/fusion/chat", json={
        "messages": [{"role": "user", "content": "hi"}], "models": ["deepseek-v4-flash"]})
    assert r.status_code == 503
    assert "系统繁忙" in r.get_json()["error"]

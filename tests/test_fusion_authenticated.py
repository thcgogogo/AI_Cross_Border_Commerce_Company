"""
test_fusion_authenticated.py — the SIGN-IN-REQUIRED, multi-turn Fusion chatbot with
per-conversation memory (the upgrade from the anonymous single-shot demo).

Asserts the new contract:
  - login is required (anonymous → 401), and no ¥ machinery leaks to the customer;
  - a turn persists the user + assistant messages, and message content is ENCRYPTED
    at rest (the raw DB column is a Fernet token that decrypts back);
  - multi-turn: a 2nd turn forwards the prior conversation (incl. the fused reply) to
    the models;
  - the history-trim rule bounds what reaches the models while the DB keeps every turn;
  - the FREE-tier model split clamps a premium request to the cheap allowlist, while a
    PAID account keeps the full fan-out;
  - the per-account rate limit is keyed by ACCOUNT (two accounts on one IP are
    independent; one over its cap gets 429);
  - ownership isolation: account B gets 404 on account A's conversation (read/rename/del);
  - COGS is billed to the account but the ¥ balance is NOT charged (Fusion is free);
  - a partial failure persists the per-model breakdown; a TOTAL failure writes a
    'failed' marker (never a half-saved success);
  - rename + soft-delete (a deleted conversation is hidden from the list + 404 on fetch);
  - LOCAL_MODE hides the persistent Fusion routes (404).

Network-free: providers.gateway_chat is monkeypatched. Run from the project root:

    .venv/bin/python -m pytest tests/test_fusion_authenticated.py -v
"""
import os
import sqlite3
import sys

import pytest
from cryptography.fernet import Fernet

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — one-time, idempotent module boot
from agent_team import config, crypto, db, metrics_store, providers  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.models import Account, FusionMessage  # noqa: E402

A_EMAIL = "alice@corp.com"
B_EMAIL = "bob@corp.com"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh DB + customer store, with encryption ON (FERNET_KEYS) so the at-rest test
    is meaningful. Two accounts (Alice, Bob) for the ownership tests."""
    monkeypatch.setenv("FERNET_KEYS", Fernet.generate_key().decode())
    dbpath = str(tmp_path / "fusion.db")
    db.init_db(dbpath)
    monkeypatch.setenv("ADMIN_EMAILS", "")
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    store.create_customer([], name="Alice", starting_balance=1000.0,
                          email=A_EMAIL, password="password1")
    store.create_customer([], name="Bob", starting_balance=1000.0,
                          email=B_EMAIL, password="password1")
    monkeypatch.setattr(app, "CUSTOMERS", store)
    return {"store": store, "dbpath": dbpath}


def _client_for(store, email):
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return client


def _fake_gateway(captured=None, fail=()):
    """Faked-but-billed gateway: returns real token counts (so record_usage meters COGS)
    and can simulate per-model failure. `fail` is a set of model ids that raise."""
    def fake(model_id, messages, max_tokens=1024):
        if captured is not None:
            captured.setdefault("calls", []).append(
                {"model": model_id, "messages": messages})
        if model_id in fail:
            raise providers.LLMError(f"{model_id} unavailable")
        # Real-ish token counts so record_usage meters a non-zero ¥ COGS (a handful of
        # tokens rounds to ¥0.00 at fen precision and wouldn't prove billing ran).
        return {"text": f"[{model_id}] reply", "input_tokens": 100_000,
                "output_tokens": 20_000, "model": model_id,
                "provider": config.provider_for(model_id)}
    return fake


def _account(store, email):
    acc_id = store.find_by_email(email)["id"]
    with db.session_scope() as s:
        a = s.get(Account, acc_id)
        return {"id": a.id, "balance": float(a.balance_yuan),
                "spent": float(a.spent_yuan or 0)}


# ---------------------------------------------------------------------------
# 1. Anonymous chat is FREE (the teaser); the conversation-memory routes need login
# ---------------------------------------------------------------------------
def test_anonymous_chat_free_but_memory_needs_login(env, monkeypatch):
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    anon = app.app.test_client()
    # an anonymous turn succeeds (server-paid teaser), with no ¥ in the body
    r = anon.post("/api/fusion/chat", json={
        "messages": [{"role": "user", "content": "你好"}], "models": ["deepseek-v4-flash"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json().get("anonymous") is True and "cost" not in r.get_json()
    # but the per-conversation memory (persistence) requires login
    assert anon.get("/api/fusion/conversations").status_code == 401
    assert anon.post("/api/fusion/conversations", json={}).status_code == 401


# ---------------------------------------------------------------------------
# 2. A turn persists user + assistant; content is encrypted at rest; no ¥ leaks
# ---------------------------------------------------------------------------
def test_turn_persists_and_encrypts(env, monkeypatch):
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    client = _client_for(env["store"], A_EMAIL)
    r = client.post("/api/fusion/chat", json={
        "text": "什么是 Fusion？", "models": ["deepseek-v4-flash", "qwen-plus"]})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    cid = body["conversation_id"]
    assert cid and body["fused"]["text"] and len(body["answers"]) == 2
    # customer-safe: model names ok, but NEVER ¥ / tokens
    assert "cost" not in body and "balance" not in body
    for a in body["answers"]:
        assert "input_tokens" not in a and "output_tokens" not in a

    # two messages stored (user + assistant), and content is a Fernet token at rest
    raw = sqlite3.connect(env["dbpath"]).execute(
        "SELECT message_index, role, content, status FROM fusion_messages "
        "ORDER BY message_index").fetchall()
    assert [(r0[0], r0[1]) for r0 in raw] == [(0, "user"), (1, "assistant")]
    for _idx, _role, content, _status in raw:
        assert content.startswith("gAAAAA"), "content not encrypted at rest"
        assert "Fusion" not in content and "reply" not in content
    # and it decrypts back to the plaintext
    assert crypto.decrypt_string(raw[0][2]) == "什么是 Fusion？"


# ---------------------------------------------------------------------------
# 3. Multi-turn: the prior conversation (incl. the fused reply) reaches the models
# ---------------------------------------------------------------------------
def test_multi_turn_history_forwarded(env, monkeypatch):
    captured = {}
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway(captured))
    client = _client_for(env["store"], A_EMAIL)
    r1 = client.post("/api/fusion/chat", json={"text": "first", "models": ["deepseek-v4-flash"]})
    cid = r1.get_json()["conversation_id"]
    captured["calls"].clear()
    r2 = client.post("/api/fusion/chat", json={
        "conversation_id": cid, "text": "second", "models": ["deepseek-v4-flash"]})
    assert r2.status_code == 200
    sent = captured["calls"][0]["messages"]
    assert [m["content"] for m in sent] == ["first", "[deepseek-v4-flash] reply", "second"]


# ---------------------------------------------------------------------------
# 4. History trim bounds the context window; the DB keeps every turn
# ---------------------------------------------------------------------------
def test_history_trim_keeps_db_but_bounds_context(env, monkeypatch):
    monkeypatch.setenv("FUSION_LOGGEDIN_MAX_HISTORY_MSGS", "2")
    captured = {}
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway(captured))
    client = _client_for(env["store"], A_EMAIL)
    r = client.post("/api/fusion/chat", json={"text": "t1", "models": ["deepseek-v4-flash"]})
    cid = r.get_json()["conversation_id"]
    client.post("/api/fusion/chat", json={"conversation_id": cid, "text": "t2",
                                          "models": ["deepseek-v4-flash"]})
    captured["calls"].clear()
    client.post("/api/fusion/chat", json={"conversation_id": cid, "text": "t3",
                                          "models": ["deepseek-v4-flash"]})
    # only the last 2 messages reached the model...
    assert len(captured["calls"][0]["messages"]) == 2
    # ...but all 6 turns (3 user + 3 assistant) are persisted
    assert len(metrics_store.get_fusion_messages(cid)) == 6


# ---------------------------------------------------------------------------
# 5. Free-tier model split clamps premium requests; paid keeps the full fan-out
# ---------------------------------------------------------------------------
def test_free_tier_model_split(env, monkeypatch):
    captured = {}
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway(captured))
    client = _client_for(env["store"], A_EMAIL)  # free account (no subscription)
    client.post("/api/fusion/chat", json={
        "text": "hi", "models": ["gpt-5.1", "gemini-2.5-pro", "claude-opus-4-7"]})
    called = {c["model"] for c in captured["calls"]}
    free_allow = set(config.fusion_free_models())
    assert called and called <= free_allow, f"premium leaked into free fan-out: {called}"
    assert not called & {"gpt-5.1", "gemini-2.5-pro", "claude-opus-4-7"}
    # candidate count is capped at the free cap
    cands = [c for c in captured["calls"] if c["model"] in free_allow]
    assert len({c["model"] for c in cands}) <= config.fusion_free_model_cap() + 1  # +fuser


def test_paid_tier_keeps_premium(env, monkeypatch):
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    monkeypatch.setattr(app.metrics_store, "effective_plan",
                        lambda *a, **k: ("pro", None, False))
    captured = {}
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway(captured))
    client = _client_for(env["store"], A_EMAIL)
    client.post("/api/fusion/chat", json={
        "text": "hi", "models": ["gpt-5.1", "gemini-2.5-pro"]})
    called = {c["model"] for c in captured["calls"]}
    assert {"gpt-5.1", "gemini-2.5-pro"} <= called, "paid fan-out was wrongly clamped"


# ---------------------------------------------------------------------------
# 6. Per-account rate limit (keyed by account, not IP)
# ---------------------------------------------------------------------------
def test_per_account_rate_limit(env, monkeypatch):
    monkeypatch.setenv("FUSION_ACCOUNT_RATE_LIMIT", "2 per hour")
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    monkeypatch.setattr(app.limiter, "enabled", True)  # conftest disables it suite-wide
    app.limiter.reset()
    try:
        a = _client_for(env["store"], A_EMAIL)
        payload = {"text": "hi", "models": ["deepseek-v4-flash"]}
        codes = [a.post("/api/fusion/chat", json=payload).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        # Bob, same process/IP, gets his own bucket — unaffected.
        b = _client_for(env["store"], B_EMAIL)
        assert b.post("/api/fusion/chat", json=payload).status_code == 200
    finally:
        app.limiter.reset()


# ---------------------------------------------------------------------------
# 7. Ownership isolation — account B cannot touch account A's conversation
# ---------------------------------------------------------------------------
def test_ownership_isolation(env, monkeypatch):
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    a = _client_for(env["store"], A_EMAIL)
    cid = a.post("/api/fusion/chat", json={"text": "mine", "models": ["deepseek-v4-flash"]}
                 ).get_json()["conversation_id"]
    b = _client_for(env["store"], B_EMAIL)
    assert b.get(f"/api/fusion/conversations/{cid}").status_code == 404
    assert b.patch(f"/api/fusion/conversations/{cid}", json={"title": "hijack"}).status_code == 404
    assert b.delete(f"/api/fusion/conversations/{cid}").status_code == 404
    # Bob's list does not include Alice's conversation
    assert b.get("/api/fusion/conversations").get_json()["conversations"] == []
    # Alice still owns it intact
    assert a.get(f"/api/fusion/conversations/{cid}").status_code == 200


# ---------------------------------------------------------------------------
# 8. COGS billed to the account, but the ¥ balance is NOT charged (free)
# ---------------------------------------------------------------------------
def test_cost_billed_but_balance_untouched(env, monkeypatch):
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    before = _account(env["store"], A_EMAIL)
    client = _client_for(env["store"], A_EMAIL)
    client.post("/api/fusion/chat", json={
        "text": "hi", "models": ["deepseek-v4-flash", "qwen-plus"]})
    after = _account(env["store"], A_EMAIL)
    assert after["balance"] == before["balance"], "Fusion must not charge the balance"
    assert after["spent"] > before["spent"], "COGS should still be metered"


# ---------------------------------------------------------------------------
# 9. Failure handling — partial keeps the breakdown; total writes a 'failed' marker
# ---------------------------------------------------------------------------
def test_partial_failure_keeps_breakdown(env, monkeypatch):
    monkeypatch.setattr(app.providers, "gateway_chat",
                        _fake_gateway(fail={"qwen-plus"}))
    client = _client_for(env["store"], A_EMAIL)
    r = client.post("/api/fusion/chat", json={
        "text": "hi", "models": ["deepseek-v4-flash", "qwen-plus"]})
    body = r.get_json()
    cid = body["conversation_id"]
    assert body["fused"], "a surviving model should still produce a fused answer"
    msgs = metrics_store.get_fusion_messages(cid)
    asst = msgs[1]
    assert asst["status"] == "ok"
    oks = {a["model"]: a["ok"] for a in asst["answers"]}
    assert oks["deepseek-v4-flash"] is True and oks["qwen-plus"] is False


def test_total_failure_writes_failed_marker(env, monkeypatch):
    monkeypatch.setattr(app.providers, "gateway_chat",
                        _fake_gateway(fail={"deepseek-v4-flash", "qwen-plus"}))
    client = _client_for(env["store"], A_EMAIL)
    r = client.post("/api/fusion/chat", json={
        "text": "hi", "models": ["deepseek-v4-flash", "qwen-plus"]})
    body = r.get_json()
    assert body["fused"] is None
    msgs = metrics_store.get_fusion_messages(body["conversation_id"])
    assert len(msgs) == 2 and msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant" and msgs[1]["status"] == "failed"
    assert (msgs[1]["content"] or "") == ""   # no half-saved success
    # a failed marker must not re-enter context on the next turn
    ctx = app._fusion_history_messages(msgs)
    assert [m["content"] for m in ctx] == ["hi"]


# ---------------------------------------------------------------------------
# 10. Rename + soft-delete
# ---------------------------------------------------------------------------
def test_rename_and_soft_delete(env, monkeypatch):
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    client = _client_for(env["store"], A_EMAIL)
    cid = client.post("/api/fusion/chat", json={"text": "hi", "models": ["deepseek-v4-flash"]}
                      ).get_json()["conversation_id"]
    assert client.patch(f"/api/fusion/conversations/{cid}",
                        json={"title": "我的对话"}).status_code == 200
    assert client.get(f"/api/fusion/conversations/{cid}").get_json()["title"] == "我的对话"
    # soft-delete hides it from the list and 404s on fetch
    assert client.delete(f"/api/fusion/conversations/{cid}").status_code == 200
    assert client.get("/api/fusion/conversations").get_json()["conversations"] == []
    assert client.get(f"/api/fusion/conversations/{cid}").status_code == 404


# ---------------------------------------------------------------------------
# 11. LOCAL_MODE hides the persistent Fusion surface (cloud-only)
# ---------------------------------------------------------------------------
def test_local_mode_blocks_persistent_fusion(env, monkeypatch):
    monkeypatch.setattr(app, "LOCAL_MODE", True)
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    client = _client_for(env["store"], A_EMAIL)
    assert client.post("/api/fusion/chat",
                       json={"text": "hi", "models": ["deepseek-v4-flash"]}).status_code == 404
    assert client.get("/api/fusion/conversations").status_code == 404
    # the public model list stays reachable (names only)
    assert client.get("/api/fusion/models").status_code == 200

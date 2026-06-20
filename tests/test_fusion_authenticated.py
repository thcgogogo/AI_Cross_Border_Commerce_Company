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
    # The trim keeps the last 2 messages = [assistant r2, user t3]; normalization then
    # drops the leading assistant (a context must START on a user turn, else Anthropic
    # 400s), leaving just the newest user turn — provider-valid and still bounded.
    sent = captured["calls"][0]["messages"]
    assert [m["role"] for m in sent] == ["user"] and sent[0]["content"] == "t3"
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


def test_models_cap_reflects_tier(env, monkeypatch):
    """The public picker advertises the cap the server will actually honor, so the UI can't
    silently cap a paid account at the free council size: free/anon → free cap, paid → full
    cap."""
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    free_cap, full_cap = config.fusion_free_model_cap(), config.fusion_full_model_cap()
    assert full_cap > free_cap  # the bug only matters when paid is genuinely wider
    # anonymous → free cap
    anon = app.app.test_client().get("/api/fusion/models").get_json()
    assert anon["model_cap"] == free_cap and anon["account_tier"] == "free"
    # free signed-in account → free cap
    client = _client_for(env["store"], A_EMAIL)
    assert client.get("/api/fusion/models").get_json()["model_cap"] == free_cap
    # paid account → full cap
    monkeypatch.setattr(app.metrics_store, "effective_plan",
                        lambda *a, **k: ("pro", None, False))
    paid = client.get("/api/fusion/models").get_json()
    assert paid["model_cap"] == full_cap and paid["account_tier"] == "full"


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


def test_failed_turn_does_not_poison_alternation(env, monkeypatch):
    """Regression: a turn where every model fails leaves a 'failed' assistant marker and a
    dangling user turn. The NEXT turn must not feed the models two consecutive user
    messages — Anthropic-family models reject that with a 400, and because the bad shape is
    rebuilt every turn it would permanently wedge the conversation."""
    # Turn 1: the only model fails → user persisted + assistant 'failed' marker persisted.
    monkeypatch.setattr(app.providers, "gateway_chat",
                        _fake_gateway(fail={"deepseek-v4-flash"}))
    client = _client_for(env["store"], A_EMAIL)
    cid = client.post("/api/fusion/chat", json={"text": "q1", "models": ["deepseek-v4-flash"]}
                      ).get_json()["conversation_id"]

    # Turn 2: a working model — capture exactly what reaches the gateway.
    captured = {}
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway(captured))
    r2 = client.post("/api/fusion/chat", json={"conversation_id": cid, "text": "q2",
                                               "models": ["deepseek-v4-flash"]})
    assert r2.status_code == 200
    sent = captured["calls"][0]["messages"]
    roles = [m["role"] for m in sent]
    assert roles[0] == "user", f"context must start on a user turn: {roles}"
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)), roles
    # the dangling 'q1' is dropped — context is just the new question.
    assert [m["content"] for m in sent] == ["q2"]
    # every turn is still kept in the DB (2 from the failed turn + 2 from this one).
    assert len(metrics_store.get_fusion_messages(cid)) == 4


def test_normalize_alternation_helper():
    """Unit-level guarantee for the context normalizer used by both chat paths."""
    norm = app._fusion_normalize_alternation
    U = lambda c: {"role": "user", "content": c}       # noqa: E731
    A = lambda c: {"role": "assistant", "content": c}  # noqa: E731
    # a dangling user (its assistant turn was dropped) is removed
    assert norm([U("a"), A("b"), U("c"), U("d")]) == [U("a"), A("b"), U("d")]
    # a context left starting with an assistant (after clamping) drops the lead assistant
    assert norm([A("b"), U("c")]) == [U("c")]
    # a clean alternating context is unchanged
    assert norm([U("a"), A("b"), U("c")]) == [U("a"), A("b"), U("c")]
    # a single user turn is valid as-is; two consecutive users keep the newest
    assert norm([U("a")]) == [U("a")]
    assert norm([U("a"), U("b")]) == [U("b")]
    assert norm([]) == []


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
# 11b. Carry an anonymous teaser conversation into per-account memory on login
# ---------------------------------------------------------------------------
def test_import_anonymous_transcript(env, monkeypatch):
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    client = _client_for(env["store"], A_EMAIL)
    transcript = [
        {"role": "user", "content": "出口退税是什么？"},
        {"role": "assistant", "content": "出口退税是…的政策。",
         "answers": [{"model": "deepseek-v4-flash", "label": "DSF", "ok": True, "text": "A"}],
         "by_label": "DeepSeek V4 Flash", "note": "Fused from 1."},
    ]
    r = client.post("/api/fusion/conversations/import", json={"messages": transcript})
    assert r.status_code == 200, r.get_data(as_text=True)
    cid = r.get_json()["conversation_id"]
    # the transcript is now a persisted per-account conversation (decrypts back)
    msgs = metrics_store.get_fusion_messages(cid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "出口退税是什么？"
    assert msgs[1]["answers"][0]["label"] == "DSF"
    # it shows in the account's rail, and a follow-up turn continues IN it (full memory)
    listing = client.get("/api/fusion/conversations").get_json()["conversations"]
    assert any(c["id"] == cid for c in listing)
    follow = client.post("/api/fusion/chat", json={
        "conversation_id": cid, "text": "再详细点", "models": ["deepseek-v4-flash"]})
    assert follow.status_code == 200 and follow.get_json()["conversation_id"] == cid
    assert len(metrics_store.get_fusion_messages(cid)) == 4   # 2 imported + 2 new


def test_import_requires_login_and_rejects_empty(env):
    anon = app.app.test_client()
    assert anon.post("/api/fusion/conversations/import",
                     json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 401
    client = _client_for(env["store"], A_EMAIL)
    assert client.post("/api/fusion/conversations/import",
                       json={"messages": []}).status_code == 400


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


# ---------------------------------------------------------------------------
# 12. Batch-2 audit fixes (persistence / crypto / fuser context / import / anon)
# ---------------------------------------------------------------------------
def test_append_rejects_soft_deleted_conversation(env, monkeypatch):
    """A late append to a conversation soft-deleted mid-flight is a no-op — it can't land
    turns (and bill COGS) into a thread the user can no longer see."""
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    client = _client_for(env["store"], A_EMAIL)
    cid = client.post("/api/fusion/chat", json={"text": "hi", "models": ["deepseek-v4-flash"]}
                      ).get_json()["conversation_id"]
    before = len(metrics_store.get_fusion_messages(cid))
    acc_id = env["store"].find_by_email(A_EMAIL)["id"]
    assert metrics_store.soft_delete_fusion_conversation(acc_id, cid)
    assert metrics_store.append_fusion_message(cid, "user", "late") is None
    assert len(metrics_store.get_fusion_messages(cid)) == before


def test_strict_decrypt_drops_foreign_ciphertext(env):
    """A value encrypted under a key that's been rotated out of FERNET_KEYS is token-shaped
    but undecryptable here → strict decrypt returns None, never the raw 'gAAAAA…' string."""
    foreign = Fernet(Fernet.generate_key()).encrypt(b"secret").decode()
    assert foreign.startswith("gAAAAA")
    assert crypto.decrypt_string_strict(foreign) is None
    assert crypto.decrypt_string_strict(crypto.encrypt_string("hi")) == "hi"  # active key
    assert crypto.decrypt_string_strict("plain text") == "plain text"          # legacy plaintext


def test_undecryptable_message_content_not_surfaced(env):
    """If a stored turn's content can't be decrypted (key rotated out), the read path drops
    it to '' — it never reaches the UI nor re-enters the next turn's model context."""
    acc_id = env["store"].find_by_email(A_EMAIL)["id"]
    cid = metrics_store.create_fusion_conversation(acc_id, model_tier="free")
    metrics_store.append_fusion_message(cid, "user", "hello")
    foreign = Fernet(Fernet.generate_key()).encrypt("乱码".encode()).decode()
    with sqlite3.connect(env["dbpath"]) as conn:
        conn.execute("UPDATE fusion_messages SET content=? WHERE conversation_id=?",
                     (foreign, cid))
        conn.commit()
    msgs = metrics_store.get_fusion_messages(cid)
    assert msgs and msgs[0]["content"] == "" and not msgs[0]["content"].startswith("gAAAAA")
    assert app._fusion_history_messages(msgs) == []   # ciphertext can't re-enter context


def test_fuser_receives_conversation_history(env, monkeypatch):
    """On a follow-up, the fuser is given the prior conversation (not just the bare last
    turn) so it can reconcile a history-dependent answer."""
    captured = {}
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway(captured))
    client = _client_for(env["store"], A_EMAIL)
    cid = client.post("/api/fusion/chat", json={
        "text": "首都是哪", "models": ["deepseek-v4-flash", "qwen-plus"]}).get_json()["conversation_id"]
    captured["calls"].clear()
    client.post("/api/fusion/chat", json={
        "conversation_id": cid, "text": "再详细点", "models": ["deepseek-v4-flash", "qwen-plus"]})
    fuse_calls = [c for c in captured["calls"]
                  if any("Fusion judge" in m.get("content", "") for m in c["messages"])]
    assert fuse_calls, "no fuser call was captured"
    user_msg = next(m["content"] for m in fuse_calls[0]["messages"] if m["role"] == "user")
    assert "首都是哪" in user_msg and "再详细点" in user_msg


def test_bad_models_dont_create_orphan_conversation(env, monkeypatch):
    """A paid request whose models are all invalid 400s WITHOUT leaving an orphan empty
    conversation behind in the rail."""
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    monkeypatch.setattr(app.metrics_store, "effective_plan",
                        lambda *a, **k: ("pro", None, False))  # paid: no allowlist fallback
    client = _client_for(env["store"], A_EMAIL)
    r = client.post("/api/fusion/chat", json={"text": "hi", "models": ["no-such-model-xyz"]})
    assert r.status_code == 400
    assert client.get("/api/fusion/conversations").get_json()["conversations"] == []


def test_import_keeps_newest_turns(env, monkeypatch):
    """Carry-over keeps the MOST RECENT turns (not the oldest), so the latest question is
    never dropped when the transcript exceeds the import cap."""
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    client = _client_for(env["store"], A_EMAIL)
    cap = 2 * config.fusion_free_anon_turns() + 2
    transcript = [{"role": "user", "content": f"q{i}"} for i in range(cap + 3)]
    cid = client.post("/api/fusion/conversations/import",
                      json={"messages": transcript}).get_json()["conversation_id"]
    contents = [m["content"] for m in metrics_store.get_fusion_messages(cid)]
    assert len(contents) == cap
    assert contents[-1] == f"q{cap + 2}"                  # newest question survived
    assert "q0" not in contents and "q1" not in contents  # oldest dropped


def test_import_marks_failed_turn(env, monkeypatch):
    """A failed teaser turn is imported as 'failed' so it never re-enters model context."""
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    client = _client_for(env["store"], A_EMAIL)
    transcript = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "(no answer)", "failed": True,
         "answers": [{"model": "deepseek-v4-flash", "label": "DSF", "ok": False, "error": "x"}]},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "real answer", "by_label": "DSF", "note": "Fused from 1.",
         "answers": [{"model": "deepseek-v4-flash", "label": "DSF", "ok": True, "text": "A"}]},
    ]
    cid = client.post("/api/fusion/conversations/import",
                      json={"messages": transcript}).get_json()["conversation_id"]
    msgs = metrics_store.get_fusion_messages(cid)
    assert [(m["role"], m["status"]) for m in msgs] == [
        ("user", "ok"), ("assistant", "failed"), ("user", "ok"), ("assistant", "ok")]
    assert [m["content"] for m in app._fusion_history_messages(msgs)] == ["q1", "q2", "real answer"]


def test_anon_failed_turn_not_charged(env, monkeypatch):
    """An anonymous turn where every model fails returns fused=None and must NOT spend a
    free turn — else a misconfigured deployment gates the visitor having answered nothing."""
    monkeypatch.setattr(app.providers, "gateway_chat",
                        _fake_gateway(fail={"deepseek-v4-flash"}))
    anon = app.app.test_client()
    before = anon.get("/api/fusion/quota").get_json()["free_remaining"]
    r = anon.post("/api/fusion/chat", json={
        "messages": [{"role": "user", "content": "hi"}], "models": ["deepseek-v4-flash"]})
    assert r.status_code == 200 and r.get_json()["fused"] is None
    assert r.get_json()["free_remaining"] == before, "a failed teaser turn must not be charged"


def test_anon_free_turns_enforced_server_side(env, monkeypatch):
    """The teaser cap is enforced SERVER-SIDE (anon_fusion_usage, keyed on the anon_id
    cookie) — not the editable session cookie. The turn over the cap 401s with
    login_required; a different visitor gets its own fresh allowance."""
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway())
    monkeypatch.setenv("FUSION_FREE_ANON_TURNS", "2")
    anon = app.app.test_client()
    payload = {"messages": [{"role": "user", "content": "hi"}], "models": ["deepseek-v4-flash"]}
    r1 = anon.post("/api/fusion/chat", json=payload)
    assert r1.status_code == 200 and r1.get_json()["free_remaining"] == 1
    r2 = anon.post("/api/fusion/chat", json=payload)
    assert r2.status_code == 200 and r2.get_json()["free_remaining"] == 0
    r3 = anon.post("/api/fusion/chat", json=payload)
    assert r3.status_code == 401 and r3.get_json().get("login_required") is True
    # a different visitor (fresh client → fresh anon_id) has its own allowance
    other = app.app.test_client()
    assert other.post("/api/fusion/chat", json=payload).status_code == 200


def test_reserve_and_refund_anon_turns(env):
    """The server-side teaser counter caps at the limit and refunds a returned turn; with
    no anon_id it never blocks (the per-IP limit + spend breaker are the backstops)."""
    aid = "anon-unit-xyz"
    assert metrics_store.reserve_anon_fusion_turn(aid, 2) == (True, 1)
    assert metrics_store.reserve_anon_fusion_turn(aid, 2) == (True, 0)
    assert metrics_store.reserve_anon_fusion_turn(aid, 2) == (False, 0)   # capped
    assert metrics_store.anon_fusion_remaining(aid, 2) == 0
    metrics_store.refund_anon_fusion_turn(aid)
    assert metrics_store.anon_fusion_remaining(aid, 2) == 1               # gave one back
    assert metrics_store.reserve_anon_fusion_turn(aid, 2) == (True, 0)
    assert metrics_store.reserve_anon_fusion_turn(None, 2)[0] is True
    assert metrics_store.anon_fusion_remaining(None, 2) == 2


def test_admin_gets_full_fusion_fanout(env, monkeypatch):
    """A founder/admin is NOT tier-clamped: a custom selection of models OUTSIDE the cheap
    free allowlist fans out across ALL of them (not silently reduced to one), and the picker
    advertises the full cap. (Regression for the founder seeing only DeepSeek answer.)"""
    monkeypatch.setenv("ADMIN_EMAILS", A_EMAIL)
    captured = {}
    monkeypatch.setattr(app.providers, "gateway_chat", _fake_gateway(captured))
    client = _client_for(env["store"], A_EMAIL)
    # the picker advertises full tier for the admin
    resp = client.get("/api/fusion/models").get_json()
    assert resp["account_tier"] == "full" and resp["model_cap"] == config.fusion_full_model_cap()
    # a custom selection of models OUTSIDE the free allowlist all run (no clamp)
    picks = ["deepseek-v4-flash", "MiniMax-M3", "qwen3.7-max"]
    assert not set(picks) <= set(config.fusion_free_models()), "premise: picks are non-free"
    client.post("/api/fusion/chat", json={"text": "hi", "models": picks})
    called = {c["model"] for c in captured["calls"]}
    assert set(picks) <= called, f"admin fan-out was clamped to: {called}"

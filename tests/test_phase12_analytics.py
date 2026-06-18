"""First-party, founder-only analytics layer — acceptance tests (hermetic).

Covers the plan's checklist: track() writes a row + is best-effort (a DB failure
never breaks the request); the page_view hook excludes /healthz / static / /api/*;
the anon_id cookie is set on first visit; IPs are stored HASHED never raw; signup
links the anon_id to the new account; /api/fusion/chat records the anonymous query;
GET /api/analytics is 403 for a non-admin and 200 for an admin; and the new
admin/queries aggregations return correct counts against seeded data.

No network, no dependence on the developer's .env (conftest disables the limiter +
no-ops load_dotenv).
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — one-time idempotent module boot
from admin import queries  # noqa: E402
from agent_team import analytics, config, db  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.models import Event  # noqa: E402


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


def _events(name=None):
    with db.session_scope() as s:
        q = s.query(Event)
        if name:
            q = q.filter(Event.name == name)
        return [{"name": e.name, "account_id": e.account_id, "anon_id": e.anon_id,
                 "path": e.path, "ip_hash": e.ip_hash, "props": e.props}
                for e in q.all()]


# ---------------------------------------------------------------------------
# 1. track() writes a row; invalid name → None; a DB failure never raises
# ---------------------------------------------------------------------------
def test_track_writes_row_and_is_best_effort(fresh_db, monkeypatch):
    assert analytics.track("page_view", anon_id="a1", path="/") is not None
    assert analytics.track("not_a_kind") is None          # invalid name → None, no raise
    assert len(_events("page_view")) == 1

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(analytics.db, "session_scope", boom)
    assert analytics.track("page_view", anon_id="a2") is None   # swallowed, returns None


def test_request_succeeds_even_if_track_raises(fresh_db, monkeypatch):
    monkeypatch.setattr(app.analytics, "track", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    r = app.app.test_client().get("/")
    assert r.status_code == 200          # the after_request hook swallows analytics errors


# ---------------------------------------------------------------------------
# 2. anon_id cookie set on first visit
# ---------------------------------------------------------------------------
def test_anon_id_cookie_set_on_first_visit(fresh_db):
    r = app.app.test_client().get("/")
    setc = r.headers.get("Set-Cookie", "")
    assert "anon_id=" in setc and "HttpOnly" in setc and "SameSite=Lax" in setc


# ---------------------------------------------------------------------------
# 3. page_view excludes /healthz, static, /api/*; records real page routes
# ---------------------------------------------------------------------------
def test_pageview_exclusions_and_capture(fresh_db):
    c = app.app.test_client()
    c.get("/healthz")
    c.get("/style.css")            # static serves at root (static_url_path="")
    c.get("/api/industries")       # any /api/* is excluded
    paths = {e["path"] for e in _events("page_view")}
    assert "/healthz" not in paths and "/style.css" not in paths
    assert not any(p and p.startswith("/api/") for p in paths)
    c.get("/")
    assert "/" in {e["path"] for e in _events("page_view")}


# ---------------------------------------------------------------------------
# 4. IPs are stored hashed, never raw
# ---------------------------------------------------------------------------
def test_ip_is_hashed_never_raw(fresh_db):
    app.app.test_client().get("/", environ_overrides={"REMOTE_ADDR": "203.0.113.7"})
    rows = _events("page_view")
    assert rows, "a page_view should have been recorded"
    ip_hash = rows[-1]["ip_hash"]
    assert ip_hash and ip_hash != "203.0.113.7" and len(ip_hash) == 64
    assert ip_hash == analytics.hash_ip("203.0.113.7")


# ---------------------------------------------------------------------------
# 5. signup links the current anon_id to the new account
# ---------------------------------------------------------------------------
def test_signup_links_anon_id_to_account(fresh_db, tmp_path, monkeypatch):
    _store(tmp_path)                                   # empty store; signup creates the account
    monkeypatch.setattr(config, "phone_verification_required", lambda: False)
    c = app.app.test_client()
    c.set_cookie("anon_id", "anonVISITOR")             # a prior anonymous visitor
    r = c.post("/api/signup", json={"name": "Boss", "email": "boss@corp.com",
                                    "password": "password1"})
    assert r.status_code == 200, r.get_data(as_text=True)
    created = _events("account_created")
    assert len(created) == 1
    assert created[0]["anon_id"] == "anonVISITOR"      # the pre-signup trail resolves
    assert created[0]["account_id"] and created[0]["account_id"].startswith("cus_")


# ---------------------------------------------------------------------------
# 6. /api/fusion/chat records the anonymous front-door query (account_id NULL)
# ---------------------------------------------------------------------------
def test_fusion_query_records_anonymous_event(fresh_db, monkeypatch):
    def fake_gateway(model_id, messages, max_tokens=1024):
        return {"text": f"[{model_id}] ok", "input_tokens": 3, "output_tokens": 4,
                "model": model_id, "provider": config.provider_for(model_id)}
    monkeypatch.setattr(app.providers, "gateway_chat", fake_gateway)
    r = app.app.test_client().post("/api/fusion/chat", json={
        "messages": [{"role": "user", "content": "你好"}],
        "models": ["deepseek-v4-flash"], "fuser": "auto"})
    assert r.status_code == 200, r.get_data(as_text=True)
    fq = _events("fusion_query")
    assert len(fq) == 1 and fq[0]["account_id"] is None and fq[0]["anon_id"]
    assert "deepseek-v4-flash" in (fq[0]["props"] or "")


# ---------------------------------------------------------------------------
# 7. /api/analytics — 403 for a non-admin, 200 for an admin
# ---------------------------------------------------------------------------
def test_analytics_endpoint_founder_gated(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")
    monkeypatch.delenv("LOCAL_MODE", raising=False)
    store = _store(tmp_path, **{"user@corp.com": "password1", "founder@corp.com": "password1"})
    assert app.app.test_client().get("/api/analytics").status_code == 403   # anonymous → not admin
    assert _login(store, "user@corp.com").get("/api/analytics").status_code == 403
    r = _login(store, "founder@corp.com").get("/api/analytics")
    assert r.status_code == 200
    body = r.get_json()
    for key in ("page_views", "sources", "funnel", "fusion", "packs_agents", "runs_breakdown"):
        assert key in body


# ---------------------------------------------------------------------------
# 8. The new queries return correct counts against seeded data
# ---------------------------------------------------------------------------
def test_queries_match_seeded_counts(fresh_db, tmp_path):
    _store(tmp_path, **{"a@corp.com": "password1", "b@corp.com": "password1"})  # 2 signups
    for i in range(4):
        analytics.track("page_view", anon_id="anon%d" % (i % 2), path="/")     # 4 visits, 2 sessions
    analytics.track("fusion_query", anon_id="anon0", props={"models": ["gpt-5.1"]})
    analytics.track("phone_verified", anon_id="anon0")
    analytics.track("report_dim", account_id="cus_x",
                    props={"industry_key": "trading_company", "specialists": ["data_analyst"]})

    pv = queries.page_views_per_day(days=7)
    assert sum(d["visits"] for d in pv) == 4 and pv[-1]["sessions"] == 2
    assert queries.fusion_usage(days=7)["total"] == 1

    stages = {s["key"]: s["count"] for s in queries.acquisition_funnel(days=7)["stages"]}
    assert stages["visitors"] == 2 and stages["fusion"] == 1
    assert stages["verified"] == 1 and stages["signups"] == 2

    pa = queries.pack_and_agent_usage(days=7)
    assert pa["packs"][0]["pack"] == "trading_company" and pa["agents"][0]["agent"] == "data_analyst"
    assert queries.scheduled_vs_manual(days=7) == {"total": 0, "scheduled": 0, "manual": 0}

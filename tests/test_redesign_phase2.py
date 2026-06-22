"""UI redesign Phase 2 — design system & shell acceptance tests (plan §7).

The phase is mostly frontend (verified by hand in the running app); these tests
pin down the backend/server-side part of its acceptance criteria:

  1. "/" serves the NEW shell: Chinese-first (lang="zh-CN"), an ES-module boot
     (js/main.js), and none of the old "AI model platform" hero copy (Get API
     Key / One API key for every model) — the shell must not resurrect the
     machinery pitch for customers.
  2. The SPA path router (§5): /new /reports /reports/<id> /schedule /brain
     /account and the founder pages serve the same shell to a logged-in
     session, and 302 logged-out visitors to /login — the /company-brain
     pattern, extended to the new IA.
  3. Unknown /founder/* pages 404 instead of silently serving the shell.

Same isolation pattern as the other redesign suites: a fresh CustomerStore
wired into app.CUSTOMERS, sessions established directly on the test client.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot is one-time & idempotent
from agent_team import db  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402


@pytest.fixture
def logged_in_client(tmp_path):
    """A test client with a fresh account's session cookie established, on a
    throwaway SQLite DB (same isolation pattern as the Phase 1 suite)."""
    db.init_db(str(tmp_path / "phase2.db"))
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    store.create_customer([], name="boss", starting_balance=100.0,
                          email="boss@corp.com", password="password1")
    app.CUSTOMERS = store
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email("boss@corp.com")["id"]
    return client


# ---------------------------------------------------------------------------
# 1. The new shell
# ---------------------------------------------------------------------------
def test_root_serves_chinese_module_shell():
    html = app.app.test_client().get("/").get_data(as_text=True)

    assert 'lang="zh-CN"' in html                      # Chinese-first shell
    assert 'type="module"' in html and "js/main.js" in html  # ES-module boot
    assert 'src="app.js"' not in html                  # the old monolith is gone

    # No customer-visible machinery pitch in the served shell (plan §2 rule 1).
    for forbidden in ("Get API Key", "One API key", "Explore Models and Pricing",
                      "Unified Agent Gateway"):
        assert forbidden not in html


# ---------------------------------------------------------------------------
# 2. SPA paths: logged-out → /login, logged-in → the shell
# ---------------------------------------------------------------------------
SPA_PATHS = ["/workflow", "/new", "/reports", "/reports/7", "/schedule", "/brain",
             "/account", "/founder/team", "/founder/costs", "/founder/models",
             "/founder/fusion", "/founder/api"]


@pytest.mark.parametrize("path", SPA_PATHS)
def test_spa_paths_redirect_logged_out_visitors_to_login(path):
    resp = app.app.test_client().get(path)
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


def test_spa_paths_serve_shell_to_logged_in_session(logged_in_client):
    for path in SPA_PATHS:
        resp = logged_in_client.get(path)
        assert resp.status_code == 200, path
        html = resp.get_data(as_text=True)
        assert 'id="view"' in html, path  # the SPA shell, not an error page


# ---------------------------------------------------------------------------
# 3. Unknown founder pages 404 (no silent shell for arbitrary paths)
# ---------------------------------------------------------------------------
def test_unknown_founder_page_404s(logged_in_client):
    assert logged_in_client.get("/founder/nonsense").status_code == 404

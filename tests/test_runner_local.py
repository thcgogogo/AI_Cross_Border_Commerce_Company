"""WS01 — local desktop daemon acceptance tests (Phase 0).

Covers the supervisor/runner package, the filesystem allowlist chokepoint
(agent_team/workspace.py), the LOCAL_MODE loopback hardening in app.py (founder
lockdown, payment/scheduler route hiding, per-session CSRF/origin token), and the
path-aware /api/run/local intake.

Hermetic, same style as the other suites: a throwaway SQLite brain per test, a
fresh CustomerStore wired into app.CUSTOMERS, sessions set directly on the client,
no network / no Anthropic key / no GUI. LOCAL_MODE is toggled per test via
monkeypatch on the `app` module globals (auto-reverted), so the rest of the suite —
which runs with LOCAL_MODE off — is untouched.
"""
import os
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot is one-time & idempotent
from agent_team import config, db, workspace  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures (mirror test_redesign_phase1.py)
# ---------------------------------------------------------------------------
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


def _client_for(store, email=None):
    client = app.app.test_client()
    if email:
        with client.session_transaction() as sess:
            sess["customer_id"] = store.find_by_email(email)["id"]
    return client


@pytest.fixture
def local_mode(monkeypatch):
    """Turn the app into the desktop daemon for one test (auto-reverted)."""
    monkeypatch.setattr(app, "LOCAL_MODE", True)
    monkeypatch.setattr(app, "LOCAL_CSRF_TOKEN", "testtoken")
    monkeypatch.setattr(workspace, "_roots", [])  # default-deny baseline
    yield "testtoken"


_TOK = {"X-Tangent-Local-Token": "testtoken"}


# ===========================================================================
# 1. agent_team/workspace.py — the default-deny path chokepoint (pure unit)
# ===========================================================================
def test_workspace_allows_path_inside_approved_root(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    f = root / "data.csv"
    f.write_text("a,b\n1,2\n")
    real = workspace.assert_in_workspace(str(f), roots=[str(root)])
    assert os.path.realpath(str(f)) == real


def test_workspace_rejects_dotdot_escape(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    escape = root / ".." / "secret.txt"
    with pytest.raises(workspace.WorkspaceError):
        workspace.assert_in_workspace(str(escape), roots=[str(root)])


def test_workspace_rejects_symlink_pointing_outside(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    link = root / "link.csv"
    os.symlink(str(secret), str(link))   # symlink lives INSIDE the root...
    # ...but resolves OUTSIDE it, so realpath-before-commonpath must reject it.
    with pytest.raises(workspace.WorkspaceError):
        workspace.assert_in_workspace(str(link), roots=[str(root)])


def test_workspace_default_deny_when_no_roots(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("1")
    with pytest.raises(workspace.WorkspaceError):
        workspace.assert_in_workspace(str(f), roots=[])
    with pytest.raises(workspace.WorkspaceError):
        workspace.assert_in_workspace(str(f), roots=None)  # process roots empty here


def test_workspace_rejects_nonexistent_path(tmp_path):
    root = tmp_path / "approved"
    root.mkdir()
    with pytest.raises(workspace.WorkspaceError):
        workspace.assert_in_workspace(str(root / "ghost.csv"), roots=[str(root)])


def test_workspace_set_and_add_roots_roundtrip(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    workspace.set_roots([str(a)])
    assert os.path.realpath(str(a)) in workspace.approved_roots()
    workspace.add_root(str(b))
    assert os.path.realpath(str(b)) in workspace.approved_roots()
    workspace.set_roots([])  # reset shared state for other tests


# ===========================================================================
# 2. runner package — port picker + side-effect-free import
# ===========================================================================
def test_pick_free_port_returns_bindable_port():
    import socket
    from agent_team import runner
    port = runner.pick_free_port()
    assert isinstance(port, int) and 1024 < port < 65536
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))   # actually bindable right after
    finally:
        s.close()


def test_importing_runner_is_side_effect_free():
    import importlib
    from agent_team import runner
    importlib.reload(runner)
    # No server thread spun up by import.
    assert not any("tangent-waitress" in t.name for t in threading.enumerate())
    assert callable(runner.main) and hasattr(runner, "Supervisor")


# ===========================================================================
# 3. tray menu — pure data spec (no GUI, never calls .run())
# ===========================================================================
def test_tray_menu_spec_items_quit_and_pause():
    from agent_team.runner import tray, state
    state.set_paused(False)
    quit_called = {"n": 0}
    spec = tray.build_menu_spec(
        on_open=lambda: None, on_pair=lambda: None, on_watched=lambda: None,
        on_pause_toggle=state.toggle_paused,
        on_quit=lambda: quit_called.__setitem__("n", quit_called["n"] + 1))
    labels = [label for (label, _cb, _chk) in spec]
    assert labels == [tray.LABEL_OPEN, tray.LABEL_PAIR, tray.LABEL_WATCHED,
                      tray.LABEL_PAUSE, tray.LABEL_QUIT]
    # Pause is the checkable item and toggles the shared flag.
    pause = next(s for s in spec if s[0] == tray.LABEL_PAUSE)
    assert pause[2] is True
    pause[1]()
    assert state.is_paused() is True
    pause[1]()
    assert state.is_paused() is False
    # Quit invokes its callback.
    next(s for s in spec if s[0] == tray.LABEL_QUIT)[1]()
    assert quit_called["n"] == 1


# ===========================================================================
# 4. LOCAL_MODE founder lockdown — the desktop user is never an admin
# ===========================================================================
def test_local_mode_locks_founder_routes(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")
    store = _store(tmp_path, **{"founder@corp.com": "pw123456"})

    # LOCAL_MODE OFF: the admin can reach the founder catalog (proves the lockdown,
    # not a missing session, is what blocks it under LOCAL_MODE).
    monkeypatch.setattr(app, "LOCAL_MODE", False)
    admin = _client_for(store, "founder@corp.com")
    assert admin.get("/api/models").status_code == 200

    # LOCAL_MODE ON: the SAME admin is forced non-admin → 403.
    monkeypatch.setattr(app, "LOCAL_MODE", True)
    assert _is_admin_false_now()
    admin2 = _client_for(store, "founder@corp.com")
    assert admin2.get("/api/models").status_code == 403


def _is_admin_false_now():
    return app._is_admin({"email": "founder@corp.com"}) is False


# ===========================================================================
# 5. LOCAL_MODE hides payment + scheduler surfaces (404, not 403)
# ===========================================================================
def test_local_mode_hides_payment_and_scheduler_routes(fresh_db, tmp_path, local_mode):
    store = _store(tmp_path, **{"u@corp.com": "pw123456"})
    client = _client_for(store, "u@corp.com")
    assert client.get("/api/orders").status_code == 404
    assert client.get("/api/plans").status_code == 404
    assert client.get("/api/scheduled-jobs").status_code == 404
    assert client.post("/api/pay/create", json={}, headers=_TOK).status_code == 404


def test_payment_routes_present_when_not_local(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "LOCAL_MODE", False)
    store = _store(tmp_path, **{"u@corp.com": "pw123456"})
    client = _client_for(store, "u@corp.com")
    # /api/plans is public + present on the server build (definitely not 404).
    assert client.get("/api/plans").status_code != 404


# ===========================================================================
# 6. LOCAL_MODE CSRF/origin hardening on mutating /api/* calls
# ===========================================================================
def test_local_mode_requires_csrf_token_on_mutations(fresh_db, tmp_path, local_mode):
    store = _store(tmp_path, **{"u@corp.com": "pw123456"})
    client = _client_for(store, "u@corp.com")
    # Missing token → 403 (the hook fires before the route logic).
    assert client.post("/api/run/local", json={"goal": "x", "paths": []}).status_code == 403
    # Cross-origin Origin + valid token → 403 (DNS-rebinding defense).
    r = client.post("/api/run/local", json={"goal": "x", "paths": []},
                    headers={**_TOK, "Origin": "http://evil.example"})
    assert r.status_code == 403
    # A GET needs no token.
    assert client.get("/api/me").status_code == 200


def test_local_mode_injects_token_and_banner_into_index(fresh_db, tmp_path, local_mode):
    store = _store(tmp_path, **{"u@corp.com": "pw123456"})
    client = _client_for(store, "u@corp.com")
    html = client.get("/").get_data(as_text=True)
    assert "testtoken" in html                      # CSRF token substituted
    assert "local-privacy-banner" in html           # privacy banner present
    assert "window.__LOCAL_MODE__ = true" in html
    assert "__TANGENT_LOCAL_TOKEN__" not in html     # value placeholders fully substituted
    assert "__TANGENT_LOCAL_MODE__" not in html


# ===========================================================================
# 7. /api/run/local — the path-aware intake
# ===========================================================================
def test_run_local_404_when_not_local(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "LOCAL_MODE", False)
    store = _store(tmp_path, **{"u@corp.com": "pw123456"})
    client = _client_for(store, "u@corp.com")
    assert client.post("/api/run/local", json={"goal": "x", "paths": []}).status_code == 404


def test_run_local_401_without_session(fresh_db, tmp_path, local_mode):
    _store(tmp_path)  # no accounts; client has no session
    client = app.app.test_client()
    r = client.post("/api/run/local", json={"goal": "x", "paths": []}, headers=_TOK)
    assert r.status_code == 401


def test_run_local_rejects_path_outside_workspace(fresh_db, tmp_path, local_mode):
    store = _store(tmp_path, **{"u@corp.com": "pw123456"})
    client = _client_for(store, "u@corp.com")
    approved = tmp_path / "approved"; approved.mkdir()
    workspace.set_roots([str(approved)])
    outside = tmp_path / "outside.csv"
    outside.write_text("a,b\n1,2\n")
    r = client.post("/api/run/local",
                    json={"goal": "analyze", "paths": [str(outside)]}, headers=_TOK)
    assert r.status_code == 400
    workspace.set_roots([])


def test_run_local_rejects_executable_and_oversize(fresh_db, tmp_path, local_mode, monkeypatch):
    store = _store(tmp_path, **{"u@corp.com": "pw123456"})
    client = _client_for(store, "u@corp.com")
    approved = tmp_path / "approved"; approved.mkdir()
    workspace.set_roots([str(approved)])

    exe = approved / "evil.csv"            # .csv name, ELF magic bytes
    exe.write_bytes(b"\x7fELF" + b"\x00" * 64)
    assert client.post("/api/run/local", json={"goal": "go", "paths": [str(exe)]},
                       headers=_TOK).status_code == 400

    big = approved / "big.csv"
    big.write_text("a,b\n" + "1,2\n" * 50)
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES_PER_FILE", 8)
    assert client.post("/api/run/local", json={"goal": "go", "paths": [str(big)]},
                       headers=_TOK).status_code == 400
    workspace.set_roots([])


def test_run_local_paused_returns_503(fresh_db, tmp_path, local_mode):
    from agent_team.runner import state
    store = _store(tmp_path, **{"u@corp.com": "pw123456"})
    client = _client_for(store, "u@corp.com")
    state.set_paused(True)
    try:
        r = client.post("/api/run/local", json={"goal": "x", "paths": []}, headers=_TOK)
        assert r.status_code == 503
    finally:
        state.set_paused(False)


def test_run_local_happy_path_reads_file_in_place(fresh_db, tmp_path, local_mode, monkeypatch):
    store = _store(tmp_path, **{"u@corp.com": "pw123456"})
    client = _client_for(store, "u@corp.com")
    approved = tmp_path / "approved"; approved.mkdir()
    workspace.set_roots([str(approved)])
    csv = approved / "sales.csv"
    original = "region,sales\n华东,120\n华南,98\n"
    csv.write_text(original, encoding="utf-8")

    # Don't spawn a real LLM thread — assert the request reaches the team handoff.
    started = {"n": 0}
    monkeypatch.setattr(app, "_run_team", lambda *a, **k: started.__setitem__("n", 1))

    r = client.post("/api/run/local",
                    json={"goal": "分析这个销售表", "paths": [str(csv)]}, headers=_TOK)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json().get("job_id")
    # The user's ORIGINAL file is untouched (read in place, never encrypted/moved).
    assert csv.read_text(encoding="utf-8") == original
    workspace.set_roots([])

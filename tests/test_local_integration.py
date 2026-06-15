"""
test_local_integration.py — the daemon↔cloud glue that makes a local run use the
cloud-proxied RemoteRouter + the user's approved folders, end to end.

Covers: _local_remote_router() env gating, /api/run/local injecting the RemoteRouter
into the run, Orchestrator routing an injected router's cost through the run wrappers,
and base_agent resolving the daemon's workspace roots for its local tools.
"""
import os
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402
from agent_team import db, providers, workspace  # noqa: E402
from agent_team.base_agent import Agent  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.memory import SharedMemory  # noqa: E402
from agent_team.orchestrator import Orchestrator  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "brain.db"))
    yield


@pytest.fixture
def local_mode(monkeypatch):
    monkeypatch.setattr(app, "LOCAL_MODE", True)
    monkeypatch.setattr(app, "LOCAL_CSRF_TOKEN", "testtoken")
    monkeypatch.setattr(workspace, "_roots", [])
    yield


# ---------------------------------------------------------------------------
# _local_remote_router env gating
# ---------------------------------------------------------------------------
def test_remote_router_none_without_env(local_mode, monkeypatch):
    monkeypatch.delenv("TANGENT_CLOUD_URL", raising=False)
    monkeypatch.delenv("TANGENT_DEVICE_TOKEN", raising=False)
    assert app._local_remote_router() is None


def test_remote_router_built_from_env(local_mode, monkeypatch):
    monkeypatch.setenv("TANGENT_CLOUD_URL", "https://api.tangent.example")
    monkeypatch.setenv("TANGENT_DEVICE_TOKEN", "tng-dev-abc")
    rr = app._local_remote_router()
    assert isinstance(rr, providers.RemoteRouter)
    assert rr._endpoint == "https://api.tangent.example/v1/chat/completions"


def test_remote_router_none_when_not_local_mode(monkeypatch):
    monkeypatch.setattr(app, "LOCAL_MODE", False)
    monkeypatch.setenv("TANGENT_CLOUD_URL", "https://api.tangent.example")
    monkeypatch.setenv("TANGENT_DEVICE_TOKEN", "tng-dev-abc")
    assert app._local_remote_router() is None   # the cloud server never proxies to itself


# ---------------------------------------------------------------------------
# /api/run/local injects the RemoteRouter into the run
# ---------------------------------------------------------------------------
def test_run_local_injects_remote_router(fresh_db, tmp_path, local_mode, monkeypatch):
    monkeypatch.setenv("TANGENT_CLOUD_URL", "https://api.tangent.example")
    monkeypatch.setenv("TANGENT_DEVICE_TOKEN", "tng-dev-abc")
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    store.create_customer([], name="u", starting_balance=100.0,
                          email="u@corp.com", password="pw123456")
    app.CUSTOMERS = store

    approved = tmp_path / "approved"; approved.mkdir()
    workspace.set_roots([str(approved)])
    csv = approved / "sales.csv"
    csv.write_text("region,sales\n华东,120\n", encoding="utf-8")

    captured = {}
    done = threading.Event()

    def fake_run_team(*a, **k):
        captured["kwargs"] = k
        done.set()
    monkeypatch.setattr(app, "_run_team", fake_run_team)

    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email("u@corp.com")["id"]
    r = client.post("/api/run/local",
                    json={"goal": "分析", "paths": [str(csv)]},
                    headers={"X-Tangent-Local-Token": "testtoken"})
    assert r.status_code == 200
    assert done.wait(2)
    assert isinstance(captured["kwargs"].get("llm"), providers.RemoteRouter)
    workspace.set_roots([])


# ---------------------------------------------------------------------------
# Orchestrator routes an injected router's cost through the run wrappers
# ---------------------------------------------------------------------------
class _FakeRouter:
    def __init__(self):
        self.cost_listener = None
        self.token_manager = None


def test_orchestrator_wires_injected_router(fresh_db, tmp_path):
    mem = SharedMemory("job1", store_dir=str(tmp_path), goal="g",
                       output_dir=str(tmp_path / "out"))
    fake = _FakeRouter()
    sentinel_tm = object()
    orch = Orchestrator(mem, llm=fake, token_manager=sentinel_tm)
    assert orch.llm is fake                       # the injected router is used as-is
    assert callable(fake.cost_listener)           # …with the run's ¥-tally wrapper attached
    assert fake.token_manager is sentinel_tm       # …and the run's token_manager


# ---------------------------------------------------------------------------
# base_agent resolves the daemon's workspace roots for its local tools
# ---------------------------------------------------------------------------
def test_agent_approved_roots_uses_workspace(tmp_path):
    agent = Agent({"id": "x", "name": "X", "title": "T"}, object(), None)
    workspace.set_roots([str(tmp_path)])
    try:
        assert agent._approved_roots("/some/out") == [os.path.realpath(str(tmp_path))]
        workspace.set_roots([])
        assert agent._approved_roots("/some/out") == ["/some/out"]   # cloud fallback
    finally:
        workspace.set_roots([])

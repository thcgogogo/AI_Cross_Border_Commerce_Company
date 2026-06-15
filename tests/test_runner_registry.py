"""
test_runner_registry.py — WS08: the local agent registry loader.

The daemon pulls installed agent MANIFESTS from the cloud catalog, caches by
content_hash, and merges them into config.AGENTS so the framework instantiates them
with no new class. Hermetic: an injected fake `fetch` (no network/keychain), a tmp
cache dir, and a fake llm/memory to prove a merged agent is runnable. Domestic-safe
(no model resolution, no Anthropic key).
"""
import os
import sys
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import config  # noqa: E402
from agent_team.base_agent import Agent  # noqa: E402
from agent_team.runner.registry import LocalRegistry  # noqa: E402

_FOUNDER_ONLY = ("model", "input_per_mtok", "output_per_mtok",
                 "cost_budget_cny", "team_model_preset")


@pytest.fixture(autouse=True)
def restore_agents():
    """config.AGENTS is module-global; snapshot + restore so merge tests don't leak."""
    snap = list(config.AGENTS)
    reg_snap = set(config._REGISTERED_AGENT_IDS)
    yield
    config.AGENTS[:] = snap
    config._REGISTERED_AGENT_IDS.clear()
    config._REGISTERED_AGENT_IDS.update(reg_snap)


def _row(agent_id, prompt="You are a test agent.", ch="sha256:h1", version=1,
         tools=None, drop_prompt=False):
    manifest = {"agent_id": agent_id, "manifest_version": version,
                "name": agent_id.title(), "title": "T", "name_cn": "测试",
                "reports_to": "ceo", "effort": "high", "tools": tools or [],
                "system_prompt": prompt}
    if drop_prompt:
        manifest.pop("system_prompt")
    return {"agent_id": agent_id, "version": version, "content_hash": ch, "manifest": manifest}


class Catalog:
    """A swappable fake fetch: returns .rows, or raises when .fail is set."""
    def __init__(self, rows):
        self.rows = rows
        self.fail = False

    def __call__(self):
        if self.fail:
            raise urllib.error.URLError("network down")
        return self.rows


class FakeMemory:
    def log(self, *a, **k): pass
    def goal(self): return "do the thing"
    def documents(self): return []
    def snapshot(self): return {"artifacts": {}}
    def output_dir(self): return "/tmp/registry_test_out"
    def add_artifact(self, who, text): pass


class FakeLLM:
    def __init__(self):
        self.calls = []
        self.last_stop_reason = None

    def complete(self, **k):
        self.calls.append("complete")
        return "ok"

    def complete_with_tools(self, **k):
        self.calls.append("tools")
        return "ok"


# ---------------------------------------------------------------------------
# 1. A merged manifest agent is instantiable + runnable by the framework
# ---------------------------------------------------------------------------
def test_merged_agent_is_runnable(tmp_path):
    cat = Catalog([_row("data_analyst_remote", tools=[])])
    reg = LocalRegistry(cache_dir=str(tmp_path), fetch=cat)
    reg.sync()
    reg.apply_to_config(config)

    merged = config.get_agent("data_analyst_remote")
    assert merged is not None and merged["reports_to"] == "ceo"
    agent = Agent(merged, FakeMemory(), FakeLLM())
    agent.run("analyze this")
    assert agent.llm.calls == ["complete"]   # no tools → plain completion path


# ---------------------------------------------------------------------------
# 2. Content-hash caching is idempotent
# ---------------------------------------------------------------------------
def test_content_hash_caching_idempotent(tmp_path):
    cat = Catalog([_row("a_remote"), _row("b_remote", ch="sha256:b")])
    reg = LocalRegistry(cache_dir=str(tmp_path), fetch=cat)
    r1 = reg.sync()
    assert sorted(r1.added) == ["a_remote", "b_remote"]
    reg.apply_to_config(config)
    n = len(config.AGENTS)

    r2 = reg.sync()
    assert sorted(r2.unchanged) == ["a_remote", "b_remote"] and not r2.added and not r2.updated
    reg.apply_to_config(config)
    assert len(config.AGENTS) == n                  # no duplicates
    assert len([a for a in config.AGENTS if a["id"] == "a_remote"]) == 1


# ---------------------------------------------------------------------------
# 3 & 5. Re-pull on content/version change → re-merge with new prompt
# ---------------------------------------------------------------------------
def test_rePull_on_change(tmp_path):
    cat = Catalog([_row("c_remote", prompt="v1 prompt", ch="sha256:v1", version=1)])
    reg = LocalRegistry(cache_dir=str(tmp_path), fetch=cat)
    reg.sync(); reg.apply_to_config(config)
    assert "v1 prompt" in config.get_agent("c_remote")["system_prompt"]

    cat.rows = [_row("c_remote", prompt="v2 prompt", ch="sha256:v2", version=2)]
    res = reg.sync()
    assert res.updated == ["c_remote"]              # version/hash change → updated
    reg.apply_to_config(config)
    assert "v2 prompt" in config.get_agent("c_remote")["system_prompt"]


# ---------------------------------------------------------------------------
# 4. Uninstall/prune leaves the static built-ins intact
# ---------------------------------------------------------------------------
def test_uninstall_prunes_only_registry_agents(tmp_path):
    cat = Catalog([_row("d_remote")])
    reg = LocalRegistry(cache_dir=str(tmp_path), fetch=cat)
    reg.sync(); reg.apply_to_config(config)
    assert config.get_agent("d_remote") is not None
    assert config.get_agent("data_analyst") is not None     # static built-in present

    cat.rows = []                                            # account uninstalled everything
    res = reg.sync()
    assert res.removed == ["d_remote"]
    reg.apply_to_config(config)
    assert config.get_agent("d_remote") is None
    assert config.get_agent("data_analyst") is not None      # built-in untouched
    assert config.get_ceo() is not None                      # CEO still there


# ---------------------------------------------------------------------------
# 6. Founder-only keys never leak into a merged dict; statics never shadowed
# ---------------------------------------------------------------------------
def test_no_founder_keys_and_no_shadowing(tmp_path):
    cat = Catalog([_row("e_remote"), _row("data_analyst")])   # tries to shadow a built-in
    reg = LocalRegistry(cache_dir=str(tmp_path), fetch=cat)
    reg.sync()
    for d in reg.to_agent_dicts():
        for k in _FOUNDER_ONLY:
            assert k not in d
    reg.apply_to_config(config)
    # The built-in data_analyst is NOT replaced by the manifest one (the static wins).
    assert "_source" not in config.get_agent("data_analyst")


# ---------------------------------------------------------------------------
# 7. Resilience — fetch failure falls back to cache; bad manifest skipped
# ---------------------------------------------------------------------------
def test_fetch_failure_falls_back_to_cache(tmp_path):
    cat = Catalog([_row("f_remote")])
    reg = LocalRegistry(cache_dir=str(tmp_path), fetch=cat)
    reg.sync(); reg.apply_to_config(config)

    cat.fail = True
    res = reg.sync()                          # must NOT raise
    assert res.error is not None
    assert [m["agent_id"] for m in reg.manifests()] == ["f_remote"]   # cache intact


def test_malformed_manifest_is_skipped(tmp_path):
    cat = Catalog([_row("good_remote"), _row("bad_remote", ch="sha256:bad", drop_prompt=True)])
    reg = LocalRegistry(cache_dir=str(tmp_path), fetch=cat)
    reg.sync()
    reg.apply_to_config(config)
    assert config.get_agent("good_remote") is not None
    assert config.get_agent("bad_remote") is None     # missing system_prompt → skipped


# ---------------------------------------------------------------------------
# 8. Domestic-safe — no model resolution, no Anthropic key
# ---------------------------------------------------------------------------
def test_domestic_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_MODEL_PRESET", "domestic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("loader must not resolve a model"))
    monkeypatch.setattr(config, "provider_for", boom)
    cat = Catalog([_row("g_remote")])
    reg = LocalRegistry(cache_dir=str(tmp_path), fetch=cat)
    reg.sync()
    reg.apply_to_config(config)
    assert config.get_agent("g_remote") is not None


def test_cache_persists_across_instances(tmp_path):
    cat = Catalog([_row("h_remote")])
    LocalRegistry(cache_dir=str(tmp_path), fetch=cat).sync()
    # A fresh registry over the same cache dir sees the persisted manifest.
    reg2 = LocalRegistry(cache_dir=str(tmp_path), fetch=Catalog([]))
    assert [m["agent_id"] for m in reg2.manifests()] == ["h_remote"]

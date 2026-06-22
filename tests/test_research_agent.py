"""
test_research_agent.py — the team's Research Analyst: a dedicated, internet-connected
specialist that gathers CURRENT information and hands it to the team for analysis.

These tests assert the four things that make it actually work:
  1. it is a real member of the report team (org chart, capabilities, ordering);
  2. it always resolves to a model that can reach the live web — out of the box
     (SONNET), under the domestic preset (a domestic search-capable model), and that
     an explicit per-agent override still wins;
  3. ModelRouter.complete() actually forwards web_search to non-Anthropic providers
     (the bug that silently disabled search on the domestic models);
  4. every web-search agent is told TODAY'S date so "latest news" can't come back
     stale (e.g. a 2024 result in 2026).

Network-free: no model is ever called; we monkeypatch the one provider seam.
Run from the project root:

    .venv/bin/python -m pytest tests/test_research_agent.py -v
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import base_agent, config, orchestrator, providers  # noqa: E402


# --- 1. the Research Analyst is a real team member -------------------------------
def test_research_agent_is_a_worker_that_can_search():
    agent = config.get_agent("research_agent")
    assert agent is not None
    assert agent["reports_to"] == "ceo"
    assert "web_search" in agent["tools"]
    assert agent.get("requires_web_search") is True
    assert agent in config.get_workers()


def test_research_runs_before_the_analyst_so_it_feeds_analysis():
    ids = [w["id"] for w in config.get_workers()]
    assert "research_agent" in ids and "data_analyst" in ids
    assert ids.index("research_agent") < ids.index("data_analyst")


# --- 2. it always lands on a web-search-capable model ----------------------------
def _no_overrides(monkeypatch):
    """Strip the deployment's saved overrides + presets so we test resolution rules,
    not whatever model_overrides.json happens to hold."""
    monkeypatch.setattr(config, "MODEL_OVERRIDES", {})
    for v in ("TEAM_MODEL_PRESET", "LOCAL_MODEL_MODE", "DOMESTIC_WEB_SEARCH_MODEL"):
        monkeypatch.delenv(v, raising=False)


def test_static_default_can_search(monkeypatch):
    _no_overrides(monkeypatch)
    model = config.effective_model("research_agent")
    assert model == config.SONNET
    assert config.model_supports_web_search(model) is True


def test_domestic_preset_routes_research_to_a_search_capable_model(monkeypatch):
    _no_overrides(monkeypatch)
    monkeypatch.setenv("TEAM_MODEL_PRESET", "domestic")
    # DeepSeek (the domestic default) can't search, so the researcher must NOT clamp to it.
    assert config.model_supports_web_search(config.DOMESTIC_MODEL) is False
    model = config.effective_model("research_agent")
    assert model == config.domestic_web_search_model() == "qwen-plus"
    assert model != config.DOMESTIC_MODEL
    assert config.model_supports_web_search(model) is True
    # No regression: a normal agent still clamps to the domestic model under the preset.
    assert config.effective_model("data_analyst") == config.DOMESTIC_MODEL


def test_explicit_override_still_wins(monkeypatch):
    _no_overrides(monkeypatch)
    monkeypatch.setenv("TEAM_MODEL_PRESET", "domestic")
    monkeypatch.setitem(config.MODEL_OVERRIDES, "research_agent", "qwen3.7-max")
    # An operator's explicit per-agent choice beats the auto-redirect.
    assert config.effective_model("research_agent") == "qwen3.7-max"


# --- 3. web_search actually reaches non-Anthropic providers ----------------------
def test_modelrouter_forwards_web_search_to_non_anthropic(monkeypatch):
    captured = {}

    def fake_openai_chat(model_id, messages, max_tokens, max_retries=3, web_search=False):
        captured["web_search"] = web_search
        return {"text": "ok", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(providers, "_openai_chat", fake_openai_chat)
    router = providers.ModelRouter()
    router.complete("sys", "find today's news", model="qwen-plus", web_search=True)
    assert captured["web_search"] is True   # the bug: this used to be dropped (always False)


def test_modelrouter_no_search_when_flag_off(monkeypatch):
    captured = {}

    def fake_openai_chat(model_id, messages, max_tokens, max_retries=3, web_search=False):
        captured["web_search"] = web_search
        return {"text": "ok", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(providers, "_openai_chat", fake_openai_chat)
    providers.ModelRouter().complete("sys", "hi", model="qwen-plus")
    assert captured["web_search"] is False


def test_tool_loop_also_forwards_web_search_to_non_anthropic(monkeypatch):
    """An agent with BOTH file tools and web_search (the data analyst) must still be able
    to search on Qwen — the tool-loop path used to drop the flag."""
    captured = {}

    def fake_tool_loop(model_id, messages, tool_specs, executor, max_tokens,
                       max_retries=3, max_iterations=8, web_search=False):
        captured["web_search"] = web_search
        return {"text": "ok", "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(providers, "_openai_chat_with_tools", fake_tool_loop)
    providers.ModelRouter().complete_with_tools(
        "sys", "research and build a sheet", tool_specs=[], tool_executor=lambda *a: "",
        model="qwen-plus", web_search=True)
    assert captured["web_search"] is True


# --- the domestic search-model override can't silently disable search ------------
def test_domestic_web_search_model_default(monkeypatch):
    monkeypatch.delenv("DOMESTIC_WEB_SEARCH_MODEL", raising=False)
    assert config.domestic_web_search_model() == "qwen-plus"


def test_domestic_web_search_model_honors_valid_override(monkeypatch):
    monkeypatch.setenv("DOMESTIC_WEB_SEARCH_MODEL", "qwen-max")
    assert config.model_supports_web_search("qwen-max") is True
    assert config.domestic_web_search_model() == "qwen-max"


def test_domestic_web_search_model_rejects_non_searching_override(monkeypatch):
    # A misconfigured override (a model that can't search) must NOT disable the guarantee.
    monkeypatch.setenv("DOMESTIC_WEB_SEARCH_MODEL", "deepseek-v4-flash")
    assert config.model_supports_web_search("deepseek-v4-flash") is False
    assert config.domestic_web_search_model() == "qwen-plus"  # falls back to a real searcher


# --- 4. freshness: agents are anchored to today's real date ----------------------
class _FakeMemory:
    def goal(self):
        return "本周中国新能源车市场有什么新闻？"

    def documents(self):
        return []

    def snapshot(self):
        return {"artifacts": {}}


def test_web_search_context_states_todays_date_and_flags_stale(monkeypatch):
    monkeypatch.setattr(config, "current_date_str", lambda: "2026-06-21")
    agent = base_agent.Agent(config.get_agent("research_agent"), _FakeMemory(), llm=None)
    ctx = agent._build_context("search the web for the latest news")
    assert "2026-06-21" in ctx            # the model is told the real 'now'
    assert "2026-06" in ctx               # queries biased toward the current month
    assert "2024" in ctx                  # explicitly named as the stale year to reject
    assert "SEARCH" in ctx


def test_non_search_agent_gets_no_date_block(monkeypatch):
    monkeypatch.setattr(config, "current_date_str", lambda: "2026-06-21")
    agent = base_agent.Agent(config.get_agent("records_manager"), _FakeMemory(), llm=None)
    ctx = agent._build_context("catalog the documents")
    assert "TODAY'S DATE" not in ctx      # only web-search agents get the freshness block


def test_current_date_str_is_iso_dated():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", config.current_date_str())


# --- the fallback plan still delegates research ----------------------------------
def test_default_plan_includes_research(monkeypatch):
    # Bypass __init__ (which loads a real company brain); _default_plan only needs memory.
    orch = orchestrator.Orchestrator.__new__(orchestrator.Orchestrator)
    orch.memory = _FakeMemory()
    plan = orch._default_plan()
    research_steps = [s for s in plan if s["agent"] == "research_agent"]
    assert len(research_steps) == 1
    assert "web" in research_steps[0]["instruction"].lower()

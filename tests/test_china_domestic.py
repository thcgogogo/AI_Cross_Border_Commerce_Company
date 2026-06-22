"""
test_china_domestic.py — the mainland-China "domestic model" switch.

Verifies that TEAM_MODEL_PRESET=domestic makes the WHOLE product run on DeepSeek
V4 with NO Anthropic key:

  1. ModelRouter constructs and serves a domestic (DeepSeek) call with no
     ANTHROPIC_API_KEY — the native Anthropic client is built LAZILY and is never
     touched on the domestic path. The key requirement only bites if something
     actually routes to an Anthropic model.
  2. effective_model() precedence: per-agent UI override → domestic preset →
     static default.
  3. The utility-call accessors (extraction / audit / fuser) honor an explicit env
     override, then the domestic preset, then their Claude default.
  4. The catalog migrated to the DeepSeek V4 ids; the legacy ids are gone.

Network-free and key-free: the OpenAI-compatible adapter is monkeypatched, so no
real provider is ever called. Run from the project root:

    .venv/bin/python -m pytest tests/test_china_domestic.py -v
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402,F401 — one-time, idempotent module boot
from agent_team import config, providers  # noqa: E402
from agent_team.llm import LLMError  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Router runs the domestic path with NO Anthropic key (the launch unblocker)
# ---------------------------------------------------------------------------
def test_router_constructs_without_anthropic_key(monkeypatch):
    """ModelRouter() must not require ANTHROPIC_API_KEY just to exist — the native
    client is lazy, so a fully-domestic deployment never needs an Anthropic key."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = providers.ModelRouter()
    assert r._anthropic_obj is None  # nothing built yet


def test_domestic_complete_never_builds_anthropic_client(monkeypatch):
    """A deepseek-v4-flash completion works with no Anthropic key and never
    constructs the Anthropic client."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    captured = {}

    def fake_openai_chat(model_id, messages, max_tokens, max_retries=3, web_search=False):
        captured["model"] = model_id
        captured["web_search"] = web_search
        return {"text": "DOMESTIC-OK", "input_tokens": 10, "output_tokens": 5}

    monkeypatch.setattr(providers, "_openai_chat", fake_openai_chat)

    r = providers.ModelRouter()
    out = r.complete("sys", "user", model="deepseek-v4-flash", budget=None)
    assert out == "DOMESTIC-OK"
    assert captured["model"] == "deepseek-v4-flash"
    assert captured["web_search"] is False  # default off; ModelRouter forwards the flag
    assert r._anthropic_obj is None  # the Anthropic path was never taken


def test_anthropic_path_still_requires_key_lazily(monkeypatch):
    """The Anthropic key requirement is deferred — it raises only when a call
    actually routes to an Anthropic model, not at router construction."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = providers.ModelRouter()  # fine
    with pytest.raises(LLMError):
        r.complete("sys", "user", model=config.OPUS, budget=None)


# ---------------------------------------------------------------------------
# 2. effective_model() precedence: override → preset → static default
# ---------------------------------------------------------------------------
def test_effective_model_preset_off_uses_static_default(monkeypatch):
    monkeypatch.delenv("TEAM_MODEL_PRESET", raising=False)
    monkeypatch.setattr(config, "MODEL_OVERRIDES", {})
    assert config.effective_model("ceo") == config.OPUS


def test_effective_model_preset_on_uses_domestic(monkeypatch):
    monkeypatch.setenv("TEAM_MODEL_PRESET", "domestic")
    monkeypatch.setattr(config, "MODEL_OVERRIDES", {})
    assert config.effective_model("ceo") == config.DOMESTIC_MODEL
    assert config.effective_model("report_writer") == config.DOMESTIC_MODEL


def test_per_agent_override_wins_over_preset(monkeypatch):
    monkeypatch.setenv("TEAM_MODEL_PRESET", "domestic")
    monkeypatch.setattr(config, "MODEL_OVERRIDES", {"report_writer": "deepseek-v4-pro"})
    assert config.effective_model("report_writer") == "deepseek-v4-pro"
    # an agent without an explicit override still follows the preset
    assert config.effective_model("ceo") == config.DOMESTIC_MODEL


# ---------------------------------------------------------------------------
# 3. Utility-call models: env override → preset → Claude default
# ---------------------------------------------------------------------------
def test_utility_models_default_to_claude(monkeypatch):
    monkeypatch.delenv("TEAM_MODEL_PRESET", raising=False)
    for v in ("EXTRACTION_MODEL", "AUDIT_MODEL", "FUSER_MODEL"):
        monkeypatch.delenv(v, raising=False)
    assert config.extraction_model() == config.HAIKU
    assert config.audit_model() == config.HAIKU
    assert config.fuser_fallback_model() == config.OPUS


def test_utility_models_follow_domestic_preset(monkeypatch):
    monkeypatch.setenv("TEAM_MODEL_PRESET", "domestic")
    for v in ("EXTRACTION_MODEL", "AUDIT_MODEL", "FUSER_MODEL"):
        monkeypatch.delenv(v, raising=False)
    assert config.extraction_model() == config.DOMESTIC_MODEL
    assert config.audit_model() == config.DOMESTIC_MODEL
    assert config.fuser_fallback_model() == config.DOMESTIC_MODEL


def test_utility_model_explicit_env_wins(monkeypatch):
    """An explicit per-call env override beats even the preset."""
    monkeypatch.setenv("TEAM_MODEL_PRESET", "domestic")
    monkeypatch.setenv("EXTRACTION_MODEL", "qwen-plus")
    assert config.extraction_model() == "qwen-plus"
    # the others still follow the preset
    assert config.audit_model() == config.DOMESTIC_MODEL


# ---------------------------------------------------------------------------
# 4. Catalog migrated to DeepSeek V4; legacy ids removed
# ---------------------------------------------------------------------------
def test_v4_models_in_catalog_legacy_gone():
    flash = config.get_model("deepseek-v4-flash")
    pro = config.get_model("deepseek-v4-pro")
    assert flash and flash["provider"] == "deepseek"
    assert pro and pro["provider"] == "deepseek"
    assert config.provider_for("deepseek-v4-flash") == "deepseek"
    # the retiring legacy ids must be gone
    assert config.get_model("deepseek-chat") is None
    assert config.get_model("deepseek-reasoner") is None


def test_domestic_model_constant_is_a_real_catalog_row():
    """The constant the preset points at must be a valid, routable text model."""
    row = config.get_model(config.DOMESTIC_MODEL)
    assert row is not None
    assert config.is_text_model(config.DOMESTIC_MODEL)
    assert config.DOMESTIC_MODEL in config._VALID_MODEL_IDS

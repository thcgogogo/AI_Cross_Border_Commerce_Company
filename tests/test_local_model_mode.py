"""
test_local_model_mode.py — WS12: the OFFLINE Local-Model (Ollama) mode config.

A degraded, opt-in escape hatch: when LOCAL_MODEL_MODE is on, every agent resolves to a
local model and provider_for routes it to ollama — above the per-agent override AND the
domestic preset. Off by default; domestic mode is unchanged. ¥0 catalog cost (no COGS).
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import config, providers  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ("LOCAL_MODEL_MODE", "LOCAL_MODEL_ID", "TEAM_MODEL_PRESET", "OLLAMA_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    # Isolate from any persisted per-agent overrides (app import loads
    # memory_store/model_overrides.json into this global) so the precedence
    # assertions below test the static default, not a deployment's real config.
    monkeypatch.setattr(config, "MODEL_OVERRIDES", {})


def test_ollama_provider_registered():
    assert config.PROVIDERS["ollama"]["base_url"] == "http://127.0.0.1:11434/v1"
    assert config.PROVIDERS["ollama"]["sdk"] == "openai_compatible"


def test_local_mode_resolves_every_agent_to_local(monkeypatch):
    monkeypatch.setenv("LOCAL_MODEL_MODE", "1")
    lid = config.local_model_id()
    for agent in ("ceo", "data_analyst", "report_writer"):
        assert config.effective_model(agent) == lid
    assert config.provider_for(lid) == "ollama"


def test_local_mode_off_is_unchanged(monkeypatch):
    # OPUS default, an override win, and the domestic preset all behave as before.
    assert config.effective_model("ceo") == config.get_agent("ceo")["model"]
    monkeypatch.setitem(config.MODEL_OVERRIDES, "ceo", config.SONNET)
    assert config.effective_model("ceo") == config.SONNET
    monkeypatch.delitem(config.MODEL_OVERRIDES, "ceo")
    monkeypatch.setenv("TEAM_MODEL_PRESET", "domestic")
    assert config.effective_model("ceo") == config.DOMESTIC_MODEL


def test_local_mode_outranks_override_and_domestic(monkeypatch):
    monkeypatch.setenv("LOCAL_MODEL_MODE", "1")
    monkeypatch.setenv("TEAM_MODEL_PRESET", "domestic")
    monkeypatch.setitem(config.MODEL_OVERRIDES, "ceo", config.SONNET)
    try:
        assert config.effective_model("ceo") == config.local_model_id()   # local wins over both
    finally:
        monkeypatch.delitem(config.MODEL_OVERRIDES, "ceo")


def test_provider_key_placeholder_for_ollama(monkeypatch):
    monkeypatch.setenv("LOCAL_MODEL_MODE", "1")   # so provider_for(lid)=="ollama"
    assert providers.provider_key(config.local_model_id()) == "ollama-local"


def test_local_model_has_zero_catalog_cost():
    # Deliberate: the local model isn't in MODEL_CHOICES → ¥0 → no COGS visibility.
    assert config.cost_cny(config.local_model_id(), 1000, 1000) == 0.0


def test_custom_local_model_id(monkeypatch):
    monkeypatch.setenv("LOCAL_MODEL_MODE", "1")
    monkeypatch.setenv("LOCAL_MODEL_ID", "llama3.1:8b")
    assert config.local_model_id() == "llama3.1:8b"
    assert config.effective_model("data_analyst") == "llama3.1:8b"

"""
providers.py — One router that reaches EVERY model the catalog lists.

The team is multi-provider, but the rest of the app never has to care. This file
exposes a `ModelRouter` with the SAME interface as llm.LLMClient (`complete()`,
`complete_with_tools()`, a `.token_manager` attribute), so the Orchestrator can
swap one for the other with no other changes. The router just looks at
`config.provider_for(model)` and dispatches:

  - Anthropic models  -> the official Anthropic SDK (llm.LLMClient) — the native,
                         fully-featured path (web search, file tools, thinking…).
  - Everything else   -> ONE OpenAI-compatible adapter (`_openai_chat`). OpenAI,
                         Google Gemini, xAI, DeepSeek, Qwen, MiniMax and Moonshot
                         all speak the OpenAI Chat Completions API, so a single
                         adapter pointed at the right base_url + key covers them all.

It also exposes `gateway_chat()` — the same dispatch, but taking a raw
OpenAI-style `messages` list — which powers the unified-API gateway in app.py.

SECURITY: provider keys are read ONLY from environment variables (put them in
.env). They are never written to disk, never logged, and never returned to a
caller. A model whose key is absent fails with a clear "add KEY to .env" message
instead of silently doing nothing.
"""

import os

from . import config
from .llm import LLMClient, LLMError, _text_of, _count_tokens  # noqa: F401  (_text_of/_count_tokens reused)


class ProviderKeyMissing(LLMError):
    """Raised when a model's provider key is not present in the environment."""


# ---------------------------------------------------------------------------
# Provider lookup + key handling
# ---------------------------------------------------------------------------
def _provider_cfg(model_id):
    """(provider_id, provider_dict) for a model id."""
    pid = config.provider_for(model_id)
    return pid, config.PROVIDERS.get(pid, {})


def provider_key(model_id):
    """The provider's API key from the environment, cleaned — or None if absent.

    Strips the whitespace/quotes a key often picks up when pasted into .env. We
    return None (not "") so callers can do a simple truthiness check."""
    _, p = _provider_cfg(model_id)
    env = p.get("env_key")
    raw = os.environ.get(env, "") if env else ""
    cleaned = (raw or "").strip().strip('"').strip("'").strip()
    return cleaned or None


def model_available(model_id):
    """True if this model can actually run right now (its provider key is set)."""
    return provider_key(model_id) is not None


def _missing_key_message(model_id):
    pid, p = _provider_cfg(model_id)
    label = p.get("label", pid)
    env = p.get("env_key", "the provider key")
    return (f"{label} model '{model_id}' needs {env} in your .env to run. "
            f"Add it and try again — nothing else changes.")


# ---------------------------------------------------------------------------
# OpenAI-compatible adapter (OpenAI, Google, xAI, DeepSeek, Qwen, MiniMax, Moonshot)
# ---------------------------------------------------------------------------
def _openai_chat(model_id, messages, max_tokens, max_retries=3):
    """Call a non-Anthropic, OpenAI-compatible provider and return a result dict.

    Returns {"text", "input_tokens", "output_tokens"}. Raises ProviderKeyMissing
    if the provider key is absent, or LLMError on an API failure. Uses the official
    `openai` SDK pointed at the provider's base_url (per config.PROVIDERS)."""
    pid, p = _provider_cfg(model_id)
    key = provider_key(model_id)
    if not key:
        raise ProviderKeyMissing(_missing_key_message(model_id))

    try:
        import openai
    except ImportError:
        raise LLMError(
            "The 'openai' package is required to call non-Anthropic models. "
            "Install it in this app's virtual environment: pip install openai"
        )

    client = openai.OpenAI(
        api_key=key,
        base_url=p.get("base_url"),
        timeout=120.0,
        max_retries=max_retries,
    )
    kwargs = {"model": model_id, "messages": messages, "max_tokens": max_tokens}
    try:
        resp = client.chat.completions.create(**kwargs)
    except openai.BadRequestError as e:
        # Some newer models reject `max_tokens` and require `max_completion_tokens`.
        if "max_tokens" in str(e) or "max_completion_tokens" in str(e):
            kwargs.pop("max_tokens", None)
            kwargs["max_completion_tokens"] = max_tokens
            try:
                resp = client.chat.completions.create(**kwargs)
            except openai.OpenAIError as e2:
                raise LLMError(f"{p.get('label', pid)} API error: {e2}")
        else:
            raise LLMError(f"{p.get('label', pid)} API error: {e}")
    except openai.OpenAIError as e:
        raise LLMError(f"{p.get('label', pid)} API error: {e}")

    choice = (resp.choices or [None])[0]
    msg = getattr(choice, "message", None) if choice else None
    text = ((getattr(msg, "content", "") if msg else "") or "").strip()
    usage = getattr(resp, "usage", None)
    return {
        "text": text or "(The model returned no text.)",
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }


# ---------------------------------------------------------------------------
# Anthropic adapter for the gateway (raw messages -> Claude)
# ---------------------------------------------------------------------------
def _anthropic_chat(model_id, messages, max_tokens):
    """Call Claude with an OpenAI-style `messages` list (used by the gateway).

    Splits out system messages, maps the rest to Anthropic's user/assistant
    turns, streams the response, and returns {"text", "input_tokens",
    "output_tokens"} using the real usage the API reports."""
    import anthropic

    key = provider_key(model_id)  # reads ANTHROPIC_API_KEY for the anthropic provider
    if not key:
        raise ProviderKeyMissing(_missing_key_message(model_id))

    # Separate the system prompt(s) from the conversational turns.
    system_parts, convo = [], []
    for m in messages or []:
        role = (m.get("role") or "user").strip()
        content = m.get("content") or ""
        if isinstance(content, list):  # OpenAI content-parts -> plain text
            content = "".join(part.get("text", "") for part in content
                              if isinstance(part, dict))
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        convo.append({"role": "assistant" if role == "assistant" else "user",
                      "content": content})
    if not convo:
        convo = [{"role": "user", "content": ""}]

    client = anthropic.Anthropic(api_key=key)
    kwargs = dict(model=model_id, max_tokens=max_tokens, messages=convo)
    if system_parts:
        kwargs["system"] = "\n\n".join(system_parts)
    if model_id in config.MODELS_WITH_ADAPTIVE_THINKING:
        kwargs["extra_body"] = {"thinking": {"type": "adaptive"}}

    try:
        with client.messages.stream(**kwargs) as stream:
            final = stream.get_final_message()
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude API error ({getattr(e, 'status_code', '?')}): {e}")
    except anthropic.AnthropicError as e:
        raise LLMError(f"Claude API error: {e}")

    usage = getattr(final, "usage", None)
    return {
        "text": _text_of(final),
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
    }


def gateway_chat(model_id, messages, max_tokens=1024):
    """Route a raw OpenAI-style chat request to the right provider.

    Returns {"text", "input_tokens", "output_tokens", "model", "provider"}.
    This is the single entry point the unified-API gateway (/v1/chat/completions)
    calls once it has authenticated the customer and checked model access."""
    provider = config.provider_for(model_id)
    if provider == "anthropic":
        result = _anthropic_chat(model_id, messages, max_tokens)
    else:
        result = _openai_chat(model_id, messages, max_tokens)
    return {**result, "model": model_id, "provider": provider}


# ---------------------------------------------------------------------------
# ModelRouter — a drop-in replacement for LLMClient that spans every provider
# ---------------------------------------------------------------------------
class ModelRouter:
    """Same surface as llm.LLMClient, but routes each call by the model's provider.

    The Orchestrator builds ONE of these and hands it to every Agent. Anthropic
    models get the full native experience (thinking, effort, web search, file
    tools); other providers run through the OpenAI-compatible adapter, which
    returns plain text. Budget accounting (via the shared token_manager) works
    the same on both paths."""

    def __init__(self, api_key=None, token_manager=None, cost_listener=None):
        self.token_manager = token_manager
        self.cost_listener = cost_listener  # callback(model, input_tokens, output_tokens)
        # The Anthropic client is always available (the app validates its key on
        # startup). Non-Anthropic providers are reached lazily via _openai_chat.
        self._anthropic = LLMClient(api_key=api_key, token_manager=token_manager,
                                    cost_listener=cost_listener)
        # Mirrors LLMClient.last_stop_reason ("max_tokens" == the answer was cut
        # off at the output ceiling). None on the OpenAI-compatible path, which
        # doesn't surface a finish reason.
        self.last_stop_reason = None

    # -- mirror LLMClient.complete -----------------------------------------
    def complete(self, system_prompt, user_content, model=config.OPUS, effort="high",
                 max_tokens=8000, max_retries=3, budget=None, web_search=False):
        if config.provider_for(model) == "anthropic":
            text = self._anthropic.complete(
                system_prompt, user_content, model=model, effort=effort,
                max_tokens=max_tokens, max_retries=max_retries, budget=budget,
                web_search=web_search,
            )
            self.last_stop_reason = self._anthropic.last_stop_reason
            return text
        # Non-Anthropic path: a single OpenAI-compatible chat call.
        self.last_stop_reason = None
        if self.token_manager and budget:
            self.token_manager.ensure_can_spend(budget)  # may raise BudgetExceededError
        messages = [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}]
        result = _openai_chat(model, messages, max_tokens, max_retries=max_retries)
        inp, out = result["input_tokens"], result["output_tokens"]
        if self.token_manager and budget:
            self.token_manager.record_cost(budget, config.cost_cny(model, inp, out))
        if self.cost_listener:
            self.cost_listener(model, inp, out)
        return result["text"]

    # -- mirror LLMClient.complete_with_tools ------------------------------
    def complete_with_tools(self, system_prompt, user_content, tool_specs, tool_executor,
                            model=config.OPUS, effort="high", max_tokens=12000,
                            max_retries=3, budget=None, max_iterations=12,
                            web_search=False):
        if config.provider_for(model) == "anthropic":
            text = self._anthropic.complete_with_tools(
                system_prompt, user_content, tool_specs, tool_executor, model=model,
                effort=effort, max_tokens=max_tokens, max_retries=max_retries,
                budget=budget, max_iterations=max_iterations, web_search=web_search,
            )
            self.last_stop_reason = self._anthropic.last_stop_reason
            return text
        # File-generation tools and web search are Anthropic-only features. Rather
        # than fake them, run a normal completion on the chosen model and add an
        # honest note so the agent (and user) know why no files were produced.
        note = ("\n\n[Note: file-generation tools and live web search currently run "
                "only on Claude models. You are running on a non-Anthropic model, so "
                "produce your deliverable as text/Markdown instead of calling tools.]")
        return self.complete(
            system_prompt + note, user_content, model=model, effort=effort,
            max_tokens=max_tokens, max_retries=max_retries, budget=budget,
        )

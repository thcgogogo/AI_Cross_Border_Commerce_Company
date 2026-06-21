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

import json
import os
import re

from . import config
from .llm import (LLMClient, LLMError, WEB_SEARCH_TOOL, _serialize_blocks,  # noqa: F401
                  _text_of, _count_tokens)


class ProviderKeyMissing(LLMError):
    """Raised when a model's provider key is not present in the environment."""


# ---------------------------------------------------------------------------
# Provider lookup + key handling
# ---------------------------------------------------------------------------
def _provider_cfg(model_id):
    """(provider_id, provider_dict) for a model id."""
    pid = config.provider_for(model_id)
    return pid, config.PROVIDERS.get(pid, {})


# Some reasoning models (e.g. MiniMax-M3) emit their chain-of-thought INLINE as a
# leading <think>…</think> block in `content` rather than a separate reasoning_content
# field. Strip a block at the very start so the agent's real answer isn't prefixed with
# raw reasoning (which would pollute deliverables / break SOURCES_JSON parsing). Only a
# leading, closed block is removed — genuine later mentions of the tag are left intact.
_THINK_RE = re.compile(r"\A\s*<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def _strip_reasoning(text):
    if not text:
        return text
    return _THINK_RE.sub("", text, count=1).strip()


def provider_key(model_id):
    """The provider's API key from the environment, cleaned — or None if absent.

    Strips the whitespace/quotes a key often picks up when pasted into .env. We
    return None (not "") so callers can do a simple truthiness check."""
    pid, p = _provider_cfg(model_id)
    env = p.get("env_key")
    raw = os.environ.get(env, "") if env else ""
    cleaned = (raw or "").strip().strip('"').strip("'").strip()
    # Ollama's local server ignores the API key; return a placeholder when unset so the
    # OpenAI-compatible adapter doesn't raise ProviderKeyMissing (loopback, no real secret).
    # This does NOT weaken the missing-key behaviour for any real provider.
    if not cleaned and pid == "ollama":
        return "ollama-local"
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
def _openai_chat(model_id, messages, max_tokens, max_retries=3, web_search=False):
    """Call a non-Anthropic, OpenAI-compatible provider and return a result dict.

    Returns {"text", "input_tokens", "output_tokens"}. Raises ProviderKeyMissing
    if the provider key is absent, or LLMError on an API failure. Uses the official
    `openai` SDK pointed at the provider's base_url (per config.PROVIDERS). With
    web_search=True on a DashScope (Qwen) model, flips on DashScope's `enable_search`
    so the model can browse the live web; ignored for providers without a switch."""
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
        base_url=config.provider_base_url(pid),
        timeout=120.0,
        max_retries=max_retries,
    )
    kwargs = {"model": model_id, "messages": messages, "max_tokens": max_tokens}
    if web_search and pid == "qwen":
        kwargs["extra_body"] = {"enable_search": True}   # DashScope's web-browse switch
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
    text = _strip_reasoning(((getattr(msg, "content", "") if msg else "") or "").strip())
    usage = getattr(resp, "usage", None)
    return {
        "text": text or "(The model returned no text.)",
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
    }


# ---------------------------------------------------------------------------
# Anthropic adapter for the gateway (raw messages -> Claude)
# ---------------------------------------------------------------------------
def _anthropic_image_block(url):
    """An Anthropic image content block from an OpenAI image_url. Handles a base64 data URL
    (data:<media_type>;base64,<data>) and a plain http(s) URL. Returns None if unusable."""
    if not url:
        return None
    if url.startswith("data:"):
        try:
            header, data = url.split(",", 1)
            media_type = header[5:].split(";")[0] or "image/png"   # strip leading 'data:'
        except ValueError:
            return None
        return {"type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data}}
    if url.startswith("http://") or url.startswith("https://"):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _anthropic_blocks(parts):
    """Convert OpenAI content-parts (text + image_url) to Anthropic content blocks, KEEPING
    images (the gateway's vision path). Returns a blocks list, or '' if nothing usable."""
    blocks = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            if part.get("text"):
                blocks.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "") if isinstance(
                part.get("image_url"), dict) else ""
            block = _anthropic_image_block(url)
            if block:
                blocks.append(block)
    return blocks or ""


def _anthropic_chat(model_id, messages, max_tokens, web_search=False):
    """Call Claude with an OpenAI-style `messages` list (used by the gateway).

    Splits out system messages, maps the rest to Anthropic's user/assistant
    turns, streams the response, and returns {"text", "input_tokens",
    "output_tokens"} using the real usage the API reports. With web_search=True,
    adds Claude's native web-search tool and resumes the turn while a search is
    still running (stop_reason='pause_turn')."""
    import anthropic

    key = provider_key(model_id)  # reads ANTHROPIC_API_KEY for the anthropic provider
    if not key:
        raise ProviderKeyMissing(_missing_key_message(model_id))

    # Separate the system prompt(s) from the conversational turns.
    system_parts, convo = [], []
    for m in messages or []:
        role = (m.get("role") or "user").strip()
        content = m.get("content") or ""
        if role == "system":
            # System can't carry images — flatten any parts to plain text.
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            if content:
                system_parts.append(content)
            continue
        if isinstance(content, list):  # OpenAI content-parts -> Anthropic blocks (keep images)
            content = _anthropic_blocks(content)
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
    if web_search:
        kwargs["tools"] = [WEB_SEARCH_TOOL]

    final, in_tok, out_tok = None, 0, 0
    for _ in range(6):   # cap web-search 'pause_turn' resumes
        try:
            with client.messages.stream(**kwargs) as stream:
                final = stream.get_final_message()
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error ({getattr(e, 'status_code', '?')}): {e}")
        except anthropic.AnthropicError as e:
            raise LLMError(f"Claude API error: {e}")
        usage = getattr(final, "usage", None)
        in_tok += int(getattr(usage, "input_tokens", 0) or 0)
        out_tok += int(getattr(usage, "output_tokens", 0) or 0)
        if getattr(final, "stop_reason", None) != "pause_turn":
            break
        # A web search is still running — hand the partial turn back to resume it.
        convo.append({"role": "assistant",
                      "content": _serialize_blocks(getattr(final, "content", []) or [])})
        kwargs["messages"] = convo

    return {"text": _text_of(final), "input_tokens": in_tok, "output_tokens": out_tok}


def gateway_chat(model_id, messages, max_tokens=1024, web_search=False):
    """Route a raw OpenAI-style chat request to the right provider.

    Returns {"text", "input_tokens", "output_tokens", "model", "provider"}.
    With web_search=True, providers that support live search use it (Claude's native
    tool / Qwen's enable_search); others ignore the flag and answer normally.
    This is the single entry point the unified-API gateway (/v1/chat/completions)
    calls once it has authenticated the customer and checked model access."""
    provider = config.provider_for(model_id)
    if provider == "anthropic":
        result = _anthropic_chat(model_id, messages, max_tokens, web_search=web_search)
    else:
        result = _openai_chat(model_id, messages, max_tokens, web_search=web_search)
    return {**result, "model": model_id, "provider": provider}


# ---------------------------------------------------------------------------
# Tool-use loop for the gateway (Fusion R5 "agent mode": let a model call the
# deliverables file builders). The deliverables specs are in Anthropic format
# {name, description, input_schema}; we translate to OpenAI's tools shape for the
# OpenAI-compatible providers (Claude uses them natively).
# ---------------------------------------------------------------------------
def _anthropic_to_openai_tools(tool_specs):
    return [{"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in (tool_specs or [])]


def _openai_chat_with_tools(model_id, messages, tool_specs, executor, max_tokens,
                            max_retries=3, max_iterations=8):
    """An OpenAI-compatible function-calling loop so a NON-Anthropic model (DeepSeek, Qwen,
    MiniMax, Kimi…) can call tools — e.g. the deliverables file builders. `executor(name,
    args) -> str` runs the tool and returns a short result string. Returns {text,
    input_tokens, output_tokens}."""
    pid, p = _provider_cfg(model_id)
    key = provider_key(model_id)
    if not key:
        raise ProviderKeyMissing(_missing_key_message(model_id))
    import openai
    client = openai.OpenAI(api_key=key, base_url=config.provider_base_url(pid),
                           timeout=120.0, max_retries=max_retries)
    tools = _anthropic_to_openai_tools(tool_specs)
    msgs, texts, in_tok, out_tok = list(messages), [], 0, 0
    for _ in range(max_iterations):
        try:
            resp = client.chat.completions.create(model=model_id, messages=msgs,
                                                  tools=tools, max_tokens=max_tokens)
        except openai.OpenAIError as e:
            raise LLMError(f"{p.get('label', pid)} API error: {e}")
        usage = getattr(resp, "usage", None)
        in_tok += int(getattr(usage, "prompt_tokens", 0) or 0)
        out_tok += int(getattr(usage, "completion_tokens", 0) or 0)
        msg = getattr((resp.choices or [None])[0], "message", None)
        if msg is None:
            break
        content = _strip_reasoning((getattr(msg, "content", "") or "").strip())
        if content:
            texts.append(content)
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            break
        msgs.append({"role": "assistant", "content": getattr(msg, "content", "") or "",
                     "tool_calls": [{"id": tc.id, "type": "function",
                                     "function": {"name": tc.function.name,
                                                  "arguments": tc.function.arguments}}
                                    for tc in tool_calls]})
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (ValueError, TypeError):
                args = {}
            try:
                result = executor(tc.function.name, args)
            except Exception as e:  # noqa: BLE001 — surface tool errors to the model
                result = f"Tool error: {type(e).__name__}: {e}"
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
    return {"text": "\n".join(texts) or "(完成。)", "input_tokens": in_tok, "output_tokens": out_tok}


def _anthropic_chat_with_tools(model_id, messages, tool_specs, executor, max_tokens,
                               max_iterations=8):
    """Claude's native tool-use loop over an OpenAI-style `messages` list (the gateway's
    agent path). Same return shape as _openai_chat_with_tools."""
    import anthropic
    key = provider_key(model_id)
    if not key:
        raise ProviderKeyMissing(_missing_key_message(model_id))
    system_parts, convo = [], []
    for m in messages or []:
        role = (m.get("role") or "user").strip()
        content = m.get("content") or ""
        if role == "system":
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            if content:
                system_parts.append(content)
            continue
        if isinstance(content, list):
            content = _anthropic_blocks(content)
        convo.append({"role": "assistant" if role == "assistant" else "user", "content": content})
    if not convo:
        convo = [{"role": "user", "content": ""}]

    client = anthropic.Anthropic(api_key=key)
    texts, in_tok, out_tok = [], 0, 0
    for _ in range(max_iterations):
        kwargs = dict(model=model_id, max_tokens=max_tokens, messages=convo, tools=tool_specs)
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)
        try:
            with client.messages.stream(**kwargs) as stream:
                final = stream.get_final_message()
        except anthropic.AnthropicError as e:
            raise LLMError(f"Claude API error: {e}")
        usage = getattr(final, "usage", None)
        in_tok += int(getattr(usage, "input_tokens", 0) or 0)
        out_tok += int(getattr(usage, "output_tokens", 0) or 0)
        blocks = getattr(final, "content", []) or []
        texts += [b.text for b in blocks if getattr(b, "type", None) == "text"]
        tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
        if getattr(final, "stop_reason", None) != "tool_use" or not tool_uses:
            break
        convo.append({"role": "assistant", "content": _serialize_blocks(blocks)})
        results = []
        for tu in tool_uses:
            try:
                result = executor(tu.name, tu.input or {})
            except Exception as e:  # noqa: BLE001
                result = f"Tool error: {type(e).__name__}: {e}"
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": str(result)})
        convo.append({"role": "user", "content": results})
    return {"text": "\n".join(texts) or "(完成。)", "input_tokens": in_tok, "output_tokens": out_tok}


def gateway_chat_with_tools(model_id, messages, tool_specs, executor, max_tokens=2048,
                            max_iterations=8):
    """Run a tool-use loop for `model_id` (Claude native / OpenAI-compatible), returning
    {text, input_tokens, output_tokens, model, provider}. `executor(name, args) -> str`."""
    provider = config.provider_for(model_id)
    if provider == "anthropic":
        result = _anthropic_chat_with_tools(model_id, messages, tool_specs, executor,
                                            max_tokens, max_iterations=max_iterations)
    else:
        result = _openai_chat_with_tools(model_id, messages, tool_specs, executor, max_tokens,
                                         max_iterations=max_iterations)
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
        # The Anthropic client is built LAZILY — only when a call actually routes to
        # an Anthropic model. This lets a fully-domestic team (TEAM_MODEL_PRESET=
        # domestic, every model on DeepSeek/Qwen) run with NO ANTHROPIC_API_KEY: the
        # router constructs fine and LLMClient (which requires the key) is never
        # built. We stash the constructor args and memoize the client on first use.
        self._api_key = api_key
        self._anthropic_obj = None
        # Mirrors LLMClient.last_stop_reason ("max_tokens" == the answer was cut
        # off at the output ceiling). None on the OpenAI-compatible path, which
        # doesn't surface a finish reason.
        self.last_stop_reason = None

    def _anthropic_client(self):
        """The native Anthropic LLMClient, built on first use and reused thereafter.

        Raises LLMError (no ANTHROPIC_API_KEY) only if something actually routes to
        an Anthropic model without a key — never just from constructing the router."""
        if self._anthropic_obj is None:
            self._anthropic_obj = LLMClient(
                api_key=self._api_key, token_manager=self.token_manager,
                cost_listener=self.cost_listener)
        return self._anthropic_obj

    # -- mirror LLMClient.complete -----------------------------------------
    def complete(self, system_prompt, user_content, model=config.OPUS, effort="high",
                 max_tokens=8000, max_retries=3, budget=None, web_search=False):
        if config.provider_for(model) == "anthropic":
            anthropic_client = self._anthropic_client()
            text = anthropic_client.complete(
                system_prompt, user_content, model=model, effort=effort,
                max_tokens=max_tokens, max_retries=max_retries, budget=budget,
                web_search=web_search,
            )
            self.last_stop_reason = anthropic_client.last_stop_reason
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
            anthropic_client = self._anthropic_client()
            text = anthropic_client.complete_with_tools(
                system_prompt, user_content, tool_specs, tool_executor, model=model,
                effort=effort, max_tokens=max_tokens, max_retries=max_retries,
                budget=budget, max_iterations=max_iterations, web_search=web_search,
            )
            self.last_stop_reason = anthropic_client.last_stop_reason
            return text
        # Non-Anthropic: run the OpenAI-compatible tool loop so the model can actually CALL
        # the file builders (it used to degrade to a text-only completion). Web search stays
        # Anthropic-only here; the file-generation tools are what this unblocks.
        self.last_stop_reason = None
        if self.token_manager and budget:
            self.token_manager.ensure_can_spend(budget)   # may raise BudgetExceededError
        messages = [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}]
        result = _openai_chat_with_tools(model, messages, tool_specs, tool_executor,
                                         max_tokens, max_retries=max_retries,
                                         max_iterations=max_iterations)
        inp, out = result["input_tokens"], result["output_tokens"]
        if self.token_manager and budget:
            self.token_manager.record_cost(budget, config.cost_cny(model, inp, out))
        if self.cost_listener:
            self.cost_listener(model, inp, out)
        return result["text"]


# ---------------------------------------------------------------------------
# RemoteRouter — the desktop daemon's inference path (Phase 11)
# ---------------------------------------------------------------------------
# A ModelRouter that sends EVERY model turn to the CLOUD /v1/chat/completions
# gateway with a per-device token, instead of calling any provider SDK directly. So
# no provider keys live on the customer machine, and the cloud resolves the model +
# China routing + cost-control server-side. Because /v1 is plain-chat-only (it has no
# native tool use), tool-calling agents drive tools via a TEXT protocol and execute
# them LOCALLY (the deliverable builders + the read_file/list_dir/write_file
# filesystem tools), interleaved with remote text-only turns. The loop is therefore
# provider-agnostic on the wire: it works whether the cloud picks Claude or DeepSeek.

_TOOL_CALL_RE = re.compile(r"```tool_call\s*\n(.*?)```", re.DOTALL)


def _default_http_post(url, headers, json_body, timeout):
    """Default transport: POST JSON over stdlib urllib (no `requests` dependency).
    Returns (status_code, parsed_json_dict). A non-2xx HTTPError is returned as its
    (code, body) so the caller maps it to a clear LLMError, not a stack trace."""
    import urllib.error
    import urllib.request
    data = json.dumps(json_body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, (json.loads(body) if body else {})
        except Exception:
            return e.code, {}


def _remote_error_message(status, data):
    """Map a gateway non-200 to a clear message (mirrors app._gateway_error codes)."""
    detail = ""
    if isinstance(data, dict):
        err = data.get("error")
        detail = err.get("message", "") if isinstance(err, dict) else (err or "")
    base = {
        401: "Device token rejected — re-pair this computer (设置 → 设备 → 重新配对).",
        402: "Account balance is exhausted — top up to keep going.",
        429: "Rate limited — too many requests; slow down and retry.",
        503: "The platform is busy or a provider key is missing — try again shortly.",
    }.get(status, f"Cloud gateway error ({status}).")
    return f"{base} {detail}".strip()


def _tool_protocol_preamble(tool_specs):
    """The instruction block appended to the system prompt that documents the tool
    TEXT protocol (since /v1 has no native tool use). Lists each tool's name +
    JSON input schema and the exact ```tool_call``` block the model must emit."""
    specs = list(tool_specs or [])
    if not specs:
        return ""
    lines = [
        "\n\n=== TOOLS ===",
        "You can use tools. To call one, output EXACTLY one fenced block of this form "
        "and nothing after it:",
        "```tool_call",
        '{"tool": "<tool_name>", "input": { ... }}',
        "```",
        "The tool result arrives as the next message. Then either call another tool, "
        "or write your FINAL answer with NO tool_call block. Available tools:",
    ]
    for s in specs:
        lines.append(f"\n- {s['name']}: {(s.get('description') or '').strip()}")
        lines.append("  input JSON schema: "
                     + json.dumps(s.get("input_schema", {}), ensure_ascii=False))
    return "\n".join(lines)


def _parse_tool_call(text):
    """Find a ```tool_call``` block in `text`. Returns a {'tool','input'} dict on a
    valid call, an error STRING on a malformed block, or None when there is none."""
    m = _TOOL_CALL_RE.search(text or "")
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        obj = json.loads(raw)
    except Exception as e:
        return f"invalid JSON in tool_call block: {e}"
    if not isinstance(obj, dict) or "tool" not in obj:
        return "tool_call block must be a JSON object with a 'tool' field"
    return {"tool": str(obj["tool"]), "input": obj.get("input") or {}}


def _strip_tool_call_block(text):
    return _TOOL_CALL_RE.sub("", text or "").strip()


class RemoteRouter(ModelRouter):
    """Drop-in ModelRouter whose turns go to the cloud /v1 gateway via a device token.
    Holds ONLY the tng-dev-… token (never a provider key). See module note above."""

    def __init__(self, cloud_base_url, device_token, token_manager=None,
                 cost_listener=None, *, timeout=120.0, http_post=None):
        super().__init__(token_manager=token_manager, cost_listener=cost_listener)
        base = (cloud_base_url or "").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3].rstrip("/")
        self._endpoint = base + "/v1/chat/completions"
        self._device_token = device_token       # never logged, never written to disk
        self._timeout = timeout
        self._http_post = http_post or _default_http_post

    # -- one remote turn ----------------------------------------------------
    def _remote_chat(self, model, messages, max_tokens):
        headers = {"Authorization": f"Bearer {self._device_token}",
                   "Content-Type": "application/json"}
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        try:
            status, data = self._http_post(self._endpoint, headers, body, self._timeout)
        except Exception as e:
            raise LLMError(f"Could not reach the Tangent cloud gateway: {e}")
        if status != 200:
            raise LLMError(_remote_error_message(status, data if isinstance(data, dict) else {}))
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        return {
            "text": (msg.get("content") or "").strip(),
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "finish_reason": choice.get("finish_reason"),
        }

    def _account(self, model, result, budget):
        """Record ¥ + fire the cost listener after each remote turn — so the local
        budget guard and the run's ¥ tally stay intact even though inference is
        remote (the cloud also records usage server-side, independently)."""
        inp, out = result["input_tokens"], result["output_tokens"]
        if self.token_manager and budget:
            self.token_manager.record_cost(budget, config.cost_cny(model, inp, out))
        if self.cost_listener:
            self.cost_listener(model, inp, out)

    # -- mirror ModelRouter.complete (remote) -------------------------------
    def complete(self, system_prompt, user_content, model=config.OPUS, effort="high",
                 max_tokens=8000, max_retries=3, budget=None, web_search=False):
        # effort/web_search are server-side concerns on /v1 plain chat — accept and drop.
        self.last_stop_reason = None
        if self.token_manager and budget:
            self.token_manager.ensure_can_spend(budget)   # may raise BudgetExceededError
        messages = [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}]
        result = self._remote_chat(model, messages, max_tokens)
        self._account(model, result, budget)
        self.last_stop_reason = "max_tokens" if result.get("finish_reason") == "length" else None
        return result["text"] or "(The model returned no text.)"

    # -- the LOCAL tool-execution loop (remote turns + local tool runs) -----
    def complete_with_tools(self, system_prompt, user_content, tool_specs, tool_executor,
                            model=config.OPUS, effort="high", max_tokens=12000,
                            max_retries=3, budget=None, max_iterations=12,
                            web_search=False):
        self.last_stop_reason = None
        messages = [{"role": "system", "content": system_prompt + _tool_protocol_preamble(tool_specs)},
                    {"role": "user", "content": user_content}]
        texts = []
        last = None
        for _ in range(max_iterations):
            if self.token_manager and budget:
                self.token_manager.ensure_can_spend(budget)   # may raise BudgetExceededError
            last = self._remote_chat(model, messages, max_tokens)
            self._account(model, last, budget)
            assistant_text = last["text"]

            call = _parse_tool_call(assistant_text)
            if call is None:
                if assistant_text.strip():
                    texts.append(assistant_text)
                break

            # A (possibly malformed) tool call: keep any narration, echo the turn.
            narration = _strip_tool_call_block(assistant_text)
            if narration:
                texts.append(narration)
            messages.append({"role": "assistant", "content": assistant_text})

            if isinstance(call, str):   # malformed → feed the error back, keep going
                messages.append({"role": "user",
                                 "content": (f"Your tool_call was invalid ({call}). Fix the "
                                             f"JSON and try again, or give your final answer.")})
                continue

            name, tool_in = call["tool"], call.get("input") or {}
            try:
                result_str = tool_executor(name, tool_in)
            except Exception as e:
                result_str = f"Tool error: {type(e).__name__}: {e}"
            messages.append({"role": "user",
                             "content": (f"Tool result for {name}:\n{result_str}\n\n"
                                         f"Continue: call another tool, or give your final "
                                         f"answer (no tool_call block).")})
        else:
            texts.append("_(Stopped after reaching the tool-call limit.)_")

        self.last_stop_reason = "max_tokens" if (last and last.get("finish_reason") == "length") else None
        joined = "\n\n".join(t.strip() for t in texts if t and t.strip())
        return joined or "(The model produced files but no summary text.)"

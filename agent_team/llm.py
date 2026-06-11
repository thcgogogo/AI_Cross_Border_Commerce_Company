"""
llm.py — One small, robust wrapper around the Claude API.

Every agent calls Claude through this file, so all the best-practice details
live in exactly one place:
  - Uses the official Anthropic Python SDK (never raw HTTP).
  - Streams the response and uses .get_final_message() so long outputs
    never hit a request timeout.
  - Turns on "adaptive thinking" + the "effort" dial for models that support
    them, and quietly skips them for models that don't (so switching a worker
    to Haiku never crashes).
  - Caches the (stable) system prompt to save money on repeated calls.
  - Retries transient network / rate-limit errors with backoff.
"""

import os
import time

import anthropic

from . import config


class LLMError(RuntimeError):
    """Raised when Claude can't be reached after retries, or the key is missing."""


# Anthropic's server-side web search tool. Adding it to a request's `tools` lets
# Claude search the live web and fold current facts into its answer — the search
# runs on Anthropic's servers, so there is nothing for us to execute on the client.
# GA: no beta header required. `max_uses` caps searches per request to bound cost.
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}


class LLMClient:
    def __init__(self, api_key=None, token_manager=None, cost_listener=None):
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        # Strip whitespace/quotes a key can pick up when pasted into .env.
        if api_key:
            api_key = api_key.strip().strip('"').strip("'").strip()
        if not api_key:
            raise LLMError(
                "No ANTHROPIC_API_KEY found. Copy .env.example to .env and paste "
                "your key, or set the ANTHROPIC_API_KEY environment variable."
            )
        self._client = anthropic.Anthropic(api_key=api_key)
        self.token_manager = token_manager  # optional: records spend / enforces budgets
        # optional callback(model, input_tokens, output_tokens) fired after every
        # call so a caller (e.g. the web app) can bill the logged-in account.
        self.cost_listener = cost_listener

    def complete(
        self,
        system_prompt,
        user_content,
        model=config.OPUS,
        effort="high",
        max_tokens=8000,
        max_retries=3,
        budget=None,
        web_search=False,
    ):
        """Send one prompt to Claude and return the plain-text answer.

        If a token_manager + budget are set, this checks the budget BEFORE
        spending (raising BudgetExceededError if it's tapped out) and records
        the real token usage afterward.

        If web_search=True, Claude may search the live web (Anthropic's
        server-side tool) to ground its answer in current facts. A long search
        can make the API return stop_reason='pause_turn'; we resolve that by
        echoing the partial turn back and re-calling until the model finishes."""
        system = [
            {
                "type": "text",
                "text": system_prompt,
                # Caching only kicks in once a prompt is large enough, but
                # marking it here means we benefit automatically as prompts grow.
                "cache_control": {"type": "ephemeral"},
            }
        ]
        messages = [{"role": "user", "content": user_content}]

        # Build the "new" parameters via extra_body so this works across a wide
        # range of installed SDK versions — the API validates them server-side.
        extra_body = {}
        if model in config.MODELS_WITH_ADAPTIVE_THINKING:
            extra_body["thinking"] = {"type": "adaptive"}
        if model in config.MODELS_WITH_EFFORT and effort:
            extra_body["output_config"] = {"effort": effort}

        tools = [WEB_SEARCH_TOOL] if web_search else None

        final = None
        for _ in range(6):  # cap web-search 'pause_turn' resumes
            if self.token_manager and budget:
                self.token_manager.ensure_can_spend(budget)  # may raise BudgetExceededError
            final = self._stream_final(
                model=model, max_tokens=max_tokens, system=system, messages=messages,
                extra_body=extra_body or None, tools=tools, max_retries=max_retries,
            )
            self._meter(model, final, budget)
            if getattr(final, "stop_reason", None) != "pause_turn":
                break
            # A web search is still running: hand the partial turn back so the
            # next call resumes it rather than starting over.
            messages.append({"role": "assistant",
                             "content": _serialize_blocks(getattr(final, "content", []) or [])})
        return _text_of(final)

    # ----------------------------------------------------------------------
    def complete_with_tools(
        self,
        system_prompt,
        user_content,
        tool_specs,
        tool_executor,
        model=config.OPUS,
        effort="high",
        max_tokens=12000,
        max_retries=3,
        budget=None,
        max_iterations=12,
        web_search=False,
    ):
        """Run an agent that can CALL TOOLS, looping until it's done.

        `tool_specs`    : Anthropic tool schemas the model may call.
        `tool_executor` : callable(name, input_dict) -> str. It performs the tool
                          (e.g. writes a real file) and returns a short status string
                          that is handed back to the model as the tool result.

        Returns the model's accumulated text (its narration + closing summary). Budget
        is checked before every API call and usage recorded after each, exactly like
        complete()."""
        system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
        messages = [{"role": "user", "content": user_content}]

        extra_body = {}
        if model in config.MODELS_WITH_ADAPTIVE_THINKING:
            extra_body["thinking"] = {"type": "adaptive"}
        if model in config.MODELS_WITH_EFFORT and effort:
            extra_body["output_config"] = {"effort": effort}

        # Server-side web search (if enabled) rides alongside the agent's own
        # client tools in the same `tools` array. Anthropic runs the search inline,
        # so the executor below never sees it — only the custom tools come back.
        api_tools = list(tool_specs or [])
        if web_search:
            api_tools.append(WEB_SEARCH_TOOL)

        texts = []
        for _ in range(max_iterations):
            if self.token_manager and budget:
                self.token_manager.ensure_can_spend(budget)  # may raise BudgetExceededError
            final = self._stream_final(
                model=model, max_tokens=max_tokens, system=system, messages=messages,
                extra_body=extra_body or None, tools=api_tools, max_retries=max_retries,
            )
            self._meter(model, final, budget)

            blocks = list(getattr(final, "content", []) or [])
            for b in blocks:
                if getattr(b, "type", None) == "text" and (b.text or "").strip():
                    texts.append(b.text)

            stop_reason = getattr(final, "stop_reason", None)
            if stop_reason == "pause_turn":
                # A web search is still running; echo the partial turn back to resume it.
                messages.append({"role": "assistant", "content": _serialize_blocks(blocks)})
                continue

            tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
            if stop_reason != "tool_use" or not tool_uses:
                break

            # Echo the assistant's FULL turn back (preserving any thinking blocks +
            # signatures), then answer every tool call with a tool_result.
            messages.append({"role": "assistant", "content": _serialize_blocks(blocks)})
            results = []
            for tu in tool_uses:
                try:
                    out = tool_executor(tu.name, tu.input)
                except Exception as e:
                    out = f"Tool error: {type(e).__name__}: {e}"
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": str(out)})
            messages.append({"role": "user", "content": results})
        else:
            texts.append("_(Stopped after reaching the tool-call limit.)_")

        joined = "\n\n".join(t.strip() for t in texts if t.strip())
        return joined or "(The model produced files but no summary text.)"

    # ----------------------------------------------------------------------
    def _meter(self, model, final, budget):
        """After a finished call: charge its ¥ cost to the budget (the team's CFO)
        and notify the optional cost_listener (so the web app can bill the account).

        The listener may raise BudgetExceededError to cut a run off when the
        logged-in account runs out of credit — that propagates up like any other
        budget stop, so the orchestrator assembles a partial deliverable."""
        inp, out = _usage_split(final)
        if self.token_manager and budget:
            self.token_manager.record_cost(budget, config.cost_cny(model, inp, out))
        if self.cost_listener:
            self.cost_listener(model, inp, out)

    # ----------------------------------------------------------------------
    def _stream_final(self, *, model, max_tokens, system, messages, extra_body,
                      tools, max_retries):
        """One streamed Messages call with retry/backoff. Returns the final Message."""
        attempt = 0
        while True:
            attempt += 1
            try:
                kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                              messages=messages)
                if tools:
                    kwargs["tools"] = tools
                if extra_body:
                    kwargs["extra_body"] = extra_body
                with self._client.messages.stream(**kwargs) as stream:
                    return stream.get_final_message()
            except (anthropic.RateLimitError, anthropic.APIConnectionError,
                    anthropic.InternalServerError) as e:
                if attempt >= max_retries:
                    raise LLMError(f"Claude API failed after {attempt} tries: {e}")
                time.sleep(2 ** attempt)  # 2s, 4s, 8s backoff
            except anthropic.APIStatusError as e:
                # 4xx (bad request, auth, etc.) won't get better by retrying.
                if e.status_code == 401:
                    raise LLMError(
                        "Claude rejected your API key (401 - invalid key). Open the .env file "
                        "and make sure ANTHROPIC_API_KEY is your full, current key from "
                        "https://console.anthropic.com/settings/keys. Also check you don't have "
                        "an old ANTHROPIC_API_KEY set in your shell (run `echo $ANTHROPIC_API_KEY`) "
                        "that could be overriding it."
                    )
                raise LLMError(f"Claude API error ({e.status_code}): {e}")


def _count_tokens(message):
    """Total tokens a response actually used (input + output + cache), from the API."""
    u = getattr(message, "usage", None)
    if not u:
        return 0
    fields = ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(int(getattr(u, f, 0) or 0) for f in fields)


def _usage_split(message):
    """(input_tokens, output_tokens) from a response, for per-call cost.

    Cache-read + cache-creation tokens are folded into the input side (the catalog
    prices input vs. output, not cache tiers), so costing stays simple and never
    under-counts what the prompt consumed."""
    u = getattr(message, "usage", None)
    if not u:
        return 0, 0
    inp = sum(int(getattr(u, f, 0) or 0) for f in
              ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    out = int(getattr(u, "output_tokens", 0) or 0)
    return inp, out


def _text_of(message):
    """Pull the visible answer out of a response, skipping thinking blocks."""
    parts = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    text = "\n".join(parts).strip()
    return text or "(The model returned no text.)"


# Fields the API returns ON a response block but REJECTS when that block is sent
# back as input (e.g. `parsed_output`, added to text blocks by newer SDKs). Echo
# one back unchanged and the next call 400s with "Extra inputs are not permitted".
_OUTPUT_ONLY_BLOCK_FIELDS = ("parsed_output",)


def _serialize_blocks(blocks):
    """Turn response content blocks back into plain dicts we can resend as an
    assistant turn. Preserves every INPUT-valid field (incl. thinking signatures
    + tool_use input) so a tool-use conversation can continue across turns, while
    dropping null fields and output-only ones the API won't accept back."""
    out = []
    for b in blocks:
        if hasattr(b, "model_dump"):
            d = b.model_dump(exclude_none=True)
        elif isinstance(b, dict):
            d = {k: v for k, v in b.items() if v is not None}
        elif getattr(b, "text", None) is not None:
            d = {"type": "text", "text": b.text}
        else:
            continue
        for f in _OUTPUT_ONLY_BLOCK_FIELDS:
            d.pop(f, None)
        out.append(d)
    return out

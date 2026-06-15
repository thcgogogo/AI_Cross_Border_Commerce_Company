<!-- WS05 · Phase 2 · depends_on: WS1, WS4 -->
# WS05 — RemoteRouter + local tool-execution loop

> **Phase 2** · **Depends on:** WS1, WS4 · **Key files:** agent_team/providers.py, agent_team/base_agent.py (the executor seam at _run_with_tools), agent_team/deliverables.py (LOCAL_TOOL_SPECS + run_tool)

---

## Objective

Add a **RemoteRouter** to `agent_team/providers.py` — a subclass of the existing `ModelRouter` whose `complete()` and `complete_with_tools()` send every model turn to a remote **`${CLOUD}/v1/chat/completions`** gateway authenticated by a per-device token (Bearer `tng-dev-…`), instead of calling the Anthropic/OpenAI SDKs directly. Because that `/v1` gateway is **plain-chat-only** (it does NOT support Anthropic-style tool use), and the deliverable file-builders are Anthropic-tool-only today, the make-or-break new piece is a **LOCAL tool-execution loop**: a Python loop that runs the deliverable builders (and three new filesystem tools `read_file` / `list_dir` / `write_file`) **on the device**, interleaved with remote text-only model turns over `/v1`. The deliverable builders are pure-Python (openpyxl / reportlab / python-docx / python-pptx) and need no LLM, so they run locally; only the text turns go to the cloud.

This is the glue that lets file-building agents (Data Analyst → real `.xlsx`, Media Producer → `.pptx/.pdf/.docx/.svg`) work on the desktop daemon against **both** Claude and domestic DeepSeek/Qwen, since the device never relies on the gateway to execute tools.

This workstream is the **inference + tool-execution seam only**. It does NOT build the daemon, the pairing flow, the device-auth endpoints, the catalog endpoints, the database tables, or the migration. Those are other workstreams. See **Out of scope** below — stay inside the seam.

## Read these first (exact files/functions, with the patterns to copy)

Read fully before writing any code:

1. **`agent_team/providers.py`** — the whole file. The class you are extending is `ModelRouter` (line ~198). Note:
   - `complete(self, system_prompt, user_content, model=config.OPUS, effort="high", max_tokens=8000, max_retries=3, budget=None, web_search=False)` — returns a plain string.
   - `complete_with_tools(self, system_prompt, user_content, tool_specs, tool_executor, model=config.OPUS, effort="high", max_tokens=12000, max_retries=3, budget=None, max_iterations=12, web_search=False)` — returns a plain string. `tool_executor` is `callable(name, input_dict) -> str`.
   - The `.token_manager`, `.cost_listener`, and `.last_stop_reason` attributes — preserve all three on the subclass.
   - `gateway_chat(model_id, messages, max_tokens)` returns `{"text","input_tokens","output_tokens","model","provider"}` — this is the **shape the remote `/v1` endpoint mirrors** (the `x_tangent` extra field carries `cost`/`balance`/`provider`).
   - `ProviderKeyMissing` / `LLMError` exception classes (imported from `.llm`).

2. **`agent_team/llm.py`** — read `LLMClient.complete_with_tools` (line ~117) **carefully**: this is the canonical Anthropic native tool loop you must re-implement against `/v1` plain-chat messages. Specifically copy its CONTROL FLOW: loop up to `max_iterations`, append assistant turns, run each requested tool via `tool_executor`, feed results back, accumulate text, stop when no more tool calls. Also read `_text_of`, `_serialize_blocks`, and the `_meter` helper for the accounting pattern. **Do NOT** reuse its Anthropic `tool_use`/`tool_result` block format over the wire — `/v1` is OpenAI-shaped plain chat; your loop must drive tool selection through TEXT (see the spec below), because the gateway will not return native tool-call structures.

3. **`agent_team/base_agent.py`** — `Agent._run_with_tools` (line ~82) is **the executor seam**. It builds an `executor(name, tool_input)` closure that calls `deliverables.run_tool(name, tool_input, out_dir)` and registers each produced file in shared memory, then passes that executor to `self.llm.complete_with_tools(...)`. Your RemoteRouter's `complete_with_tools` receives exactly this `tool_executor` and `tool_specs` and must call the executor the same way. **You may extend `_run_with_tools`'s executor to also dispatch the three new filesystem tools** (`read_file`/`list_dir`/`write_file`) — see spec — but keep the existing deliverable path byte-for-byte compatible.

4. **`agent_team/deliverables.py`** — `TOOL_SPECS`, `TOOL_NAMES`, `specs_for(names)`, and `run_tool(name, tool_input, out_dir)` (returns a result dict `{ok, message, kind, ext, title, filename, path}`, never raises). The five builders (`create_slide_deck`, `create_pdf_document`, `create_word_document`, `create_svg_image`, `create_excel_workbook`) are already registered in `_BUILDERS`. You will ADD a new constant `LOCAL_TOOL_SPECS` and extend `run_tool` (or add a sibling dispatcher) for the read/list/write filesystem tools.

5. **`agent_team/file_tools.py`** — `parse_file(path)` returns `{name, ext, chars, text, meta}` and reads xlsx/csv/docx/pdf/json/txt verbatim into text (60k-char cap). This is what `read_file` must delegate to so the model reads local files the same way an uploaded document is read.

6. **`agent_team/orchestrator.py`** lines 36–67 — how `ModelRouter` is constructed today: `self.llm = llm or ModelRouter(token_manager=token_manager, cost_listener=_tally)`. The Orchestrator accepts an injected `llm`. **Do not change the orchestrator's default**; RemoteRouter is injected by the daemon (a future workstream) via this same `llm=` seam. Your job is only to make RemoteRouter a valid drop-in for that seam.

7. **`agent_team/token_manager.py`** — `ensure_can_spend(budget)` (raises `BudgetExceededError`) and `record_cost(budget, cny)`. Read how `config.cost_cny(model, in, out)` converts token usage to ¥.

8. **`tests/test_deliverables.py`** and **`tests/conftest.py`** — the test conventions (path-insertion header, `crypto.read_plaintext` to load encrypted-at-rest files, `tmp_path`, hermetic env). Mirror these in your new test file.

## Implementation spec

### A. RemoteRouter in `agent_team/providers.py`

Add a `RemoteRouter(ModelRouter)` subclass. Constructor:

```
def __init__(self, cloud_base_url, device_token, token_manager=None, cost_listener=None, *, timeout=120.0, http_post=None):
```

- `cloud_base_url` — e.g. `https://api.tangent.example` (no trailing `/v1`); the router appends `/v1/chat/completions`. Strip a trailing slash and a trailing `/v1` defensively.
- `device_token` — the `tng-dev-…` token; sent as `Authorization: Bearer <token>`. **Never log it, never write it to disk** (this router only receives it; storage is another workstream).
- `http_post` — an **injectable transport** `callable(url, headers, json_body, timeout) -> (status_code:int, json_dict:dict)`. Default to a thin wrapper over `requests.post`. This injection point is mandatory so tests drive the router with a fake transport and **no real network**.
- Preserve `self.token_manager`, `self.cost_listener`, `self.last_stop_reason`.

Add a private `_remote_chat(self, model, messages, max_tokens)` that:
1. Calls `self.token_manager.ensure_can_spend(budget)` is the CALLER's job (mirror `ModelRouter.complete`); `_remote_chat` itself just performs the HTTP call.
2. POSTs `{"model": model, "messages": messages, "max_tokens": max_tokens}` to `${cloud}/v1/chat/completions` with the Bearer header via `self._http_post`.
3. Parses the OpenAI-shaped response: `text = choices[0].message.content`; `usage.prompt_tokens` / `usage.completion_tokens`. Returns `{"text","input_tokens","output_tokens"}`.
4. Maps non-200s to `LLMError` with a clear message (401 → "device token rejected — re-pair this computer"; 402 → balance/insufficient; 429 → rate limited; 503 → platform busy / provider key missing; other → upstream error). These mirror the gateway's `_gateway_error` codes.

**`complete()` override:** identical structure to `ModelRouter.complete`'s non-Anthropic branch, but routed through `_remote_chat`:
- `self.last_stop_reason = None`
- if `self.token_manager and budget`: `self.token_manager.ensure_can_spend(budget)` (may raise `BudgetExceededError`).
- Build `messages = [{"role":"system","content":system_prompt},{"role":"user","content":user_content}]`, call `_remote_chat`.
- After the call: if `token_manager and budget`: `record_cost(budget, config.cost_cny(model, inp, out))`; if `self.cost_listener`: `self.cost_listener(model, inp, out)`. **This keeps token_manager + COGS accounting intact even though inference is remote** — the cloud also records usage server-side; this client-side tally feeds the run's ¥ summary and the local budget guard.
- Ignore `effort`/`web_search` over the wire (the cloud resolves model behavior server-side; `/v1` plain chat has no web_search). Do not crash on those kwargs — accept and drop them, matching how the OpenAI-compatible path already ignores Anthropic-only features.

**`complete_with_tools()` override — THE LOCAL TOOL-EXECUTION LOOP.** This is the core deliverable. Because `/v1` returns only assistant text (no native tool calls), implement a **text-protocol tool loop**:

1. Compose a tool-instruction preamble appended to the system prompt that documents the available tools (their names + JSON input schemas, drawn from the passed `tool_specs` PLUS `deliverables.LOCAL_TOOL_SPECS` when the agent has filesystem tools) and instructs the model: when it wants to use a tool, emit a single fenced block of the exact form:

   ```
   ```tool_call
   {"tool": "<name>", "input": { ... }}
   ```
   ```

   and STOP; otherwise produce its final deliverable text. Keep the contract tight and unambiguous (mirror the discipline of the existing `TOOL_SPECS` descriptions).
2. Loop up to `max_iterations` (default 12, same as the Anthropic path):
   - Before each remote call: `ensure_can_spend(budget)` (raise `BudgetExceededError` to cut off).
   - Call `_remote_chat(model, messages, max_tokens)`; record cost + fire `cost_listener` exactly as in `complete()` (one `record_cost` per remote turn).
   - Parse the assistant text for a `tool_call` block. **If none**: this is the final answer — accumulate it and break.
   - **If a tool_call is present**: extract `{tool, input}`, append the assistant turn to `messages`, then DISPATCH:
     - If `tool` is in `deliverables.TOOL_NAMES` or a filesystem tool → call the passed `tool_executor(tool, input)` (the base_agent closure already routes deliverables; you extend it for filesystem tools — see C). The executor returns a status string.
     - Append a `{"role":"user","content": "<tool result for `tool`>:\n" + result_string}` turn so the next remote turn sees the outcome (this is the OpenAI-shaped analog of an Anthropic `tool_result`).
   - Continue the loop.
   - If the loop hits `max_iterations` without a final answer, append a `_(Stopped after reaching the tool-call limit.)_` note, same as `llm.py`.
3. Set `self.last_stop_reason` (`"max_tokens"` only if the remote `finish_reason`/usage indicates truncation — otherwise `None` or `"end_turn"`). Return the accumulated narration text (joined non-empty text segments), exactly like `LLMClient.complete_with_tools`. Be robust to malformed/partial JSON in a `tool_call` block: on a parse failure, feed an error string back to the model as a tool result and continue (do not crash the run) — honor the honesty rule, never fabricate a file.

Keep the loop **provider-agnostic on the wire**: it must work whether the cloud resolves the agent to Claude or to DeepSeek/Qwen, because tool selection happens in your text protocol, not via Anthropic tool-use blocks.

### B. `LOCAL_TOOL_SPECS` + filesystem tools in `agent_team/deliverables.py`

Add a new module-level constant `LOCAL_TOOL_SPECS` — a list of three tool specs in the SAME shape as `TOOL_SPECS` (`name`, `description`, `input_schema`), for:

- **`read_file`** — input `{ "path": string }`. Reads a local file into text via `file_tools.parse_file(path)` and returns its `text` (so the model reads xlsx/csv/docx/pdf verbatim). Description must say it reads a file the user has approved into the workspace.
- **`list_dir`** — input `{ "path": string }`. Lists names + sizes of files in an approved directory (one per line). No recursion required for v1.
- **`write_file`** — input `{ "path": string, "content": string }`. Writes a plain-text/markdown artifact to an approved path. (The structured builders — xlsx/pdf/docx/pptx/svg — remain the `TOOL_SPECS` path; `write_file` is for plain text the model authors directly.)

Add a dispatcher `run_local_tool(name, tool_input, workspace_guard)` (or extend `run_tool` with a clearly-separated branch) that executes these three. It MUST:

- Route EVERY path through a single **default-deny chokepoint** before any open/read/write. The chokepoint is `agent_team/workspace.py:assert_in_workspace(path, approved_roots)` — **a function this workstream depends on (WS4); assume it exists with that signature and raises on an escape.** If WS4's `workspace.py` is not yet present in the repo when you implement this, create a minimal `agent_team/workspace.py` with `assert_in_workspace(path, approved_roots)` that canonicalizes with `os.path.realpath` + `os.path.commonpath` against `approved_roots` and raises `PermissionError` on a `..`/symlink escape — and leave a `# WS4 owns this; minimal placeholder` comment so the dependency is explicit. Do NOT open any file before the guard returns clean.
- Never raise out of `run_local_tool` — return the same `{ok, message, ...}` result-dict contract as `run_tool`, so the executor can hand a clean status string back to the model (honesty rule: a denied path returns a clear "outside the approved workspace" message, not a fake success).
- For `write_file`: call `crypto.encrypt_file_in_place(path)` after writing, exactly as `run_tool` does for builders (files-encrypted-at-rest invariant; no-op when FERNET unset).

Keep `specs_for` working unchanged. Add a helper so callers can fetch the local specs (e.g. `local_specs()` returning `LOCAL_TOOL_SPECS`), and ensure `TOOL_NAMES` and a new `LOCAL_TOOL_NAMES` set are both available for the router's dispatch decision.

### C. The executor seam in `agent_team/base_agent.py`

Extend `_run_with_tools`'s `executor(name, tool_input)` closure so that when `name` is a filesystem tool (`read_file`/`list_dir`/`write_file`), it calls the new `deliverables.run_local_tool(...)` against the run's approved workspace roots, and otherwise keeps the **exact existing** `deliverables.run_tool(name, tool_input, out_dir)` behavior (register produced deliverable in memory, log to the feed, return the status string). The approved workspace roots arrive via the memory/run context the daemon sets up (WS4) — read them from `self.memory` (e.g. an `approved_roots()` accessor) with a safe fallback to `[out_dir]` so existing cloud runs (no daemon, no extra roots) keep working unchanged. **Do not change the cloud path's behavior**: when the agent runs with the normal `ModelRouter` (Anthropic native tools), `complete_with_tools` still uses Anthropic tool-use and the executor is called identically — the filesystem tools simply aren't in `tool_specs` for a cloud run.

The agent must remain config-driven: an agent gains a filesystem tool the same way it gains a builder — by listing the tool NAME in `cfg["tools"]`. Update `Agent.run`'s tool-name partitioning so `read_file`/`list_dir`/`write_file` resolve to `LOCAL_TOOL_SPECS` (currently `deliverables.specs_for(...)` only knows `TOOL_SPECS`). Make `specs_for` (or a new combined helper) return specs from both `TOOL_SPECS` and `LOCAL_TOOL_SPECS` so a mixed tool list (`["read_file","create_excel_workbook","web_search"]`) is handled correctly, with `web_search` still pulled out as a capability flag as today.

## Invariants you MUST respect

- **China domestic mode keeps working.** RemoteRouter must NOT depend on Anthropic reachability or any provider SDK on the client. It only speaks HTTP to `${CLOUD}/v1/chat/completions`. The cloud resolves the model (`config.effective_model` + `provider_for` + `TEAM_MODEL_PRESET`) server-side; the device never picks a model. The local tool loop must produce real files on BOTH Claude and DeepSeek/Qwen.
- **No model/provider keys on the customer machine.** RemoteRouter holds only the `tng-dev-…` device token. Never read `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / any provider key in the RemoteRouter path. Never call `providers.gateway_chat` / `_openai_chat` / `_anthropic_chat` from RemoteRouter (those read provider keys from env). All inference goes over the wire.
- **¥ cost-control stays intact.** Keep `token_manager.ensure_can_spend` before every remote turn and `record_cost`/`cost_listener` after — so the local budget guard and the run's ¥ tally still work. (Server-side `record_usage` + `platform_guard` on `/v1` is owned by the gateway/device-auth workstream; do not duplicate or bypass it here.)
- **Founder-gating / no machinery leak to the device.** RemoteRouter never receives or serializes raw model ids, per-token ¥ rates, or provider names from the founder-only manifest half. It sends only `model` strings the agent config already carries through the normal seam; do not add any new field that exposes COGS to the device.
- **Files encrypted at rest.** `write_file` calls `crypto.encrypt_file_in_place` after writing, identical to `run_tool`.
- **Honesty / label-demo-vs-real.** Never fabricate a tool result or a file. A denied workspace path, a builder failure, or a malformed tool_call returns a clear message that the model (and ultimately the user) sees — never a silent fake success. The deliverable builders already produce REAL files; the local loop must preserve that — no stubbed/placeholder outputs.
- **Drop-in compatibility.** RemoteRouter must satisfy the same surface the Orchestrator/Agent rely on: `complete`, `complete_with_tools`, `token_manager`, `last_stop_reason`. A run injected with a RemoteRouter must complete without any other orchestrator/agent change.

## Acceptance criteria + tests

Add a new test file **`tests/test_remote_router.py`** (follow `tests/test_deliverables.py` conventions: project-root path insertion, `tmp_path`, `crypto.read_plaintext` for produced files, hermetic — **no real network, no real provider keys**). Drive RemoteRouter with a **fake `http_post`** that returns canned OpenAI-shaped responses. Cover:

1. **`complete()` posts to `/v1` and meters.** Assert the fake transport receives `Authorization: Bearer tng-dev-…`, the URL ends `/v1/chat/completions`, the body has `model`/`messages`/`max_tokens`; assert the returned text equals the canned `choices[0].message.content`; assert `token_manager.record_cost` was called with the ¥ from `config.cost_cny(model, prompt_tokens, completion_tokens)` and `cost_listener` fired with `(model, in, out)`.
2. **No provider keys / no Anthropic SDK touched.** With `ANTHROPIC_API_KEY` and all provider keys unset in the env (monkeypatch.delenv), `complete()` and `complete_with_tools()` still succeed through the fake transport — proving the client needs no keys (domestic-mode parity).
3. **Local tool loop builds a REAL file end-to-end.** Script the fake transport to return, on turn 1, an assistant message containing a ```tool_call``` block invoking `create_excel_workbook` with valid `{title, sheets}`; on turn 2, a plain final-summary text (no tool_call). Run `complete_with_tools` with the base_agent-style executor (or a thin stand-in calling `deliverables.run_tool`) and a `tmp_path` out_dir. Assert: a real `.xlsx` is written and registered (load it back via `crypto.read_plaintext` + openpyxl, verify a header/number survives as a real number), and the returned narration includes the final summary. Assert one `record_cost` per remote turn (2 calls).
4. **`read_file` goes through the workspace chokepoint.** With an approved root = `tmp_path`, a `tool_call` invoking `read_file` on a file inside `tmp_path` returns its parsed text; a `read_file` on a path OUTSIDE the approved roots (e.g. `/etc/hosts` or `tmp_path/../escape`) returns a clear "outside the approved workspace" denial result and does NOT read the file. Assert `assert_in_workspace` is the gate (the denial happens before any open).
5. **`max_iterations` guard.** A transport that returns a tool_call EVERY turn terminates after `max_iterations` with the "Stopped after reaching the tool-call limit." marker and does not loop forever.
6. **Budget cutoff.** A `token_manager` whose budget is exhausted raises `BudgetExceededError` before the first remote POST (assert the fake transport was never called).
7. **HTTP error mapping.** A transport returning a 401 raises `LLMError` mentioning re-pairing; a 402 maps to a balance message — assert the run surfaces a clear error rather than a stack trace.
8. **Malformed tool_call resilience.** A turn with a broken-JSON ```tool_call``` block feeds an error string back to the model (next turn) instead of raising; the loop continues and can still finish.

Also extend **`tests/test_deliverables.py`** (or add to the new file) to assert: `LOCAL_TOOL_SPECS` contains exactly `read_file`/`list_dir`/`write_file` with `path` required (and `content` required on `write_file`); `run_local_tool` writes a real `write_file` artifact, encrypts it at rest, and denies an out-of-workspace write.

**Run the full suite** with `.venv/bin/python -m pytest -q` and confirm the existing 200+ tests still pass (no regressions in base_agent / providers / deliverables). Run your new file with `-v`.

## Explicitly OUT of scope (do NOT build these here)

- The desktop daemon / supervisor (`agent_team/runner/`), tray icon, waitress boot, `LOCAL_MODE` flag, port picking, watched folders, native pickers — **not this workstream.**
- The pairing flow and **device-auth endpoints** (`/api/devices/pair-code`, `/api/devices/exchange`, `/api/devices`, `/api/devices/<id>/revoke`), `_current_device()` in app.py, and the `devices` token mint/verify-by-hash store — **other workstream.** RemoteRouter only *uses* a token it is handed.
- The **catalog endpoints** (`/api/agents/catalog`, `/api/agents/<id>/publish`, `/api/agents/<id>/install`), the manifest split (runner-visible vs founder-only), and the local registry loader — **other workstream.**
- The **run/telemetry sync** endpoints (`/api/devices/run/start`, `/finish`, `/event`), the `runs.origin`/`device_id` columns, `kind='device_run'`, and `metrics_store` changes — **other workstream.**
- The **`0007_phase11_devices.py` migration** and the `devices`/`agent_catalog`/`agent_install`/`agent_run` models — **other workstream.**
- Extending the cloud `/v1/chat/completions` auth to accept device tokens (`verify_device_token`, per-device RPM, `min(device_cap, account_cap)`, device model-subset intersection) — **other (gateway/device-auth) workstream.** This workstream is the CLIENT side only.
- The OS-keychain token storage (`keyring`), the Tauri shell, PyInstaller packaging, the websocket bridge fallback (Form Factor C), and the offline/local-model (`ollama`) escape hatch — **all out of scope.**
- `agent_team/workspace.py`'s full implementation is **WS4's**; only create a minimal placeholder `assert_in_workspace` if it is genuinely absent when you implement, clearly commented as WS4-owned.

Keep the diff confined to: `agent_team/providers.py` (RemoteRouter), `agent_team/deliverables.py` (`LOCAL_TOOL_SPECS` + `run_local_tool` + name sets + spec helper), `agent_team/base_agent.py` (executor seam extension + tool-name partitioning), the new `tests/test_remote_router.py`, a small extension to `tests/test_deliverables.py`, and — only if absent — a minimal `agent_team/workspace.py` placeholder.
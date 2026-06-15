<!-- WS12 · Phase 4 · depends_on: WS1, WS5 -->
# WS12 — Watched-folder automation + offline Local-Model mode (optional)

> **Phase 4** · **Depends on:** WS1, WS5 · **Key files:** agent_team/runner/watcher.py (new), agent_team/config.py (PROVIDERS + LOCAL_MODEL_MODE), agent_team/providers.py, agent_team/runner/tray.py

> **LOCKED DECISION (2026-06-15) — offline Local-Model mode is v2, additive-only.** Defer the Ollama/offline mode to v2 (this workstream). It must be **purely additive**: one `PROVIDERS['ollama']` entry (`base_url http://127.0.0.1:11434/v1`) + a `LOCAL_MODEL_MODE` toggle that makes `effective_model()` resolve every agent to the local endpoint — **no changes to the cloud-proxy default path**. Position it as the *degraded* tier (no tools/web_search, no ¥/COGS metering, lower quality), clearly labeled per the honesty rule; never the default. Phases 0–3 must keep all model resolution behind `effective_model()`/`PROVIDERS` so this drops in cleanly.

---

# Execution Prompt — Watched-folder automation + optional offline Local-Model mode

## 0. OBJECTIVE

Add two capabilities to the local "Tangent Agent" runner in this repo (`/Users/brucetang/Company_Agent_Demo`):

1. **Watched-folder automation** — a `watchdog`-based directory watcher that maps a watched folder to an agent id and **enqueues a local agent run** whenever a supported file lands or changes in that folder. It must **debounce** rapid filesystem events and move each handled file into a `processed/` archive subfolder so the same file never re-triggers an infinite loop. The list of watch rules is registered from the runner's tray menu and persisted to disk.

2. **Optional offline Local-Model mode** — add a `PROVIDERS['ollama']` entry (`base_url` `http://127.0.0.1:11434/v1`, OpenAI-compatible) plus a `LOCAL_MODEL_MODE` toggle so that, when enabled, `config.effective_model()` resolves **every** agent to a local Ollama model and inference runs entirely on-device (file content never leaves the machine). This mode is a **degraded, explicitly-labelled escape hatch** for the regulated/air-gapped segment: it bypasses the ¥ wallet / `platform_guard` / `record_usage` COGS accounting, loses all Anthropic-only tools (file builders via the LLM, `web_search`), and a small local model is materially weaker than the cloud models. It is **never the default** and every output produced in this mode must be labelled as degraded per the repo's honesty rule.

This workstream is **Phase 4** and **depends on WS1 (the tray-resident local daemon / `agent_team/runner/` package and `agent_team/runner/tray.py`) and WS5 (the local tool-execution loop + `agent_team/workspace.py:assert_in_workspace` path chokepoint + the path-aware local run entry point)**. Those packages may not be fully present yet. **Read §2 carefully** — you must detect what WS1/WS5 already provide and integrate with their real interfaces if present, or build against the documented contract and clearly-isolated seams if not, so this code drops in cleanly when they land. Do **not** re-implement the cloud daemon, pairing/device tables, the `/v1` gateway auth changes, or the local tool loop here — those belong to WS1/WS5 and are out of scope (see §6).

---

## 1. READ THESE FIRST (exact paths + why)

Read each of these before writing any code; the patterns below are the ones you must follow, not invent around.

- `agent_team/config.py` — the single config file. Study especially:
  - `PROVIDERS` dict (lines ~53–80): every non-Anthropic provider is `{"label","sdk":"openai_compatible","env_key","base_url"}`. You will add an `ollama` entry in exactly this shape.
  - `effective_model(agent_id)` (bottom of file, ~line 1145): precedence is per-agent UI override → domestic preset → static default. You will add the `LOCAL_MODEL_MODE` branch here with the **highest** precedence.
  - `team_model_preset()` / `_env()` / `_TRUE` / `_env_int()` helpers — the env-flag idiom you must reuse for the new toggle (a function that reads an env var, lower-cased, truthy against `_TRUE`).
  - `provider_for(model_id)` (~line 822) and `MODEL_CHOICES` — note the catalog is keyed by model id; a local model id will not be in `MODEL_CHOICES`, so `provider_for` defaults it to `"anthropic"`. You must handle that (see §3.B).
  - `AGENTS` list shape (`id`/`name`/`title`/`name_cn`/`reports_to`/`model`/`effort`/`tools`/`system_prompt`) — the watcher maps a folder to one of these `id`s.
  - `COST_BUDGETS_CNY` / `AGENT_TEAM_BUDGET` and the honesty/branding helpers (`product_byline`, etc.).
- `agent_team/providers.py` — `ModelRouter` (drop-in for `llm.LLMClient`), `_openai_chat()` (the single OpenAI-compatible adapter that already speaks every non-Anthropic provider), `provider_key(model_id)` (reads the provider's `env_key` from the environment), `complete_with_tools()` (lines ~260–282: file builders + web_search are **Anthropic-only** and degrade to a plain-text note on every other provider — Ollama included). This is why Local-Model mode is "no tools".
- `agent_team/orchestrator.py` — how a run is actually executed (`Orchestrator(memory, ...).run()`), how `ModelRouter` is constructed and shared, how `config.effective_model("ceo")` is called per step, and how `_finish_run` records cost. The watcher must launch a run through the **same** orchestrator path WS1/WS5 expose, not a parallel one.
- `agent_team/file_tools.py` — `parse_file(path)` and the supported extensions (`pdf`, `docx`, `xlsx`/`xls`, `csv`, `json`, `txt`/`md`/`markdown`/`log`/`text`). The watcher's "supported file types" set must be derived from / consistent with this.
- `agent_team/deliverables.py` — `run_tool()` and `_BUILDERS` (pure-python openpyxl/reportlab/python-docx/python-pptx — **no LLM**), and the module docstring's **honesty rule**: "We never fake a binary we can't actually make." Your degraded-mode labelling must be in the same spirit.
- `app.py` — `run()` (~line 788) is the canonical cloud run launcher: validate → create `SharedMemory(job_id, ...)` → `parse_file` each upload into memory → `metrics_store.get_or_create_company` + `create_run` → launch `_run_team` in a daemon `threading.Thread`. Read `_run_team` (~line 1000) and `_make_biller` (~line 943). The watcher's enqueue path is the local analogue of this. **Do not** add Flask routes in this workstream.
- `agent_team/memory.py` — `SharedMemory` constructor signature (`job_id`, `store_dir`, `goal`, `output_dir`) and `.log(...)` for the live feed.
- `tests/conftest.py` — the test harness (limiter disabled, `app.load_dotenv` no-op'd for hermeticity). New tests must respect this.
- `tests/test_deliverables.py` and `tests/test_china_domestic.py` — the style of the per-feature pytest suites and how `effective_model` / domestic mode are asserted. Mirror this style.
- `requirements.txt` — where to add the new `watchdog` dependency.
- `migrations/versions/0006_phase7_single_report.py` — the inspector-guarded, idempotent migration idiom. **This workstream does NOT add a migration** (see §6), but read it so you understand why: it is owned by WS1's device tables. Do not create `0007` here.

Run this first to see what WS1/WS5 have already landed:
```
ls agent_team/runner/ 2>/dev/null; ls agent_team/workspace.py 2>/dev/null
grep -rniE "assert_in_workspace|LOCAL_MODE|def .*local.*run|run_local|enqueue" agent_team/ app.py
```

---

## 2. INTEGRATION CONTRACT WITH WS1 / WS5 (detect-then-integrate)

Because this is Phase 4 and depends on WS1/WS5, **do not hard-fail if their code is absent**. Instead:

**A. The runner package (`agent_team/runner/`)** — WS1 owns this directory, `tray.py`, the waitress supervisor, pairing, and the `RemoteRouter`. If `agent_team/runner/__init__.py` does not exist, create the package directory with a minimal `__init__.py` (a module docstring only) so `agent_team/runner/watcher.py` can live there, and leave a clear `# WS1 owns the supervisor/tray; this module is the folder-watcher only.` comment. If it exists, add your files alongside without modifying WS1's supervisor.

**B. The local run entry point** — WS5/WS1 expose the function that actually starts an on-device agent run from local file **paths** (the path-aware analogue of `app.run()` / `_run_team`). Discover it via grep. Define a **single indirection** in `watcher.py`:
```
def _resolve_run_enqueuer():
    """Return a callable enqueue(agent_id, file_paths, *, source) -> run_handle.
    Prefer the WS1/WS5 daemon's real enqueue function when present; otherwise
    fall back to the local _default_enqueue below so the watcher is testable
    standalone."""
```
The fallback `_default_enqueue` must build the run the **same way `app.run()` does**: create a `SharedMemory`, call `file_tools.parse_file(path)` for each file and `memory.add_document(doc)`, then run `Orchestrator(memory, ...).run()` in a daemon thread. It MUST route every path through **`agent_team.workspace.assert_in_workspace(path)` if that function exists** (WS5's default-deny chokepoint) before `parse_file` opens anything; if `workspace.py` is absent, call a local `_assert_supported_and_safe(path)` that at minimum: `os.path.realpath`-canonicalizes the path, rejects it if it is not inside one of the watcher's configured (approved) roots via `os.path.commonpath`, and rejects symlinks/`..` escapes. Leave a `# TODO(WS5): delegate to workspace.assert_in_workspace once present` marker. **Never open a file the watcher saw without this check.**

**C. The tray menu** — WS1 owns `agent_team/runner/tray.py` and its pystray menu (`Open dashboard / Pair account / Watched folders… / Pause / Quit`). You implement the **"Watched folders…"** action's backing logic in `watcher.py` (add/remove/list watch rules, start/stop the observer, pause/resume), exposing a clean API the tray calls. If `tray.py` exists, wire the menu item to your API with the smallest possible edit; if it does not, still provide the API and a short docstring stating which menu item it backs, so WS1 can wire it. Do **not** build a full GUI here — a watch rule is added programmatically / via a simple prompt; the native folder picker is a v2 concern (out of scope).

---

## 3. IMPLEMENTATION SPEC

### A. `agent_team/runner/watcher.py` (NEW — the bulk of this workstream)

A self-contained module implementing the folder watcher. Use the `watchdog` library (`from watchdog.observers import Observer`; `from watchdog.events import FileSystemEventHandler`). Structure:

**Watch rules + persistence**
- A watch rule is a dataclass / dict: `{root: <abs canonicalized dir>, agent_id: <config.AGENTS id>, enabled: bool}`.
- Persist the rule list as JSON to a runner state dir. Reuse whatever state-dir convention WS1 established (grep for it); if none, default to `os.path.join(<repo data dir or ~/.tangent-agent>, "watch_rules.json")` via a module-level `_state_path()` helper. Provide `load_rules()` / `save_rules()`.
- `add_rule(root, agent_id)`: validate `config.get_agent(agent_id) is not None` (raise `ValueError` with a clear message otherwise, mirroring `config.set_model_override`); canonicalize `root` with `os.path.realpath` and require it to be an existing directory; refuse to nest a new root inside (or as a parent of) an existing root (overlapping watches double-fire) — compare with `os.path.commonpath`. Persist and, if the observer is running, schedule it live.
- `remove_rule(root)` / `list_rules()` — straightforward.

**Supported file types**
- `SUPPORTED_EXTS` — a frozenset derived from `file_tools.parse_file`'s handled extensions: `{"pdf","docx","xlsx","xls","csv","json","txt","md","markdown","log","text"}`. A landed file whose extension is not in this set is **ignored** (logged at debug, not enqueued). Do not enqueue files inside the `processed/` archive (see below) or hidden/temp files (skip names starting with `.` or `~$`, and skip `*.crdownload`/`*.part`/`*.tmp` partial-download artifacts).

**Debounce**
- Filesystem writes fire many `modified`/`created` events for one logical save (especially large xlsx). Implement a per-path debounce: on each event for a candidate path, (re)start a `threading.Timer` keyed by the path; only when the timer fires (default **`DEBOUNCE_SECONDS = 2.0`**, overridable via an env var read through the `config._env_int`-style idiom) do you treat the file as "settled" and proceed. Additionally guard with a **stat-stability check**: before enqueuing, confirm the file's size is non-zero and unchanged across two reads ~`0.5s` apart (a file still being written keeps growing). This prevents enqueuing a half-copied file. Make debounce/stability windows injectable so tests can drive them fast.

**Archive (no-loop guarantee)**
- On enqueue, **move** the file into a `processed/` subfolder of its watched root (create it lazily), with a timestamp/uniqueness suffix to avoid clobbering (reuse the `_unique`-style logic spirit from `deliverables.py`). The `processed/` dir is itself excluded from watching (the handler ignores any path whose parts contain `processed`). Moving (not copying) the source out of the watched root is what guarantees a file is handled exactly once and the watcher can never loop on its own output. If the move fails (cross-device, permissions), fall back to writing a sibling `<name>.processed` marker file and skip the source on future events; log the degradation honestly — never silently drop the file.

**Enqueue**
- For a settled, supported, in-workspace file: resolve the run enqueuer (§2.B) and call it with `(agent_id, [path], source="watched_folder:<root>")`. Wrap in try/except so one bad file never kills the observer thread; log failures to the runner log and (if a `SharedMemory`/feed is available) the live feed. The actual model calls happen inside the orchestrator/local-tool-loop — the watcher does not call any LLM itself.

**Observer lifecycle / API for the tray**
- `class FolderWatcher:` with `start()`, `stop()`, `pause()`, `resume()`, `reload_rules()`, `add_rule(...)`, `remove_rule(...)`, `list_rules()`. `start()` creates one `Observer`, schedules a handler per enabled rule (`recursive=True`), and starts the observer thread. `pause()` sets a flag the handler checks so events are dropped without tearing down the observer (matches the tray "Pause"). Idempotent start/stop. A module-level singleton accessor `get_watcher()` so the tray and the supervisor share one instance.
- Everything thread-safe (a `threading.Lock` around rule mutation + the debounce timer dict), because watchdog callbacks run on the observer thread while the tray mutates rules on the main thread — mirror the thread-safety discipline already used in `SharedMemory`.

### B. `agent_team/config.py` (EDIT — Local-Model mode)

1. **Add the Ollama provider** to `PROVIDERS`, in the exact existing shape. Ollama's local OpenAI-compatible server ignores the API key, but `providers._openai_chat` requires a truthy key (it raises `ProviderKeyMissing` on a falsy one), so give it an env_key with a harmless default:
```
"ollama": {"label": "Local (Ollama)", "sdk": "openai_compatible",
           "env_key": "OLLAMA_API_KEY", "base_url": "http://127.0.0.1:11434/v1"},
```
   Document in a comment that the key is unused by Ollama and defaults to a placeholder when unset.

2. **Add the toggle + local-model id helpers** near `team_model_preset()`:
```
LOCAL_MODEL_DEFAULT = "qwen2.5:7b-instruct"   # a sensible small local default; document it's a placeholder

def local_model_mode():
    """True only when LOCAL_MODEL_MODE is explicitly enabled. This is the OFFLINE,
    DEGRADED escape hatch (no cloud, no tools, no ¥/COGS visibility). OFF by default
    and never the default path — see effective_model() and the honesty note."""
    val = _env("LOCAL_MODEL_MODE")
    return bool(val) and val.lower() in _TRUE

def local_model_id():
    """The local model id every agent resolves to under LOCAL_MODEL_MODE.
    LOCAL_MODEL_ID env wins, else LOCAL_MODEL_DEFAULT."""
    return _env("LOCAL_MODEL_ID") or LOCAL_MODEL_DEFAULT
```

3. **`effective_model(agent_id)` — add LOCAL_MODEL_MODE as the HIGHEST-precedence branch**, ABOVE the per-agent override and the domestic preset, because offline mode is a hard environment constraint (no network), not a preference:
```
def effective_model(agent_id):
    if local_model_mode():
        return local_model_id()
    if agent_id in MODEL_OVERRIDES:
        ...
```
   Update the docstring to describe the new top branch and explicitly note it is the degraded offline path.

4. **Make `provider_for` resolve the local model to the `ollama` provider.** Because `local_model_id()` is not in `MODEL_CHOICES`, `provider_for` would default it to `"anthropic"` and break. Fix `provider_for` (or add a small guard) so that when `local_model_mode()` is on and the model id equals `local_model_id()`, it returns `"ollama"`. Keep the change minimal and well-commented; do not add the local model to `MODEL_CHOICES` (it has no ¥ catalog pricing and must not appear in the customer/founder storefront).

5. **`cost_cny(local_model_id, …)` returns 0** automatically (the model isn't in the catalog so the per-MTok rates are 0). That is the intended "¥0 marginal, zero COGS visibility" behaviour — add a one-line comment at the `local_model_mode()` site stating this is deliberate and is why local runs are excluded from spend/COGS reporting.

### C. `agent_team/providers.py` (EDIT — minimal)

- Confirm `_openai_chat` already covers Ollama via `base_url` (it does — it's just another OpenAI-compatible provider). The only change needed: in `provider_key(model_id)`, when the provider is `ollama` and `OLLAMA_API_KEY` is unset, return a non-None placeholder (e.g. `"ollama-local"`) so the adapter doesn't raise `ProviderKeyMissing` for a server that ignores the key. Implement this as a tiny, well-commented special case in `provider_key` (or in `_openai_chat`'s key resolution) — do not weaken the missing-key behaviour for any real provider.
- Do **not** change `complete_with_tools`'s degrade-to-text behaviour. Under Local-Model mode the model is non-Anthropic, so file builders/web_search already degrade to the honest plain-text note — that is the documented "no tools" tradeoff. (The on-device deliverables loop, if WS5 is present, still runs pure-python builders; that is WS5's concern, not this edit.)

### D. Honesty / degraded-mode labelling

When `config.local_model_mode()` is on, every run launched (by the watcher OR any local run) must carry a visible "degraded mode" label so the output is never mistaken for a full-quality cloud run, honouring `deliverables.py`'s honesty rule. Implement this once, where the run is assembled (in `_default_enqueue` and, if you touch it, in any shared local-run helper): after creating `SharedMemory`, `memory.log("system", <degraded-mode banner>, type="warning")`, and prepend a short labelled note to the run's goal/prompt context, e.g.:
```
[本地离线模式 / LOCAL OFFLINE MODE] 本次分析在本机本地模型上运行，未联网、未使用云端模型，
无法调用文件生成与联网搜索工具，质量低于云端，结果仅供参考。
```
Keep the wording bilingual-friendly and consistent with the repo's Chinese-first customer copy. Do **not** label cloud runs.

### E. `requirements.txt` (EDIT)

Add under the existing dependency groups (with a short comment), pinned conservatively:
```
# Local runner — watched-folder automation (Phase 4)
watchdog>=3.0.0       # cross-platform filesystem watcher for the desktop daemon's folder watch
```
Do not add `ollama` as a Python dependency — the Ollama server is reached over its HTTP OpenAI-compatible endpoint via the existing `openai` client, so no extra package is needed.

---

## 4. INVARIANTS TO RESPECT (do not violate any)

- **China domestic mode keeps working.** `TEAM_MODEL_PRESET=domestic` must still route the whole team to DeepSeek when `LOCAL_MODEL_MODE` is OFF. Your `effective_model` change adds a branch ABOVE the domestic branch but must not alter domestic behaviour when local mode is off. The `test_china_domestic.py` suite must still pass unchanged.
- **No model keys on the customer machine / cloud-by-default.** Local-Model mode is the **only** path where inference is local, and it is opt-in via an explicit env flag. The default and the watched-folder runs (when local mode is off) still go through the normal cloud-proxied inference path WS1/WS5 own. Do not introduce any provider API key into the watcher or default path.
- **¥ cost-control via `platform_guard` + `token_manager`.** Cloud runs (local mode OFF) must continue to flow through the existing billing/guard machinery (the orchestrator's `cost_listener`/biller). Local-Model mode legitimately bypasses `record_usage`/`platform_guard`/the wallet — that is the documented tradeoff — but you must (a) state it plainly in code comments and the degraded-mode banner, and (b) ensure such runs are **not** silently counted as billable or as quota — they simply have ¥0 catalog cost. Do not remove or weaken any existing guard on the cloud path.
- **Founder-gating.** Do not surface model ids, providers, per-token ¥ rates, or the Ollama provider in any customer-facing payload. The local model id stays out of `MODEL_CHOICES` and out of `_agents_payload`'s non-admin rows. This workstream adds no new HTTP routes, so there is nothing new to gate — keep it that way.
- **Files encrypted at rest (Fernet).** Any file the watcher moves into `processed/` that becomes a persisted artifact should preserve the repo's at-rest posture. The raw watched file is the user's own file in their own folder (do not encrypt it in place — it's theirs). But any copy the run persists into app-managed storage must go through the same `crypto.encrypt_file_in_place` discipline the upload path uses (no-op when FERNET unset). Do not introduce a new plaintext persistence sink.
- **Outbound egress is SSRF-locked** (only `qyapi.weixin.qq.com`). The watcher dials nothing outbound. The Ollama endpoint is `127.0.0.1` (loopback), which is fine. Do not add any new outbound network call.
- **Honesty / label-demo-vs-real.** Per §3.D — degraded mode is always labelled; you never present a local-model result as a full cloud-quality deliverable, and you never fake a file builder under a model that can't run it.
- **Path safety (default-deny).** Every file the watcher acts on passes the workspace chokepoint (WS5's `assert_in_workspace`, or the local fallback) BEFORE `parse_file`. Watched roots ARE the approved roots — a symlink or `..` inside a watched folder that resolves outside all approved roots must be rejected.

---

## 5. ACCEPTANCE CRITERIA + TESTS

Add a new suite `tests/test_runner_watcher.py` (mirror the structure of `tests/test_deliverables.py` / `tests/test_china_domestic.py`; use `tmp_path`, `monkeypatch`, and dependency-injected fast debounce/stability windows — no real `sleep`-driven flakiness; inject a fake/stub enqueuer so no real LLM call is made). Extend `tests/test_china_domestic.py` (or add a focused `tests/test_local_model_mode.py`) for the config changes. The suite must cover:

**Local-Model mode (config):**
- `LOCAL_MODEL_MODE=1` makes `config.effective_model(<any agent id>)` return `config.local_model_id()` for **every** agent, and `config.provider_for(local_model_id())` returns `"ollama"`.
- With `LOCAL_MODEL_MODE` unset/falsy, `effective_model` is byte-for-byte unchanged (per-agent default + override + domestic preset all behave exactly as before — assert OPUS default, a `MODEL_OVERRIDES` win, and a `TEAM_MODEL_PRESET=domestic` win).
- `LOCAL_MODEL_MODE=1` takes precedence over BOTH a `MODEL_OVERRIDES[agent]` entry AND `TEAM_MODEL_PRESET=domestic`.
- `PROVIDERS["ollama"]["base_url"] == "http://127.0.0.1:11434/v1"` and `providers.provider_key(local_model_id())` is non-None even with `OLLAMA_API_KEY` unset (so the adapter won't raise ProviderKeyMissing).
- `config.cost_cny(config.local_model_id(), 1000, 1000) == 0.0` (the deliberate ¥0/no-COGS behaviour).

**Watcher:**
- `add_rule` rejects an unknown `agent_id` (`ValueError`) and a non-existent directory; accepts a valid `(dir, "data_analyst")`; rejects a second root that overlaps an existing root.
- Dropping a **supported** file (e.g. a `.csv`) into a watched root, after the debounce+stability window, calls the injected enqueuer exactly **once** with `(agent_id, [<path>], source=...)`, and the file is afterwards **moved into `<root>/processed/`** (the original path no longer exists, the archived copy does).
- Dropping an **unsupported** file (e.g. `.zip`) does **not** call the enqueuer and is not moved.
- A file written in several chunks (simulate by emitting multiple `modified` events / growing size) enqueues **once**, not per event (debounce + stability proven).
- A file placed in `<root>/processed/` (or a `~$`/`.`-prefixed/`.tmp` temp file) is ignored — proves the no-loop / no-temp guarantee.
- A path that canonicalizes (via realpath / symlink) **outside** all approved roots is rejected before any parse/enqueue (use the local fallback chokepoint if `workspace.assert_in_workspace` is absent; if present, assert it is invoked).
- `pause()` then drop-a-file → no enqueue; `resume()` → subsequent drops enqueue again. `start()`/`stop()` are idempotent.
- Degraded-mode labelling: when `LOCAL_MODEL_MODE=1`, the fallback `_default_enqueue` path logs/prepends the degraded-mode banner (assert the banner text appears in the run's `SharedMemory` log or goal context); when off, it does not.

**Regression:** the full existing suite must still pass. Run:
```
python -m pytest -q
```
and confirm the count is the prior total **plus** your new tests, with **zero** failures. Pay special attention to `tests/test_china_domestic.py`, `tests/test_deliverables.py`, and any `tests/test_redesign_phase*.py` that touch `effective_model`/`provider_for`/`PROVIDERS`.

**Final manual smoke (document the result, do not require Ollama installed):** `LOCAL_MODEL_MODE=1 python -c "from agent_team import config; print(config.effective_model('ceo'), config.provider_for(config.local_model_id()))"` prints the local model id and `ollama`.

---

## 6. EXPLICITLY OUT OF SCOPE (do NOT build these here)

- The tray-resident daemon / waitress supervisor / pystray icon themselves, PyInstaller packaging, the Tauri v2 shell, auto-update, OS-keychain token storage (`keyring`). (WS1.)
- Device **pairing / device tokens / the `devices`, `agent_catalog`, `agent_install`, `agent_run` tables / migration `0007` / `_current_device()` / the `/v1` gateway device-auth resolution / the `/api/devices/*`, `/api/agents/catalog`, `/api/devices/run/*` cloud routes / run telemetry sync / the manifest split.** All of that is WS1 and must NOT be added or migrated in this workstream. **Do not create `migrations/versions/0007*.py`.**
- The local tool-execution loop that interleaves on-device `deliverables.run_tool` with remote `/v1` turns, the `RemoteRouter`, and `agent_team/workspace.py:assert_in_workspace` itself. (WS5.) Integrate with them per §2; provide only a clearly-marked local fallback so this module is testable standalone.
- The path-aware `/api/run` variant and any Flask route changes (this workstream adds **no** HTTP routes).
- Adding the local model to `MODEL_CHOICES`, the storefront, Fusion, or any customer/founder UI surface.
- Pulling/installing Ollama models, GPU detection, or any Ollama lifecycle management — you only point the existing OpenAI-compatible adapter at `127.0.0.1:11434/v1`.
- Native folder-picker UI for adding watch rules (v2 concern) — programmatic `add_rule` is sufficient here.

## 7. DELIVERABLES CHECKLIST
- [ ] `agent_team/runner/watcher.py` (new) + `agent_team/runner/__init__.py` if the package is absent.
- [ ] `agent_team/config.py` edits: `PROVIDERS['ollama']`, `local_model_mode()`, `local_model_id()`/`LOCAL_MODEL_DEFAULT`, `effective_model` top branch, `provider_for` ollama resolution.
- [ ] `agent_team/providers.py` edit: `provider_key` ollama placeholder.
- [ ] `requirements.txt` edit: `watchdog`.
- [ ] `tests/test_runner_watcher.py` (new) + local-model-mode config tests; full suite green.
- [ ] Tray "Watched folders…" API wired (or documented seam if `tray.py` absent).
- No new HTTP routes, no new DB tables, no `0007` migration.

When done, report the absolute paths of every file you created or modified, the final pytest pass count (before/after), and a one-line note on whether WS1's `agent_team/runner/`/`tray.py` and WS5's `agent_team/workspace.py` were present (so you integrated) or absent (so you used the documented fallback seams).
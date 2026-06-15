<!-- WS08 · Phase 3 · depends_on: WS7, WS1 -->
# WS08 — Local agent registry loader

> **Phase 3** · **Depends on:** WS7, WS1 · **Key files:** agent_team/runner/registry.py (new), agent_team/config.py, agent_team/base_agent.py

---


## Objective

Build a **local registry loader** for the Tangent Agent desktop sidecar (the tray-resident local daemon). At sidecar startup the loader pulls the account's installed agent **manifests** from the cloud endpoint `GET /api/agents/catalog` (authenticated with the per-device token), caches each manifest by its `content_hash`, and **merges them into `agent_team/config.py:AGENTS`** so that the existing framework instantiates them with **no new class** — honoring the project's core convention: *"add a dict, the framework picks it up."* The loader must re-pull only when a `content_hash` changes, and must honor pin/unpin via `agent_install.pinned_version`.

This is a self-contained workstream. You have the repo at `/Users/brucetang/Company_Agent_Demo` but no memory of the planning conversation. Read the cited files before writing code.

---

## Required reading (do this FIRST, in this order)

1. **`agent_team/config.py`** — Study:
   - `AGENTS` (lines ~880–1059): each agent is a plain dict with keys `id`, `name`, `title`, `name_cn`, `reports_to`, `model`, `effort`, optional `tools`, optional `max_tokens`, and `system_prompt`. THIS is the structure your loader appends to.
   - `get_agent(agent_id)`, `get_ceo()`, `get_workers()` (lines ~1064–1080) — these read `AGENTS` directly and are how the rest of the framework discovers agents.
   - `effective_model(agent_id)`, `provider_for(model_id)`, `team_model_preset()` (lines ~822–824, ~1106–1158) — **these resolve the model/provider/¥ rates and MUST NOT be touched or relied on by the loader.** The manifest carries behavior only; the model resolves server-side.
2. **`agent_team/base_agent.py`** (whole file, ~155 lines) — `Agent.__init__` reads `agent_config["id"/"name"/"title"]`; `run()` reads `cfg["system_prompt"]`, `cfg.get("tools")`, `cfg.get("effort")`, `cfg.get("max_tokens")`, `cfg.get("budget")`, and calls `config.effective_model(self.id)`. This is the role-agnostic consumer of the dict. **A merged manifest dict must carry every key this file reads** or instantiation will KeyError. Note `cfg["system_prompt"]` and `cfg["name"/"title"/"id"]` are accessed without `.get()` — they are REQUIRED keys.
3. **`agent_team/orchestrator.py` lines ~88–148** — `Orchestrator.__init__` builds the team by iterating `config.get_workers()` and `config.get_ceo()`. This confirms the merge point: if a manifest dict is present in `config.AGENTS` before an Orchestrator is constructed, it is automatically picked up. Your loader must run **before** any Orchestrator is built in the local daemon.
4. **`agent_team/customers.py` lines 37–55, 131–141, and `_hash_key` at line ~528** — the secret discipline to MIRROR for device-token verification on the cloud side (sha256 hash, never plaintext, verify-by-hash). The companion WS owns the cloud `/api/agents/catalog` route and device auth; you consume them, but match this discipline in any client-side token handling (the loader reads the device token, never logs it).
5. **`app.py` lines 464–477 (`_agents_payload`)** — the precedent for **field-stripping**: `row["model"]` is added ONLY for `admin` callers. The device is "just another non-admin caller." The cloud `/api/agents/catalog` builds `manifest_json` from a **whitelist** of runner-visible keys, never by dumping the config dict. Your loader must treat any extra/unknown keys defensively and must NOT expect `model`, `input_per_mtok`, `output_per_mtok`, `cost_budget_cny`, or `team_model_preset` to ever appear in a manifest (they are founder-only and never serialized to a device).
6. **`agent_team/deliverables.py` lines 195–230** — `specs_for(names)` and `run_tool(name, tool_input, out_dir)` and `TOOL_NAMES`. The manifest's `tools` list contains **tool NAMES only**; base_agent already maps them to specs via `deliverables.specs_for(...)`. Your loader passes `tools` through verbatim — it does not resolve specs.
7. **`tests/conftest.py`** and any existing `tests/test_*.py` (e.g. `tests/test_china_domestic.py`, `tests/test_redesign_phase8.py`) for the test style (pytest, `monkeypatch`, hermetic-vs-`.env`).

---

## Architecture context you must respect

- **Form factor**: v1 is a tray-resident LOCAL DAEMON (`agent_team/runner/` supervisor) that boots the existing Flask app on `127.0.0.1:<random-port>` with a `LOCAL_MODE` flag and runs agent jobs in background threads exactly as the server does. Inference is **cloud-proxied** through the existing `/v1/chat/completions` gateway with a per-DEVICE token (`tng-dev-…`). **No provider/Anthropic keys ever live on the customer machine.**
- **The manifest is split.** Only the **RUNNER-VISIBLE half** is ever serialized into `agent_catalog.manifest_json` and returned to a device token. The **FOUNDER-ONLY half** (`model`, `provider`, `input_per_mtok`, `output_per_mtok`, `cost_budget_cny`, `team_model_preset`) is resolved server-side and **NEVER** placed in a manifest. Your loader consumes the runner-visible half only.
- **The runner does NOT choose a model.** Every model turn is sent to `/v1` with the device token; the cloud resolves the model server-side (so China domestic routing + ¥ cost-control stay server-owned). A merged manifest dict therefore has **no usable `model` key**; `config.effective_model()` will fall back to its `OPUS` default for a merged agent, but on the device the actual model is chosen by the cloud gateway, not by `effective_model`. **Do not** synthesize a fake `model` price into the dict.
- **DEPENDS ON WS7 (devices table + device-token auth + `_current_device()`) and WS1 (data model: `devices`, `agent_catalog`, `agent_install`, `agent_run` tables + migration 0007).** Those workstreams own the cloud-side route `GET /api/agents/catalog` and the device token storage (OS keychain via `keyring`). **If those are not yet merged when you implement this, you MUST stub the HTTP boundary behind a single injectable fetch function** (see step 4) so this workstream is testable in isolation and trivially wired once WS7/WS1 land. Do not re-implement device auth or the cloud route here.

### The runner-visible manifest shape (what `GET /api/agents/catalog` returns)

The catalog endpoint returns a JSON list; each element is:
```
{
  "agent_id": "data_analyst",
  "version": 3,
  "content_hash": "sha256:<64 hex>",
  "manifest": {
    "agent_id": "data_analyst",
    "manifest_version": 3,
    "name": "Mei", "name_cn": "数据分析师", "title": "Data Analyst",
    "reports_to": "ceo",
    "plan_role": "specialist",
    "system_prompt": "...verbatim from config.AGENTS...",
    "tools": ["read_file","list_dir","create_excel_workbook","web_search"],
    "effort": "high",
    "wants_file_types": ["xlsx","csv"],
    "deliverable_kinds": ["xlsx"],
    "display": {"blurb": "...", "icon": "spreadsheet"}
  }
}
```
Note: `manifest` carries NO `model`, NO per-token ¥ rates, NO `max_tokens` price knob. `manifest_version` / `version` are integers. `content_hash` is the canonical-JSON sha256 of the runner-visible manifest, prefixed `sha256:`.

---

## Implementation spec

### New file: `agent_team/runner/__init__.py`
Empty package marker (or a one-line module docstring). Create the `agent_team/runner/` directory.

### New file: `agent_team/runner/registry.py`

Implement a `LocalRegistry` that owns the cache + merge logic. Keep it pure-python, thread-safe, and free of any LLM or provider-key dependency. Suggested surface (adapt names sensibly, but keep these responsibilities):

```python
class LocalRegistry:
    def __init__(self, cache_dir=None, fetch=None):
        # cache_dir: where to persist the by-content_hash manifest cache as JSON
        #   (default: a per-user app dir; in tests pass a tmp_path). NEVER store the
        #   device token here — only manifests, which are non-secret behavior.
        # fetch: an injectable callable () -> list[dict] that performs the
        #   authenticated GET /api/agents/catalog and returns the parsed list.
        #   Default wires the real device-auth HTTP client (WS7). Injectable so
        #   tests run with a fake fetch and no network/keychain.
        ...

    def sync(self) -> "SyncResult":
        """Pull the catalog, diff against the cache by content_hash, re-pull/refresh
        only changed entries, drop entries no longer installed, and return a summary
        (added/updated/unchanged/removed agent_ids). Persists the cache."""

    def manifests(self) -> list[dict]:
        """The current set of cached runner-visible manifests (post-sync)."""

    def to_agent_dicts(self) -> list[dict]:
        """Translate each cached manifest into a config.AGENTS-shaped dict
        (the merge payload). See _manifest_to_agent_dict below."""

    def apply_to_config(self, config_module) -> list[str]:
        """Idempotently merge to_agent_dicts() into config_module.AGENTS:
        upsert by id (replace an existing merged entry, never duplicate),
        and remove previously-merged entries that are no longer installed.
        Returns the list of agent_ids now present from the registry. MUST be
        re-runnable: calling it twice yields the same AGENTS list."""
```

**`_manifest_to_agent_dict(manifest)` — the translation (the heart of this WS):**
Build a dict with EXACTLY the keys base_agent/orchestrator read, from the runner-visible manifest, with safe defaults:
- `"id"`           ← `manifest["agent_id"]`  (REQUIRED; reject manifest if missing)
- `"name"`         ← `manifest.get("name")` (REQUIRED; reject if missing/blank)
- `"title"`        ← `manifest.get("title", manifest["agent_id"])`
- `"name_cn"`      ← `manifest.get("name_cn", "")`
- `"reports_to"`   ← `manifest.get("reports_to", "ceo")`  (a merged specialist defaults to reporting to the CEO; the CEO itself is never shipped as a merged manifest — see invariants)
- `"effort"`       ← `manifest.get("effort", "high")`
- `"tools"`        ← `manifest.get("tools", [])` (pass through verbatim; do NOT validate against `deliverables.TOOL_NAMES` here — unknown tool names are tolerated by base_agent, which filters via `specs_for`; an unknown name simply yields no spec)
- `"system_prompt"`← `manifest["system_prompt"]` (REQUIRED; reject if missing/blank)
- **Do NOT set `"model"`.** Let `config.effective_model()` fall through to its default; on the device the cloud gateway picks the real model. (Setting a model would leak a founder concern into a device and could desync from server routing.)
- Optionally stamp a marker key, e.g. `"_source": "registry"` and `"_content_hash": <hash>`, used by `apply_to_config` to know which `AGENTS` entries it owns (so it can cleanly remove/replace them without touching the built-in static agents). Pick a marker that the rest of the framework ignores (base_agent/orchestrator only read the keys listed above), and ensure `_agents_payload` in `app.py` never leaks it — it already whitelists keys, so confirm by reading lines 472–476.

**Caching / re-pull discipline:**
- Persist the cache as a JSON file keyed by `agent_id` → `{version, content_hash, manifest}`.
- On `sync()`: for each row returned by the catalog, if `content_hash` matches the cached one, mark **unchanged** (reuse cached manifest — no re-pull of body needed since the catalog already returns the full manifest inline; the content_hash diff is what gates a *rebuild/merge*, avoiding needless `apply_to_config` churn and log noise). If different or new, **update**. Any cached `agent_id` not present in the freshly fetched list is **removed** (it was uninstalled or unpinned to an unpublished version).
- **Pin/unpin** is resolved server-side: the catalog endpoint already returns the pinned version (from `agent_install.pinned_version`, null ⇒ latest published). The loader simply trusts the returned `version`/`content_hash`. It does **not** re-implement pin logic — but it MUST surface the `version` in the cache and treat a version change as a content change (re-merge). Document this in a comment.

**Robustness (the loader must never crash the daemon):**
- A malformed manifest (missing required `agent_id`/`name`/`system_prompt`) is **skipped with a warning**, not fatal — one bad agent must not blacklist the whole team.
- A network/auth failure in `fetch` ⇒ `sync()` falls back to the **last-good persisted cache** and logs a warning; the daemon still boots with whatever was last merged. Never raise out of `sync()` for an I/O error.
- An empty catalog (account has zero installs) is valid ⇒ remove all registry-merged agents, leave the static built-in `AGENTS` intact.

### Edited file: `agent_team/config.py`
- Add a small, well-commented hook so the merge target is explicit and reversible. Do **NOT** hardcode a network call in config (config must stay import-safe and side-effect-free — it is imported by tests, migrations, and the server). Instead add a module-level helper such as:
  ```python
  def register_runtime_agents(agent_dicts):
      """Merge externally-provided agent dicts (e.g. from the local registry
      loader) into AGENTS, upserting by id. Static built-in agents are never
      removed by this; only entries previously registered through here are
      replaced/pruned. Used ONLY by the local daemon (agent_team/runner) — the
      cloud server never calls this. Model/provider/¥ still resolve via
      effective_model(); a registered dict carries behavior only, never a price."""
  ```
  Track registered ids in a module-level set so the helper is idempotent and can prune. Keep `get_agent`/`get_ceo`/`get_workers` working unchanged (they read `AGENTS`). `LocalRegistry.apply_to_config` calls this helper rather than mutating `AGENTS` directly, so the ownership bookkeeping lives in one place.

### Edited file: `agent_team/base_agent.py`
- Read it carefully; it likely needs **no change** if `_manifest_to_agent_dict` supplies every required key. Only edit if you find a key it reads without `.get()` that a merged dict could lack (e.g. confirm `name`, `title`, `id`, `system_prompt` are always present — they are required in the translation). If `Agent.__init__` or `run()` would KeyError on a manifest-merged dict, fix by supplying the default in the translation, NOT by loosening base_agent (keep base_agent role-agnostic and unchanged where possible). Document any change with a one-line comment tying it to registry-merged agents.

### Wiring note (boundary to WS7/runner supervisor)
- The actual call site (`LocalRegistry(...).sync(); registry.apply_to_config(config)`) belongs in the **daemon supervisor startup** under `agent_team/runner/` — invoked once, *before* any `Orchestrator` is constructed, *after* the device is paired (token available). If the supervisor module does not yet exist (WS7), add a clearly-marked `# WIRE-UP (WS7 supervisor): call registry.sync()+apply_to_config(config) at daemon startup` TODO and a tiny `bootstrap(config_module, fetch=None, cache_dir=None)` convenience function in `registry.py` that does both steps, so the supervisor's wiring is one line. Do not start a Flask app or open a port in this workstream.

---

## Invariants you MUST respect

1. **China domestic mode keeps working.** The loader must NEVER call `effective_model()`, `provider_for()`, or read provider keys — a fully-domestic cloud has no Anthropic key, and the catalog path must work there. Manifests carry no model id. (Mirror the catalog endpoint's own rule: it must never call `effective_model()`/`provider_for()`.)
2. **No model/provider keys on the customer machine.** The loader handles only manifests (non-secret behavior) and a device token it never logs/persists in the manifest cache. The device token lives in the OS keychain (WS7), not in this cache file.
3. **¥ cost-control stays server-owned.** Do not put `input_per_mtok`/`output_per_mtok`/`cost_budget_cny`/`team_model_preset`/`model` into any merged dict. Billing happens server-side on each `/v1` call via `record_usage` + `platform_guard`. The loader touches none of it.
4. **Founder-gating preserved.** The device is a non-admin caller; the manifest is the runner-visible half only, built from a key whitelist. Confirm `_agents_payload` (app.py 464–477) does not leak your `_source`/`_content_hash` marker (it whitelists keys — verify and, if needed, ensure the marker key name can't collide).
5. **Files encrypted at rest / honesty rule** — not directly exercised here, but do not weaken them: the manifest cache contains no file bytes and no secrets, so plaintext JSON is fine; add a comment stating the cache holds only non-secret behavior.
6. **"Add a dict, the framework picks it up."** The whole point: a merged manifest must be indistinguishable to base_agent/orchestrator from a static `config.AGENTS` entry. No new Agent subclass, no class registry.

---

## Acceptance criteria

A merged manifest agent is instantiable and runnable by the existing framework with no code path special-casing it:
1. `LocalRegistry.apply_to_config(config)` after a `sync()` makes `config.get_agent("data_analyst_remote")` (or whatever test id) return a dict, and `agent_team.base_agent.Agent(that_dict, memory, fake_llm)` constructs without error and `.run()` reaches `llm.complete`/`complete_with_tools` (use a fake `llm` double like existing tests do).
2. **Content-hash caching:** two consecutive `sync()` calls with the same fake catalog produce `unchanged` for every agent on the second call and do NOT re-merge (assert idempotency: `config.AGENTS` length is stable, no duplicate ids).
3. **Re-pull on change:** bumping a manifest's `content_hash`/`version` in the fake catalog flips that agent to `updated` and the merged dict reflects the new `system_prompt`.
4. **Uninstall/prune:** removing an agent from the fake catalog removes it from `config.AGENTS` on the next `sync()`+`apply_to_config`, while leaving the static built-in agents (`ceo`, `data_analyst`, etc.) untouched.
5. **Pin honored (via server):** a catalog row whose `version` differs from cache re-merges (assert the version-change path), proving the loader trusts server-resolved `pinned_version`.
6. **Founder-only keys never leak:** assert no merged dict contains `model`, `input_per_mtok`, `output_per_mtok`, `cost_budget_cny`, or `team_model_preset`.
7. **Resilience:** a `fetch` that raises ⇒ `sync()` returns a result backed by the last-good cache and does not raise; a manifest missing `system_prompt` is skipped (not merged) and the rest still merge.
8. **Domestic-safe:** the entire test module passes with `TEAM_MODEL_PRESET=domestic` set via `monkeypatch.setenv` and with NO `ANTHROPIC_API_KEY` in the environment (the loader never touches model resolution).

### Tests to add
- New file **`tests/test_runner_registry.py`** (the repo uses pytest with per-feature test modules in `tests/`; follow the style in `tests/test_china_domestic.py` and `tests/conftest.py`). Cover all 8 acceptance criteria with a fake injected `fetch` (a Python callable returning a hand-built catalog list) and a `tmp_path` cache dir — **no network, no keyring, no Flask client** needed for the core loader tests. Use a minimal fake `llm` object (with `complete`/`complete_with_tools` recording calls) to prove instantiation, mirroring how existing agent tests stub the LLM.
- Restore `config.AGENTS` between tests (fixture: snapshot the static list at setup, restore at teardown) so merge tests don't leak state into the rest of the suite — this is critical since `config.AGENTS` is module-global.
- Run the FULL suite at the end (`python -m pytest -q`) and confirm no regressions; the suite is ~202 tests today.

---

## Explicitly OUT OF SCOPE (do NOT build these here)

- The cloud-side `GET /api/agents/catalog` route, `POST /api/agents/<id>/publish`, `POST /api/agents/<id>/install`, the `agent_catalog`/`agent_install`/`devices`/`agent_run` tables, and **migration `0007_phase11_devices.py`** — those are WS1/WS7. You only CONSUME the catalog endpoint's response shape, behind an injectable `fetch`.
- Device pairing, the `tng-dev-` token mint/exchange/revoke flow, `_current_device()`, OS-keychain (`keyring`) storage — WS7.
- The local tool-execution loop / `RemoteRouter` / proxying model turns through `/v1` — separate workstream (WS5). The loader does not run agents; it only makes their dicts available.
- The daemon supervisor itself (port selection, `waitress` boot, `pystray` tray, `LOCAL_MODE` flag), file pickers / `watchdog`, `workspace.assert_in_workspace` path sandboxing, the path-aware `/api/run` variant, and the run-summary sync (`/api/devices/run/start|finish`) — other workstreams. Leave a single one-line `# WIRE-UP (WS7 supervisor)` TODO and the `bootstrap()` convenience function, nothing more.
- Adding the `create_excel_workbook` xlsx builder or any `deliverables.py` builder — already present; not this WS.
- Any change to `effective_model`/`provider_for`/pricing/`platform_guard`/`record_usage`.

Deliver: `agent_team/runner/__init__.py`, `agent_team/runner/registry.py`, a small `register_runtime_agents` helper added to `agent_team/config.py`, any minimal defensive edit to `agent_team/base_agent.py` (only if strictly required), and `tests/test_runner_registry.py`. Then run the full pytest suite and report results.
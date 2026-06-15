# Locally-Deployable Agents — Architecture & Execution Plan

*Status: PLAN (for review). Generated 2026-06-15. Turns the single report product into an extensible catalog of agents customers **install on their own computer** to work on their **local files**. Report-delivery pricing is discarded for now; auth/accounts/wallet are kept.*

> Companion: the ready-to-hand-out coding-agent prompts live in [PROMPTS-INDEX.md](PROMPTS-INDEX.md) (one per workstream).

---

## 0. Decisions locked (2026-06-15)

The owner confirmed the three execution-gating decisions:

1. **Form factor — tray daemon → Tauri v2.** Ship the localhost daemon first (Phases 0–3), wrap in Tauri later (Phase 4). The WSS-bridge fallback is shelved unless a hard "no file content in a localhost server" buyer requirement appears.
2. **v1 scope — file-builder agents on Claude *and* domestic.** Build the local tool-execution loop (WS05) so the flagship Excel/Word agents return real files on both Claude and DeepSeek. WS05 is the gating dependency, treated as make-or-break.
3. **Web product — kept as a no-install fallback tier.** The browser-upload product stays alongside the local app. Report-delivery **pricing is still discarded for now across both paths** (per the original instruction): `_plan_gate` is stubbed to always-allow rather than deleted (PLANS config stays dormant for later), and device runs never enter the quota path. → refines **WS10**.

**v1 = WS01–WS10 (Phases 0–3). v2 = WS11–WS12 (Phase 4).**

---

## 1. Recommended architecture

v1 = a tray-resident LOCAL DAEMON that runs the EXISTING Flask app (app.py) bound to 127.0.0.1:<random-port>, serves the existing vanilla SPA from frontend/ verbatim, and runs agent jobs in background threads exactly as it does today — shipped as a code-signed PyInstaller --onedir binary (and a `pip install tangent-agent` wheel for power users). Inference is cloud-proxied through the EXISTING /v1/chat/completions gateway using a NEW per-DEVICE token (never the account key, never provider keys). v2 = wrap that identical daemon in a Tauri 2.0 shell for native file pickers, "reveal in Finder/Explorer", OS-keychain token storage, auto-update, and a small (~5MB) download — the daemon and its localhost HTTP contract do not change between v1 and v2.

Fallback: if the tray/loopback-CSRF/AV-false-positive surface proves too painful on Windows for the China channel, fall back to the cloud-orchestrated thin-websocket bridge (Form Factor C): the agent brain stays in the cloud and a tiny outbound-WSS daemon answers read_file/list_dir/write_file RPCs. It ships a far smaller binary and sidesteps the loopback-server attack surface, at the cost of per-tool-call cloud round-trips and a new ASGI /bridge hub.

### Why this wins

The tray-daemon path wins because ~90% of it already exists and passed a live DoD sweep: app.py already binds host=127.0.0.1, threaded=True, serves frontend/ as a static SPA, and runs /api/run jobs in daemon background threads. The "local web UI", the "watch the team work" live feed (run_feed.startRunFeed polling /api/status), file_tools.parse_file reading local xlsx/docx/pdf, and the deliverables builders (INCLUDING the now-present create_excel_workbook/_build_xlsx via openpyxl) all work unchanged when pointed at local paths. So v1 is mostly a re-host plus a thin shell, not a rewrite — the lightest path by a wide margin.

It satisfies every hard invariant for free by KEEPING inference in the existing /v1 gateway: provider keys stay server-side (providers.provider_key reads os.environ on the server), the ¥ wallet + per-key RPM + platform_guard circuit breaker + per-account daily ¥ cap + record_usage billing all stay server-side, and China domestic mode (TEAM_MODEL_PRESET=domestic → DeepSeek V4, no Anthropic key) stays a SERVER concern the daemon never sees. The catalog model ("an agent = a config.AGENTS dict; add a dict, the framework picks it up") maps 1:1 onto "an extensible catalog of deployable agents", honoring the CLAUDE.md config-driven convention.

Tauri is correctly the v2 polish, not the v1 substrate: it adds real value (native pickers, keychain, small download, auto-update) but none of it is needed to prove the product, and bundling/signing/notarization is where the real calendar weeks go. The thin-websocket bridge (C) is the better-isolated architecture but introduces a net-new ASGI hub + thread-safe-Future glue + per-call latency we don't need to take on for v1.

The device-token gateway from the deep-dive is folded in as the inference backbone because shipping the account key (tng-sk-) to laptops is unsafe (it is the full wallet + all models, shown once) — a separate, per-device, independently-revocable credential that RESOLVES to the same account keeps record_usage/platform_guard/can_use unchanged.

### End-to-end narrative

END TO END. The customer downloads "Tangent Agent" (a signed binary; or `pip install tangent-agent`). On launch a tiny supervisor (agent_team/runner/) picks a free 127.0.0.1 port, boots the existing Flask app via waitress (a production WSGI server; gunicorn is fork-based and not Windows-friendly) with a LOCAL_MODE flag, drops a tray icon (pystray: Open dashboard / Pair account / Watched folders… / Pause / Quit), and opens http://127.0.0.1:PORT in the default browser — the real SPA from frontend/index.html, with __BASE_URL__ pointing auth at the cloud and runs at the local daemon.

PAIRING. The SPA shows "Connect this computer". The user clicks Pair → the daemon opens the cloud's /device authorize page → the user logs in via the existing customers.verify_login flow → the cloud mints a per-DEVICE token (tng-dev-…, modeled byte-for-byte on customers.py's mint-once / store-sha256 / verify-by-hash discipline) bound to the account and returns it to the polling daemon over a one-time code exchange. The daemon stores it in the OS keychain (keyring lib: Keychain / Credential Manager / libsecret), never plaintext on disk. From then on the local ModelRouter is repointed at the cloud /v1/chat/completions with Authorization: Bearer tng-dev-… for EVERY model — Claude and DeepSeek alike already route through gateway_chat server-side, so the daemon needs no Anthropic key and no mainland reachability to anthropic.com.

DEPLOY. The "Agents" dropdown becomes the catalog. The daemon pulls GET /api/agents/catalog (device-auth) — a list of installed agent MANIFESTS in the config.AGENTS shape (id/name/title/effort/tools/system_prompt) plus display copy, wanted local file types, and which deliverable tools it uses — and merges them into config.AGENTS at startup (a local registry loader, matching "add a dict, the framework picks it up"). The manifest carries BEHAVIOR only; the model id, provider routing, and per-token ¥ rates resolve server-side and are NEVER serialized to a device.

RUN ON LOCAL FILES. The user picks a folder/files (native picker in v2; HTML file-input in v1) OR drops a file in a watched folder (watchdog). The renderer POSTs local file PATHS (not bytes) to a path-aware variant of /api/run. A single default-deny chokepoint (agent_team/workspace.py:assert_in_workspace) canonicalizes the path with os.path.realpath + commonpath against the user-approved roots and rejects symlink/`..` escapes BEFORE file_tools.parse_file opens anything. parse_file reads xlsx/csv/docx/pdf into text VERBATIM, SharedMemory is built, and the Orchestrator runs the team in a background thread exactly as on the server. The live activity feed plays unchanged.

INFERENCE + TOOLS. Every LLM turn inside agent_team goes through a RemoteRouter (a ModelRouter subclass) that POSTs to the cloud /v1 gateway with the device token. Because /v1 is plain-chat-only and file builders are Anthropic-tool-only today, the ONE genuinely new piece of glue is a LOCAL tool-execution loop: deliverables.run_tool executes ON THE DEVICE (it is pure-python openpyxl/reportlab/python-docx, no LLM) while each model turn is delegated to the remote proxy. token_manager accounting stays intact because record_usage still fires server-side on each /v1 call.

SEE RESULTS. The deliverables builders write REAL files straight into the user's chosen output folder (a populated analysis.xlsx). "Download" becomes "reveal in Finder/Explorer". On completion the daemon syncs a run SUMMARY (goal, status, ¥ cost, deliverable filenames, a report sha256 + byte count — NO file bytes) back to the cloud via the SAME metrics_store.create_run/finish_run lifecycle, so cloud-runs and device-runs land in ONE runs table and ONE 历史 list, badged origin='device'.

---

## 2. Where the LLM runs & how cost-control holds

Inference runs in the CLOUD, via the existing /v1/chat/completions gateway, authenticated by a per-DEVICE token. This single decision makes all four constraints hold simultaneously:

NO KEYS ON THE MACHINE: real Anthropic/DeepSeek/Qwen keys live only in the cloud server env (providers.provider_key reads os.environ on the server). A decompiled/leaked desktop binary exposes nothing. The device holds only a tng-dev- token in the OS keychain, scoped to one account and instantly revocable.

¥ COST-CONTROL: the gateway already enforces, in order (app.py:4325+): LAYER 1 auth (verify_key / now also verify_device_token), per-key/per-DEVICE RPM (_rpm_check keyed on an arbitrary string → pass the device id), platform_guard.check (circuit breaker + per-account ¥/day cap; pass min(device_cap, account_cap)), LAYER 3 access (can_use ∩ device model subset), LAYER 2 billing (has_balance pre-check, record_usage post-call → row-locked wallet decrement in ¥ via config.cost_cny + record_platform_spend feeding the breaker). Moving inference local would throw ALL of this away — so we don't.

CHINA MODE: keeping inference cloud-side means config.provider_for(model) picks DeepSeek/Qwen for domestic accounts and TEAM_MODEL_PRESET=domestic stays a SERVER concern; the desktop never needs an Anthropic key or anthropic.com reachability. The CN build simply pairs against a mainland-hosted, domestic-mode gateway — no client change.

THE HONEST TOOL CAVEAT: complete_with_tools (file builders, web_search) is Anthropic-ONLY and degrades to plain text on non-Anthropic models (providers.py:277); and /v1 is plain-chat-only regardless. So the flagship file-building agents CANNOT get builders "through the gateway". Resolution: run the deliverables builders (pure-python openpyxl/reportlab/python-docx — no LLM) and file_tools.parse_file LOCALLY on the device, and proxy only the text model turns through /v1. Concretely that means a local tool-execution loop (Workstream WS5) that interleaves local deliverables.run_tool calls with remote /v1 turns. For v1 this works on BOTH Claude and domestic DeepSeek because the device never relies on the gateway to execute tools.

LOCAL-MODEL / OFFLINE MODE (explicit v2 escape hatch, NOT default): providers.py already speaks OpenAI-compatible, so adding PROVIDERS['ollama']={base_url:'http://127.0.0.1:11434/v1'} + a LOCAL_MODEL_MODE toggle makes effective_model() resolve every agent to a local model — file content then never leaves the device. Tradeoffs (state them plainly, honor the honesty rule by labeling outputs as degraded): it bypasses record_usage/platform_guard/the ¥ wallet (¥0 marginal but zero COGS visibility), loses all tools/web_search, and a 7-8B local model is materially below DeepSeek V4/Claude for analytics work. Gate it behind an explicit toggle for the regulated/air-gapped segment; never lead with it.

---

## 3. The cloud ↔ local protocol

All three sync surfaces are RUNNER→CLOUD only — the cloud never dials the device (outbound egress is SSRF-locked to qyapi.weixin.qq.com; the runner is always the dialer; long-poll/interval, never a webhook).

PAIRING / AUTH (clone of customers.py secret discipline; token = "tng-dev-"+token_urlsafe(32), store ONLY sha256 in devices.token_hash, verify by hash):
- POST /api/devices/pair-code (session-auth) → {pairing_code, expires_at}. Writes a devices row status='pending', pairing_code_hash, pairing_expires_at = now+10min.
- POST /api/devices/exchange (NO session; {pairing_code, device_name, platform}) → matches a pending non-expired row by sha256(code), flips status='active', stores token_hash, returns {device_token, device_id, account_id} ONCE. Burns the pairing row so a replayed code can't mint a second token.
- GET /api/devices (session-auth) → list this account's devices (name/platform/last_seen/status) — NEVER the raw token, ¥, or model ids (mirror _account_view field-stripping).
- POST /api/devices/<id>/revoke (session-auth) → status='revoked'; verify fails closed thereafter (instant kill switch, no key rotation needed).
- Runner auth: new _current_device() in app.py mirrors _current_customer(): reads Bearer tng-dev-…, calls devices.verify_token(raw)→record (sha256 lookup, status=='active'), bumps last_seen_at. Every runner→cloud route starts `dev=_current_device(); if not dev: return 401`. The device inherits account_id, so company_id resolves via metrics_store.get_or_create_company exactly as a web run.

CATALOG SYNC:
- POST /api/agents/<id>/publish (admin-gated inline 403) → snapshots a config.AGENTS dict into an agent_catalog row: manifest_json = RUNNER-VISIBLE half only, content_hash = sha256 of canonical json, version bumped.
- POST /api/agents/<id>/install (session-auth) → add/remove an agent_install row for the account.
- GET /api/agents/catalog (device-auth) → for each agent_install row, return {agent_id, version, content_hash, manifest}. Runner caches by content_hash and re-pulls only on change. CRITICAL: this path must NEVER call effective_model()/provider_for() (so it works even on a fully-domestic cloud with no model keys) and must BUILD manifest_json from a whitelist of runner-visible keys, never dump the dict.

LLM PROXY (inference): the EXISTING POST /v1/chat/completions, unchanged in shape. Extend ONLY its auth resolution: after _bearer_key(), try CUSTOMERS.verify_key(raw) first (account key) else DEVICES.verify_device_token(raw). On a device hit: set customer = resolved.account, but key _rpm_check on the DEVICE id (per-device RPM for free), pass min(device_cap, account_cap) into platform_guard.check, intersect can_use against the device's live model subset (computed at call time, never frozen). record_usage still charges the ACCOUNT wallet + feeds the breaker + COGS — zero billing changes.

RUN / TELEMETRY SYNC (reuse the existing two-call lifecycle):
- POST /api/devices/run/start (device-auth; {agent_id, goal_text, manifest_hash, agent_install_id}) → metrics_store.create_run(company_id, goal_text, kind='device_run') for the shared run_id, INSERT an agent_run row (status='running'), stamp runs.origin='device' + runs.device_id. Returns {run_id, agent_run_id}.
- POST /api/devices/run/<run_id>/finish (device-auth; {status, cost_yuan, report_summary, report_sha256, report_bytes, deliverable_kind}) → metrics_store.finish_run(run_id, status, total_cost_yuan=cost_yuan, report_path=None) so it appears in 历史; update the agent_run row. report_path stays NULL (no cloud PDF render). cost_yuan is a SUMMARY only — it is NOT routed through record_platform_spend/the breaker (the user paid their own metered /v1 calls already). kind='device_run' is excluded from the quota path and from the default 历史报告 report filter exactly as kind='import' is.
- (optional, low priority) POST /api/devices/run/<run_id>/event for richer live feed.

---

## 4. The agent manifest (runner-visible vs founder-only)

The manifest is split into a RUNNER-VISIBLE half (the only thing ever serialized into agent_catalog.manifest_json and returned to a device token) and a FOUNDER-ONLY half (resolved server-side, NEVER placed in the manifest, NEVER returned to a device). The split is enforced the same way _agents_payload already strips row['model'] for non-admins (app.py:474) — the device is just another non-admin caller, and manifest_json is BUILT from a key whitelist, never by dumping the config.AGENTS dict (which carries 'model' + the catalog's input_per_mtok/output_per_mtok ¥ rates).

RUNNER-VISIBLE (sent over the wire):
{
  "agent_id": "data_analyst",
  "manifest_version": 3,
  "name": "Dana", "name_cn": "数据分析师", "title": "Data Analyst",
  "reports_to": "ceo",
  "plan_role": "specialist",                 // org-chart wiring so the runner can assemble the team
  "system_prompt": "...verbatim from config.AGENTS...",
  "tools": ["read_file","list_dir","create_excel_workbook","web_search"],  // capability/tool NAMES only
  "effort": "high",                          // behavioral hint, NOT a price
  "wants_file_types": ["xlsx","csv"],        // display: which local files this agent asks for
  "deliverable_kinds": ["xlsx"],             // display: what it returns
  "display": {"blurb": "Point me at an Excel and I'll analyze it and hand back a populated workbook.",
              "icon": "spreadsheet"},
  "content_hash": "sha256:..."
}

FOUNDER-ONLY (resolved server-side; never serialized, never returned to a device):
{
  "model": effective_model(agent_id),        // raw 'claude-opus-*' / 'deepseek-*'
  "provider": provider_for(model),
  "input_per_mtok"/"output_per_mtok": ...,    // per-token ¥ COGS
  "cost_budget_cny": COST_BUDGETS_CNY[agent_id],
  "team_model_preset": TEAM_MODEL_PRESET
}

The runner does NOT choose a model: it sends every turn to /v1 with the device token, and the cloud resolves the model server-side (so domestic routing + cost-control stay server-owned). Tools that are Anthropic-only degrade to text if the cloud resolves the agent to a non-Claude model — but file builders still run on-device via the local tool loop, so the deliverable is produced regardless.

---

## 5. Data-model changes

- devices -> id String(32) PK ('dev_'+token_hex(6)); account_id String(32) index, loose ref accounts.id (no FK, Phase-8-10 convention); name String(128); platform String(32) ('darwin'|'win'|'linux'); token_hash String(64) index (sha256 of device token); pairing_code_hash String(64); pairing_expires_at DateTime; status Enum('pending','active','revoked') name='device_status' default 'pending'; last_seen_at DateTime; created_at DateTime default utcnow
- agent_catalog -> id Integer PK; agent_id String(64) index (config.AGENTS id); version Integer default 1; name_cn String(128); manifest_json Text (RUNNER-VISIBLE half ONLY, no model/¥); content_hash String(71) index ('sha256:'+64); published Boolean default True server_default '1'; created_at DateTime; UniqueConstraint(agent_id, version)
- agent_install -> id Integer PK; account_id String(32) index (loose ref accounts.id); agent_id String(64) index; pinned_version Integer nullable (null = always latest published); active Boolean default True server_default '1'; created_at DateTime; UniqueConstraint(account_id, agent_id)
- agent_run -> id Integer PK; run_id Integer ForeignKey('runs.id') index (HARD FK — shares the run lifecycle); device_id String(32) index (loose ref devices.id); account_id String(32) index; agent_catalog_id Integer ForeignKey('agent_catalog.id') index; agent_id String(64) index; manifest_hash String(71); status Enum('running','succeeded','failed','stopped') name='agent_run_status' default 'running'; cost_yuan Numeric(12,4) default 0 (SUMMARY only — user's own metered spend); report_summary Text (text only, never bytes); report_sha256 String(64); report_bytes Integer; deliverable_kind String(32) ('xlsx'|'pdf'|'docx'|'pptx'|'svg'|'text'); started_at DateTime; finished_at DateTime
- runs (EXISTING table, add 2 columns) -> origin String(16) NOT NULL default 'cloud' server_default 'cloud'; device_id String(32) nullable index
- RUN_KINDS (constant in models.py) -> add 'device_run'; runs.kind is String(32) NOT a named Enum so NO Postgres ALTER TYPE is needed; teach metrics_store.list_runs + the plan-quota counters to treat 'device_run' like 'import' (excluded from the default 历史报告 report filter AND from quota / the ¥29 credit consume in finish_run)
- migration 0007_phase11_devices.py -> down_revision='0006_phase7_single_report'; copy the 0006 inspector-guarded helper trio (_inspector/_has_table/_has_column/_column_missing); create devices/agent_catalog/agent_install/agent_run each behind `if not _has_table`; add runs.origin + runs.device_id each behind `if _column_missing`; create every index=True index + the UniqueConstraints; NO ALTER TYPE for runs.kind (it is String(32)); SQLite dev path is satisfied by create_all+_ensure_sqlite_columns so the guards make 0007 a no-op there, Postgres gets the real additive DDL

---

## 6. Example agent catalog (honest maturity labels)

| Agent | Surface | What it does | In → Out | Maturity |
|-------|---------|--------------|----------|----------|
| **Excel Data Analyst (data_analyst) — FLAGSHIP** | Local daemon UI + watched folder; runs on a local .xlsx/.csv | Reads the user's spreadsheet (shape, columns, numeric describe(), 30-row preview via file_tools.parse_file), runs the analysis through cloud-proxied model turns, and hands back a POPULATED .xlsx (summary tab, pivots, computed columns) written next to the source via create_excel_workbook/_build_xlsx. | A local Excel/CSV path + a goal ('analyze last quarter's sales, add a summary tab') → A real analysis.xlsx in the user's output folder (revealed in Finder/Explorer), plus a synced run summary | live (config.AGENTS already has data_analyst with tools=['web_search','create_excel_workbook'] at config.py:957, and _build_xlsx EXISTS in the working tree) — what is NOT yet built: read_file/list_dir LOCAL tools, the local tool-execution loop (WS5), and drive_excel in-place editing |
| **Word Writer (writer)** | Local daemon UI; runs on a local .docx or a blank goal | Drafts/rewrites a document (memo, report, proposal) and emits a real .docx via create_word_document; can read an existing .docx as a starting point. | Optional source .docx path + a writing brief → A formatted .docx in the output folder | live (writer agent + create_word_document builder both exist; only the local file-path intake is new) |
| **Deck Builder (media_producer)** | Local daemon UI | Turns analysis/notes into a .pptx slide deck (and can also emit .xlsx/.pdf/.svg). | A goal + optional local source files → A real .pptx in the output folder | live (media_producer at config.py:1028 already lists create_slide_deck/create_pdf_document/create_excel_workbook) |
| **JD Shopping Researcher (jd_shopper)** | Local daemon UI; cloud web_search capability | Researches products/prices and compiles a comparison; uses web_search (Anthropic-only capability) for current context and writes a comparison .xlsx/.pdf. | A product/shopping brief → A comparison workbook or PDF | demo — depends on web_search which is Anthropic-only; works when the cloud resolves the agent to Claude, degrades to text on domestic DeepSeek (label clearly per the honesty rule) |
| **Marketing Copy Agent (marketer)** | Local daemon UI | Generates campaign copy / content calendars from a brief and optional brand docs read locally; outputs .docx or .xlsx. | A marketing brief + optional local brand .docx/.xlsx → A .docx/.xlsx deliverable | roadmap — agent dict + manifest to be added to the catalog (pure config; framework instantiates it with no new class) |
| **In-place Excel Editor (drive_excel capability)** | Local daemon; edits the user's existing workbook in place | Loads the user's existing .xlsx (openpyxl load_workbook), mutates cells/formulas, saves — distinct from create_excel_workbook which emits a NEW file. The headline 'it works ON your file' capability. | A local .xlsx path + an edit instruction → The same .xlsx, edited in place (write confined to an approved root; per-write user approval in the tray; encrypt_outputs OFF for user-owned dirs so we never corrupt their working files) | roadmap — net-new local tool (openpyxl in-place edit + the workspace allowlist + write-approval UX); the one genuinely new builder beyond what exists |

---

## 7. Removing report-delivery pricing

Report-delivery pricing is DISCARDED cleanly and orthogonally: it lives ONLY in app._plan_gate, which reads config.PLANS.monthly_report_quota + metrics_store.count_runs_*. Removing the _plan_gate call from the run path (and not porting it into the device-run path) leaves auth (customers.verify_login/session tokens), the ¥ wallet (Decimal NUMERIC wallet, row-locked record_usage), platform_guard, and the COGS machinery 100% UNTOUCHED — they are independent subsystems. Concretely: (1) the LOCAL daemon's run path simply does not call _plan_gate; (2) device runs use kind='device_run', which is excluded from the quota counters and from finish_run's consume_report_credit_if_over_quota (gate it off for kind='device_run' so a local run on the user's own metered /v1 calls never burns a cloud ¥29 report credit); (3) the existing cloud web product can keep _plan_gate or have it stubbed to always-allow — that is the owner's call (see open_questions). Cost-control is preserved end to end because billing now happens per /v1 call through record_usage + the breaker, which is wallet/RPM/cap-based, NOT report-quota-based. The customer UI continues to NEVER expose models/tokens/per-token ¥; the device-management page mirrors _account_view field-stripping (label/prefix/last4/last-seen/status only).

---

## 8. Phases (each independently demo-able)

### Phase 0 — Local re-host spike (proves the core, days, UNSIGNED)

**Deliverable.** `python -m agent_team.runner` boots the existing Flask app on 127.0.0.1, drops a pystray tray icon, opens the SPA in the browser; ModelRouter hardcoded-pointed at the deployed cloud /v1 gateway with a real tng-sk- key; a real local .xlsx is picked, the existing Orchestrator + parse_file + create_excel_workbook produce a populated analysis.xlsx in a local folder; live feed plays.

**Demo.** On the founder's own laptop: pick a real sales.xlsx, watch the AI team work in the live feed, get a populated analysis.xlsx written to disk — ~90% already-shipped code. Label it clearly as an unsigned dev demo.

### Phase 1 — Device-token gateway + pairing (cloud)

**Deliverable.** devices table + 0007 migration; devices.py (mint-once/sha256/verify-by-hash); /api/devices/pair-code, /exchange, list, revoke; _current_device(); /v1 auth extended to accept device tokens with per-device RPM + min(device,account) cap + model-subset intersection; record_usage unchanged.

**Demo.** Pair a laptop from the web UI, get a tng-dev- token in the keychain, make a metered /v1 call that bills the account wallet and respects a per-device ¥ sub-cap; revoke the device and watch the next call 401.

### Phase 2 — Local tool loop + flagship Excel agent (honest)

**Deliverable.** LOCAL_TOOL_SPECS (read_file/list_dir/write_file) + the local tool-execution loop (local deliverables.run_tool + remote /v1 model turns); workspace.py default-deny path allowlist wired in front of parse_file and every builder; data_analyst manifest + path-based /api/run intake; drive_excel in-place editor.

**Demo.** Deploy 'Excel Data Analyst' in the local app, point it at a folder, type a goal; it lists the folder, reads sales.xlsx LOCALLY, reasons via the cloud proxy, and writes sales_analyzed.xlsx back next to the source — works on Claude AND domestic DeepSeek because tools execute on-device.

### Phase 3 — Catalog sync + run telemetry + cross-device history

**Deliverable.** agent_catalog/agent_install/agent_run tables (in the 0007 migration); /api/agents/<id>/publish (admin), /install (session), /catalog (device, runner-visible whitelist only); local registry loader merges manifests into config.AGENTS at startup; /api/devices/run/start+finish reusing metrics_store.create_run/finish_run with kind='device_run', origin='device', report_path=None, quota/credit gated off.

**Demo.** Browse a real catalog, install two agents to the account, run one locally, and see it appear in the cloud 历史 list badged 'ran on your machine' — with NO file bytes synced, only a summary + sha256 + cost.

### Phase 4 — Real product: Tauri shell, signing, packaging, distribution

**Deliverable.** Tauri 2.0 shell wrapping the SAME daemon (native pickers, reveal-in-OS, keyring token storage, per-launch shared-secret + loopback Origin/CSRF check, admin routes stripped from the local build); PyInstaller --onedir sidecar with pandas/reportlab/CJK-font hooks; macOS Developer ID + Hardened Runtime + notarytool + staple; Windows Azure Trusted Signing (NOT EV); Tauri signed auto-update; CI matrix macos-arm64/macos-x64/windows-x64; CN: 软著 filing, domestic-route default, AV whitelisting (360/Huorong/Tencent).

**Demo.** A signed .dmg / .msi a non-technical buyer double-clicks past Gatekeeper/SmartScreen; it pairs, deploys the Excel agent, runs on local files, and auto-updates. This is the 'NEEDS REAL WORK, not fakeable' tier.

---

## 9. Workstreams → execution prompts

Each maps to a self-contained prompt in `execution-prompts/`. Build in dependency order (lowest phase first).

| WS | Phase | Workstream | Goal (short) | Depends on |
|----|-------|-----------|--------------|------------|
| WS01 | 0 | Local runner supervisor + tray + path-based run intake | Build agent_team/runner/ : a console entrypoint `tangent-agent` (pyproject [project.scripts]) and a supervisor that picks a free 127.0.0.1 port, boots app.py… | — |
| WS02 | 2 | Filesystem allowlist chokepoint | Add agent_team/workspace.py with assert_in_workspace(path, roots) = os.path.realpath + os.path.commonpath default-deny check (rejects symlink/`..` escapes).… | WS1 |
| WS03 | 1 | Device-token model, store, migration | Add the Device model (loose account_id String(32) ref) to agent_team/models.py and agent_team/devices.py (a DeviceTokenStore cloning customers.py: mint… | — |
| WS04 | 1 | Pairing endpoints + _current_device + /v1 device auth | Add POST /api/devices/pair-code, POST /api/devices/exchange (one-time code -> device token, burn the row), GET /api/devices, POST /api/devices/<id>/revoke (all… | WS3 |
| WS05 | 2 | RemoteRouter + local tool-execution loop | Add a RemoteRouter (subclass/replacement of providers.ModelRouter) whose complete()/complete_with_tools() POST to ${CLOUD}/v1/chat/completions with the device… | WS1, WS4 |
| WS06 | 2 | Flagship Excel agent: read_file/list_dir LOCAL tools + drive_excel in-place editor | Add LOCAL_TOOL_SPECS for list_dir/read_file/write_file (Anthropic input_schema shape; the pattern in deliverables.TOOL_SPECS/specs_for/run_tool) wrapping… | WS2, WS5 |
| WS07 | 3 | Agent catalog: tables, publish/install/catalog endpoints, manifest split | Add AgentCatalog/AgentInstall models (in the 0007 migration). Add POST /api/agents/<id>/publish (admin inline-403; snapshot a config.AGENTS dict into… | WS3 |
| WS08 | 3 | Local agent registry loader | At sidecar startup, read deployed agent manifests pulled from GET /api/agents/catalog (cached by content_hash) and merge them into config.AGENTS so base_agent… | WS7, WS1 |
| WS09 | 3 | Run telemetry sync (shared run lifecycle) | Add AgentRun model (in 0007). Add POST /api/devices/run/start (device-auth; metrics_store.create_run kind='device_run', INSERT agent_run, stamp… | WS3, WS4 |
| WS10 | 3 | Remove report-delivery pricing from the run path | Drop the _plan_gate call from the local daemon run path (and do not port it into device runs) so report-delivery pricing is discarded while auth + ¥ wallet +… | — |
| WS11 | 4 | Tauri shell, PyInstaller sidecar, signing, auto-update, CI | Wrap the SAME daemon in a Tauri 2.0 shell: native dialog.open picker, reveal-in-OS, keyring device-token storage, per-launch shared-secret + loopback Origin… | WS1, WS4, WS6 |
| WS12 | 4 | Watched-folder automation + offline Local-Model mode (optional) | Add a watchdog folder-watcher that maps a watched directory -> an agent id and enqueues a local run when a supported file lands/changes (debounce + a… | WS1, WS5 |

---

## 10. Decisions — RESOLVED (2026-06-15)

All eight open questions now have decisions. The three execution-gating ones are in §0; the remaining five are resolved here.

| # | Question | Decision |
|---|----------|----------|
| 1 | Form factor | **Tray daemon → Tauri v2.** WSS bridge shelved. (§0) |
| 2 | How real for v1 | **File-builder agents on Claude + domestic** via the local tool loop (WS05). (§0) |
| 5 | Web product | **Kept** as a no-install fallback; pricing stubbed off both paths (WS10). (§0) |
| 3 | Local-model (Ollama) mode | **Defer to v2** (WS12), but reserve the seam now: keep all model resolution behind `effective_model()`/`PROVIDERS` so adding `PROVIDERS['ollama']` + a toggle later is purely additive. Never the default; positioned as the *degraded, no-tools, air-gapped* tier for regulated/CN-enterprise buyers. |
| 7 | Per-device ¥ sub-cap | **Yes, default it.** Each paired device gets a daily ¥ sub-cap = `min(account_daily_cap × 0.3, ¥20)` (tunable), passed as `min(device_cap, account_cap)` into `platform_guard.check`. Surfaced in device-management as a plain "daily limit" — never tokens/¥-per-call. Cheap insurance against a stolen device token draining the wallet. → refines **WS04**. |
| 8 | Write-approval UX | **Tiered, default-safe.** New files → write freely into an `outputs/` subdir of the chosen folder. In-place edits (`drive_excel`) → **per-session folder approval** + write to a `*_edited.xlsx` **copy by default**, with an explicit per-run "overwrite original" opt-in. No per-write prompts (too much friction). `encrypt_outputs` stays **OFF** for user-owned dirs (encrypting in place would corrupt their working files). → refines **WS06**. |
| 6 | Privacy copy | **Say it plainly** (no overclaim). Canonical wording, shown in the daemon UI + device-management page: *"Tangent Agent runs on your computer and reads your files locally. To analyze them, the content of the files you select is sent—encrypted in transit—to our AI service and the model provider, exactly as the web version does. Files are never uploaded or stored on our servers; only a run summary (no file contents) syncs to your account. For fully offline analysis where nothing leaves your machine, enable Local Model mode (lower quality, no web research)."* → wired in **WS01** (daemon UI) + **WS04** (device page). |
| 4 | Signing / distribution budget | **Confirmed approach; needs owner $ sign-off before Phase 4 only.** Apple Developer Program (~$99/yr) + Windows Azure Trusted Signing (~$10/mo) + CN 软著 filing + AV-vendor whitelisting. Does **not** block Phases 0–3, but start the CN 软著 + AV-whitelisting filings early (long lead time). This is the only item still needing an owner action (the spend itself). |

---

## 11. Risks & mitigations

- The flagship file-building agent half-works on first release if WS5 (local tool loop) slips: /v1 is plain-chat-only and complete_with_tools degrades to text on non-Anthropic, so without the local loop the 'returns a populated .xlsx' agent silently drops its deliverable on domestic DeepSeek. Treat WS5 as the make-or-break, not a footnote.
- A loopback HTTP server (the daemon) is reachable by ANY local process or browser tab (CSRF / DNS-rebinding); without a per-session Origin/CSRF token a malicious page could drive the paired daemon to spend the account's ¥ balance. The Origin check in WS1 is load-bearing.
- Remote/local-driven file WRITES (drive_excel/write_file) are the scariest trust surface: a server bug, a prompt-injected spreadsheet, or a stolen device token could read or overwrite the user's real documents. Mitigations are mandatory: the WS2 default-deny allowlist (realpath+commonpath+symlink-resolve), writes confined to an outputs/ subdir by default, per-write tray approval, instant device revoke, and encrypt_outputs OFF for user-owned dirs (encrypting files the user owns in place would CORRUPT their working documents — do NOT port the Fernet-at-rest server invariant to ~/Documents).
- PyInstaller produces a sidecar that imports fine in dev but crashes on a clean customer machine (missing pandas/numpy/reportlab hidden imports or the CJK font; the live 2026 pyinstaller#9360 msvcp140 bug breaks numpy import on Windows). Mitigate with smoke-tests on bare VMs in CI. --onefile is a trap (re-extracts ~180MB per launch, AV-flagged) — use --onedir.
- Unsigned / improperly-notarized builds get hard-blocked by macOS Gatekeeper and scare buyers via Windows SmartScreen; nested PyInstaller Mach-O binaries are easy to forget to sign and silently fail notarization. EV no longer skips SmartScreen (changed 2024) — don't overpay for it; reputation still accrues over downloads.
- China AV vendors (360/Huorong/Tencent) flag a frozen-Python exe that opens a localhost port and phones a server as malware — needs vendor whitelisting with lead time, plus a 软著 filing and the existing ICP/算法备案 dependency for the phone-home endpoint.
- Shipping the ACCOUNT key (tng-sk-) to laptops instead of a per-device token would be a platform-wide credential leak (full wallet + all models, shown once). The separate revocable device-token credential (WS3/WS4) is non-negotiable.
- Privacy overclaim: marketing 'your data stays local' is FALSE under cloud-proxy (file CONTENT transits to the model in prompts). Only Local-Model mode makes that claim true. Imprecise copy is a trust/compliance liability, especially in China.
- Domestic-mode regression: any new endpoint that constructs an Anthropic client at import, or calls effective_model()/provider_for() on the catalog-pull path, breaks a fully-domestic cloud that boots with NO ANTHROPIC_API_KEY. Keep the lazy-router discipline; the catalog path must not resolve models at all.
- Telemetry double-charge / wrong-render: if the device run path inherits finish_run's consume_report_credit_if_over_quota or the orchestrator's local-PDF render, every local run burns a cloud ¥29 credit and the cloud 404s trying to render a PDF that only exists on the device. kind='device_run' must be excluded from quota AND report_path must stay NULL (WS9).
- Cloud-proxy means every local run needs connectivity; laptops sleeping or dropping network mid-run leave half-finished jobs the web product never had to handle — needs resume/cleanup and an honest 'went offline' failure path.
- Shell<->sidecar version skew on partial updates breaking the localhost HTTP contract — ship the Tauri shell + frozen sidecar as ONE atomic versioned bundle, and carry a protocol version in the handshake.


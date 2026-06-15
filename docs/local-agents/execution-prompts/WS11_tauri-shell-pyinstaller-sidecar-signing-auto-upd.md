<!-- WS11 · Phase 4 · depends_on: WS1, WS4, WS6 -->
# WS11 — Tauri shell, PyInstaller sidecar, signing, auto-update, CI

> **Phase 4** · **Depends on:** WS1, WS4, WS6 · **Key files:** src-tauri/tauri.conf.json (new), src-tauri/src/main.rs (new), requirements-desktop.txt (new), build/agentcore.spec (new), .github/workflows/desktop-release.yml (new), frontend/index.html (__BASE_URL__ substitution)

---


## 0. Objective

The repo at `/Users/brucetang/Company_Agent_Demo` is "Tangent AI" — a Flask + vanilla-ES-module SPA multi-agent product. A separate set of workstreams builds a **local daemon** ("Tangent Agent"): a tray-resident supervisor that boots the EXISTING Flask `app.py` via `waitress` bound to `127.0.0.1:<random-port>` in a `LOCAL_MODE`, serves the existing SPA from `frontend/` verbatim, pairs the machine to a cloud account with a per-DEVICE token, and runs agent jobs locally while proxying every LLM turn through the cloud `/v1/chat/completions` gateway. **That daemon is NOT your job.**

**Your job (Phase 4 / packaging & distribution layer):**
1. Wrap that SAME daemon in a **Tauri 2.0** desktop shell that adds: native `dialog.open` folder/file picker, "reveal in Finder/Explorer", OS-keychain device-token storage, a per-launch shared-secret + loopback `Origin` check between the renderer and the daemon, and a hard guard that strips admin/payment/scheduler routes out of the local build.
2. Package the Python core as a **PyInstaller `--onedir` sidecar** with a desktop-only requirements set and the platform hooks needed to actually build (pandas/numpy msvcp140; reportlab CJK font data).
3. **Code-sign**: macOS Developer ID + Hardened Runtime + notarytool + staple; Windows **Azure Trusted Signing** (NOT EV).
4. Wire **Tauri signed auto-update** against a `latest.json` hosted on the ICP-filed endpoint, with Ed25519 update-signing keys.
5. Build a **GitHub Actions release matrix**: `macos-arm64`, `macos-x64`, `windows-x64` (PyInstaller cannot cross-compile — one native runner per target).
6. Add the China-channel scaffolding: default the domestic route in the CN build, and document the 软著 / AV-whitelist (360 / Huorong / Tencent) requirements.

This is the **build, shell, and distribution** layer. You do NOT implement device pairing endpoints, the catalog sync, the run-telemetry sync, the RemoteRouter, the workspace path-guard, or the tray supervisor — those are other workstreams. Where you need them, code against the **contract** documented in §4 and provide thin, well-marked integration seams + fakes so this workstream is independently testable.

---

## 1. Read these files first (do not skip — match their established patterns)

Run these reads before writing anything. The patterns here are load-bearing; copy them, don't reinvent.

- `app.py`
  - `_bearer_key()` (~line 1437), `_current_customer()` (~1486), `_is_admin()` / `_admin_emails()` (~1516–1530) — the auth + founder-gating idiom (inline `if not _is_admin(...): return 403`, NO decorators).
  - `_agents_payload(admin=False)` (~464) — the **field-whitelist** precedent: the row is BUILT from named keys and `model` is added ONLY when `admin`. This is exactly how the local build must strip machinery, and how a device manifest is built (whitelist, never dump).
  - `gateway_chat_completions()` (~4320) and `gateway_models()` (~4308) — the `/v1` gateway shape and its 3-layer guard (auth → rpm → platform_guard → access → billing). The local daemon proxies model turns here; your build must NOT alter billing.
  - `_spa_shell()` (~530) — the server-side placeholder substitution (`__PRODUCT_NAME__`, `__PRODUCT_TAGLINE__`, `__BASE_URL__`) read fresh per request. The desktop build reuses this mechanism for `__BASE_URL__`.
  - The `/api/run` handler (~789) and its upload validation (`security.looks_executable`, `crypto.encrypt_file_in_place`, size caps) — the "files encrypted at rest" + "reject executables" invariants you must NOT weaken.
- `frontend/index.html` — the SPA shell. `__BASE_URL__` already appears (og:image). The shell loads `/js/main.js`.
- `frontend/js/main.js` — `ROUTES`, `AGENTS_NAV`, the `mount(el, ctx) -> cleanup` view contract, `requireAdmin`. (Read top ~120 lines; you only need the routing/nav contract and how machinery views are gated.)
- `agent_team/config.py` — `product_name()` / `product_tagline()` (~575), `team_model_preset()` (~1107), `effective_model()` (~1145), `TEAM_MODEL_PRESET` handling. The CN-build "default domestic route" is a SERVER/env concern (`TEAM_MODEL_PRESET=domestic`), never a client model choice.
- `agent_team/providers.py` — `gateway_chat()` (~181), `class ModelRouter` (~198), `PROVIDERS`/`base_url` adapter shape (~78–94). Context only — you wrap, you do not change inference.
- `agent_team/file_tools.py` — `parse_file()` (~22), `MAX_CHARS_PER_DOC` (~18). Context for what the daemon reads locally.
- `agent_team/customers.py` — the **secret discipline** to mirror: `KEY_PREFIX` (~51), `secrets.token_urlsafe(32)` mint (~97), store-sha256-only (`_hash_key`, ~530), verify-by-hash (`verify_key`, ~131), `_account_to_record` field-stripping. The device token storage you do (keychain) must NEVER write the raw token to disk.
- `requirements.txt` — the FULL dependency list; your `requirements-desktop.txt` is a curated SUBSET.
- `tests/conftest.py` — how the suite imports `app` and disables the limiter / neutralizes `load_dotenv`. Your tests live in the same suite and must stay hermetic.
- `tests/test_phase8_security.py` — the style for security-invariant assertions (route gating, hash-not-plaintext). Mirror this style.
- `migrations/versions/0006_phase7_single_report.py` — the inspector-guarded migration idiom. (You do NOT write a migration — that's WS dependencies — but read it to know the head is `0006` so you do not collide.)

There is currently **no** `.github/workflows/`, **no** `src-tauri/`, **no** `agent_team/runner/`, **no** `agent_team/workspace.py`, and **no** `requirements-desktop.txt`. You are creating the first three of your files net-new.

---

## 2. Invariants you MUST preserve (hard requirements — a violation fails the workstream)

1. **No model keys, provider keys, or per-token ¥ rates on the customer machine.** The sidecar ships NO Anthropic/DeepSeek/Qwen key. Inference is cloud-proxied via `/v1/chat/completions` with a `tng-dev-…` device token. Verify by grepping the packaged sidecar spec's hidden-imports and bundled data: nothing that reads `os.environ["ANTHROPIC_API_KEY"]` at build time, no `.env` with provider keys baked in.
2. **China domestic mode keeps working with zero client change.** Model routing, `TEAM_MODEL_PRESET=domestic`, `effective_model()`, `provider_for()` are all SERVER-side. The desktop never needs `anthropic.com` reachability and never chooses a model. The CN build differs ONLY by `__BASE_URL__` pointing at a mainland-hosted gateway and a packaged default that prefers the domestic endpoint.
3. **¥ cost-control stays server-owned.** `platform_guard.check` + `token_manager` + `record_usage` all run cloud-side on each `/v1` call. Your packaging must not provide any path that bypasses the gateway for billed inference.
4. **Founder machinery is stripped from the local build AND server-gated regardless.** In `LOCAL_MODE` the admin/payments/scheduler routes must be unreachable (return 404/410), AND the existing inline `_is_admin` 403 gates remain. Defense in depth: even if a route leaks, a device/local session is never admin.
5. **Files encrypted at rest; executables rejected.** The local-file path (other workstreams) keeps `crypto.encrypt_file_in_place` + `security.looks_executable`. Your shell's native picker hands PATHS to the daemon; it must not introduce an un-validated read path.
6. **Honesty rule.** Never ship or imply a signed/notarized/AV-whitelisted artifact that the pipeline did not actually produce. Unsigned dev builds must be clearly labeled; the CI must FAIL (not warn) if a required signing secret is absent on a release tag.
7. **Loopback CSRF defense.** The daemon binds `127.0.0.1` only. The renderer↔daemon channel must carry a per-launch shared secret AND the daemon must reject requests whose `Origin`/`Host` is not the expected loopback. A browser tab on a malicious site must not be able to drive the local daemon.
8. **Brand = Tangent AI.** Window title, bundle identifier, installer copy all read from `config.product_name()` / the brand constants — no hard-coded competitor or placeholder names.

---

## 3. Deliverables — files to create / edit

### 3.1 `requirements-desktop.txt` (NEW)

A curated subset of `requirements.txt` for the PyInstaller sidecar. The daemon runs the Flask app locally via `waitress`, parses local files, and BUILDS deliverables on-device, but does NOT talk to Postgres/Redis and does NOT run Alembic.

- **EXCLUDE** (server-only): `psycopg2-binary`, `gunicorn`, `redis`, `alembic`, `sentry-sdk[flask]`, `flask-limiter` (the local daemon is single-user; if any code imports it, pin a `memory://` shim instead — confirm by grepping `app.py` for hard `flask_limiter` imports and document the resolution).
- **KEEP / ADD** (needed on-device): `flask`, `werkzeug`, `python-dotenv`, `SQLAlchemy` (SQLite only — bundled stdlib `sqlite3`), `PyYAML`, `pandas`, `openpyxl`, `pypdf`, `python-docx`, `python-pptx`, `reportlab`, `cryptography`, `keyring`, plus **`waitress`** (the local WSGI server — gunicorn is fork-based and not Windows-friendly) and **`watchdog`** (watched-folder feature).
- Keep `anthropic` OUT unless the daemon imports it unconditionally at startup. Check: grep `agent_team/providers.py` and `app.py` for top-level `import anthropic`. If it's lazy (it is, per the "lazy router" design), exclude it; if a top-level import would crash the sidecar, include it but document that NO key is bundled.
- Pin each line with the SAME version floors as `requirements.txt` (copy the `>=` constraints) so dev and desktop resolve compatibly. Add a header comment explaining this is the **desktop sidecar** subset and why each server-only dep is excluded.

### 3.2 `build/agentcore.spec` (NEW — PyInstaller spec)

A `--onedir` spec that produces the sidecar binary the Tauri shell launches.

- **Entry point:** `agent_team/runner/__main__.py` (the supervisor entry from the runner workstream). That file may not exist yet in your branch — if so, create a **minimal placeholder** `agent_team/runner/__main__.py` that: prints a chosen free `127.0.0.1` port to stdout in a parseable line (`TANGENT_DAEMON_PORT=<n>`), reads `TANGENT_LOCAL_SHARED_SECRET` from the env, and serves the existing `app.py` Flask object via `waitress.serve(app.app, host="127.0.0.1", port=port)`. Mark it clearly as a SEAM the runner workstream will replace; keep your spec pointed at it.
- **`name`/output:** the binary must carry the **target-triple suffix Tauri's sidecar mechanism requires**: Tauri looks for `tangent-agentcore-<target-triple>` (e.g. `tangent-agentcore-aarch64-apple-darwin`, `tangent-agentcore-x86_64-apple-darwin`, `tangent-agentcore-x86_64-pc-windows-msvc.exe`). The spec must accept the triple via an env var / `--` argument so the same spec builds all three; in CI you pass the runner's triple. Document the exact naming so `externalBin` in `tauri.conf.json` resolves it.
- **Hidden imports / hooks:**
  - **pandas/numpy msvcp140 hook (pyinstaller#9360):** on Windows, numpy ≥ 2 needs the MSVC runtime DLLs collected. Add the documented workaround — collect `msvcp140.dll` (and siblings) via a runtime hook / `binaries` entry, referencing pyinstaller issue #9360 in a comment so a maintainer can re-check when the upstream hook lands. Verify the built Windows sidecar imports `pandas` without a missing-DLL error.
  - **reportlab CJK font data files:** reportlab ships CJK CMap/font data that PyInstaller misses by default. Use `collect_data_files('reportlab')` (and explicitly add the `reportlab/fonts` + CID font resource dir) so the on-device PDF builder can embed the CJK font (the existing branded report depends on this — see `agent_team/pdf_report.py`). Verify the built binary renders a CJK PDF.
  - `collect_data_files` / `collect_submodules` as needed for `openpyxl`, `python-pptx` (XML templates), `keyring` backends (`keyring.backends.macOS`, `keyring.backends.Windows`), `pandas`.
- **Exclusions:** explicitly `excludes=[...]` the server-only modules so they don't get pulled transitively and bloat the bundle: `psycopg2`, `gunicorn`, `alembic`, `redis`, `sentry_sdk`.
- The spec must be **deterministic across the 3 CI runners** (no absolute paths from your machine; derive paths from `os.path.dirname(SPECPATH)` / the repo root).

### 3.3 `src-tauri/tauri.conf.json` (NEW)

Tauri 2.0 config. Key sections:

- **`identifier`**: a stable reverse-DNS bundle id (e.g. `ai.tangent.agent`). Used for keychain service name and update channel.
- **`productName`**: "Tangent Agent" (or sourced from brand). Window title set from the same brand string.
- **`bundle.externalBin`**: `["binaries/tangent-agentcore"]` — Tauri appends the target triple automatically when resolving the sidecar at build time. Document where CI drops the PyInstaller output so this resolves.
- **`bundle.resources`**: bundle the `frontend/` directory verbatim so the shell can serve / point the webview at the SPA. (The daemon serves it over loopback; the webview navigates to `http://127.0.0.1:<port>` with the shared secret — see main.rs.)
- **Security / CSP:** a strict `app.security.csp` allowing the webview to talk ONLY to `http://127.0.0.1:*` (the daemon) and the cloud `__BASE_URL__` origin (auth + `/v1`). No `unsafe-eval`. Disallow remote origins beyond those two.
- **`plugins.updater`**: enable Tauri's signed updater. `endpoints` = the ICP-filed `latest.json` URL (e.g. `https://<icp-host>/desktop/latest.json`), templated per channel/target. `pubkey` = the **Ed25519 public** update-signing key (the matching private key lives ONLY in CI secrets and a backed-up vault — see §3.6). Set `dialog: true` (or a custom flow) so updates are user-acknowledged.
- **`plugins.dialog`** and **`plugins.shell`** (for reveal-in-OS via `open`/`opener`) enabled with **minimum capabilities** — scope the shell open to file/folder reveal only, the dialog to open (not arbitrary fs).
- **Capabilities** (Tauri 2.0 `capabilities/` ACL): grant the renderer ONLY `dialog:allow-open`, `opener:allow-reveal-item-in-dir` (reveal in Finder/Explorer), and the loopback HTTP fetch to the daemon. Deny everything else (no `fs:*`, no `shell:execute`).
- **`bundle.macOS`**: `minimumSystemVersion`, hardened-runtime entitlements file reference (see §3.5), `signingIdentity` sourced from env in CI.
- **`bundle.windows`**: installer = NSIS or WiX; reference Azure Trusted Signing via the CI signing step (see §3.5). Do NOT configure EV cert fields.

### 3.4 `src-tauri/src/main.rs` (NEW — Rust shell)

The native shell. Responsibilities (keep it thin — the Python daemon does the work):

1. **Spawn the sidecar** on launch using Tauri's sidecar/`Command` API. Before spawning, generate a **per-launch shared secret** (`token_urlsafe`-equivalent, 32 bytes from a CSPRNG) and pass it to the sidecar via env (`TANGENT_LOCAL_SHARED_SECRET`). Read the sidecar's stdout for the `TANGENT_DAEMON_PORT=<n>` line to learn the chosen loopback port.
2. **Loopback Origin/secret enforcement (renderer side):** when the webview navigates to `http://127.0.0.1:<port>`, every fetch from the SPA to the daemon must carry the shared secret (inject it as a header via an init script or a Tauri command the SPA calls to obtain it). The DAEMON side validates the secret + `Origin`/`Host` (that validation lives in the daemon/LOCAL_MODE middleware — see §3.7 seam). main.rs owns generating + transporting the secret; document the contract precisely.
3. **Native folder/file picker:** expose a Tauri command (or rely on the `dialog` plugin invoked from JS) that returns selected PATHS. The SPA posts those paths to the daemon's path-aware run endpoint (other workstream). main.rs only needs to ensure the `dialog` capability is wired.
4. **Reveal in OS:** expose a `reveal_in_os(path)` command that calls the `opener` plugin's reveal-item-in-dir. The SPA's "Download" → "Reveal in Finder/Explorer" calls this.
5. **Keyring device-token storage:** expose two commands — `store_device_token(token)` and `get_device_token()` — backed by the `keyring`/`keytar`-equivalent (use the Rust `keyring` crate). Service name = the bundle identifier, account = "device-token". NEVER write the token to a file. The pairing flow (other workstream) calls `store_device_token`; the daemon retrieves it at startup via a command or the renderer passes it. Document which side holds it: per the architecture, the token lives in the OS keychain and is read by the daemon — provide the command and a clear note on the handoff.
6. **Graceful shutdown:** kill the sidecar on window close / app exit so no orphaned loopback server survives.
7. **Updater:** initialize the Tauri updater plugin (the config does the heavy lifting; main.rs registers it and, if you use a manual flow, checks on launch).

Add a `src-tauri/Cargo.toml`, `src-tauri/build.rs`, and `src-tauri/capabilities/default.json` as required by Tauri 2.0 — these are part of a working shell even though the task names only `main.rs`. Keep capability grants minimal.

### 3.5 Signing configuration + entitlements

- **macOS:** create `src-tauri/entitlements.plist` with the **minimum** Hardened Runtime entitlements the app actually needs. Start from the smallest set: it must allow the bundled Python sidecar to run, so include `com.apple.security.cs.allow-unsigned-executable-memory` and/or `com.apple.security.cs.allow-jit` ONLY if a runtime test proves CPython needs it under Hardened Runtime — prefer NOT granting them and add only what notarization/run testing forces. Do NOT add network-server entitlements beyond what loopback needs. Document each entitlement with a one-line justification. The CI signing step uses Developer ID Application identity, then `xcrun notarytool submit --wait` and `xcrun stapler staple`.
- **Windows:** use **Azure Trusted Signing** (the `azure/trusted-signing-action` GitHub Action, or `signtool` with the Trusted Signing dlib). Document explicitly in a comment that **EV certificates are deliberately NOT used because they no longer grant SmartScreen reputation skip** — Trusted Signing builds reputation over installs like a standard cert, so there is no EV advantage and Trusted Signing is the chosen path. Sign BOTH the sidecar `.exe` and the final installer.

### 3.6 Auto-update artifacts

- The Tauri updater needs a `latest.json` per channel listing version, per-target download URL, and the **Tauri update signature** (Ed25519 over the bundle, separate from the OS code-signing). CI generates these with `tauri signer sign` using the **Ed25519 private key** held in `TAURI_SIGNING_PRIVATE_KEY` (+ password) secret.
- **CRITICAL, document prominently** (in `src-tauri/UPDATER.md` or a top-of-file comment in the workflow): the Ed25519 **private key loss is unrecoverable** — if lost, no future build can produce an update the installed base will accept, forcing every user to manually reinstall. The key MUST be backed up out-of-band (sealed vault) the moment it is generated. Provide the exact `npx @tauri-apps/cli signer generate` command and the backup checklist.
- CI uploads the per-target bundle + its `.sig` and regenerates/updates `latest.json` on the ICP-filed endpoint (the upload/publish step can be a documented manual or scripted `gh release` + endpoint sync — implement the artifact production and a `latest.json` generator script `src-tauri/scripts/gen_latest_json.py` or `.mjs`; the actual upload to the ICP host is an ops step you script but flag as requiring the ICP-filed host).

### 3.7 `frontend/index.html` edit (`__BASE_URL__` substitution)

`__BASE_URL__` already exists in `index.html` and is substituted by `_spa_shell()` server-side. For the desktop build the SPA is served by the LOCAL daemon over loopback, but **auth and `/v1` inference must point at the CLOUD**. So:

- Confirm/extend the placeholder set so the daemon's `LOCAL_MODE` shell substitution injects **two** roots: the LOCAL daemon root (for `/api/run`, status, reveal) and the CLOUD base (`__BASE_URL__`, for `/device` auth + `/v1`). If a single `__BASE_URL__` is insufficient, add a `__CLOUD_BASE_URL__` placeholder and have the daemon's local `_spa_shell` equivalent fill it from the paired account's cloud host. Keep the substitution mechanism identical to `_spa_shell()` (read fresh, string-replace) — do not introduce a build-time bake that would break the "edit and reload" property.
- Make the minimal HTML change only; the heavy lifting (which fetches go local vs cloud) is the SPA/runner workstream. Your edit is the placeholder + a documented contract for what each root means.

### 3.8 `.github/workflows/desktop-release.yml` (NEW — release CI)

A tag-triggered (`on: push: tags: ['v*-desktop']`) matrix workflow.

- **Matrix** (PyInstaller cannot cross-compile — one native runner per target):
  - `macos-14` (Apple Silicon) → `aarch64-apple-darwin`
  - `macos-13` (Intel) → `x86_64-apple-darwin`
  - `windows-latest` → `x86_64-pc-windows-msvc`
- **Per-job steps:**
  1. Checkout; set up Python (matching the project's version) + `pip install -r requirements-desktop.txt` + `pip install pyinstaller`.
  2. Build the sidecar: `pyinstaller build/agentcore.spec` passing the runner's target triple so the output is named `tangent-agentcore-<triple>(.exe)`; move it to `src-tauri/binaries/`.
  3. Set up Rust toolchain + Tauri CLI; `npm`/`pnpm` install for the Tauri JS deps if used.
  4. **Sign + build the app:**
     - macOS: import the Developer ID cert from secrets into a temp keychain; `tauri build` with `APPLE_SIGNING_IDENTITY`, `APPLE_CERTIFICATE`, `APPLE_ID`/`APPLE_PASSWORD`/`APPLE_TEAM_ID` for notarization; the build runs hardened-runtime signing; then `notarytool submit --wait` + `stapler staple` (Tauri 2.0 can do notarization inline if `APPLE_*` env is set — prefer that, else explicit steps).
     - Windows: `tauri build`, then Azure Trusted Signing action signs the produced installer + sidecar exe (`AZURE_TENANT_ID`/`AZURE_CLIENT_ID`/`AZURE_CLIENT_SECRET`, endpoint, account, cert-profile from secrets).
  5. Generate the Tauri update signature with `TAURI_SIGNING_PRIVATE_KEY` (this is what makes the updater accept the artifact).
  6. Upload artifacts (installer/.app/.dmg/.msi/.exe + `.sig`) to the GitHub Release.
  7. A final aggregation job assembles `latest.json` (all three targets) and uploads it (and documents the ICP-host publish step).
- **Honesty gate:** if a `v*-desktop` tag build runs without the required signing secrets present, the job must **fail** (not silently produce an unsigned artifact labeled as signed). Provide a separate, clearly-named `dev-build` path (manual `workflow_dispatch`) that produces **unsigned** artifacts labeled `-unsigned`.

### 3.9 China-channel documentation (`src-tauri/CN_CHANNEL.md` or a `docs/desktop/cn.md`)

- The CN build differs only by defaulting `__CLOUD_BASE_URL__` to the mainland gateway host (ICP-filed) and shipping with `TEAM_MODEL_PRESET=domestic` as the SERVER default (no client change — restate invariant 2).
- Document the launch checklist: file **软著 (software copyright registration)** for the binary; submit the signed installer to **360**, **Huorong (火绒)**, and **Tencent (腾讯电脑管家)** AV vendors for whitelisting to avoid false-positive quarantine (a known pain point for PyInstaller binaries on the China channel).
- Note the documented **fallback**: if tray/loopback-CSRF/AV-false-positive on the Windows China channel proves too painful, fall back to Form Factor C (cloud-orchestrated thin outbound-WSS bridge) — reference it as the escape hatch but do NOT implement it here.

---

## 4. The daemon contract you depend on (WS1 / WS4 / WS6) — code against this, fake it for tests

These are provided by OTHER workstreams. Do not implement them; depend on the shapes below and stub where needed.

- **`LOCAL_MODE`** (env flag the supervisor sets): when set, `app.py` (a) serves the SPA, (b) enables the loopback-secret + Origin middleware that validates `TANGENT_LOCAL_SHARED_SECRET` and rejects non-loopback `Origin`/`Host`, and (c) makes admin/payments/scheduler routes return 404/410. Your shell SETS the secret and triple-checks the gating in tests; the middleware itself is WS6. If `LOCAL_MODE` handling doesn't exist in `app.py` yet, add the **minimum** seam: a single `_local_mode()` helper + a `before_request` that returns 404 for a defined blocklist of admin/payment/scheduler URL prefixes when `_local_mode()` is true, and validates the shared secret header on `/api/*`. Keep it small, well-commented as the WS6 seam, and fully test-covered.
- **Device token**: `tng-dev-` + `token_urlsafe(32)`, stored sha256-only server-side, kept raw ONLY in the OS keychain on-device. Your keyring commands store/retrieve it. The pairing/exchange endpoints are WS1 — do not build them.
- **Path-aware run + workspace guard** (`agent_team/workspace.py:assert_in_workspace`): WS6. Your native picker hands paths to it; you don't implement it.
- **`/v1/chat/completions` device-auth extension** (verify_device_token after verify_key): WS1. Unchanged by you.
- **Run telemetry sync** (`/api/devices/run/start` + `/finish`): WS4. Unchanged by you.

For any seam you must touch in `app.py` to make THIS workstream testable (the `LOCAL_MODE` route-blocklist + shared-secret check), implement the smallest correct version and mark it `# WS6 SEAM:` so the runner workstream can extend it without conflict.

---

## 5. Acceptance criteria + tests

Add `tests/test_desktop_packaging.py` (pure-pytest, hermetic, no network, no actual `tauri build`). It must assert the **contracts** the heavy CI can't cheaply verify locally:

1. **`requirements-desktop.txt` exclusions:** parse the file; assert NONE of `psycopg2`, `gunicorn`, `redis`, `alembic`, `sentry-sdk` appear, and that ALL of `pandas`, `openpyxl`, `pypdf`, `python-docx`, `python-pptx`, `reportlab`, `cryptography`, `keyring`, `waitress` DO. Assert every version floor matches `requirements.txt` for packages present in both.
2. **PyInstaller spec sanity:** import/parse `build/agentcore.spec` as text; assert it references the target-triple naming, the reportlab data-file collection, the numpy/msvcp140 workaround comment (pyinstaller#9360), and the server-only `excludes`.
3. **No keys baked:** assert the spec + `requirements-desktop.txt` contain no provider API key literals and no `.env` is added as a data file; assert `anthropic` is not an unconditional sidecar dependency (or, if present, that no key is bundled).
4. **`tauri.conf.json` invariants:** load it as JSON; assert `externalBin` uses the `tangent-agentcore` base, the updater `pubkey` is set and the private key is NOT in the file, the CSP restricts to `127.0.0.1` + the cloud base, and capabilities grant ONLY dialog-open + reveal-in-dir + loopback fetch (no `fs:*`, no `shell:execute`).
5. **LOCAL_MODE route gating (the seam you add):** using the existing test client pattern from `tests/conftest.py` / `tests/test_phase8_security.py`, set `LOCAL_MODE` on and assert representative admin/payment/scheduler routes return 404/410, while `/`, `/api/run`, `/v1/chat/completions` remain reachable. With `LOCAL_MODE` off, behavior is unchanged (no regression — run a couple of existing admin routes and confirm they still gate via `_is_admin` 403, not 404).
6. **Shared-secret enforcement (the seam you add):** with `LOCAL_MODE` on and a `TANGENT_LOCAL_SHARED_SECRET` set, assert an `/api/*` request WITHOUT the secret header (or with a cross-origin `Origin`) is rejected, and WITH the correct secret + loopback origin is accepted.
7. **`__BASE_URL__` / cloud-root substitution:** assert the desktop substitution fills both the local and cloud roots and that no provider/model id or ¥ rate is ever placed in the served HTML.
8. **Updater key-loss doc exists:** assert the "private key loss is unrecoverable + backup" warning is present in the updater doc/workflow.
9. **CI honesty gate (static):** parse `.github/workflows/desktop-release.yml` as YAML; assert the matrix has exactly the three targets, that the release path requires signing secrets (a step/condition that fails without them), and that a separate unsigned `dev-build` path exists and labels artifacts `-unsigned`.

Run the FULL suite at the end (`python -m pytest -q`) and confirm the current baseline still passes (the project's current suite is in the ~190–200 range; do not regress it). Your new tests must pass on a machine with NO Tauri/Rust/PyInstaller installed — they assert files and the Python seam, never invoke the native toolchain.

Also document a **manual smoke checklist** (in the CN/updater doc) for the things tests can't cover cheaply: a built `.dmg`/`.msi` launches the tray, opens the SPA on loopback, the native picker returns paths, "reveal in Finder/Explorer" works, the keychain stores/reads the device token, and an update from `vN` → `vN+1` via `latest.json` is accepted.

---

## 6. Explicitly OUT OF SCOPE (do NOT build these here)

- Device pairing endpoints (`/api/devices/pair-code`, `/exchange`, `/revoke`, `GET /api/devices`) and the `devices` table/migration — **WS1**.
- The `/v1` device-token auth extension and the `_current_device()` helper — **WS1**.
- The agent catalog (`/api/agents/catalog`, `agent_catalog`/`agent_install` tables, manifest publish/install) — **WS4**.
- Run-telemetry sync (`/api/devices/run/start|finish`, `agent_run` table, `runs.origin`/`device_id`, `device_run` kind) and **migration `0007`** — **WS4** (head is currently `0006`; do NOT add a migration in this workstream).
- The tray supervisor logic, `pystray` menu, free-port picking beyond the placeholder, watched-folder `watchdog` wiring, the `RemoteRouter`, the local tool-execution loop, the `workspace.py` path guard, and the on-device deliverables/parse glue — **WS6**.
- The new `xlsx` (openpyxl) deliverable builder — separate workstream.
- The Form Factor C outbound-WSS bridge fallback — documented only, not implemented.
- Any change to `record_usage` / `platform_guard` / `token_manager` billing logic — must remain byte-for-byte intact.

Keep every file you create self-contained and heavily commented at the seams (`# WS6 SEAM:` / `# WS1 CONTRACT:`), so the dependency workstreams plug in without merge conflicts. Use absolute paths in any docs. Brand everything "Tangent AI".

<!-- WS01 · Phase 0 · depends_on: none -->
# WS01 — Local runner supervisor + tray + path-based run intake

> **Phase 0** · **Depends on:** none · **Key files:** agent_team/runner/__init__.py (new), agent_team/runner/tray.py (new), app.py, agent_team/file_tools.py, pyproject.toml (new)

> **LOCKED DECISION (2026-06-15) — privacy copy.** The daemon-served dashboard must display this exact statement (small banner/footer; reused on the device page in WS04). Do not soften or overclaim it: *"Tangent Agent runs on your computer and reads your files locally. To analyze them, the content of the files you select is sent—encrypted in transit—to our AI service and the model provider, exactly as the web version does. Files are never uploaded or stored on our servers; only a run summary (no file contents) syncs to your account. For fully offline analysis where nothing leaves your machine, enable Local Model mode (lower quality, no web research)."* Surface it via a `LOCAL_MODE` meta value / static string the SPA reads.

---


## Objective

Build a **local desktop daemon** for the existing Flask app at `/Users/brucetang/Company_Agent_Demo`. Ship a console entrypoint `tangent-agent` (declared in a NEW `pyproject.toml` under `[project.scripts]`) and a supervisor package `agent_team/runner/` that:

1. Picks a free `127.0.0.1` port, boots the EXISTING `app.py` WSGI app via **waitress** (NOT gunicorn — gunicorn is fork-based and not Windows-friendly) with a new `LOCAL_MODE` flag set.
2. Drops a **pystray** tray icon with menu items: *Open dashboard / Pair account / Watched folders… / Pause / Quit*.
3. Opens the default browser at `http://127.0.0.1:<port>`.
4. In `LOCAL_MODE`, **disables/strips founder-admin + payment + scheduler routes**, and adds a **per-session Origin/CSRF token** required on every `/api/*` mutation so that other local processes (a malicious page in the user's browser, another app on the loopback) cannot drive the daemon.
5. Adds a **path-aware variant of `/api/run`** that accepts local file **PATHS** (not multipart bytes) and feeds them to `agent_team/file_tools.py:parse_file`, gated by a new default-deny path chokepoint.

This is **v1 of the desktop product**: the daemon runs the same SPA from `frontend/` and runs agent jobs in background threads exactly as today. Inference still flows through the cloud `/v1/chat/completions` gateway in the full product, but the PROXY/device-token/pairing/catalog-sync work is OTHER workstreams — see "Out of scope" below. This workstream is the **process supervisor, tray, loopback hardening, and path-based run intake** only.

---

## Read these first (exact files/functions — follow their patterns)

- `app.py`
  - Top: `app = Flask(__name__, static_folder="frontend", static_url_path="")` (line ~131); `app.secret_key = _flask_secret()` (~189); `db.init_db()` (~361); `if __name__ == "__main__":` block (~4716) which currently does `app.run(host="127.0.0.1", port=port, ...)`. **The supervisor must boot the SAME `app` object via waitress instead of calling `app.run`.**
  - `@app.route("/api/run", methods=["POST"])` → `def run()` (~786): the multipart handler. Note its guard order: `_current_customer()` → `platform_guard.check(...)` → `_plan_gate(customer)` → upload validation (`config.MAX_UPLOAD_FILES_PER_RUN`, `security.looks_executable`, size cap) → `parse_file(path)` → `crypto.encrypt_file_in_place(path)` → `memory.add_document(doc)` → `metrics_store.create_run(...)` → `threading.Thread(target=_run_team, ...)`. **The path-aware variant reuses this skeleton but takes paths, not `request.files`.**
  - `def _run_team(memory, customer_id, company_id, run_id, industry_key)` (~1000): the background worker. Reuse verbatim.
  - `def _current_customer()` (~1486), `def _is_admin(customer)` (~1524), `def _admin_emails()` (~1516): admin gating is enforced INLINE (`if not _is_admin(...): return 403`), no decorator. There are ~30 `_is_admin` call sites.
  - `def _bearer_key()` (~1437), `def _account_view(customer)` (~1453): field-stripping pattern for non-admins (drops `balance/spent/requests/usage/key_prefix/key_last4` and strips per-model machinery).
  - `def _agents_payload(admin=False)` (~464): shows the **whitelist** discipline — only `row["model"]` is added when `admin=True`; the device/non-admin never sees the model id. Mirror this whitelist mindset.
  - `@app.route("/v1/chat/completions")` → `gateway_chat_completions()` (~4320): the cloud gateway with its 3 layers (auth → `_rpm_check` → `platform_guard.check` → `can_use` → `has_balance` → `providers.gateway_chat` → `record_usage`). **Do NOT modify this in this workstream** — just know it exists so `LOCAL_MODE` does not break it.
  - Payment routes to disable in LOCAL_MODE: `/pay/notify/*`, `/pay/mock/<order_no>`, `/api/pay/create`, `/api/pay/status/*`, `/api/orders`, `/api/plans`, `/api/subscription/*`, `/api/fapiao/*` (lines ~1694–1950). Scheduler routes: `/api/scheduled-jobs*` (~2563–2657) and the in-process scheduler started at the bottom of `app.py` via `config.run_scheduler_in_process()` / `_start_scheduler()`.
- `agent_team/file_tools.py`: `parse_file(path)` (line 22) — reads xlsx/xls/csv/docx/pdf/json/txt → `{name, ext, chars, text, meta}`, 60k cap. **This is the local-file reader; the path-aware run feeds it directly.**
- `agent_team/config.py`: `MAX_UPLOAD_FILES_PER_RUN`, `MAX_UPLOAD_BYTES_PER_FILE`, `team_model_preset()`, `run_scheduler_in_process()`, `product_name()`. AGENTS dict at line 880.
- `agent_team/crypto.py`: `encrypt_file_in_place(path)` (no-op when FERNET unset). **CAUTION:** today `/api/run` encrypts the *uploaded copy* it saved into `UPLOAD_DIR`. In the path-aware run the file is the **user's own file in their own folder** — DO NOT encrypt-in-place over the user's original. See Step 4.
- `agent_team/security.py`: `looks_executable(head_bytes)` magic-byte check.
- `agent_team/memory.py`: `SharedMemory(job_id, store_dir=..., goal=..., output_dir=...)`, `.add_document(doc)`, `.log(...)`.
- `tests/conftest.py`: the suite imports `app`, sets `app.limiter.enabled = False`, and no-ops `app.load_dotenv`. **Tests are hermetic against the local `.env`.** Match this; your tests must not need a real `.env`, Anthropic key, or network.
- `migrations/versions/0006_phase7_single_report.py`: the inspector-guarded migration pattern (`_inspector`, `_column_missing`, `if dialect == "postgresql"`). Head revision is `0006_phase7_single_report`. (This workstream adds NO new tables — see "Out of scope" — but you must not break the existing `db.init_db()` boot path.)
- `requirements.txt`: already has `flask`, `openpyxl`, `gunicorn`. Does NOT yet have `waitress`, `pystray`, `pillow`, `keyring`, `watchdog`.

---

## Implementation spec

### Step 1 — `pyproject.toml` (NEW, repo root)

Create a minimal PEP 621 `pyproject.toml` that makes the runner installable as a wheel and registers the console script. Do NOT rip out `requirements.txt` (the server deploy still uses it).

- `[build-system]`: setuptools backend.
- `[project]`: `name = "tangent-agent"`, a version, `requires-python = ">=3.10"`, and `dependencies` covering what the runner needs at minimum: `waitress`, `pystray`, `pillow` (pystray needs a PIL `Image` for the icon), `flask`, plus the app's own runtime libs OR a path reference to `requirements.txt`. Keep it pragmatic — the server still installs via `requirements.txt`; the wheel path is for power users.
- `[project.scripts]`: `tangent-agent = "agent_team.runner:main"`.
- `[project.optional-dependencies]`: a `desktop` extra with `keyring` and `watchdog` (these belong to pairing/watched-folders workstreams but declare them here so the extra exists; do NOT import them at module top level in this workstream's code).
- `[tool.setuptools]`: make sure `agent_team` and `agent_team.runner` are discoverable as packages, and that `frontend/` ships as package data (or document that v1 runs from a source checkout). Prefer `packages = ["agent_team", "agent_team.runner"]` or `find` with includes.

Append `waitress`, `pystray`, `pillow`, `keyring`, `watchdog` to `requirements.txt` as well (commented to note they are desktop-only is fine), so a dev `pip install -r requirements.txt` can run the daemon.

### Step 2 — `agent_team/runner/__init__.py` (NEW): the supervisor

Expose a `main(argv=None)` function (the `[project.scripts]` target) plus a `Supervisor` class. Responsibilities:

1. **Set `LOCAL_MODE`**: before importing `app`, set `os.environ["LOCAL_MODE"] = "1"` (and set `os.environ["RUN_SCHEDULER"] = "0"` so the in-process scheduler never starts). Importing `app` runs `db.init_db()` and registers routes; the `LOCAL_MODE` env must be visible to `app.py` at import time (see Step 5 — `app.py` reads it once into a module-level `LOCAL_MODE = ...`).
2. **Pick a free loopback port**: bind a throwaway `socket` to `("127.0.0.1", 0)`, read `getsockname()[1]`, close it, use that port. Do not hardcode 8000.
3. **Mint a per-session origin/CSRF token**: `secrets.token_urlsafe(32)`. Inject it into the app so `app.py`'s `LOCAL_MODE` request hook can require it (Step 5). Pass it via `os.environ["LOCAL_CSRF_TOKEN"]` (set before importing app) OR via a setter on the app — pick one and be consistent. The token is per-process and never written to disk.
4. **Boot waitress in a background thread**: `from waitress import serve; serve(app.app, host="127.0.0.1", port=port, threads=8)`. Run it in a `threading.Thread(daemon=True)` so the tray (which must own the main thread on macOS for the GUI loop) stays responsive. Provide a clean shutdown path (waitress `create_server` + `server.close()` is cleaner than `serve()` if you need to stop it for Quit — prefer `create_server(...)` so Quit can call `.close()`).
5. **Open the browser** at `http://127.0.0.1:<port>/` once the server is accepting connections (poll `GET /healthz` until 200, then `webbrowser.open(url)`).
6. **Start the tray** (Step 3) on the main thread.
7. **Graceful shutdown**: Quit stops the waitress server, stops the tray icon, and exits.

`main()` orchestrates: set env → import app → pick port → start server thread → wait-for-healthz → open browser → run tray (blocks) → on tray-quit, shut down.

Keep all of this **import-safe**: importing `agent_team.runner` must NOT start a server or a tray (only `main()` does). The test suite imports the package; side-effect-free import is required.

### Step 3 — `agent_team/runner/tray.py` (NEW): the tray icon

A small module that builds and runs a `pystray.Icon`. Functions:

- `build_icon(*, on_open, on_pair, on_watched, on_pause_toggle, on_quit, url)` → returns a configured `pystray.Icon` with a `pystray.Menu` of `MenuItem`s: **Open dashboard** (calls `on_open`, default `webbrowser.open(url)`), **Pair account** (`on_pair`), **Watched folders…** (`on_watched`), **Pause** (a checkable item driven by `on_pause_toggle`), **Quit** (`on_quit`, stops the icon).
- A generated icon image via Pillow (`PIL.Image.new(...)` drawn with `ImageDraw`) — a simple branded glyph (Tangent AI). Do NOT require an external icon file.
- **Pair / Watched folders are STUBS in this workstream**: wire the menu items to callbacks that, for now, just open the relevant SPA route in the browser (e.g. `Pair` → open `/account` or a `#pair` deep link, `Watched folders…` → open a placeholder) and log "not yet implemented". The real pairing (device-token exchange) and watchdog folder-watching are SEPARATE workstreams. The menu wiring must exist and be testable; the behavior behind Pair/Watched is intentionally minimal.
- **Pause**: a real toggle that flips a process-level flag the path-aware run endpoint checks — when paused, the daemon refuses NEW runs with a clear 503 (existing runs finish). Expose this as a simple shared boolean/event the supervisor owns and the app reads (e.g. via a module-level `agent_team.runner.state` flag, or an env/app-config value the request hook checks).

Keep `tray.py` importable without a display (guard the actual `Icon.run()` behind the function call, not import). The headless CI/test environment has no GUI, so **tests must never call `icon.run()`** — they only assert the menu is constructed with the right items/callbacks.

### Step 4 — Path-aware run intake (edit `app.py`)

Add a new route, active **only in LOCAL_MODE**, that accepts local file PATHS instead of multipart bytes:

```
POST /api/run/local      (LOCAL_MODE only; 404 otherwise)
Body (JSON): { "goal": str, "paths": [str, ...], "industry": str|null }
```

Handler `run_local()`:

1. **CSRF/origin check** (shared LOCAL_MODE before_request hook, Step 5 — so it is already enforced).
2. **Pause check**: if the daemon is paused (Step 3), return `503 {"error": "已暂停"}`.
3. **Auth**: still call `_current_customer()`. In LOCAL_MODE a paired device session stands in for the customer — BUT device pairing is a separate workstream. For THIS workstream, accept the existing session-cookie customer (the daemon serves the same SPA; the user is "logged in" locally). If `_current_customer()` is None, return 401 exactly as `/api/run` does. Do NOT invent a device-token path here.
4. **Guards**: reuse `platform_guard.check(...)` and `_plan_gate(...)` exactly as `/api/run` does (cost-control invariant — see Invariants).
5. **PATH VALIDATION — the default-deny chokepoint** (NEW, see Step 4a): for every path in `paths`, call `assert_in_workspace(path, roots)` BEFORE opening anything. Reject `..`, symlink escapes, and any path outside the user-approved roots with `400`. Also re-apply the existing safety gates per file: `security.looks_executable(first 16 bytes)` and the `config.MAX_UPLOAD_BYTES_PER_FILE` / `config.MAX_UPLOAD_FILES_PER_RUN` caps (read sizes via `os.path.getsize`).
6. **Parse**: `doc = parse_file(path)` for each validated path; `memory.add_document(doc)`; `memory.log(...)`. **Do NOT `crypto.encrypt_file_in_place()` the user's own file** — that endpoint encrypts the server's *uploaded copy*; here the file belongs to the user and lives in their folder. If you need an at-rest copy under the job's upload dir, copy it there first and encrypt the COPY, never the original. Simplest correct behavior: parse in place, never mutate the user's file.
7. **Run**: identical to `/api/run` — `metrics_store.get_or_create_company(...)`, `metrics_store.create_run(company_id, goal)`, then `threading.Thread(target=_run_team, args=(memory, customer["id"], company_id, run_id, industry_key), daemon=True).start()`, return `{"job_id": job_id}`.

The live activity feed (`/api/status/<job_id>`) and the deliverables builders run UNCHANGED. The SPA's existing run-feed works as-is.

#### Step 4a — `agent_team/workspace.py` (NEW): the path chokepoint

Create a small default-deny module:

- `approved_roots()` → the list of absolute, realpath-normalized directories the user has approved. For THIS workstream, seed it from an env var `LOCAL_WORKSPACE_ROOTS` (os.pathsep-separated) plus a sane default (e.g. the user's home `Documents`/`Downloads`, or simply the current working directory). The watched-folders UI that lets the user ADD roots at runtime is a later workstream — expose an `add_root(path)` / `set_roots(paths)` API now so that workstream can call it, but driving it from the tray is out of scope here.
- `assert_in_workspace(path, roots=None) -> str`: canonicalize with `os.path.realpath(path)`, then verify `os.path.commonpath([root, real]) == root` for at least one approved `root`. Reject (raise a `WorkspaceError` or return a sentinel the route turns into 400) on: non-existent path, path that is a directory when a file is required, symlink that resolves outside every root, or `..` traversal. **Resolve the realpath BEFORE the commonpath check so a symlink inside an approved root that points outside is rejected.** Default-deny: empty/None roots → reject everything.
- This module must be importable and unit-testable with zero Flask/app dependency.

### Step 5 — `LOCAL_MODE` plumbing + loopback hardening (edit `app.py`)

1. Near the top of `app.py` (after `load_dotenv`), add a module-level constant:
   `LOCAL_MODE = os.environ.get("LOCAL_MODE", "").strip().lower() in ("1", "true", "yes")`
   and `LOCAL_CSRF_TOKEN = os.environ.get("LOCAL_CSRF_TOKEN", "").strip() or None`.
2. **Disable founder-admin in LOCAL_MODE**: the desktop user is NOT a founder. Rather than touch 30 call sites, force `_is_admin()` to return `False` whenever `LOCAL_MODE` is on (one-line guard at the top of `_is_admin`). This makes every existing `if not _is_admin(...): return 403` fire correctly and keeps all founder machinery (`/api/models`, `/api/providers`, `/api/tokens*`, `/api/fusion`, `/api/agents/model`, `/api/meta` catalog) locked on the device — no per-route edits needed. Add a code comment explaining the single chokepoint.
3. **Strip payment + scheduler routes in LOCAL_MODE**: add a `before_request` hook (or wrap registration) so that requests to the payment/subscription/fapiao/orders/plans and `/api/scheduled-jobs*` paths return `404` (preferred — they should look absent, not forbidden) when `LOCAL_MODE`. A clean implementation: a `before_request` that checks `request.path` against a LOCAL_MODE blocklist prefix tuple and returns a 404 JSON. Document the blocklist. Also ensure the in-process scheduler never starts in LOCAL_MODE — the supervisor already sets `RUN_SCHEDULER=0`, but also guard `_start_scheduler()` behind `not LOCAL_MODE`.
4. **Per-session Origin/CSRF token on `/api/*`** (the core hardening): add a `before_request` hook that, when `LOCAL_MODE` and the request is a state-changing `/api/*` call (method in `{POST, PUT, PATCH, DELETE}` and `request.path.startswith("/api/")`), requires a header (e.g. `X-Tangent-Local-Token`) equal to `LOCAL_CSRF_TOKEN`. Reject mismatches with `403 {"error": "forbidden"}`. **The SPA must send this header** — so the supervisor must surface the token to the page: inject it into the served `index.html` (extend the existing `_spa_shell()` placeholder substitution — it already does `__PRODUCT_NAME__`/`__BASE_URL__`) as a `__LOCAL_TOKEN__` placeholder, and have a tiny inline script set `window.__LOCAL_TOKEN__`; OR expose it via a `GET /api/local/csrf` that the SPA fetches. **Prefer the index.html injection** (no extra round trip, and a cross-origin page can't read the injected token because it can't read the loopback HTML response under CORS). Document why this blocks a malicious browser tab / other local process: they cannot read the per-session token, so they cannot forge a valid mutating `/api/*` call. Reads (GET) stay open so the SPA loads.
   - Also reject requests whose `Origin`/`Referer`, when present, is not the loopback origin — defense in depth against DNS-rebinding. A missing Origin (same-origin fetch) is allowed; a present cross-origin one is rejected.
5. **Do NOT change `/v1/chat/completions`** or `verify_key`/`record_usage` — cost-control and the cloud gateway are untouched by this workstream.

Keep all LOCAL_MODE behavior **off by default** (server deploy sets no `LOCAL_MODE`), so the existing 200+ test suite and the production server are completely unaffected.

---

## Invariants (must hold)

- **China domestic mode keeps working.** Nothing here may add a hard Anthropic dependency. The daemon boots and serves the SPA with `TEAM_MODEL_PRESET=domestic` and no Anthropic key (the `__main__` block already special-cases this — preserve it). Do not call `effective_model()`/`provider_for()` in any new device-facing/LOCAL_MODE path.
- **No model keys on the customer machine.** This workstream introduces no provider keys and no key storage. Inference routing/device tokens are other workstreams; here you only run the local server + path intake. Do not embed any key.
- **¥ cost-control stays intact.** The path-aware run MUST pass through `platform_guard.check(...)` and `_plan_gate(...)` exactly like `/api/run`, and `_run_team` keeps billing via the existing `_make_biller`/`record_usage` path. Do not bypass the breaker or the wallet.
- **Founder machinery stays gated** — and in LOCAL_MODE is fully off (Step 5.2). The device is "just another non-admin caller."
- **Files encrypted at rest** — but never encrypt the USER'S OWN file in place (Step 4.6). Server-side uploaded copies keep `crypto.encrypt_file_in_place`; the path-intake reads the user's original without mutating it.
- **Honesty / label demo-vs-real.** Pair and Watched-folders are stubs this phase — the tray/UX must not imply they fully work. Log/label them as "coming soon" rather than silently no-op.
- **Default-deny path access.** `assert_in_workspace` rejects everything when no root is approved; realpath-before-commonpath; reject `..` and symlink escapes.
- **Zero impact when LOCAL_MODE is unset.** The production server and the full pytest suite must behave identically to today.

---

## Acceptance criteria + tests

Add a new suite `tests/test_runner_local.py` (pytest, mirroring the hermetic style of `tests/conftest.py` — no real `.env`, no network, no GUI, no Anthropic key). Use `monkeypatch.setenv("LOCAL_MODE", "1")` patterns; where the app reads `LOCAL_MODE` at import time, reload the module or set the module-level constant directly (`app.LOCAL_MODE = True`) within the test, matching how `conftest` pokes `app.limiter.enabled`/`app.load_dotenv`.

Cover:

1. **`agent_team/workspace.py`** (pure unit, no Flask):
   - A path inside an approved root passes and returns its realpath.
   - A `..` traversal that escapes a root is rejected.
   - A symlink inside a root pointing OUTSIDE all roots is rejected (create a real tmp symlink with `tmp_path`).
   - Empty/None roots ⇒ everything rejected (default-deny).
   - A non-existent path is rejected.
2. **Port picker** (`agent_team/runner`): returns an int in the ephemeral range and the port is actually bindable afterward.
3. **Supervisor import is side-effect-free**: `import agent_team.runner` starts no server and no tray (assert by patching `waitress.serve`/`pystray.Icon` and asserting they were NOT called on import).
4. **Tray menu construction** (`tray.build_icon`): patch `pystray.Icon`/`pystray.Menu`/`pystray.MenuItem` (or use the real classes without `.run()`), assert the five items exist with the expected labels and that selecting **Quit** invokes the quit callback and **Pause** toggles the shared flag. Never call `.run()`.
5. **LOCAL_MODE founder lockdown**: with `LOCAL_MODE` on, `_is_admin(any_customer)` is `False`; a founder-only route (e.g. `GET /api/models` or `POST /api/agents/model`) returns 403/404 even for an `$ADMIN_EMAILS` account.
6. **LOCAL_MODE strips payment/scheduler routes**: `POST /api/pay/create`, `GET /api/orders`, `GET /api/plans`, `GET/POST /api/scheduled-jobs` return 404 under LOCAL_MODE; and confirm they still work (non-404) when LOCAL_MODE is off (sanity, using the existing fixtures).
7. **CSRF/origin hardening**: with LOCAL_MODE on, a mutating `POST /api/*` WITHOUT the `X-Tangent-Local-Token` header → 403; WITH the correct token → passes the hook (reaches the handler / auth). A cross-origin `Origin` header → 403. A `GET` request needs no token. The token is present in the served `index.html` (assert the placeholder is substituted).
8. **`/api/run/local`**:
   - 404 when LOCAL_MODE is OFF (route absent in server mode).
   - 401 when no session customer (LOCAL_MODE on, not logged in).
   - 400 when a path escapes the workspace (set an approved root via `set_roots`, pass a path outside it).
   - 400 when a path looks executable (write a tmp file with an executable magic header) or exceeds the size cap.
   - Happy path: an approved CSV inside an approved root, a logged-in customer (reuse the suite's account/login fixtures), the daemon NOT paused → returns a `job_id`; assert `parse_file` was reached / `metrics_store.create_run` was called (patch `_run_team` to avoid spawning a real LLM thread). Assert the user's original file is NOT modified/encrypted on disk.
   - 503 when the daemon is paused.

**The full existing suite must still pass with zero changes to its behavior** (run `python -m pytest -q` and confirm no regressions; LOCAL_MODE defaults off so server tests are untouched). Add the new suite to whatever the repo uses to run "all tests."

Also provide a manual smoke step in your final notes (not a test): `tangent-agent` (or `python -m agent_team.runner`) boots, prints the chosen `http://127.0.0.1:<port>` URL, the tray icon appears, and the browser opens the SPA. Gate any GUI-requiring code so it degrades gracefully on a headless box (print the URL and skip the tray if no display).

---

## Explicitly OUT OF SCOPE (other workstreams — do NOT build here)

- **Device pairing / token exchange** (`POST /api/devices/pair-code`, `/api/devices/exchange`, `/api/devices`, `/api/devices/<id>/revoke`, `_current_device()`, OS-keychain storage via `keyring`). The `devices` table and migration `0007` belong to that workstream. Wire the tray **Pair** item as a stub that opens a placeholder route only.
- **The `RemoteRouter`/cloud `/v1` proxy for inference** and the local tool-execution loop. Do NOT modify `/v1/chat/completions`, `providers.py`, or `deliverables.run_tool`.
- **Agent catalog sync** (`agent_catalog`/`agent_install` tables, `/api/agents/catalog`, `/api/agents/<id>/publish|install`, manifest whitelist serialization).
- **Run/telemetry sync** (`/api/devices/run/start|finish`, `runs.origin`/`runs.device_id` columns, `kind='device_run'`, `agent_run` table). NO new DB tables or migration `0007` in this workstream.
- **Watchdog folder-watching**: the actual filesystem watcher and the UI to add/remove watched roots. Expose `workspace.set_roots/add_root` for that workstream to call, and a stub tray item — but do not implement the watcher.
- **PyInstaller packaging / code-signing / Tauri shell / auto-update.** Just the `pyproject.toml` console script + `pip install` path.
- **The xlsx writer builder** (`create_excel_workbook`) — already exists in `deliverables.py` (`_build_xlsx`, line ~333); no work needed.
- **Local-model/Ollama offline mode** (`PROVIDERS['ollama']`, `LOCAL_MODEL_MODE`).

Keep the diff tight: one new `pyproject.toml`, one new `agent_team/runner/` package (`__init__.py` + `tray.py`), one new `agent_team/workspace.py`, additive edits to `app.py` (LOCAL_MODE constant, `_is_admin` guard, two/three `before_request` hooks, the `/api/run/local` route, the `_spa_shell` token injection), `requirements.txt` additions, and one new test file. Do not refactor unrelated code.
<!-- WS09 · Phase 3 · depends_on: WS3, WS4 -->
# WS09 — Run telemetry sync (shared run lifecycle)

> **Phase 3** · **Depends on:** WS3, WS4 · **Key files:** agent_team/models.py, agent_team/metrics_store.py, app.py, migrations/versions/0007_phase11_devices.py

---


## Objective

A desktop "Tangent Agent" daemon runs agent jobs locally and must report a run SUMMARY (no file bytes) back to the cloud so that local runs and cloud runs land in ONE `runs` table and ONE 历史 list, badged `origin='device'`. Your job is the cloud-side telemetry surface for that:

1. Add a new `AgentRun` model (hard FK to `runs.id`) plus two new `runs` columns (`origin`, `device_id`), all in a new Alembic migration `0007_phase11_devices.py`.
2. Add a `'device_run'` value to `RUN_KINDS`.
3. Add two device-authenticated endpoints: `POST /api/devices/run/start` and `POST /api/devices/run/<run_id>/finish`.
4. Teach the existing quota/credit machinery and the default 历史报告 filter to EXCLUDE `kind='device_run'` exactly as they already exclude `kind='import'`, so a local run NEVER burns a cloud ¥29 report credit and NEVER tries to render a cloud PDF.
5. Make the 历史 UI safely handle device rows (no "download PDF" affordance for them).

This repo lives at `/Users/brucetang/Company_Agent_Demo`. It uses pytest with per-phase suites in `tests/`, SQLAlchemy classic-declarative models, and Alembic for Postgres (SQLite dev uses `create_all` + a column shim).

---

## Read these first (do not skip — they define the exact patterns you must mirror)

- `agent_team/models.py`:
  - `RUN_STATUSES`, `RUN_KIND_REPORT`, `RUN_KIND_IMPORT`, `RUN_KINDS` (lines ~68-77) — the constants you extend.
  - `class Run` (lines ~194-222) — note `kind = Column(String(32), …)` (NOT a named Enum, so NO Postgres `ALTER TYPE` is needed), `credit_funded`, `total_cost_yuan = Column(Float)`, `report_path = Column(String(255), nullable=True)`.
  - `class Account`, `class AuditLog`, `class OnboardingEvent` — the Phase-8/9/10 LOOSE `account_id = Column(String(32), index=True)` convention (a plain indexed string ref to `accounts.id`, NOT a SQL ForeignKey). Follow this for `agent_run.account_id` and `agent_run.device_id`.
- `agent_team/metrics_store.py`:
  - `create_run(company_id, goal_text, kind=RUN_KIND_REPORT)` (lines ~188-203) — opens a `runs` row; coerces unknown `kind` back to `report`. You must NOT break this; you ADD a device-run path.
  - `finish_run(run_id, status, total_cost_yuan=None, report_path=None)` (lines ~206-232) — sets status/finished_at/cost/report_path, then best-effort calls `consume_report_credit_if_over_quota(run_id)`.
  - `consume_report_credit_if_over_quota(run_id)` (lines ~1801-1853) — note the guard `if (run is None or run.kind != RUN_KIND_REPORT or run.status == RUN_FAILED or run.credit_funded): return False`. Because it only acts on `kind == RUN_KIND_REPORT`, a `device_run` is ALREADY excluded by this guard — you must VERIFY this and add a test asserting it, but no code change is required here. Do not weaken this guard.
  - `count_runs_this_month` / `count_runs_total` (lines ~2018-2049) — the quota meters. They already filter `Run.kind == RUN_KIND_REPORT`, so `device_run` is excluded automatically. VERIFY and test; no change needed.
  - `list_runs(company_id, limit=50, kind=RUN_KIND_REPORT)` (lines ~1033-1046) — defaults to `kind="report"`, so device runs are ALREADY hidden from the default 历史报告 filter, exactly like import runs. VERIFY and test; no change needed. (`kind=None` lists everything.)
  - `_run_to_dict(r)` (lines ~1010-1021) — the plain-dict shape returned for a run. You will ADD `origin` (and you may add `device_id`) here so the 历史/reports layer can see provenance.
- `app.py`:
  - `_current_customer()` (lines ~1486-1500) and `_brain_company()` (lines ~2157-2170) — the session-auth + company-resolution helpers. WS3 adds a parallel `_current_device()` for Bearer `tng-dev-…` device tokens; your two new endpoints START with `dev = _current_device(); if not dev: return jsonify({"error": "..."}), 401`.
  - `list_reports()` / `/api/reports` (lines ~2849-2872) — already pops `report_path`, strips `total_cost_yuan`, and computes `has_content` / `has_pdf` / `download_url`. A device run has `report_path=NULL`, so it will naturally get `has_content=False`, `has_pdf=False`, `download_url=None`. You must make sure the row still carries `origin` so the badge logic can distinguish a device run from a history-import run.
  - `report_detail()` / `/api/reports/<id>` (lines ~2875-2913) — degrades gracefully (404 with a Chinese note) when there is no job snapshot. A device run hits this path; confirm it does not 500.
  - Existing `create_run`/`finish_run` call sites (search `metrics_store.create_run` / `metrics_store.finish_run`) — e.g. the historical-import path near line 4263 stamps `kind=RUN_KIND_IMPORT`. Use these as the template for shape, NOT for behavior.
- `agent_team/db.py`:
  - `_ensure_sqlite_columns(engine)` and `_sqlite_add_column_ddl(col, engine)` (lines ~129-177) — the SQLite-only additive shim. New models registered on `Base.metadata` get `create_all`'d; new columns on existing tables get auto-added here. This is why the 0007 migration's guards make it a no-op on SQLite (tests run on SQLite).
- Migrations (read all three, then copy the structure of 0006):
  - `migrations/versions/0006_phase7_single_report.py` — the head revision. Copy its `_inspector()` / `_column_missing()` inspector-guarded helpers and its idempotent `add_column` pattern. `0007.down_revision = "0006_phase7_single_report"`.
  - `migrations/versions/0005_phase6_run_kind.py` — adds `runs.kind` as `sa.String(length=32)` with `server_default="report"`; this is your exact precedent for adding `runs.origin` as a String with a server_default (NO `ALTER TYPE`).
  - Look at any prior migration that does `op.create_table` + `op.create_index` for a brand-new table (e.g. `0002_phase8_security.py` / `0003_phase9_payments.py`) to copy the `if not _has_table(...)` create-table-behind-a-guard pattern and the per-index creation.
- Frontend: `frontend/js/views/reports.js` — `typeBadge(r)` (lines ~13-19) currently decides 周报/月报/自定义/历史导入 from `has_content`. You will teach it to badge a device run (`r.origin === "device"`) as a local/desktop run and ensure `rowHTML` never renders a PDF link for it (it already only renders `download_url` when present, which will be null for device runs — but make the intent explicit and the badge correct).
- Tests: read `tests/conftest.py` (it disables the rate limiter and neutralizes `load_dotenv` for hermeticism) and skim `tests/test_redesign_phase7.py` + `tests/test_phase9_payments.py` to copy how a test opens runs, drives the quota/credit path, and uses the Flask test client / DB session.

---

## Dependencies (WS3, WS4) — integrate, do not reimplement

This workstream is Phase 3 and DEPENDS ON:
- **WS3** delivers the `devices` table + `DeviceStore`/`verify_device_token` + `app._current_device()` (reads `Authorization: Bearer tng-dev-…`, looks up by `sha256`, requires `status=='active'`, bumps `last_seen_at`, returns a device record carrying `account_id`). Your endpoints call `_current_device()`.
- **WS4** delivers the `agent_catalog` + `agent_install` tables and the catalog-sync endpoints. The `agent_run` row references `agent_catalog.id` (hard FK) and `agent_install.id`.

If, when you start, `_current_device()` / `DeviceStore` / the `devices`, `agent_catalog`, `agent_install` tables do NOT yet exist in the codebase (grep `agent_team/` + `app.py` for `_current_device`, `verify_device_token`, `agent_catalog`), then WS3/WS4 have not landed. In that case:
- STILL implement everything in THIS workstream's scope that does not require them (the `AgentRun` model, the `runs.origin`/`runs.device_id` columns, the `RUN_KINDS` change, the migration, the metrics_store verification + tests, the frontend badge).
- For the two endpoints, write them against the EXPECTED WS3/WS4 surface described in this prompt (`dev = _current_device()`, `dev["account_id"]`, FK to `agent_catalog.id`). Guard the device-auth import so a missing `_current_device` raises a clear `NotImplementedError`/501 rather than a 500, and add a module-level comment `# DEPENDS ON WS3 _current_device / WS4 agent_catalog`.
- Make the `agent_run.agent_catalog_id` FK NULLABLE so a row can be written even before WS4 populates a catalog (the manifest_hash string is the durable record either way).
Do NOT create the `devices`/`agent_catalog`/`agent_install` tables yourself — those are WS3/WS4. You ONLY create `agent_run` and the two `runs` columns. Note your 0007 migration must still `if not _has_table` -guard those three sibling tables IF WS3/WS4 chose to put them in 0007 too; coordinate by making your create-blocks for `agent_run` independent and additive so the migrations compose. The safest approach: your 0007 migration creates ONLY `agent_run` + the two `runs` columns, and assumes WS3/WS4 created `devices`/`agent_catalog`/`agent_install` in the SAME 0007 file (since the data-model spec says all four tables ship in `0007_phase11_devices.py`). Therefore: **if a `0007_phase11_devices.py` already exists when you start (WS3/WS4 created it), ADD your `agent_run` create-block and `runs` column adds INTO that existing file behind their own `if not _has_table`/`if _column_missing` guards; do NOT create a second 0007 file.** If it does not exist, create it and define the `agent_run` + `runs` parts; WS3/WS4 will merge their tables into the same file.

---

## Implementation spec

### 1. `agent_team/models.py`

**Extend RUN_KINDS:**
```python
RUN_KIND_REPORT = "report"
RUN_KIND_IMPORT = "import"
RUN_KIND_DEVICE = "device_run"   # NEW — a local desktop daemon run, summary-only
RUN_KINDS = (RUN_KIND_REPORT, RUN_KIND_IMPORT, RUN_KIND_DEVICE)
```
Add a one-line comment matching the existing style explaining that `device_run` is summary-only telemetry from a paired desktop device: excluded from the quota meter, the ¥29 credit consume, and the default 历史报告 filter, exactly like `import`.

**Add an agent-run status tuple** near the other status constants:
```python
AGENT_RUN_RUNNING = "running"
AGENT_RUN_SUCCEEDED = "succeeded"
AGENT_RUN_FAILED = "failed"
AGENT_RUN_STOPPED = "stopped"
AGENT_RUN_STATUSES = (AGENT_RUN_RUNNING, AGENT_RUN_SUCCEEDED, AGENT_RUN_FAILED, AGENT_RUN_STOPPED)
```

**Add two columns to `class Run`** (after `report_path`):
```python
# Phase 11 (desktop): provenance of this run. 'cloud' (default) for web/scheduled
# runs; 'device' for a summary synced from a paired desktop daemon (kind='device_run').
origin = Column(String(16), nullable=False, default="cloud", server_default="cloud")
# The dev_… id of the desktop device that synced this run (loose ref devices.id), or NULL.
device_id = Column(String(32), nullable=True, index=True)
```

**Add a new `AgentRun` model** (place it after `class Run` or near the other Phase-11 tables). Match the data-model spec exactly:
```python
class AgentRun(Base):
    """One desktop (device) agent job, sharing the runs lifecycle (HARD FK to
    runs.id). The cloud stores a SUMMARY only — goal/status/¥ cost/a report
    sha256 + byte count — NEVER the file bytes. cost_yuan is the user's OWN
    metered /v1 spend (already billed at call time); it is NOT routed through
    record_platform_spend or the breaker."""
    __tablename__ = "agent_run"

    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)  # shares the run lifecycle
    device_id = Column(String(32), index=True)            # loose ref devices.id
    account_id = Column(String(32), index=True)           # loose ref accounts.id
    agent_catalog_id = Column(Integer, ForeignKey("agent_catalog.id"), nullable=True, index=True)
    agent_id = Column(String(64), index=True)             # config.AGENTS id
    manifest_hash = Column(String(71), nullable=True)     # 'sha256:'+64
    status = Column(Enum(*AGENT_RUN_STATUSES, name="agent_run_status"),
                    default=AGENT_RUN_RUNNING, nullable=False)
    cost_yuan = Column(Numeric(12, 4), nullable=False, default=0)  # SUMMARY only
    report_summary = Column(Text, nullable=True)          # text only, never bytes
    report_sha256 = Column(String(64), nullable=True)
    report_bytes = Column(Integer, nullable=True)
    deliverable_kind = Column(String(32), nullable=True)  # 'xlsx'|'pdf'|'docx'|'pptx'|'svg'|'text'
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
```
Notes:
- `agent_catalog_id` FK is nullable (see WS4 dependency note). If WS4's `AgentCatalog` table is not importable at model-registration time the `ForeignKey("agent_catalog.id")` string is fine — SQLAlchemy resolves it lazily, but the referenced table must exist in the same `Base.metadata` for `create_all`. If WS4 has not landed and `agent_catalog` is absent, make this column a plain `Column(Integer, index=True)` (loose ref) instead of a hard FK, with a `# TODO promote to FK once WS4 lands` comment, so `create_all` does not fail on SQLite.
- Update the module docstring's table list to mention `agent_run`, `devices`, `agent_catalog`, `agent_install` under a "Phase 11 (desktop)" heading (one line each).

### 2. `agent_team/metrics_store.py`

- Import `RUN_KIND_DEVICE` (and `AgentRun`, `AGENT_RUN_STATUSES`, the agent-run status constants) at the top alongside the existing model imports.
- **`create_run`**: it already accepts a `kind` and validates it against `RUN_KINDS`. Since `device_run` is now in `RUN_KINDS`, `create_run(company_id, goal_text, kind='device_run')` already works. You additionally need to stamp `origin='device'` and `device_id` on the row. Add an OPTIONAL keyword path WITHOUT breaking existing callers. Add a dedicated helper rather than overloading `create_run` with many args:
  ```python
  def create_device_run(company_id, goal_text, device_id):
      """Open a runs row for a desktop (device) run: kind='device_run',
      origin='device', device_id stamped. Returns the run_id, or None."""
      if not company_id:
          return None
      with db.session_scope() as s:
          run = Run(company_id=company_id, goal_text=goal_text or "",
                    status=RUN_RUNNING, kind=RUN_KIND_DEVICE,
                    origin="device", device_id=str(device_id)[:32] if device_id else None)
          s.add(run)
          s.flush()
          return run.id
  ```
  (Import `RUN_RUNNING` if not already imported.)
- **`_run_to_dict`**: add `"origin": r.origin or "cloud"` and `"device_id": r.device_id or ""` so `get_run` / `list_runs` consumers can see provenance.
- **`finish_run`**: NO behavior change needed for device runs IF you pass `report_path=None` — it already leaves `report_path` NULL and `consume_report_credit_if_over_quota` already no-ops for non-`report` kinds. VERIFY by reading the guard at line ~1821. Do NOT add device-specific branches here; the existing guards are the design. (Optionally add a one-line comment at the `consume_report_credit_if_over_quota` call noting device_run/import are both excluded by the kind guard.)
- **Add AgentRun CRUD helpers** (plain values in/out, never live ORM objects, mirroring the rest of this module):
  ```python
  def create_agent_run(run_id, account_id, device_id, agent_id,
                       agent_catalog_id=None, manifest_hash=None):
      """INSERT an agent_run row (status='running') tied to an open runs.id.
      Returns the agent_run id, or None."""
  def finish_agent_run(agent_run_id, status, cost_yuan=None, report_summary=None,
                       report_sha256=None, report_bytes=None, deliverable_kind=None):
      """Close out the agent_run summary. Coerces an unknown status to 'failed'.
      Returns True if a row was updated."""
  def get_agent_run(agent_run_id):  # plain dict or None, for tests/ownership checks
  ```
  - `finish_agent_run` validates `status in AGENT_RUN_STATUSES` (else `AGENT_RUN_FAILED`), sets `finished_at=utcnow()`, and coerces `cost_yuan` via the existing `_coerce_float` helper into the `Numeric(12,4)` column. `report_summary` is text only — truncate defensively (e.g. to a sane cap like 8000 chars) so a runaway client can't bloat the row. `report_sha256` to 64 chars, `deliverable_kind` to 32.
- **`count_runs_this_month` / `count_runs_total`**: already filter `Run.kind == RUN_KIND_REPORT` — device runs are excluded automatically. NO change. (Your tests assert this.)
- **`list_runs`**: default `kind=RUN_KIND_REPORT` already hides device runs from 历史报告. NO change.

### 3. `app.py` — the two device-auth endpoints

Place them near the other run/report routes. Both start with the device-auth guard.

**`POST /api/devices/run/start`**
- Body JSON: `{agent_id, goal_text, manifest_hash, agent_install_id}` (all optional except `agent_id`; tolerate missing/empty like other handlers).
- `dev = _current_device(); if not dev: return jsonify({"error": "设备未授权。"}), 401`.
- Resolve the company the SAME way a web run does: `company_id = metrics_store.get_or_create_company(<account email for dev["account_id"]>)`. The device inherits `account_id`; resolve the account's email via the account store (look at how `consume_report_credit_if_over_quota` goes account_id→email and reverse it, or add a small `metrics_store.company_id_for_account(account_id)` helper if cleaner — check whether one already exists before adding). If you cannot resolve a company, return 400 with a Chinese message.
- `run_id = metrics_store.create_device_run(company_id, goal_text, device_id=dev["id"])`.
- Resolve `agent_catalog_id` from `agent_install_id` if WS4 helpers exist; otherwise pass `None`.
- `agent_run_id = metrics_store.create_agent_run(run_id, account_id=dev["account_id"], device_id=dev["id"], agent_id=agent_id, agent_catalog_id=..., manifest_hash=manifest_hash)`.
- Return `jsonify({"run_id": run_id, "agent_run_id": agent_run_id})`.

**`POST /api/devices/run/<int:run_id>/finish`**
- Body JSON: `{status, cost_yuan, report_summary, report_sha256, report_bytes, deliverable_kind}`.
- `dev = _current_device(); if not dev: return 401`.
- OWNERSHIP CHECK (critical): load the run (`metrics_store.get_run(run_id)`), confirm it exists, that `run["origin"] == "device"` AND it belongs to this device's account/company (compare `run["device_id"]` to `dev["id"]`, or the run's company to the device's account's company). A device must only finish ITS OWN runs → else 403/404. This is the security boundary; mirror the cross-account 403 discipline in `report_detail`.
- `metrics_store.finish_run(run_id, status, total_cost_yuan=cost_yuan, report_path=None)` — `report_path` STAYS NULL (no cloud PDF render). `cost_yuan` is a SUMMARY recorded on the run; it is NOT passed to `record_platform_spend` and NOT fed to the breaker (the user already paid their own metered /v1 calls).
- Update the matching `agent_run` row: look it up by `run_id` (add `metrics_store.finish_agent_run_for_run(run_id, …)` or find the `agent_run` id first) and call `finish_agent_run(...)` with the summary fields.
- Map the run status: the daemon sends an agent-run status (`succeeded`/`failed`/`stopped`/`running`); translate to the `runs.status` vocabulary (`RUN_SUCCEEDED`/`RUN_FAILED`/`RUN_STOPPED_BUDGET`/`running`) sensibly — e.g. `succeeded→succeeded`, `failed→failed`, `stopped→stopped_budget` (or keep `failed`), and pass the agent-run vocabulary unchanged to `finish_agent_run`.
- Return `jsonify({"ok": True, "run_id": run_id})`.

(Optional, low priority, do NOT build unless trivial: `POST /api/devices/run/<run_id>/event` for a richer live feed. Out of scope by default.)

### 4. `migrations/versions/0007_phase11_devices.py`

- `revision = "0007_phase11_devices"`, `down_revision = "0006_phase7_single_report"`.
- Copy the inspector-guarded helper trio from 0006: `_inspector()`, `_column_missing(insp, table, column)`, and ADD `_has_table(insp, name)` and `_has_column(insp, table, column)` (see 0005 for `_has_column`).
- `upgrade()`:
  - `if not _has_table(insp, "agent_run"):` → `op.create_table("agent_run", … )` with every column from the model, then `op.create_index` for each `index=True` column (`run_id`, `device_id`, `account_id`, `agent_catalog_id`, `agent_id`). Use the hard FK to `runs.id` (and to `agent_catalog.id` if WS4's table is part of this same migration; if you made `agent_catalog_id` a loose ref in the model because WS4 hasn't landed, mirror that here as a plain Integer column + index, no FK constraint).
  - `if _column_missing(insp, "runs", "origin"):` → `op.add_column("runs", sa.Column("origin", sa.String(length=16), nullable=False, server_default="cloud"))`.
  - `if _column_missing(insp, "runs", "device_id"):` → `op.add_column("runs", sa.Column("device_id", sa.String(length=32), nullable=True))` then `op.create_index("ix_runs_device_id", "runs", ["device_id"])` (guard the index creation too).
  - NO `ALTER TYPE` anywhere — `runs.kind` is `String(32)` so `'device_run'` needs no enum change. The `agent_run_status` enum is a brand-new table's column, created by `create_table`; on SQLite it renders as VARCHAR, on Postgres `create_table` creates the enum type — that's fine.
- `downgrade()`: drop the indexes + `agent_run` table behind `_has_table`, and drop `runs.origin` / `runs.device_id` behind `not _column_missing`, mirroring 0006's guarded downgrade.
- Write the module docstring in the same idempotent-by-design voice as 0005/0006: explain that on a fresh DB `create_all` already built `agent_run` and the new `runs` columns so the guards no-op on SQLite, and Postgres gets the real additive DDL.

### 5. `frontend/js/views/reports.js`

- In `typeBadge(r)`: BEFORE the `has_content` branches, add `if (r.origin === "device") return ["本地", "rep-badge-device"];` (or "桌面"/"本地运行" — a clearly-labeled badge for a desktop/local run). Pick Chinese copy consistent with the page's tone; add the `.rep-badge-device` class to the reports CSS (find where `rep-badge-import` etc. are styled and add a sibling).
- In `rowHTML`: a device run has `has_content=false` and `download_url=null`, so it already shows "仅 PDF" / no PDF link — but "仅 PDF" is WRONG for a device run (there is no PDF). Make the non-viewable affordance branch aware of origin: for a device run show a neutral label like "本地结果"/"无云端文件" instead of "仅 PDF", and ensure NO `download_url` link is ever rendered for it (it won't be, since the API returns null — but assert the intent with a comment). The 历史 UI must NOT offer a "download PDF" affordance for an origin='device' / report_path-NULL run.
- Confirm `list_reports()` in `app.py` includes `origin` in each row it returns (the API pops `report_path` and `total_cost_yuan` but should NOT strip `origin`). If `_run_to_dict` now carries `origin`, it flows through automatically — just verify the `r.pop(...)` calls don't remove it.

---

## Invariants you MUST respect

- **China domestic mode**: this telemetry path touches NO model resolution. It must NEVER call `effective_model()`, `provider_for()`, the gateway, or any Anthropic API. It works identically on a fully-domestic cloud with no model keys. Adding these endpoints must not introduce any import that pulls in provider keys at module load.
- **No model keys / no ¥ rates to the device**: these endpoints RECEIVE a summary FROM the device; they return only `{run_id, agent_run_id}` / `{ok}`. NEVER serialize a model id, provider, per-token ¥ rate, or COGS back to the device.
- **¥ cost-control stays server-owned and is NOT touched here**: `cost_yuan` from the device is a SUMMARY of the user's OWN already-metered /v1 spend. Do NOT route it through `record_platform_spend`, the platform breaker, or `platform_guard.check`. Do NOT decrement any wallet. A device run must NEVER consume a ¥29 single-report credit (`consume_report_credit_if_over_quota` already excludes non-`report` kinds — keep it that way) and must NEVER count against the plan quota (`count_runs_*` already exclude non-`report` kinds — keep it that way).
- **No cloud PDF render for device runs**: `finish_run` is called with `report_path=None`; `report_path` stays NULL forever for device runs. The 历史 UI must check `origin`/`report_path` before offering any "download PDF" affordance.
- **Founder-gating untouched**: these are device-auth (`_current_device`) routes, not admin routes. Do not expose any founder machinery.
- **Files-encrypted-at-rest / honesty**: no file bytes are ever transmitted or stored — only `report_summary` (text), `report_sha256`, `report_bytes`. Do not invent or fabricate a downloadable artifact on the cloud side for a device run.
- **Loose `account_id`/`device_id` refs**: follow the Phase 8-10 convention — `String(32)` indexed columns, hard FK ONLY for `agent_run.run_id → runs.id` (and `agent_run.agent_catalog_id → agent_catalog.id` if WS4 has landed).
- **Migration idempotency**: every DDL step behind an inspector guard; `alembic upgrade head` must end at a schema identical to the models on both a fresh and an existing DB; SQLite path stays a no-op via the guards + `_ensure_sqlite_columns`.

---

## Acceptance criteria

1. `RUN_KINDS == ("report", "import", "device_run")` and a new `AgentRun` model is registered on `Base.metadata`; `runs` has `origin` (NOT NULL default 'cloud') and `device_id` (nullable, indexed).
2. `metrics_store.create_device_run(...)` opens a `runs` row with `kind='device_run'`, `origin='device'`, `device_id` stamped, `status='running'`.
3. `metrics_store.finish_run(run_id, 'succeeded', total_cost_yuan=12.3, report_path=None)` on a `device_run`:
   - sets status/cost/finished_at, leaves `report_path` NULL,
   - does NOT consume a report credit (assert `accounts.report_credits` unchanged even when the account is over its base quota),
   - does NOT increment `count_runs_this_month` / `count_runs_total`.
4. `metrics_store.list_runs(company_id)` (default `kind='report'`) does NOT include device runs; `list_runs(company_id, kind=None)` DOES.
5. `POST /api/devices/run/start` with a valid device token returns `{run_id, agent_run_id}` and creates one `runs` row + one `agent_run` row; with NO/invalid token returns 401.
6. `POST /api/devices/run/<id>/finish` updates both rows, leaves `report_path` NULL, does not touch the wallet/breaker, and 403/404s when a device tries to finish a run it does not own.
7. `0007_phase11_devices.py` applies cleanly (`alembic upgrade head` then `downgrade` then `upgrade` again, on a Postgres-shaped run if you have one; at minimum the SQLite `create_all`+shim path works and the migration's guards make it a no-op there). No `ALTER TYPE` for `runs.kind`.
8. The 历史报告 list shows a device run with a "本地"/desktop badge and NO PDF download link; `/api/reports` returns `origin` per row.
9. The full existing suite still passes (`python -m pytest -q`). The current head migration was 0006; new migration is 0007.

### Tests to add (new file `tests/test_phase11_devices.py`, or extend an existing phase suite if WS3/WS4 created one)

Follow `tests/test_redesign_phase7.py` / `tests/test_phase9_payments.py` for fixtures (Flask test client, DB session, account/company setup). Add:
- `test_run_kinds_includes_device_run` — constant check.
- `test_create_device_run_stamps_origin_and_device` — row has `kind='device_run'`, `origin='device'`, `device_id` set.
- `test_device_run_excluded_from_quota_counters` — open base-quota-worth of report runs + a device run; assert `count_runs_this_month`/`count_runs_total` ignore the device run.
- `test_device_run_finish_never_consumes_credit` — account over base quota with `report_credits=1`; finish a device run; assert `report_credits` still 1 and the device run is NOT `credit_funded`.
- `test_device_run_hidden_from_default_history_but_visible_with_kind_none`.
- `test_finish_run_leaves_report_path_null_for_device_run`.
- `test_agent_run_crud` — create/finish/get round-trip with summary fields, `cost_yuan` lands in `Numeric(12,4)`.
- Endpoint tests (if `_current_device` exists from WS3): `start` then `finish` happy path; `401` without token; `403/404` on cross-device finish. If WS3 has NOT landed, write these as `@pytest.mark.skipif`-guarded on the presence of `app._current_device` (or a small fake device monkeypatched in) so the suite stays green and the tests light up once WS3 merges.
- A migration smoke test if the suite has a precedent (search `tests/` for any existing alembic/migration test) — otherwise rely on `create_all` coverage.

---

## Explicitly OUT of scope (do NOT build here)

- The `devices`, `agent_catalog`, `agent_install` tables; `DeviceStore`; `verify_device_token`; `app._current_device()`; pairing/exchange/revoke endpoints — those are **WS3**.
- The agent catalog publish/install/`GET /api/agents/catalog` endpoints and manifest whitelisting — **WS4**.
- Any change to the `/v1/chat/completions` gateway, its auth resolution, RPM/`platform_guard`/`record_usage` billing — a separate workstream.
- The desktop runner itself (`agent_team/runner/`), the local tool-execution loop, `workspace.assert_in_workspace`, the xlsx builder, the Tauri shell, pairing UI — separate workstreams.
- `POST /api/devices/run/<id>/event` (optional/low-priority; skip unless trivial).
- Any model/provider/¥-rate resolution, COGS accounting, or breaker integration for device runs — device cost is a synced summary only.
- Removing or restructuring the existing report-quota/credit logic — you only VERIFY it already excludes `device_run` (which it does, via the `kind == 'report'` guards) and add tests proving it.
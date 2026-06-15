<!-- WS03 · Phase 1 · depends_on: none -->
# WS03 — Device-token model, store, migration

> **Phase 1** · **Depends on:** none · **Key files:** agent_team/models.py, agent_team/devices.py (new), agent_team/customers.py (reference for _hash_key/secret discipline), migrations/versions/0007_phase11_devices.py (new)

---


You are working in the repo at `/Users/brucetang/Company_Agent_Demo` (a Flask + SQLAlchemy multi-agent app, brand "Tangent AI"). This is Phase 1 of a larger "Tangent Agent" desktop-daemon effort. Your job is the FOUNDATIONAL DATA LAYER for per-device tokens: a new `Device` SQLAlchemy model, a new `DeviceTokenStore` accounting module, and the Alembic migration `0007_phase11_devices.py`. NO HTTP endpoints, NO app.py wiring, NO desktop runner — those are later workstreams. Stay strictly inside the files listed under "Files you will create/edit".

## Objective

Add a per-device authentication primitive that mirrors, byte-for-byte, the secret discipline of the existing customer-key system: mint a token ONCE, store ONLY its SHA-256 hash, verify by hash, and resolve to the LIVE account model subset at call time (never a frozen snapshot). Plus the migration that creates the `devices` table (and, per the data-model spec below, the sibling Phase-11 tables and two new `runs` columns) with inspector-guarded, idempotent DDL.

## Read these first (do not skip — they define the patterns you must clone)

1. `agent_team/customers.py` — THE reference for secret discipline. Study in particular:
   - The module-level constants `KEY_PREFIX = "tng-sk-"`, and the helpers `_hash_key(raw_key)` (returns `hashlib.sha256(raw_key.encode("utf-8")).hexdigest()`), `_now()`.
   - `create_customer(...)`: `raw_key = KEY_PREFIX + secrets.token_urlsafe(32)`; account id is `"cus_" + secrets.token_hex(6)`; stores `key_hash=_hash_key(raw_key)`; returns the raw key EXACTLY ONCE.
   - `verify_key(raw_key)`: strips, hashes, looks up by `key_hash`, returns a plain-dict record via `_account_to_record(acc)` (never a live ORM row) — and DELIBERATELY OMITS the hash from that record.
   - `_account_to_record(acc)`: the field-stripping snapshot pattern. Your device record + your public view must omit `token_hash` and `pairing_code_hash` exactly as this omits `key_hash`/`password_hash`.
   - The `self._lock = threading.RLock()` + `with db.session_scope() as s:` discipline used in every mutation.
2. `agent_team/models.py` — the declarative-Base schema. Note: the file header comment block enumerates tables; `Account` (`accounts`, PK `id = Column(String(32))`); Phase 8–10 tables use a LOOSE `account_id String(32)` ref with NO ForeignKey (e.g. `AuditLog.account_id`, `Order.account_id`, `OnboardingEvent.account_id`); the `Run` model (`runs` table); the run-kind constants block: `RUN_KIND_REPORT = "report"`, `RUN_KIND_IMPORT = "import"`, `RUN_KINDS = (...)`; the status-enum pattern `Enum(*ACCOUNT_STATUSES, name="account_status")`.
3. `agent_team/db.py` — `Base`, `session_scope()`, and crucially `_ensure_sqlite_columns()` + `init_db()`: on SQLite (dev/test) the schema is built by `Base.metadata.create_all` + `_ensure_sqlite_columns`; on Postgres the schema is owned by Alembic. This is WHY the migration must be inspector-guarded and a no-op on SQLite.
4. `migrations/versions/0006_phase7_single_report.py` — the CURRENT head. Your `down_revision` is `"0006_phase7_single_report"`. Copy its `_inspector()` and `_column_missing(insp, table, column)` helpers.
5. `migrations/versions/0002_phase8_security.py` and `0003_phase9_payments.py` — the canonical full-table-creation migrations. Clone their `_has_table(insp, name)` / `_has_column(insp, table, column)` helpers and the `if not _has_table(...): op.create_table(...) ; op.create_index(...)` shape, including how they import enum tuples from `agent_team.models` and how `UniqueConstraint` is declared inline in `create_table`.
6. `tests/test_redesign_phase6.py` (the `test_migration_0005_*` function near line 121) — THE pattern for testing a migration: build a pre-revision SQLite schema with raw `sqlite3`, stamp `alembic_version`, then `subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env={**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"})`, then inspect with `PRAGMA table_info`.
7. `tests/test_phase7_concurrency.py` and `tests/conftest.py` — the test harness conventions: `ROOT`/`sys.path` insertion, the `db.init_db(str(tmp_path / "x.db"))` fixture for an isolated SQLite DB, and how a store is instantiated directly in a test.

## Files you will create/edit

### A. `agent_team/models.py` (edit)

1. Add the `Device` model. Place it with the other operational tables (after `Account`). Columns EXACTLY:
   - `__tablename__ = "devices"`
   - `id = Column(String(32), primary_key=True)` — generated as `"dev_" + secrets.token_hex(6)` by the store (NOT in the model).
   - `account_id = Column(String(32), nullable=False, index=True)` — LOOSE ref to `accounts.id`, NO ForeignKey (Phase-8-10 convention; match `AuditLog.account_id`).
   - `name = Column(String(128))`
   - `platform = Column(String(32))` — free-form `'darwin'|'win'|'linux'` (do NOT make it an Enum).
   - `token_hash = Column(String(64), nullable=True, index=True)` — SHA-256 hex of the device token; NULL until the pairing code is exchanged.
   - `pairing_code_hash = Column(String(64), nullable=True)` — SHA-256 hex of the one-time pairing code.
   - `pairing_expires_at = Column(DateTime, nullable=True)`
   - `status = Column(Enum(*DEVICE_STATUSES, name="device_status"), default=DEVICE_PENDING, nullable=False)`
   - `last_seen_at = Column(DateTime, nullable=True)`
   - `created_at = Column(DateTime, default=datetime.utcnow, nullable=False)`
2. Add the status constants block near the other `*_STATUSES` blocks:
   ```python
   # Phase 11 — a paired device's lifecycle (the desktop "Tangent Agent" daemon).
   DEVICE_PENDING = "pending"    # pairing code minted, token not yet exchanged
   DEVICE_ACTIVE = "active"      # token live; verify_token resolves it
   DEVICE_REVOKED = "revoked"    # kill switch — verify fails closed forever after
   DEVICE_STATUSES = (DEVICE_PENDING, DEVICE_ACTIVE, DEVICE_REVOKED)
   ```
3. Extend the run-kind block so device runs are recognised (the cloud-sync workstreams depend on this constant existing):
   ```python
   RUN_KIND_DEVICE = "device_run"   # a run executed by a paired desktop device
   RUN_KINDS = (RUN_KIND_REPORT, RUN_KIND_IMPORT, RUN_KIND_DEVICE)
   ```
   `runs.kind` is already `Column(String(32))` (NOT a named Enum), so adding this constant requires NO `ALTER TYPE` and no schema change to the kind column. Do NOT change any quota/list_runs logic here — that is a later workstream; only add the constant.
4. On the existing `Run` model, add two columns so device runs land in the shared `runs` table:
   - `origin = Column(String(16), nullable=False, default="cloud", server_default="cloud")`
   - `device_id = Column(String(32), nullable=True, index=True)` — loose ref to `devices.id`.
   Place them after `report_path`. Match the `kind`/`credit_funded` style already on `Run`.
5. (Required by the migration's `agent_run` FK and the data-model spec) Add three more Phase-11 models so the migration and the model metadata agree. These tables are NOT exercised by your store but MUST exist as models so SQLite `create_all` and the Postgres migration produce identical schemas:
   - `AgentCatalog` (`agent_catalog`): `id Integer PK`; `agent_id String(64) index`; `version Integer default 1`; `name_cn String(128)`; `manifest_json Text` (RUNNER-VISIBLE half only — comment this); `content_hash String(71) index` (`'sha256:'+64`); `published Boolean default True server_default "1"`; `created_at DateTime default utcnow`; `__table_args__ = (UniqueConstraint("agent_id", "version", name="uq_agent_catalog_agent_version"),)`.
   - `AgentInstall` (`agent_install`): `id Integer PK`; `account_id String(32) index` (loose ref); `agent_id String(64) index`; `pinned_version Integer nullable` (null = always latest published); `active Boolean default True server_default "1"`; `created_at DateTime default utcnow`; `__table_args__ = (UniqueConstraint("account_id", "agent_id", name="uq_agent_install_account_agent"),)`.
   - `AgentRun` (`agent_run`): `id Integer PK`; `run_id = Column(Integer, ForeignKey("runs.id"), index=True)` (HARD FK — shares the run lifecycle); `device_id String(32) index` (loose ref); `account_id String(32) index`; `agent_catalog_id = Column(Integer, ForeignKey("agent_catalog.id"), index=True)`; `agent_id String(64) index`; `manifest_hash String(71)`; `status = Column(Enum(*AGENT_RUN_STATUSES, name="agent_run_status"), default=AGENT_RUN_RUNNING, nullable=False)`; `cost_yuan = Column(Numeric(12, 4), nullable=False, default=0)` (SUMMARY of the user's own metered spend — NOT platform COGS); `report_summary Text` (text only, never bytes); `report_sha256 String(64)`; `report_bytes Integer`; `deliverable_kind String(32)`; `started_at DateTime default utcnow`; `finished_at DateTime nullable`.
   Add the matching status block:
   ```python
   # Phase 11 — the per-device agent_run lifecycle (shares runs.id via FK).
   AGENT_RUN_RUNNING = "running"
   AGENT_RUN_SUCCEEDED = "succeeded"
   AGENT_RUN_FAILED = "failed"
   AGENT_RUN_STOPPED = "stopped"
   AGENT_RUN_STATUSES = (AGENT_RUN_RUNNING, AGENT_RUN_SUCCEEDED,
                         AGENT_RUN_FAILED, AGENT_RUN_STOPPED)
   ```
   Update the module docstring's table list to mention the Phase-11 tables (devices / agent_catalog / agent_install / agent_run) for consistency with the existing header.

### B. `agent_team/devices.py` (NEW — clone `customers.py`'s structure and docstring tone)

A thread-safe, LLM-free accounting module. Top-of-file docstring must state the secret discipline (mint once, store sha256 only, verify by hash, revoke = fail closed) in the same register as `customers.py`. Constants:
```python
DEVICE_TOKEN_PREFIX = "tng-dev-"   # device tokens look like tng-dev-<43 url-safe chars>
_PAIRING_TTL_SECS = 600            # pairing code lives 10 minutes
```
Reuse the SAME `_hash_key`/`_now` helpers (copy them locally as private module functions — do NOT import customers.py's private functions; keep the module self-contained, exactly as customers.py keeps its own).

Class `DeviceTokenStore`:
- `__init__(self)`: `self._lock = threading.RLock()`.
- `create_pairing_code(self, account_id, name=None, platform=None) -> dict`: mints a 6–8 char human-friendly pairing code (use `secrets.token_urlsafe(...)` or a digit/letter code — your choice, but DOCUMENT it) AND a `device_id = "dev_" + secrets.token_hex(6)`. INSERTs a `Device` row with `status=DEVICE_PENDING`, `pairing_code_hash=_hash_key(code)`, `pairing_expires_at = utcnow + _PAIRING_TTL_SECS`, `name`, `platform`, and `account_id`. Returns `{"pairing_code": <raw code, shown ONCE>, "device_id": device_id, "account_id": account_id, "expires_at": <iso>}`. The raw pairing code is returned ONCE and never persisted in plaintext (only its hash).
- `exchange_pairing_code(self, raw_code, device_name=None, platform=None) -> dict | None`: under the lock + a `with_for_update()` row lock, find the `Device` row whose `pairing_code_hash == _hash_key(raw_code)` AND `status == DEVICE_PENDING` AND `pairing_expires_at` is in the FUTURE (`> utcnow`). If none → return `None` (expired/replayed/unknown all fail the same way). On a hit: mint `raw_token = DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)`; set `token_hash = _hash_key(raw_token)`; flip `status = DEVICE_ACTIVE`; BURN the pairing material (`pairing_code_hash = None`, `pairing_expires_at = None`) so a replayed code cannot mint a SECOND token; optionally overwrite `name`/`platform` from the exchange payload if provided. Return `{"device_token": raw_token, "device_id": <id>, "account_id": <account_id>}` ONCE.
- `verify_token(self, raw_token) -> dict | None`: THE hot path. Strip, `_hash_key`, look up the `Device` by `token_hash` with `status == DEVICE_ACTIVE`. If found, bump `last_seen_at = utcnow` (best-effort; a write here is fine — keep it inside the same session). Return a plain-dict record (see "record shape" below) that RESOLVES the LIVE account at call time: do NOT freeze the account's model subset into the record. Concretely, the record carries `account_id` and the device fields; the CALLER (a later workstream) intersects against the account's live models — but document explicitly in the docstring that `verify_token` must never embed a snapshot of `account.models`; it returns the device identity + `account_id` and the live account/model resolution happens downstream at request time. Return `None` on miss, on a revoked device, or on a pending (un-exchanged) device.
- `list_for_account(self, account_id) -> list[dict]`: return field-stripped public views (NEVER `token_hash`, `pairing_code_hash`, the raw token, ¥, or any model id) for every device of this account, newest first. Mirror `_account_view` field-stripping discipline — include only `id`, `name`, `platform`, `status`, `last_seen_at` (iso or None), `created_at` (iso).
- `revoke(self, device_id, account_id=None) -> dict | None`: under the lock, set `status = DEVICE_REVOKED`. If `account_id` is passed, scope the update to `Device.account_id == account_id` so one account can never revoke another's device (return `None` if no row matched the scoped query). After this, `verify_token` fails closed for that device forever (instant kill switch; no key rotation needed). Return the updated public view, or `None` if unknown.
- Private helpers: `_device_to_record(dev)` (the verify-path record; OMITS `token_hash` and `pairing_code_hash` exactly as `_account_to_record` omits `key_hash`) and `_device_public_view(dev)` (the list/revoke view; the stricter field-stripped subset above). Both must return plain dicts, never live ORM rows, so they are safe to hand across background threads (same rationale as customers.py).

Record shape returned by `verify_token`/`_device_to_record` (plain dict): `{"id", "account_id", "name", "platform", "status", "last_seen_at", "created_at"}` — NO secrets.

### C. `migrations/versions/0007_phase11_devices.py` (NEW)

Header docstring in the same style as 0002/0006: explain it is idempotent-by-design (fresh SQLite DBs already have these objects from `create_all`+`_ensure_sqlite_columns`, so the guards make it a no-op there; a pre-Phase-11 Postgres gets the real additive DDL; zero drift either way). Revision identifiers:
```python
revision = "0007_phase11_devices"
down_revision = "0006_phase7_single_report"
branch_labels = None
depends_on = None
```
Import the enum tuples you need from `agent_team.models` (`DEVICE_STATUSES`, `AGENT_RUN_STATUSES`) the same way 0002/0003 import `CODE_PURPOSES`/`ORDER_STATUSES`. Copy the helper trio `_inspector()`, `_has_table(insp, name)`, `_has_column(insp, table, column)`, and `_column_missing(insp, table, column)` from 0002 + 0006.

`upgrade()`:
- `if not _has_table(insp, "devices")`: `op.create_table("devices", ...)` with all ten columns above (`status` as `sa.Enum(*DEVICE_STATUSES, name="device_status")`), then `op.create_index` for the `index=True` columns: `ix_devices_account_id` on `account_id` and `ix_devices_token_hash` on `token_hash`.
- `if not _has_table(insp, "agent_catalog")`: create it with the `UniqueConstraint("agent_id", "version", name="uq_agent_catalog_agent_version")` inline + indexes `ix_agent_catalog_agent_id` (agent_id) and `ix_agent_catalog_content_hash` (content_hash). Use `server_default="1"` for `published`.
- `if not _has_table(insp, "agent_install")`: create with `UniqueConstraint("account_id", "agent_id", name="uq_agent_install_account_agent")` + indexes on `account_id`, `agent_id`. `server_default="1"` for `active`.
- `if not _has_table(insp, "agent_run")`: create with `sa.ForeignKey("runs.id")` on `run_id`, `sa.ForeignKey("agent_catalog.id")` on `agent_catalog_id`, `status` as `sa.Enum(*AGENT_RUN_STATUSES, name="agent_run_status")`, `cost_yuan` as `sa.Numeric(12, 4)` with `server_default="0"`, and indexes on `run_id`, `device_id`, `account_id`, `agent_catalog_id`, `agent_id`. (Create `agent_catalog` BEFORE `agent_run` so the FK target exists.)
- `if _column_missing(insp, "runs", "origin")`: `op.add_column("runs", sa.Column("origin", sa.String(length=16), nullable=False, server_default="cloud"))`.
- `if _column_missing(insp, "runs", "device_id")`: `op.add_column("runs", sa.Column("device_id", sa.String(length=32), nullable=True))` then `op.create_index("ix_runs_device_id", "runs", ["device_id"])` (guard the index too — only create if the column was just added, or wrap defensively).
- NO `ALTER TYPE` for `runs.kind` (it is `String(32)`, not a named enum — adding `'device_run'` needs no DDL).

`downgrade()`: drop in reverse dependency order: `op.drop_column("runs", "device_id")` (drop its index first if your dialect needs it) and `op.drop_column("runs", "origin")` (each guarded by `_has_column`), then `op.drop_table` for `agent_run`, `agent_install`, `agent_catalog`, `devices` (each guarded by `_has_table`). Match 0002/0003's guarded-downgrade style.

## Invariants you MUST respect

- **No model ids / ¥ / per-token pricing in any device-facing surface.** `verify_token`'s record and `list_for_account`'s views carry ONLY device identity + `account_id`. The customer/device UI never sees models or money here. (Mirrors `_account_to_record` omitting secrets and `public_view` field-stripping.)
- **Secrets are hashed, never stored or returned in plaintext after creation.** Only `token_hash` and `pairing_code_hash` persist; the raw token and raw pairing code are each returned exactly ONCE.
- **Live resolution, never frozen.** `verify_token` resolves to the account at call time and must NOT embed a snapshot of the account's purchased models. The downstream gateway (later workstream) intersects `can_use` ∩ the device's live model subset at request time. Document this contract in the docstring even though the intersection itself is out of scope here.
- **Revoke fails closed instantly.** A `DEVICE_REVOKED` (or `DEVICE_PENDING`) device must never verify — `verify_token` filters on `status == DEVICE_ACTIVE`. No key rotation needed.
- **China domestic mode untouched.** This module is pure accounting (no LLM, no provider calls, no `effective_model`/`provider_for`). It must import cleanly with no Anthropic key and on a fully-domestic cloud. Do NOT import `providers`, `config.effective_model`, or anything that resolves a model.
- **Concurrency discipline.** Every mutation runs under `self._lock` (RLock) + `db.session_scope()`; the exchange uses `with_for_update()` so a replayed code under contention cannot mint two tokens (clone `customers.record_usage`'s lock pattern).
- **Migration is additive + idempotent + inspector-guarded**, a no-op on SQLite, real DDL on Postgres; `alembic upgrade head` must end at a schema identical to the models (zero drift). No `ALTER TYPE` for `runs.kind`.
- **Honesty rule:** do not fabricate behavior. This WS ships only the data layer; do not stub endpoints or pretend the desktop flow exists.

## Acceptance criteria

1. `python -c "import agent_team.models, agent_team.devices"` imports cleanly (no Anthropic key set).
2. `python -m pytest -q` — the FULL existing suite (~191+ tests) still passes; nothing regresses.
3. A NEW migration round-trips: `subprocess` `alembic upgrade head` from a simulated pre-0007 SQLite DB (stamped `alembic_version = '0006_phase7_single_report'`, with at least `accounts` and `runs` and an `alembic_version` table present) creates the `devices` table with the right columns/indexes and adds `runs.origin` (NOT NULL DEFAULT 'cloud') + `runs.device_id`. Verify with `PRAGMA table_info`.
4. `alembic upgrade head` then `alembic downgrade -1` then `alembic upgrade head` is clean (the migration is reversible).
5. On a fresh in-test SQLite DB (`db.init_db(tmp)` → `create_all`), `sa.inspect(engine).get_table_names()` includes `devices`, `agent_catalog`, `agent_install`, `agent_run`, and `runs` has `origin`/`device_id` — i.e. the model metadata and the migration agree (no drift).

## Tests to add — create `tests/test_phase11_devices.py`

Follow the harness conventions in `tests/test_phase7_concurrency.py` (the `ROOT`/`sys.path` block, a `fresh_db` fixture calling `db.init_db(str(tmp_path / "devices.db"))`) and the migration-test pattern in `tests/test_redesign_phase6.py::test_migration_0005_*`. Cover at minimum:

- **mint→exchange happy path:** `create_pairing_code(account_id="cus_xxx", name="Bruce-MBP", platform="darwin")` returns a pairing code; `exchange_pairing_code(code, ...)` returns a `device_token` starting with `"tng-dev-"`, a `device_id` starting with `"dev_"`, and the right `account_id`; the device row is now `DEVICE_ACTIVE`.
- **verify_token resolves an active device** and returns a record with the right `account_id`, `id`, `status="active"`, and NO `token_hash`/`pairing_code_hash` keys.
- **replay defense:** a SECOND `exchange_pairing_code(code)` with the same code returns `None` (the pairing material was burned), so a replayed code cannot mint a second token.
- **expiry:** monkeypatch/seed a `Device` row with `pairing_expires_at` in the past (or set `_PAIRING_TTL_SECS` low) → `exchange_pairing_code` returns `None`.
- **revoke is a kill switch:** after `revoke(device_id, account_id)`, `verify_token(raw_token)` returns `None`; and `revoke` scoped to a DIFFERENT `account_id` returns `None` and does NOT change status (cross-account isolation).
- **bad token** (`verify_token("tng-dev-garbage")`, `verify_token("")`, `verify_token(None)`) returns `None` without raising.
- **list_for_account** field-stripping: the returned dicts contain `id/name/platform/status/last_seen_at/created_at` and NONE of `token_hash`, `pairing_code_hash`, or any token/model/¥ key.
- **migration test:** simulate a pre-0007 SQLite DB (raw `sqlite3`, create minimal `accounts`/`runs`/`alembic_version` stamped at `0006_phase7_single_report`), run real `alembic upgrade head` via `subprocess` with `DATABASE_URL=sqlite:///<path>`, assert `returncode == 0`, then `PRAGMA table_info(devices)` shows the columns and `PRAGMA table_info(runs)` shows `origin` (notnull=1, default mentions 'cloud') and `device_id`.

## Explicitly OUT OF SCOPE (do NOT touch — later workstreams own these)

- Any change to `app.py` (no `_current_device`, no `/api/devices/*`, no `/v1` auth resolution, no `/api/agents/catalog`).
- `metrics_store.list_runs` / quota counters / `finish_run` behavior for `device_run` (you only ADD the `RUN_KIND_DEVICE` constant; do NOT change filtering logic).
- The desktop runner (`agent_team/runner/`), `workspace.py`, the local tool loop, manifest building/publishing, the xlsx builder in `deliverables.py`, any frontend / `main.js` changes, keychain/pairing UI.
- Wiring a global `DEVICES = DeviceTokenStore()` singleton into `app.py` (a later workstream instantiates it). You MAY add nothing to app.py.
- Populating `agent_catalog`/`agent_install`/`agent_run` from code — you only create the empty tables/models so the schema matches; no store methods for them in this WS.

Keep the diff tight and the new modules self-contained. When done, run the full pytest suite and the alembic up/down/up round-trip and report the results.
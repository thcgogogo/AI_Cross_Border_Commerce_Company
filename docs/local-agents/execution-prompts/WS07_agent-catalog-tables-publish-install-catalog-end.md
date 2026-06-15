<!-- WS07 · Phase 3 · depends_on: WS3 -->
# WS07 — Agent catalog: tables, publish/install/catalog endpoints, manifest split

> **Phase 3** · **Depends on:** WS3 · **Key files:** agent_team/models.py, app.py, agent_team/config.py, migrations/versions/0007_phase11_devices.py

---


You are working in the repo at `/Users/brucetang/Company_Agent_Demo` (a Flask + SQLAlchemy multi-agent product). This is one workstream of a larger "Tangent Agent" desktop-runner feature (Phase 11). You have NO memory of the planning conversation — everything you need is below. Implement ONLY what this prompt specifies; other workstreams own the rest.

## OBJECTIVE

Add a server-side **agent catalog** so a paired desktop device can pull the BEHAVIOR-ONLY manifests of the agents an account has installed, while founder-only machinery (raw model ids, provider routing, per-token ¥ COGS) is NEVER serialized to a device. Concretely, deliver:

1. Three new SQLAlchemy models — `AgentCatalog`, `AgentInstall`, plus two additive columns on the existing `runs` table (`origin`, `device_id`) and a new `'device_run'` value in `RUN_KINDS` — in `agent_team/models.py`.
2. A single Alembic migration `migrations/versions/0007_phase11_devices.py` (down_revision `0006_phase7_single_report`) that creates the new tables and adds the `runs` columns behind inspector guards, mirroring the 0006 idempotent-DDL pattern.
3. Three endpoints in `app.py`:
   - `POST /api/agents/<agent_id>/publish` — admin-gated (inline 403), snapshots a `config.AGENTS` dict into an `agent_catalog` row.
   - `POST /api/agents/<agent_id>/install` — session-auth, add/remove an `agent_install` row for the logged-in account.
   - `GET /api/agents/catalog` — **device-auth** (a `tng-dev-…` bearer token, resolved by `_current_device()` which is delivered by another workstream), returns the runner-visible manifests for that account's installed agents.
4. A manifest builder that constructs the runner-visible JSON from a **key whitelist** (never by dumping the `config.AGENTS` dict), mirroring how `_agents_payload` strips `model` for non-admins.

### CRITICAL invariant for the catalog path
`GET /api/agents/catalog` and the manifest builder **MUST NEVER call `config.effective_model()` or `config.provider_for()`** (and must never read per-token ¥ rates). The reason: the manifest must resolve on a fully-domestic China cloud that has no Anthropic key and where `effective_model()`/provider routing may not be configured for every agent. Behavior travels to the device; model/provider/¥ resolution stays server-side, OUT of the manifest. Build `manifest_json` from a fixed whitelist of keys only.

## DEPENDENCY — what another workstream (WS3) delivers; do NOT build it here

A separate workstream adds the **`devices`** table and device authentication. You DEPEND on these symbols existing; reference them, do not re-create them:

- A `devices` model/table (id `dev_…`, `account_id`, `token_hash`, `status` enum `pending|active|revoked`, `last_seen_at`, etc.).
- A `DEVICES` store object on the `app` module (analogous to `app.CUSTOMERS`) exposing a `verify_device_token(raw)` → device-record-or-None (sha256 lookup, `status=='active'`, bumps `last_seen_at`).
- A helper `_current_device()` in `app.py` that mirrors `_current_customer()`: reads the `Authorization: Bearer tng-dev-…` header via `_bearer_key()`, calls `DEVICES.verify_device_token(raw)`, returns the device record (carrying `account_id`) or `None`.

If, when you start, `_current_device()` / `DEVICES.verify_device_token` / the `devices` table do NOT yet exist in the repo (WS3 not merged), then:
- Still implement everything else.
- For `GET /api/agents/catalog`, call `_current_device()` and treat a falsy result as 401 — write the route so it works the instant WS3 lands.
- Add a **thin local fallback** ONLY if the symbols are absent: define a minimal `_current_device()` in `app.py` guarded by `getattr(app-module, ...)`/`try import` that returns `None` (so the route 401s cleanly) AND leave a clearly-marked `# WS3 owns the real implementation` comment. Do NOT create the `devices` table or a real device store — that is WS3's job and a duplicate model will collide on `migrations`/`Base`. Prefer to detect WS3's presence and use it; only stub the absence.
- Your `0007` migration creates `agent_catalog`, `agent_install`, and the `runs` columns. If WS3's migration also numbers itself `0007`/touches `devices`, coordinate by having YOUR migration create ONLY your three objects (`agent_catalog`, `agent_install`, `runs.origin`, `runs.device_id`) — each behind `if not _has_table` / `if _column_missing` guards — and assume WS3 contributes the `devices` table via its own guarded block. If both workstreams must share one `0007` file, ADD your guarded blocks to it without removing WS3's; never delete another block. (The guards make double-creation a no-op, so an over-inclusive file is safe.)

## READ THESE FIRST (exact files / functions / patterns to follow)

1. **`agent_team/models.py`** — declarative-classic style on a shared `Base` (works on SQLAlchemy 1.4 + 2.x). Study:
   - The `Run` class (≈ line 194): `runs` table. You will add two columns to it: `origin String(16) NOT NULL default 'cloud' server_default 'cloud'` and `device_id String(16) nullable index` (use `String(16)` to match WS3's `dev_`+token_hex(6) id width — confirm WS3's `devices.id` length and match it; the data-model spec says `String(32)` for `runs.device_id`, so prefer `String(32)` if uncertain — a wider column is harmless).
   - The `RUN_KIND_*` constants and `RUN_KINDS` tuple (≈ lines 74-77). Add `RUN_KIND_DEVICE_RUN = "device_run"` and include it in `RUN_KINDS`. NOTE: `runs.kind` is `String(32)`, NOT a named Enum — so adding a value needs NO Postgres `ALTER TYPE`.
   - The Phase 8-10 loose-ref convention: tables like `AuditLog`/`Order` key on `account_id = Column(String(32), ... index=True)` as a LOOSE ref to `accounts.id` (NO ForeignKey). Follow this for `AgentCatalog.agent_id` / `AgentInstall.account_id`.
   - How `__table_args__ = (UniqueConstraint(...),)` is declared (see `GlossaryTerm` ≈ line 264, `ReconReport` ≈ line 573).

2. **`migrations/versions/0006_phase7_single_report.py`** — copy its idempotent pattern EXACTLY: the `_inspector()` helper, the `_column_missing(insp, table, column)` helper, the `revision`/`down_revision` block, and the "absent table needs no work" contract. You will ADD a `_has_table(insp, name)` helper (the data-model calls for it) alongside `_column_missing`. Your migration's `down_revision = "0006_phase7_single_report"`, `revision = "0007_phase11_devices"`.

3. **`agent_team/db.py`** — `init_db()` (≈ line 86) and `_ensure_sqlite_columns()` (≈ line 129). KEY FACT: on SQLite (all tests + local dev) the schema is built by `Base.metadata.create_all()` + `_ensure_sqlite_columns()`, so once your models are declared, SQLite gets the tables/columns automatically and your Alembic guards become a no-op there. Postgres is owned by Alembic, so the `0007` migration is what gives prod the real additive DDL. You do NOT need to touch `db.py` — declaring the models is enough for SQLite.

4. **`app.py`** — the patterns to mirror:
   - `_agents_payload(admin=False)` (≈ line 464): THE precedent for the founder/non-founder split — it builds a row from a fixed set of keys and only adds `row["model"] = config.effective_model(a["id"])` when `admin`. The device is "just another non-admin caller": your manifest builder is the same shape but a WIDER whitelist, and it must NEVER add `model`/provider/¥.
   - `/api/agents` route (≈ line 604) and `/api/agents/model` route (≈ line 701): the latter shows the inline admin gate verbatim — `if not _is_admin(_current_customer()): return jsonify({"error": "仅限管理员操作。"}), 403`. Use this exact gate (same Chinese string) for `/publish`.
   - `_current_customer()` (≈ line 1486), `_is_admin(customer)` (≈ line 1524), `_bearer_key()` (≈ line 1437), `_account_view()` field-stripping (≈ line 1453) — the field-stripping discipline your catalog must follow.
   - `/v1/chat/completions` (≈ line 4320) — do NOT modify it in this workstream (another WS extends its auth). Read only to understand that device tokens authenticate via `DEVICES.verify_device_token`.

5. **`agent_team/config.py`** — the `AGENTS` list (≈ line 880). Each agent dict currently has keys: `id`, `name`, `title`, `name_cn`, `reports_to`, `model`, `effort`, optional `tools` (a list of capability/tool NAMES), `system_prompt`. **It does NOT currently have `plan_role`, `wants_file_types`, `deliverable_kinds`, or `display`.** So your manifest builder MUST use `.get(key, default)` for those (default `plan_role` to `"specialist"` for a non-CEO and `"ceo"`/`"orchestrator"` for `reports_to is None`; default the others to `[]`/`{}`). Also read `provider_for` (≈ 822), `effective_model`, `COST_BUDGETS_CNY` (≈ 865), `TEAM_MODEL_PRESET` — these are the FOUNDER-ONLY fields you must EXCLUDE from the manifest.

6. **`agent_team/customers.py`** — the mint-once / store-sha256 / verify-by-hash discipline (`_hash_key` ≈ line 528 uses `hashlib.sha256(raw.encode()).hexdigest()`; `verify_key` ≈ 131). You do NOT mint tokens here, but `content_hash` uses the same `hashlib.sha256(...).hexdigest()` primitive.

7. **`tests/test_redesign_phase7.py`** (top ≈ lines 1-99) and **`tests/conftest.py`** — the test harness pattern: throwaway SQLite per test via `db.init_db(str(tmp_path/"x.db"))`, a fresh `CustomerStore` wired into `app.CUSTOMERS` via `monkeypatch.setattr(app, "CUSTOMERS", store)`, and `_client_for(store, email)` which sets `sess["customer_id"]` in a `session_transaction()`. Admin sessions are made by `monkeypatch.setenv("ADMIN_EMAILS", email)` (see how `_is_admin` reads `$ADMIN_EMAILS` fresh each call via `_admin_emails()` ≈ line 1516). Follow this exact harness in your new test file.

## IMPLEMENTATION SPEC

### 1. `agent_team/models.py`

**Constants** — extend the run-kind block:
```python
RUN_KIND_DEVICE_RUN = "device_run"   # a run executed on a paired desktop device
RUN_KINDS = (RUN_KIND_REPORT, RUN_KIND_IMPORT, RUN_KIND_DEVICE_RUN)
```

**`Run` — add two columns** (additive, both with server_defaults so SQLite/PG backfill cleanly):
```python
origin = Column(String(16), nullable=False, default="cloud", server_default="cloud")
device_id = Column(String(32), nullable=True, index=True)   # loose ref devices.id; null for cloud runs
```

**`AgentCatalog`** (new):
```python
class AgentCatalog(Base):
    __tablename__ = "agent_catalog"
    __table_args__ = (UniqueConstraint("agent_id", "version", name="uq_agent_catalog_agent_version"),)
    id = Column(Integer, primary_key=True)
    agent_id = Column(String(64), nullable=False, index=True)   # a config.AGENTS id
    version = Column(Integer, nullable=False, default=1)
    name_cn = Column(String(128), nullable=True)
    manifest_json = Column(Text, nullable=False)               # RUNNER-VISIBLE half ONLY — no model / provider / ¥
    content_hash = Column(String(71), nullable=False, index=True)  # 'sha256:' + 64 hex
    published = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
```

**`AgentInstall`** (new):
```python
class AgentInstall(Base):
    __tablename__ = "agent_install"
    __table_args__ = (UniqueConstraint("account_id", "agent_id", name="uq_agent_install_account_agent"),)
    id = Column(Integer, primary_key=True)
    account_id = Column(String(32), nullable=False, index=True)  # loose ref accounts.id
    agent_id = Column(String(64), nullable=False, index=True)
    pinned_version = Column(Integer, nullable=True)             # null = always latest published
    active = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
```

Add a one-paragraph docstring to each class in the house style (see the existing models). Do NOT add the `agent_run` table in this workstream — that is the telemetry-sync workstream's; if you find it convenient to mention it, leave it out (it has a HARD FK to `runs.id` and belongs with the run-sync endpoints). Update the module-level table-overview docstring at the top of `models.py` to list `agent_catalog` and `agent_install` under a "Phase 11" heading.

### 2. `migrations/versions/0007_phase11_devices.py`

Create the file. Header/docstring in the 0006 style. Body:

```python
revision = "0007_phase11_devices"
down_revision = "0006_phase7_single_report"
branch_labels = None
depends_on = None

def _inspector(): return sa.inspect(op.get_bind())
def _has_table(insp, name): return name in insp.get_table_names()
def _column_missing(insp, table, column):
    if table not in insp.get_table_names():
        return False
    return all(c["name"] != column for c in insp.get_columns(table))
```

`upgrade()`:
- `if not _has_table(insp, "agent_catalog"): op.create_table("agent_catalog", ...)` with every column above, the `UniqueConstraint("agent_id","version", ...)`, AND `op.create_index(...)` for each `index=True` column (`agent_id`, `content_hash`).
- `if not _has_table(insp, "agent_install"): op.create_table("agent_install", ...)` + its `UniqueConstraint` + indexes (`account_id`, `agent_id`).
- `if _column_missing(insp, "runs", "origin"): op.add_column("runs", sa.Column("origin", sa.String(16), nullable=False, server_default="cloud"))`.
- `if _column_missing(insp, "runs", "device_id"): op.add_column("runs", sa.Column("device_id", sa.String(32), nullable=True))` then `op.create_index("ix_runs_device_id", "runs", ["device_id"])` (guard the index creation too — check it isn't already present, e.g. via `insp.get_indexes("runs")`).
- **NO `ALTER TYPE`** for `runs.kind` — it is `String(32)`, so the new `'device_run'` value needs no DDL.
- (If WS3's `devices` table is to be created here, add `if not _has_table(insp, "devices"): op.create_table("devices", ...)`. Only do this if WS3 has NOT already shipped a migration that owns it — check `migrations/versions/` for any file creating `devices` first. If WS3 owns it, leave `devices` out of your file entirely.)

`downgrade()`: drop in reverse, each behind a `_has_table`/`not _column_missing` guard; drop the two `runs` columns and the two tables. Postgres-safe.

Confirm the chain: run `python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; s=ScriptDirectory.from_config(Config('migrations/alembic.ini' if __import__('os').path.exists('migrations/alembic.ini') else 'alembic.ini')); print([sc.revision for sc in s.walk_revisions()])"` (or simply `alembic history`) and verify `0007_phase11_devices` sits on top of `0006_phase7_single_report` with no branch.

### 3. `app.py` — manifest builder + three endpoints

**Manifest builder** — add a module-level helper near `_agents_payload` (≈ line 464):

```python
# The ONLY config.AGENTS keys ever serialized into a device manifest. The device
# is a non-admin caller (like _agents_payload's non-admin branch): model id,
# provider routing and per-token ¥ COGS are founder-only and resolve server-side.
_RUNNER_VISIBLE_AGENT_KEYS = (
    "name", "name_cn", "title", "reports_to", "plan_role",
    "system_prompt", "tools", "effort", "wants_file_types",
    "deliverable_kinds", "display",
)

def _agent_manifest(agent):
    """Build the RUNNER-VISIBLE manifest for one config.AGENTS dict — a WHITELIST,
    never a dict dump. NEVER calls effective_model()/provider_for() and NEVER emits
    model/provider/¥ (works on a domestic cloud with no model keys)."""
    m = {"agent_id": agent["id"]}
    for k in _RUNNER_VISIBLE_AGENT_KEYS:
        if k == "plan_role":
            m[k] = agent.get("plan_role") or ("ceo" if agent.get("reports_to") is None else "specialist")
        elif k in ("tools", "wants_file_types", "deliverable_kinds"):
            m[k] = list(agent.get(k) or [])
        elif k == "display":
            m[k] = dict(agent.get(k) or {})
        else:
            m[k] = agent.get(k, "")
    return m

def _agent_content_hash(manifest):
    """sha256 over canonical JSON of the manifest (sort_keys, no whitespace, ensure_ascii=False)."""
    canon = json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()
```

Ensure `import json`, `import hashlib` are present at the top of `app.py` (they almost certainly are — verify, don't duplicate). Look up the agent dict with a small `config.get_agent(agent_id)` if one exists, else `next((a for a in config.AGENTS if a["id"] == agent_id), None)`.

**`POST /api/agents/<agent_id>/publish`** (admin inline-403):
- `if not _is_admin(_current_customer()): return jsonify({"error": "仅限管理员操作。"}), 403`.
- Resolve the agent dict from `config.AGENTS`; 404 `{"error": "未知的智能体。"}` if absent.
- Build `manifest = _agent_manifest(agent)`; compute `content_hash = _agent_content_hash(manifest)`.
- Determine the next `version`: query `max(AgentCatalog.version)` for this `agent_id`; new version = `(max or 0) + 1`. Set `manifest["manifest_version"] = version` and `manifest["content_hash"] = content_hash` BEFORE serializing into `manifest_json` (so the device receives them inline), but compute `content_hash` over the manifest WITHOUT the `content_hash` key to avoid a self-reference (hash the manifest with `manifest_version` set but `content_hash` absent, then insert `content_hash`). Pick ONE consistent ordering and document it in a comment.
- **Idempotency**: if the latest published row for this `agent_id` already has the same `content_hash`, do NOT bump — return the existing `{agent_id, version, content_hash}` with a `"changed": false` flag. Only INSERT a new versioned row when the content_hash differs (this prevents version inflation on a no-op republish).
- INSERT an `AgentCatalog` row (`agent_id`, `version`, `name_cn=agent.get("name_cn")`, `manifest_json=json.dumps(manifest, ensure_ascii=False)`, `content_hash`, `published=True`).
- Return `jsonify({"agent_id": agent_id, "version": version, "content_hash": content_hash, "changed": True})`.
- Do the DB work through a new `metrics_store` function (preferred, matches the codebase's store pattern) OR an inline `db.session_scope()` block. Prefer adding `metrics_store.publish_agent(agent_id, name_cn, manifest_json, content_hash)` returning `(version, changed)` and `metrics_store.latest_agent_catalog(agent_id)`.

**`POST /api/agents/<agent_id>/install`** (session-auth):
- `customer = _current_customer(); if not customer: return jsonify({"error": "请先登录。"}), 401` (match the codebase's existing 401 copy for logged-out — check what `/api/...` session routes return and reuse it).
- Body `{"active": true|false}` (default `true`); optional `{"pinned_version": <int|null>}`.
- 404 if `agent_id` is not a known `config.AGENTS` id.
- Upsert an `AgentInstall` row keyed by `(account_id=customer["id"], agent_id)`: insert if absent, else update `active`/`pinned_version`. Setting `active=false` is the "uninstall" (keep the row for history, just deactivate — the catalog query filters on `active`).
- Return the account's current install list: `jsonify({"installs": [...]})` where each item is `{"agent_id", "active", "pinned_version"}`.
- Add `metrics_store.set_agent_install(account_id, agent_id, active, pinned_version)` and `metrics_store.list_agent_installs(account_id)`.

**`GET /api/agents/catalog`** (device-auth):
- `dev = _current_device(); if not dev: return jsonify({"error": "设备未授权。"}), 401`.
- `account_id = dev["account_id"]` (the device inherits its account).
- For each ACTIVE `AgentInstall` row of that account: resolve the catalog row to return — if `pinned_version` is set, that exact version; else the latest PUBLISHED version for the `agent_id`. Skip installs with no published catalog row.
- Return `jsonify({"agents": [{"agent_id", "version", "content_hash", "manifest": <parsed manifest_json dict>} ...]})`. The `manifest` value is the parsed JSON (so the device gets a dict, not a string).
- **Re-assert the invariant here**: this handler MUST NOT touch `config.effective_model`/`config.provider_for`/¥ pricing. It reads stored `manifest_json` (already whitelisted at publish time) and returns it verbatim. Add a comment saying so.
- Add `metrics_store.catalog_for_account(account_id)` returning the resolved list of `{agent_id, version, content_hash, manifest_json}`.

Place the three routes adjacent to the existing `/api/agents` routes (≈ lines 604-717) for discoverability.

### 4. `metrics_store.py` helpers
Add the small functions referenced above (`publish_agent`, `latest_agent_catalog`, `set_agent_install`, `list_agent_installs`, `catalog_for_account`). Follow the file's existing `with db.session_scope() as s:` idiom and import the new models (`AgentCatalog`, `AgentInstall`) at the top alongside the existing model imports. Keep each function small and pure-DB; JSON-parse `manifest_json` in `catalog_for_account` so the route can return a dict.

## INVARIANTS TO RESPECT (do not violate any)

- **Catalog never resolves a model**: neither `_agent_manifest`, `GET /api/agents/catalog`, nor any helper it calls may invoke `config.effective_model()` / `config.provider_for()` / read `input_per_mtok`/`output_per_mtok`/`COST_BUDGETS_CNY`/`TEAM_MODEL_PRESET`. The manifest carries BEHAVIOR only. (This is what makes the desktop work on a domestic China cloud with no Anthropic key.)
- **Whitelist, never dump**: `manifest_json` is built from `_RUNNER_VISIBLE_AGENT_KEYS` only. Never `json.dumps(agent)` or `dict(agent)` — that would leak `model` and any ¥ rates that ride on the dict.
- **Founder-gating preserved**: `/publish` uses the exact inline `if not _is_admin(_current_customer()): return ..., 403` gate with the existing Chinese string `"仅限管理员操作。"` — no decorator, server-side.
- **No model keys reach the device**: a device token only ever sees the runner-visible manifest. The catalog endpoint returns no raw model id, provider name, or per-token price — verify by asserting the returned manifest dict has NONE of the keys `model`, `provider`, `input_per_mtok`, `output_per_mtok`, `cost_budget_cny`, `team_model_preset`.
- **¥ cost-control untouched**: this workstream adds NO billing path. It does not call `platform_guard`, `record_usage`, `token_manager`, or the wallet. (Those stay with the gateway/run-sync workstreams.)
- **China domestic mode keeps working**: because the catalog path never resolves a model, `TEAM_MODEL_PRESET=domestic` and a no-Anthropic-key cloud both still serve the catalog. Do not import anything that eagerly constructs a provider client.
- **Loose-ref convention**: `account_id`/`agent_id`/`device_id` are loose `String` refs (no SQL ForeignKey), matching Phase 8-10 tables. (The one HARD FK in this whole feature, `agent_run.run_id → runs.id`, belongs to a DIFFERENT workstream — do not add it here.)
- **Migration idempotency**: every DDL step in `0007` is guarded by `_has_table`/`_column_missing` so it is a no-op on a DB that already has the object (and a no-op on SQLite, where `create_all` already built everything). `runs.kind` gets NO `ALTER TYPE` (it is `String(32)`).
- **Honesty / no fabrication**: do not invent manifest fields the device can't honor; `wants_file_types`/`deliverable_kinds`/`display` default to empty when an agent dict doesn't declare them — never fabricate plausible-looking values.

## ACCEPTANCE CRITERIA

1. `agent_team/models.py` defines `AgentCatalog` and `AgentInstall`, adds `Run.origin` + `Run.device_id`, and `RUN_KINDS` contains `"device_run"`. `python -c "from agent_team import models; print(models.RUN_KINDS); print(models.AgentCatalog.__tablename__, models.AgentInstall.__tablename__)"` succeeds and prints `device_run` in the tuple.
2. A fresh SQLite DB built by `db.init_db()` has `agent_catalog`, `agent_install`, and `runs.origin`/`runs.device_id` (verify via `sqlalchemy.inspect`).
3. `0007_phase11_devices.py` exists, `down_revision == "0006_phase7_single_report"`, `alembic history` shows a single linear chain ending at `0007`, and `alembic upgrade head` then `alembic downgrade -1` runs cleanly against a throwaway SQLite URL (or at minimum, the migration imports without error and the guard helpers behave: on a DB that already has the tables, `upgrade()` is a no-op).
4. `POST /api/agents/<id>/publish` returns 403 for a non-admin session, 404 for an unknown agent id, and for an admin: 200 with `{agent_id, version, content_hash, changed:true}` on first publish; a second publish with no config change returns `changed:false` and does NOT bump the version. The stored `manifest_json` parses to a dict containing `agent_id`, `manifest_version`, `system_prompt`, `tools`, and NONE of `model`/`provider`/`input_per_mtok`/`output_per_mtok`/`cost_budget_cny`/`team_model_preset`.
5. `POST /api/agents/<id>/install` requires a session (401 logged-out), upserts the install (insert then update is idempotent on `(account_id, agent_id)`), and `active:false` deactivates without deleting.
6. `GET /api/agents/catalog` returns 401 without a valid device token; with a valid device whose account has installed + published agents, returns `{"agents":[{agent_id, version, content_hash, manifest}, ...]}` where `manifest` is a dict; a pinned install returns the pinned version, an unpinned install returns the latest published version; deactivated installs and agents with no published row are excluded.
7. A test asserts the catalog handler path does NOT call `config.effective_model`/`config.provider_for` — e.g. `monkeypatch.setattr(config, "effective_model", lambda *a, **k: (_ for _ in ()).throw(AssertionError("manifest must not resolve a model")))` and `monkeypatch.setattr(config, "provider_for", ...)` similarly, then publish + GET catalog and assert it still 200s. (Publish must also not call them — wrap publish in the same guard.)

### Tests to ADD
Create `tests/test_phase11_catalog.py` (new per-phase suite; the repo numbers suites per phase). Mirror the harness in `tests/test_redesign_phase7.py`: throwaway SQLite via `db.init_db(str(tmp_path/"phase11.db"))`, a fresh `CustomerStore` wired into `app.CUSTOMERS`, `_client_for(store, email)` setting `sess["customer_id"]`, and `monkeypatch.setenv("ADMIN_EMAILS", admin_email)` for the founder. For the device-auth test: if WS3's `DEVICES`/`_current_device` exists, create + activate a device row and pass its `tng-dev-…` token as a Bearer header; if WS3 is NOT yet present, `monkeypatch.setattr(app, "_current_device", lambda: {"account_id": <id>})` to exercise the catalog handler in isolation and leave a `# TODO(WS3): swap for a real paired device` comment. Cover every acceptance criterion above. After writing, run `python -m pytest tests/test_phase11_catalog.py -q` and the full suite `python -m pytest -q` and ensure no regressions (the existing suite is ~202 tests and must stay green).

## OUT OF SCOPE (do NOT implement — other workstreams own these)
- The `devices` table, device pairing endpoints (`/api/devices/pair-code`, `/exchange`, list, revoke), `DEVICES` store, and the real `_current_device()`/`verify_device_token` (WS3). Only consume them; stub `_current_device()` to return `None` ONLY if absent.
- Extending `/v1/chat/completions` auth to accept device tokens, RPM/breaker/billing changes (the gateway/LLM-proxy workstream).
- The `agent_run` table and the run-telemetry endpoints (`/api/devices/run/start`, `/finish`, `/event`), and teaching `metrics_store.list_runs`/quota counters to special-case `kind='device_run'` (the run-sync workstream — you only ADD the `device_run` constant + the `runs.origin`/`runs.device_id` columns so they're ready).
- The desktop runner, tray/PyInstaller/Tauri, RemoteRouter, local tool loop, workspace sandbox, the xlsx builder (`create_excel_workbook`).
- Any frontend/SPA change (the "Agents" dropdown becoming the catalog UI).
- Adding `plan_role`/`wants_file_types`/`deliverable_kinds`/`display` keys to the actual `config.AGENTS` dicts (out of scope — the manifest builder defaults them gracefully; a later workstream can enrich the dicts).

## DELIVERABLE
Edited `agent_team/models.py`, `app.py`, `agent_team/metrics_store.py`; new `migrations/versions/0007_phase11_devices.py`; new `tests/test_phase11_catalog.py`. All tests green. Do not commit or push unless asked.
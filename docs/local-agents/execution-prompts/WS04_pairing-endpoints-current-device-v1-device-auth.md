<!-- WS04 · Phase 1 · depends_on: WS3 -->
# WS04 — Pairing endpoints + _current_device + /v1 device auth

> **Phase 1** · **Depends on:** WS3 · **Key files:** app.py, agent_team/devices.py, agent_team/customers.py, agent_team/providers.py (gateway_chat)

> **LOCKED DECISIONS (2026-06-15).** (1) **Per-device ¥ daily sub-cap** — each device carries its own daily limit `device_cap = min(account_daily_cap × 0.3, ¥20)` (make the 0.3 / ¥20 tunable constants in config). Pass `min(device_cap, account_cap)` into `platform_guard.check` for device-authed `/v1` calls. The device-management page shows it as a plain "daily limit" — **never** tokens, ¥-per-call, or model ids (mirror `_account_view` field-stripping). (2) **Device page privacy line** — render the canonical privacy statement from WS01 on the device-management page too.

---


## Objective

Add the cloud-side HTTP surface that lets a paired desktop daemon authenticate to this Flask app with a per-DEVICE bearer token (`tng-dev-…`) instead of an account key. Concretely:

1. Four device-management routes on `app.py`:
   - `POST /api/devices/pair-code` (session-auth) — mint a one-time pairing code.
   - `POST /api/devices/exchange` (NO auth) — redeem a pairing code for a device token, then burn the pairing row.
   - `GET /api/devices` (session-auth) — list the logged-in account's devices, with founder-machinery fields stripped.
   - `POST /api/devices/<device_id>/revoke` (session-auth) — flip a device to `revoked` (instant kill switch).
2. A new `_current_device()` helper in `app.py` mirroring `_current_customer()` — reads the `tng-dev-…` bearer token, verifies it, bumps `last_seen_at`, returns a device record or `None`.
3. Extend the EXISTING `/v1/chat/completions` gateway (`gateway_chat_completions` in `app.py`) auth resolution so a device token is accepted in addition to an account key, with per-device RPM, capped daily ¥, and a device-scoped model subset — while billing still charges the ACCOUNT wallet, unchanged.

This workstream is **Phase 1** and **DEPENDS ON WS3**. WS3 creates `agent_team/devices.py` (the `DeviceStore` class), the `devices` SQL table model in `agent_team/models.py`, and migration `0007`. **Before you start, check whether WS3 has landed** (see "Dependency check" below). If the `DeviceStore` API you consume here differs from what is described, adapt to the real signatures in `agent_team/devices.py` — the contract below is the intended shape, but the WS3 implementation is authoritative.

This repo is a Flask + SQLAlchemy app. Money is in Chinese Yuan (¥). The product is "Tangent AI". Read this whole prompt before writing code.

---

## Dependency check (do this first)

Run:
```
ls -la /Users/brucetang/Company_Agent_Demo/agent_team/devices.py
grep -n "class Device\b\|devices\|token_hash\|pairing_code_hash" /Users/brucetang/Company_Agent_Demo/agent_team/models.py
```
- If `agent_team/devices.py` and a `Device` model exist, READ them and use their real method signatures. The `DeviceStore` method names below (`create_pairing`, `redeem_pairing`, `verify_device_token`, `list_for_account`, `revoke`, `model_subset_for`) are the *expected* contract; map to whatever WS3 actually named them.
- If WS3 has NOT landed yet, you must still deliver a working, tested slice. In that case you MAY create a minimal `agent_team/devices.py` `DeviceStore` plus the `Device` model + `0007` migration yourself, following the data-model spec in the "Fallback: DeviceStore contract" section — but keep it strictly to what THIS workstream's endpoints need, and leave the `agent_catalog` / `agent_install` / `agent_run` tables and the runner/run-telemetry routes to their own workstreams (they are OUT OF SCOPE here; see below).

---

## Context — read these first (exact paths/anchors)

All paths absolute under `/Users/brucetang/Company_Agent_Demo`.

### Auth & gateway patterns to mirror (in `app.py`)
- `_bearer_key()` (~line 1437) — pulls the bearer token from `Authorization: Bearer …` (or `X-API-Key`). Reuse this verbatim; both `tng-sk-` and `tng-dev-` tokens arrive the same way.
- `_account_view(customer)` (~line 1453) — **THE field-stripping pattern to mirror.** For a non-admin it `pop`s `balance`, `spent`, `requests`, `usage`, `key_prefix`, `key_last4`, and reduces each model row to only display-kind fields. Your `GET /api/devices` output must follow the same discipline: NEVER emit the raw device token, ¥ figures, or raw model ids to a customer session.
- `_current_customer()` (~line 1486) — the session→record resolver you will mirror for `_current_device()` (but device auth reads a bearer token, not the session cookie).
- `_gateway_error(message, status, code)` (~line 1448) — OpenAI-style error body. Use it for `/v1` failures.
- `_rpm_check(key, limit)` (~line 250) — per-key RPM, keyed on an arbitrary string, fail-open. You will call it with the DEVICE id for a device-authenticated `/v1` call.
- `_is_admin(customer)` / `_admin_emails()` (~line 1516–1529) — founder gating by `$ADMIN_EMAILS`, enforced INLINE (`if not _is_admin(...): return 403`). No decorators in this codebase.
- `_agents_payload(admin=False)` (~line 464) — shows the EXACT precedent that `row["model"]` is added ONLY for admins. The device-management list is "just another non-admin caller."
- `gateway_chat_completions()` — `@app.route("/v1/chat/completions", methods=["POST"])` (~line 4320). This is the function you EXTEND. Study its current 3-layer order carefully (it is the load-bearing part of this WS):
  1. `customer = CUSTOMERS.verify_key(_bearer_key())` → 401 on miss.
  2. `rpm = customer.get("rpm_limit") or config.DEFAULT_KEY_RPM; if not _rpm_check(customer["id"], rpm): 429`.
  3. `allowed, reason = platform_guard.check(customer["id"], customer.get("daily_cap_yuan")); if not allowed: 503`.
  4. parse `model`/`messages`; validate model exists + is a text model.
  5. LAYER 3 access: `if not CUSTOMERS.can_use(customer, model): 403`.
  6. LAYER 2 pre-check: `if not CUSTOMERS.has_balance(customer): 402`.
  7. `result = providers.gateway_chat(model, messages, max_tokens=…)`.
  8. `billed = CUSTOMERS.record_usage(customer["id"], model, in_tok, out_tok)` then build the OpenAI-style response.
- `CUSTOMERS = CustomerStore(...)` is instantiated at ~line 355. You will add a `DEVICES = DeviceStore(...)` next to it.
- Module imports at the top of `app.py` (~line 45–102): `customers` is imported via `from agent_team.customers import CustomerStore`. Add an analogous `from agent_team.devices import DeviceStore`.

### Account / store patterns (in `agent_team/customers.py`)
- The "mint-once / store-sha256 / verify-by-hash" secret discipline: `KEY_PREFIX = "tng-sk-"`, `raw_key = KEY_PREFIX + secrets.token_urlsafe(32)`, store only `_hash_key(raw_key)` (`hashlib.sha256(...).hexdigest()`), `verify_key` looks up by hash. **Clone this byte-for-byte for the device token** (`"tng-dev-" + secrets.token_urlsafe(32)`) and for the pairing code (`sha256(code)`).
- `public_view()` / `_account_to_record()` show how a store returns plain dict snapshots (never live ORM rows) that are safe to hand across threads. Your device records must do the same.

### Data-model conventions (in `agent_team/models.py`)
- Phase 8–10 tables use a LOOSE `account_id = Column(String(32), nullable=True/False, index=True)` ref to `accounts.id` (NO hard FK) — see `Order` (~line 502), `Subscription` (~line 529). Account ids look like `"cus_" + token_hex(6)`.
- `Run` table (~line 194): `kind = Column(String(32), …)` (a plain String, NOT a named Enum). `RUN_KIND_REPORT="report"`, `RUN_KIND_IMPORT="import"`, `RUN_KINDS=(…)` at lines 75–77.

### Migration pattern (in `migrations/versions/0006_phase7_single_report.py`)
- Head migration is `0006_phase7_single_report`. Copy its inspector-guarded helper style: `_inspector()`, `_column_missing(insp, table, column)`, each DDL behind a guard so the migration is idempotent and a no-op on a fresh DB / SQLite. `down_revision` for the new `0007` is `"0006_phase7_single_report"`.
- SQLite/dev gets its schema from `agent_team/db.py:_ensure_sqlite_columns()` + `create_all()` (~line 124–177), so the migration guards make `0007` a no-op there; Postgres gets the real additive DDL.

### Platform guard (in `agent_team/platform_guard.py`)
- `check(account_id=None, account_daily_cap=None)` (~line 79) → `(allowed, reason)`. The breaker + per-account ¥/day cap.

### Providers (in `agent_team/providers.py`)
- `gateway_chat(model_id, messages, max_tokens=1024)` (~line 181) → `{"text","input_tokens","output_tokens","model","provider"}`. Raises `providers.ProviderKeyMissing` / `providers.LLMError`. UNCHANGED by this WS.

### Test harness (in `tests/`)
- `tests/conftest.py` disables the limiter suite-wide and no-ops `app.load_dotenv`.
- `tests/test_phase8_security.py` and `tests/test_phase9_payments.py` are the model for fixtures: a `fresh_db` fixture (`db.init_db(str(tmp_path / "brain.db"))`), a `_store(tmp_path, **accounts)` helper that builds a `CustomerStore`, wires it into `app.CUSTOMERS`, and creates accounts via `store.create_customer([], …, email=…, password=…)`. Use `app.app.test_client()`. Log a session in by POSTing the real login route, or by setting `session["customer_id"]`/`session["sid"]` via `client.session_transaction()`.

---

## Implementation spec

### A. `DEVICES` store wiring in `app.py`

Add near `CUSTOMERS = CustomerStore(...)` (~line 355):
```
DEVICES = DeviceStore()
```
and the import near line 64:
```
from agent_team.devices import DeviceStore
```
(Match the real `DeviceStore.__init__` signature from WS3 — it is DB-backed like `CustomerStore`, so it likely takes no required args.)

### B. `_current_device()` in `app.py` (place beside `_current_customer`)

```
def _current_device():
    """The paired device for this runner request, or None.

    Reads the tng-dev-… bearer token (same header path as _bearer_key), verifies
    it against the DeviceStore (sha256 lookup, status=='active'), bumps last_seen_at,
    and returns a plain device record dict (never a live ORM row). Mirrors
    _current_customer(): every runner→cloud route starts
        dev = _current_device()
        if not dev: return <401>
    """
    raw = _bearer_key()
    if not raw or not raw.startswith("tng-dev-"):
        return None
    return DEVICES.verify_device_token(raw)   # returns record incl. account_id, or None
```
- `verify_device_token` MUST: hash the raw token (`sha256`), look up an `active` device, bump `last_seen_at`, and return a record dict carrying at least `id`, `account_id`, `name`, `platform`, `status`. A `pending` or `revoked` row must fail closed (return `None`).

### C. The four device-management routes in `app.py`

Place them together, near the other `/api/account/*` routes. All session-gated EXCEPT `/exchange`.

**1. `POST /api/devices/pair-code` (session-auth)**
```
@app.route("/api/devices/pair-code", methods=["POST"])
def device_pair_code():
    customer = _current_customer()
    if not customer:
        return jsonify({"error": "请先登录后再配对设备。"}), 401
    out = DEVICES.create_pairing(customer["id"])   # writes a pending devices row
    return jsonify({"pairing_code": out["pairing_code"],
                    "expires_at": out["expires_at"]})
```
- `create_pairing(account_id)` writes a `devices` row with `status='pending'`, `pairing_code_hash=sha256(code)`, `pairing_expires_at = utcnow()+10min`, returns the **plaintext** `pairing_code` ONCE plus the ISO `expires_at`. The pairing code is a short, user-copyable one-time secret (e.g. `secrets.token_urlsafe(...)` or an 8–10 char code) — its plaintext is returned only here, only the hash is stored.

**2. `POST /api/devices/exchange` (NO auth — the daemon, not yet holding a token, calls this)**
```
@app.route("/api/devices/exchange", methods=["POST"])
def device_exchange():
    data = request.get_json(silent=True) or {}
    code = (data.get("pairing_code") or "").strip()
    name = (data.get("device_name") or "").strip()[:128] or "Tangent device"
    platform = (data.get("platform") or "").strip().lower()
    if platform not in ("darwin", "win", "linux"):
        platform = "linux"
    out = DEVICES.redeem_pairing(code, name, platform)
    if not out:
        return jsonify({"error": "配对码无效或已过期。"}), 400
    return jsonify({"device_token": out["device_token"],
                    "device_id": out["device_id"],
                    "account_id": out["account_id"]})
```
- `redeem_pairing(code, name, platform)` MUST: match a `pending`, non-expired row by `sha256(code)`; flip it to `status='active'`; mint `device_token = "tng-dev-" + secrets.token_urlsafe(32)` and store ONLY `token_hash=sha256(token)`; **BURN the pairing fields** (`pairing_code_hash=None`, `pairing_expires_at=None`) so a replayed code can't mint a second token; persist `name`/`platform`/`last_seen_at`. Return `{device_token (ONCE), device_id, account_id}` or `None` on no-match/expired/already-redeemed. This is the ONLY place the device token is ever returned.
- This endpoint is intentionally unauthenticated: possession of the one-time pairing code IS the proof. Do not add `_current_customer()` here.

**3. `GET /api/devices` (session-auth) — field-stripped**
```
@app.route("/api/devices", methods=["GET"])
def devices_list():
    customer = _current_customer()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    devices = DEVICES.list_for_account(customer["id"])
    return jsonify({"devices": [_device_view(d) for d in devices]})
```
Add a `_device_view(d)` helper next to `_account_view`, mirroring its field-strip discipline:
```
def _device_view(d):
    """Customer-safe device snapshot. NEVER the raw token/token_hash, ¥, or model
    ids (mirrors _account_view's non-admin field-stripping)."""
    return {
        "id": d.get("id"),
        "name": d.get("name"),
        "platform": d.get("platform"),
        "status": d.get("status"),
        "last_seen_at": d.get("last_seen_at"),
        "created_at": d.get("created_at"),
    }
```
- Do NOT include `token_hash`, `pairing_code_hash`, `account_id` (redundant — it's the caller's own), any ¥ figure, or any model id. `list_for_account` returns only this account's rows; never expose `pending` rows' codes.

**4. `POST /api/devices/<device_id>/revoke` (session-auth)**
```
@app.route("/api/devices/<device_id>/revoke", methods=["POST"])
def device_revoke(device_id):
    customer = _current_customer()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    ok = DEVICES.revoke(customer["id"], device_id)   # only if the device belongs to this account
    if not ok:
        return jsonify({"error": "未找到该设备。"}), 404
    return jsonify({"ok": True})
```
- `revoke(account_id, device_id)` MUST set `status='revoked'` ONLY when the row's `account_id` matches the caller (an account cannot revoke another account's device → 404, not 403, to avoid leaking existence). After revoke, `verify_device_token` fails closed — no key rotation needed. Returns truthy on success, falsy when not found / not owned.

### D. Extend `gateway_chat_completions()` auth in `app.py` (the load-bearing change)

Modify ONLY the auth-resolution prologue of the existing `/v1/chat/completions` handler. The body shape (response JSON), `providers.gateway_chat`, and `record_usage` stay UNCHANGED. The flow becomes:

1. **Resolve identity (account key OR device token).** After reading the bearer key:
   ```
   raw = _bearer_key()
   customer = CUSTOMERS.verify_key(raw)
   device = None
   if not customer:
       device = DEVICES.verify_device_token(raw)   # tng-dev-… path
       if device:
           customer = CUSTOMERS.get_by_id(device["account_id"])
   if not customer:
       return _gateway_error("Invalid API key. …", 401, "invalid_api_key")
   ```
   - Order matters: try the account key first, then the device token. On a device hit, `customer` is the resolved ACCOUNT record (so balance/wallet/COGS resolve exactly as a web call). If the device resolves but its account is missing/deleted, treat as 401.

2. **RPM keyed on the DEVICE id when device-authenticated** (per-device throttle for free), else the account id as today:
   ```
   rpm_key = device["id"] if device else customer["id"]
   rpm = customer.get("rpm_limit") or config.DEFAULT_KEY_RPM
   if not _rpm_check(rpm_key, rpm):
       return _gateway_error(f"Rate limit exceeded — at most {rpm} requests per minute …", 429, "rate_limit_exceeded")
   ```

3. **platform_guard.check with `min(device_cap, account_cap)`** when device-authenticated:
   ```
   acct_cap = customer.get("daily_cap_yuan")
   if device:
       dev_cap = device.get("daily_cap_yuan")   # may be None
       cap = _min_cap(dev_cap, acct_cap)        # None means "no cap"; smaller of the two non-None
   else:
       cap = acct_cap
   allowed, reason = platform_guard.check(customer["id"], cap)
   if not allowed:
       return _gateway_error(reason, 503, "platform_busy")
   ```
   Add a small helper:
   ```
   def _min_cap(a, b):
       """Tightest of two optional ¥/day caps. None = unbounded; min of the two
       present values; None only if BOTH are None."""
       vals = [v for v in (a, b) if v is not None]
       return min(vals) if vals else None
   ```
   (If WS3's device record has no per-device cap field, `device.get("daily_cap_yuan")` is simply `None` and the account cap governs — that's fine.)

4. **LAYER 3 access — intersect with the device's live model subset** when device-authenticated. After the existing `CUSTOMERS.can_use(customer, model)` check, add a device-scoped narrowing:
   ```
   if device:
       allowed_models = DEVICES.model_subset_for(device)   # computed at call time, never frozen
       if model not in allowed_models:
           return _gateway_error(
               f"This device isn't authorized for '{model}'.", 403, "model_access_denied")
   ```
   - `model_subset_for(device)` computes the device's allowed models AT CALL TIME (never a frozen snapshot). If WS3 has not yet defined a per-device model subset, default it to the resolved account's purchased models (i.e. `customer.get("models")`) so the device inherits the account's access and the intersection is a no-op. Document that assumption in a comment; the real subset wiring belongs to the catalog workstream.
   - Keep the existing account-level `CUSTOMERS.can_use` check in place for BOTH paths — the device subset is an ADDITIONAL narrowing, never a widening.

5. **Billing UNCHANGED.** `CUSTOMERS.has_balance(customer)`, `providers.gateway_chat`, and `CUSTOMERS.record_usage(customer["id"], …)` all stay exactly as they are — the ACCOUNT wallet is charged, `record_platform_spend` feeds the breaker, COGS is recorded. A device call is billed to its account; there are ZERO billing changes.

Keep the existing comments' Phase-numbering style; add brief comments explaining the device branch.

### E. Fallback: `DeviceStore` contract (only if WS3 hasn't landed)

If you must create `agent_team/devices.py` yourself, implement a DB-backed `DeviceStore` mirroring `CustomerStore`'s discipline (threading.RLock, `db.session_scope()`, plain-dict snapshots, sha256 hashing helpers). Required methods, all returning plain dicts (never ORM rows):

- `create_pairing(account_id) -> {"pairing_code": <plaintext once>, "expires_at": <iso>}`
- `redeem_pairing(code, name, platform) -> {"device_token": <plaintext once>, "device_id": str, "account_id": str} | None`
- `verify_device_token(raw) -> {"id","account_id","name","platform","status","last_seen_at","created_at", ...} | None` (active only; bumps `last_seen_at`)
- `list_for_account(account_id) -> [device record dicts]`
- `revoke(account_id, device_id) -> bool`
- `model_subset_for(device) -> [model_id, ...]` (default: the account's purchased models)

And the `Device` model in `agent_team/models.py` (data-model spec):
```
devices:
  id              String(32)  PK, default 'dev_' + token_hex(6)
  account_id      String(32)  index, loose ref accounts.id (NO FK — Phase 8-10 convention)
  name            String(128)
  platform        String(32)  ('darwin'|'win'|'linux')
  token_hash      String(64)  index   (sha256 of the device token)
  pairing_code_hash   String(64) nullable
  pairing_expires_at  DateTime   nullable
  status          Enum('pending','active','revoked') name='device_status' default 'pending'
  last_seen_at    DateTime    nullable
  created_at      DateTime    default datetime.utcnow
```
And migration `migrations/versions/0007_phase11_devices.py`:
- `down_revision = "0006_phase7_single_report"`.
- Copy the `_inspector()` / `_column_missing()` helper style from `0006`; add `_has_table(insp, name)`.
- `if not _has_table(insp, "devices"): op.create_table("devices", …)` with every `index=True` index created.
- NO `ALTER TYPE` for `runs.kind` (it is `String(32)`).
- The SQLite/dev path is satisfied by `create_all` + `_ensure_sqlite_columns`, so the table guard makes `0007` a no-op there; Postgres gets the real DDL.

**Do NOT create `agent_catalog`, `agent_install`, or `agent_run` tables in this workstream** — those belong to the catalog / run-telemetry workstreams. If WS3 already created `devices`, do not duplicate it; reuse WS3's model and migration.

---

## Invariants you MUST respect

- **China domestic mode keeps working.** This WS adds NO hard Anthropic dependency. The gateway resolves the model server-side via `config.provider_for(model)` exactly as today; a device never picks a model. Do NOT import or require an Anthropic key anywhere on this path. The device-auth branch must work identically whether the cloud is in domestic (DeepSeek/Qwen) or Anthropic mode.
- **No model keys / no ¥ / no model ids on the customer or device surface.** `_device_view` and `GET /api/devices` must never emit the raw token, `token_hash`, per-token ¥ pricing, model ids, or balances — mirror `_account_view`'s non-admin field-strip. The only place a device token is EVER returned is `POST /api/devices/exchange`, once.
- **¥ cost-control stays intact.** Billing still flows through `platform_guard.check` (pass the tightest cap on the device path) and `CUSTOMERS.record_usage` (charges the account wallet + `record_platform_spend` feeds the breaker). Do not bypass or weaken any of the 6 layers; the device path is strictly an ADDITIONAL set of constraints (per-device RPM, tightest cap, model-subset narrowing) on top of the account's.
- **Founder-gating unchanged.** Do not add any founder-only data to customer/device responses. The device list is "just another non-admin caller" — model ids stay server-side, exactly as `_agents_payload` strips `row["model"]` for non-admins.
- **Secret discipline.** Device token and pairing code follow the `customers.py` mint-once / store-sha256 / verify-by-hash pattern byte-for-byte. Never store or log plaintext tokens/codes; never return them except the one designated time each.
- **Fail-closed auth.** `pending`/`revoked`/expired/unknown → `None`/401/400; revoke is the instant kill switch (no rotation). Cross-account revoke → 404 (no existence leak).
- **Files-encrypted-at-rest / SSRF egress lock / honesty rule** are untouched by this WS — do not weaken them, but you should not need to touch them.

---

## Acceptance criteria + tests

Add a new test module `tests/test_phase11_devices.py` (pytest, mirroring `tests/test_phase8_security.py` fixtures: a `fresh_db` fixture via `db.init_db(tmp_path/'brain.db')`, a store helper wiring `app.CUSTOMERS` and `app.DEVICES`, `app.app.test_client()`). The suite must currently be green; the whole suite (~202 tests) must STILL pass after your change — run `python -m pytest -q` from the repo root.

Tests to write (each an explicit acceptance criterion):

1. **Pairing happy path.** Logged-in session → `POST /api/devices/pair-code` returns `{pairing_code, expires_at}`. Then `POST /api/devices/exchange` (NO session) with that code → 200 `{device_token, device_id, account_id}`; `device_token` starts with `tng-dev-`; `account_id` equals the logged-in account.
2. **Replay is burned.** A second `POST /api/devices/exchange` with the SAME code → 400 (the pairing row was burned; no second token minted).
3. **Expired code.** Monkeypatch the pairing TTL (or directly age the row's `pairing_expires_at` in the DB) so the code is expired → exchange → 400.
4. **Device list is field-stripped.** After pairing, `GET /api/devices` (session) returns the device with `name`/`platform`/`status`/`last_seen_at`, and the JSON contains NO `device_token`/`token_hash`/`pairing_code_hash`/balance/¥/model-id keys. Assert the raw token string is NOT present anywhere in the response body.
5. **Revoke kills the token.** A `/v1/chat/completions` call with the device token succeeds (stub `providers.gateway_chat` via monkeypatch to avoid a real upstream call). Then `POST /api/devices/<id>/revoke` (session) → 200. A subsequent `/v1` call with the same device token → 401. `_current_device()` returns None for the revoked token.
6. **Cross-account isolation.** Account B cannot revoke account A's device → 404; account B's `GET /api/devices` does not list account A's device.
7. **`/v1` device auth bills the account.** With `providers.gateway_chat` monkeypatched to return fixed token counts, a device-authenticated `/v1/chat/completions` for a model on the account's plan → 200, AND the resolved account's wallet/spend was charged (assert via `CUSTOMERS.get_by_id(account_id)` spend/balance delta), exactly as an account-key call would be. Confirm the model the device used is one the account purchased.
8. **`/v1` model-subset narrowing.** A device call for a model the device subset excludes (or the account doesn't own) → 403 `model_access_denied`.
9. **Per-device RPM.** (Re-enable the limiter locally as `tests/test_phase8_security.py` does for its RPM test, OR assert `_rpm_check` is called with the device id.) Demonstrate the RPM key on the device path is the device id, not the account id (e.g. two devices on one account don't share a single bucket).
10. **Migration is a no-op-safe / idempotent on SQLite.** If you authored `0007`, add a check (or reuse the existing migration-smoke pattern if one exists) that `devices` exists after `db.init_db` on a fresh SQLite file and that re-running the inspector-guarded upgrade does not error.

Also confirm the existing `/v1` account-KEY path is UNCHANGED: an account-key call with no device still works (the existing gateway tests, if any, stay green). If the WS3 `DeviceStore` signatures differ from the contract above, adapt the tests to the real API.

---

## Explicitly OUT OF SCOPE for this workstream

- The `agent_catalog` / `agent_install` / `agent_run` tables and ALL catalog routes (`POST /api/agents/<id>/publish`, `POST /api/agents/<id>/install`, `GET /api/agents/catalog`) — separate workstream.
- The runner run-telemetry routes (`POST /api/devices/run/start`, `POST /api/devices/run/<id>/finish`, `/event`), `runs.origin` / `runs.device_id` columns, the `device_run` RUN_KIND, and the `metrics_store.list_runs`/quota changes — separate workstream.
- The manifest builder, the runner-visible vs founder-only manifest split, and anything touching `config.AGENTS` serialization — separate workstream.
- The desktop daemon itself (`agent_team/runner/`), PyInstaller packaging, tray UI, keychain storage, `RemoteRouter`, the local tool-execution loop, `workspace.assert_in_workspace`, the xlsx builder — all separate workstreams.
- Any change to `providers.gateway_chat`, `record_usage`, the response JSON shape, or the report-pricing/quota gate.

If you find yourself editing the catalog tables, the runner routes, or `config.AGENTS`, STOP — that's another workstream. Keep this PR to: the four device-management routes, `_current_device()`, the `_device_view`/`_min_cap` helpers, the `gateway_chat_completions` auth-prologue extension, the `DEVICES` wiring, and (only if WS3 hasn't landed) the minimal `DeviceStore` + `Device` model + `0007` migration.

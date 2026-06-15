<!-- WS10 · Phase 3 · depends_on: none -->
# WS10 — Remove report-delivery pricing from the run path

> **Phase 3** · **Depends on:** none · **Key files:** app.py (_plan_gate + /api/run), agent_team/config.py (PLANS), tests/ (new gate-removal regression test)

> **LOCKED DECISION (2026-06-15) — read this first, it overrides the open question below.** The cloud web product is **kept** as a no-install fallback tier, so do **NOT** delete `_plan_gate` or the web `/api/run` path. Instead **stub `_plan_gate` to always-allow** (early-return the success/None path) so report-delivery pricing is discarded for now on BOTH the web and local paths, while leaving `config.PLANS` in place and unreferenced for later re-enabling. Device runs must never enter the quota path at all (they use `kind='device_run'`). Keep auth + the ¥ wallet + `platform_guard` fully intact. The regression test should assert: (a) a web `/api/run` succeeds for a free account past its old quota, (b) `_current_customer`/session auth still works, (c) the wallet + `platform_guard` breaker still function.

---


## Objective

The owner has decided to DISCARD report-delivery pricing (the monthly/lifetime report-quota + ¥29 single-report-credit gate) from the agent run path. Your job is to remove the `_plan_gate` quota/credit billing decision from the **interactive run path** so that report-delivery pricing no longer blocks a run, while leaving **authentication, the ¥ wallet/balance accounting, the per-account daily cap, and the platform circuit breaker (`platform_guard`) completely untouched**. You will also keep the `config.PLANS` dict in the codebase (it is still read by subscription/plans pages and the scheduled-delivery feature gate) but make it **no longer referenced by the interactive run gate**. Finally, add a regression test that proves auth + ¥ wallet + breaker still gate runs correctly after the quota gate is gone.

This is groundwork for an upcoming local-daemon ("Tangent Agent" desktop) product whose device-run path must NOT carry report-delivery pricing. So the rule going forward is: **report-delivery quota pricing is dead on the run path; cost-control survives entirely through `platform_guard` + the ¥ wallet + per-run token budgets.**

You have the repo at `/Users/brucetang/Company_Agent_Demo`. You have NO memory of the planning conversation — everything you need is below. Read the cited files before editing.

---

## Context — read these first

1. **`app.py`** — the Flask monolith. Read these exact regions:
   - `def _plan_gate(customer):` at **line ~723**. This is THE report-delivery billing decision. It returns `(allowed, message, code)` where `code ∈ {None, "locked", "no_balance", "quota"}`. Internally it:
     - calls `metrics_store.effective_plan()` → `locked` (past-due subscription) returns `(False, ..., "locked")`,
     - for FREE accounts with no report_credits and no balance, returns `(False, ..., "no_balance")`,
     - reads `config.plan(plan_code)["monthly_report_quota"]`, adds `report_credits`, counts runs via `metrics_store.count_runs_total` / `count_runs_this_month`, and returns `(False, ..., "quota")` when over the effective ceiling,
     - otherwise returns `(True, None, None)`.
   - `def run():` at **line ~786** — `@app.route("/api/run", methods=["POST"])`. THIS is the interactive run path (the one the local daemon will eventually clone). Its gate order today is: (1) `_current_customer()` → 401, (2) `platform_guard.check(customer["id"], customer.get("daily_cap_yuan"))` → 503, (3) `_plan_gate(customer)` → 402, then goal validation + upload guards + job creation.
   - `def onboarding_generate():` at **line ~4435** — `@app.route("/api/onboarding/generate")`. Step-5 of the wizard; same interactive run shape: `platform_guard.check` then `_plan_gate` → 402. This IS part of the interactive run path (it produces the customer's first report exactly like `/api/run`).
   - The OTHER three `_plan_gate` call sites — DO NOT REMOVE THESE (they are NOT the run path; see Out of Scope):
     - **line ~1116** inside the scheduled-delivery worker (`_run_scheduled_job` area) — it maps `_gate_code` to distinct `last_status` values (`skipped_no_balance` / `skipped_plan_limit`) via `metrics_store.mark_job_run`. This path is the **scheduled auto-delivery** feature and must keep its gate and its use of the `code` return value.
     - **line ~3624** inside the scheduled-run create endpoint — same scheduled-delivery surface.
     - **line ~4177** inside `brain_import_start()` (`/api/company-brain/import`) — the **history import** path (`kind='import'`), not a report run. Leave it.
     - **line ~4438** inside the **Fusion** founder endpoint (admin-gated). Leave it.
   - `def _current_customer():` at **line ~1486** — session → customer resolver. Untouched; the regression test relies on it.

2. **`agent_team/config.py`** — read `PLANS` at **line ~441**, `plan(code)` at ~461, `plan_has(code, feature)` at ~466, and the constants around them (`PURCHASABLE_PLANS`, `SINGLE_REPORT_PRICE_YUAN`). `PLANS` carries `monthly_report_quota` / `lifetime_quota` / `seats` / `features`. KEEP this dict and its helpers in place — they are still read by the subscription/plans pages, `plan_has()` (scheduled_delivery / contract_review feature flags), and the scheduled + import + fusion gates you are NOT touching. Your only change is that the **interactive run gate stops consulting it**.

3. **`agent_team/platform_guard.py`** (the circuit breaker + per-account daily ¥ cap) and `agent_team/customers.py` (`CustomerStore`: `verify_login`, `has_balance`, the row-locked `Decimal` ¥ wallet) — read enough to confirm these are ORTHOGONAL to `_plan_gate`. You must not modify either. The acceptance criteria assert they still fire.

4. **`tests/conftest.py`** — note the limiter is disabled suite-wide and `app.load_dotenv` is no-op'd for hermeticity. Follow this.

5. **`tests/test_redesign_phase7.py`** — this is the canonical billing/gate suite and the model for your new test. Copy its fixture/harness patterns verbatim:
   - `env` fixture: throwaway SQLite via `db.init_db(...)`, monkeypatches `app.MEMORY_DIR/OUTPUT_DIR/UPLOAD_DIR` + `metrics_store.UPLOADS_DIR/REPORTS_DIR`, a fresh `CustomerStore` wired into `app.CUSTOMERS`, a seeded company via `metrics_store.get_or_create_company`.
   - `_client_for(store, email)` — builds a `test_client()` and sets `sess["customer_id"]` for a logged-in session.
   - `_subscribe(account_id, plan_code, ...)`, `_set_balance(store, email, amount)`, `_StaticLLM` — reuse as needed.
   - Existing assertions you MUST be aware of: this suite currently asserts `/api/run` returns **402** when the free trial's lifetime quota is exhausted (e.g. `test_*` around lines 341–383 assert `status_code == 402` after 2 lifetime runs, and that report_credits raise/consume the ceiling). **These tests will break once you remove the run-path quota gate.** See "Migrate the now-obsolete assertions" below — you must update them, not delete the file.

---

## Implementation spec

### Step 1 — Remove the quota gate from the interactive run path

In `app.py`:

- **`/api/run` (`def run()`, ~line 809)**: delete the three-line `_plan_gate` block:
  ```python
  plan_ok, plan_msg, _gate_code = _plan_gate(customer)
  if not plan_ok:
      return jsonify({"error": plan_msg}), 402
  ```
  Leave the preceding `platform_guard.check(...)` → 503 block and the `_current_customer()` → 401 block exactly as they are. Replace the removed block with a short comment explaining that report-delivery pricing was intentionally retired from the run path (cost-control now lives solely in `platform_guard` + the ¥ wallet + per-run token budgets), so the local-daemon device-run path that clones this handler never inherits report pricing.

- **`/api/onboarding/generate` (`def onboarding_generate()`, ~line 4438)**: remove the identical `_plan_gate` → 402 block, same comment. Keep its `platform_guard.check` → 503 block.

DO NOT touch the `_plan_gate` calls at lines ~1116, ~3624, ~4177, ~4438 (scheduled worker, scheduled-run create, brain import, fusion). Those are not report runs.

### Step 2 — Decide and implement the cloud web product's stance (OPEN QUESTION — resolve it this way)

The open question is whether the cloud web product should *keep* `_plan_gate` on its run path or stub it to always-allow. **Resolve it as: remove the gate from the run path entirely (Step 1 already does this for both `/api/run` and `/api/onboarding/generate`), and KEEP the `_plan_gate` function defined** so the still-live scheduled/import/fusion call sites keep working unchanged. Do NOT stub `_plan_gate` to always-allow — that would silently weaken the scheduled-delivery `locked`/quota behavior, which is out of scope and still wanted. The single source of truth becomes: *the run path does not call `_plan_gate`; everything else that legitimately needs plan/quota semantics still does.*

Document this decision in a 2–3 line comment directly above the `_plan_gate` definition (note that it is intentionally NO LONGER called by `/api/run` or `/api/onboarding/generate`, and list the remaining callers so a future reader does not "helpfully" delete it as dead code).

### Step 3 — Keep `config.PLANS` present but unreferenced by the run gate

No structural change to `config.PLANS`. Just verify (by grepping) that after Step 1, no interactive-run code path imports/reads `monthly_report_quota` or `count_runs_*` for the purpose of blocking a run. `PLANS` must still exist and still be read by the subscription pages and `plan_has()`. Add nothing; remove nothing from config.py unless a grep proves a symbol is now truly unused everywhere (it will not be — leave it).

### Step 4 — Regression test (NEW)

Create **`tests/test_run_gate_removal.py`**. Mirror the `env` / `_client_for` / `_set_balance` / `_subscribe` harness from `tests/test_redesign_phase7.py` (copy the fixture and helpers into the new file or import them; the suite convention is self-contained copies — follow whichever the repo's other phase suites do, but self-contained is safest). Stub the LLM/orchestrator the same way the phase-7 suite avoids real network calls (use `_StaticLLM` or monkeypatch the orchestrator entrypoint so `/api/run` returns its `job_id` without doing real inference — match how phase7/phase8 run-path tests start a job without a live model).

The test module MUST assert:

1. **`test_run_path_no_longer_quota_gated`** — A FREE account that has already exhausted its lifetime report quota (drive it past `monthly_report_quota=2` lifetime, e.g. create ≥2 prior runs for its company via `metrics_store.create_run` / the same mechanism phase7 uses) can STILL start a run: `POST /api/run` (with a valid `goal` and a positive balance so the wallet gate passes) returns **200** (or whatever the success shape is — a `job_id`), NOT 402. This is the core behavior change.
2. **`test_run_path_still_requires_login`** — `POST /api/run` with NO session returns **401** (auth untouched).
3. **`test_run_path_still_respects_wallet_balance`** — Confirm the ¥ wallet/balance path is intact. Pick the assertion that matches the post-change reality: with `_plan_gate` gone, the run path no longer enforces a free-account "no_balance" 402 at the gate, but the ¥ wallet is still decremented per run via `record_usage` / `token_manager` and `platform_guard` still enforces the per-account daily cap. Assert that a started run records ¥ cost against the account wallet (balance changes / a run row carries `total_cost_yuan`) — i.e. accounting still fires. If after removal there is genuinely no remaining pre-run balance refusal on `/api/run`, assert that explicitly (a zero-balance free account now reaches job creation) AND that the wallet accounting still runs, so the test documents the new contract rather than asserting a behavior that no longer exists.
4. **`test_run_path_still_breaker_gated`** — Trip `platform_guard` (set the platform spend cap so `platform_guard.check()` returns not-allowed, or call its trip/force-open path the way `tests/test_phase8_security.py` / `test_redesign_phase8.py` do), then assert `POST /api/run` returns **503** with the breaker message. This proves the breaker still guards the run path after the quota gate is gone.
5. **`test_plan_gate_still_defined_and_callable`** — Import `app._plan_gate` and assert it is still a callable function (guards against someone deleting it as "dead code"). Optionally call it on a seeded free/exhausted account and assert it still returns a `(False, msg, "quota")`-shaped tuple — proving the scheduled/import paths' dependency is intact even though `/api/run` no longer calls it.

### Step 5 — Migrate the now-obsolete assertions in `tests/test_redesign_phase7.py`

The existing phase-7 tests that assert `/api/run` returns **402** on quota exhaustion / spent free balance (the Part B "B8 billing realignment" and Part C "B4 ¥29 credit" tests, roughly lines 222–453) will now FAIL because the run path no longer 402s. You must reconcile them — do NOT just delete the suite:

- For tests whose ASSERTION TARGET was specifically the **`/api/run` (or `/api/onboarding/generate`) quota/credit 402 behavior** that you just removed: update them to reflect the new contract. The cleanest approach is to re-point those quota/credit assertions at a surface that still owns plan/quota semantics — call `app._plan_gate(customer)` directly (it is still defined and still implements the quota/credit math), and assert the `(allowed, msg, code)` tuple there, instead of asserting an HTTP 402 from `/api/run`. This preserves the B8/B4 quota+credit regression coverage at the function that actually still implements it, while acknowledging the run path no longer enforces it.
- For tests asserting credit grant/consume/refund **accounting** (`report_credits` increments on a paid `single_report` order, decrements on refund) that do NOT depend on the run-path 402: keep them as-is if they still pass; they exercise the payment/credit ledger, not the run gate.
- Add a short module-level comment in the phase-7 file noting that the run-path 402 quota gate was retired in this workstream and the quota/credit assertions were re-pointed at `_plan_gate` directly.

Run `tests/test_redesign_phase7.py` and `tests/test_scheduler_delivery.py` after your changes and make BOTH green — the scheduler suite must still see the `locked`/`no_balance`/`quota` skip statuses, which proves you did not break the still-live `_plan_gate` callers.

---

## Invariants to respect (do not violate)

- **China domestic mode** must keep working: do not introduce any hard Anthropic dependency, and do not touch `effective_model()` / `ModelRouter` / `TEAM_MODEL_PRESET` routing. This workstream is gate-only.
- **No model keys / per-token ¥ on the customer surface**: do not surface model ids, token counts, or per-token pricing anywhere. You are removing a *flat report-quota* gate, not adding any new pricing UI.
- **¥ cost-control survives**: `platform_guard.check` + `record_platform_spend` (the breaker + per-account daily cap) and `token_manager` per-run budgets (`config.COST_BUDGETS_CNY`, `ensure_can_spend`, `record_cost`) and the row-locked ¥ wallet in `customers.py` must remain fully intact and still fire on every run. The breaker block (`platform_guard.check(...) → 503`) stays in `/api/run` and `/api/onboarding/generate` exactly where it is.
- **Founder-gating** stays server-side and inline (`if not _is_admin(...): return 403`). Do not touch the admin gate on the Fusion endpoint (and do not remove its `_plan_gate` call).
- **Files-encrypted-at-rest (Fernet)** and **SSRF egress lock** are unaffected — don't touch them.
- **Honesty rule**: no change to deliverables/label-demo-vs-real behavior.
- **Brand = Tangent AI** — any new comment copy that references the product uses "Tangent Agent" / "Tangent AI".

---

## Acceptance criteria

1. `grep -n "_plan_gate" app.py` shows the function still DEFINED and still called at the scheduled-worker (~1116), scheduled-run-create (~3624), brain-import (~4177), and fusion (~4438) sites — but NOT inside `def run()` (`/api/run`) or `def onboarding_generate()`.
2. A free, quota-exhausted, signed-in account can start `/api/run` and get a `job_id`/200 (no 402). Proven by `tests/test_run_gate_removal.py::test_run_path_no_longer_quota_gated`.
3. `/api/run` still returns **401** without a session and **503** when `platform_guard` is tripped. Proven by the new test.
4. ¥ wallet accounting + `platform_guard` still fire on a started run (assert recorded ¥ cost / balance decrement). Proven by the new test.
5. `config.PLANS`, `config.plan()`, `config.plan_has()` are unchanged and still present.
6. `tests/test_redesign_phase7.py` and `tests/test_scheduler_delivery.py` are GREEN after migrating the obsolete run-path-402 assertions (re-pointed at `_plan_gate` directly, not deleted).
7. The FULL suite passes: from the repo root run `.venv/bin/python -m pytest -q` (or `python -m pytest`) and confirm no regressions. The suite is ~191–202 tests; it must stay green.

Run the new file specifically: `.venv/bin/python -m pytest tests/test_run_gate_removal.py -v`.

---

## Explicitly OUT OF SCOPE

- Do NOT touch the scheduled-delivery gate (`_plan_gate` at ~1116 / ~3624), the history-import gate (~4177), or the Fusion founder gate (~4438). They legitimately still need plan/quota/feature semantics.
- Do NOT delete or restructure `config.PLANS`, `PURCHASABLE_PLANS`, `SINGLE_REPORT_PRICE_YUAN`, `plan_has`, or the `report_credits` / single-report-credit payment ledger. The credit ledger and the plans/subscription pages stay.
- Do NOT modify `platform_guard.py`, `customers.py`, `token_manager`, `providers.py`, `effective_model`, or any model-routing / domestic-mode code.
- Do NOT build the local daemon, device tokens, `/api/devices/*`, the agent catalog, the 0007 migration, or any of the desktop-product surface. This workstream is ONLY the run-path pricing removal + regression test; the device-run path is a downstream workstream that will benefit from this gate being gone but is not built here.
- Do NOT add new migrations or schema changes (head stays 0006; no DB columns change).
- Do NOT add new pricing UI or remove the existing plans/pricing pages — they are read-only consumers of `PLANS` and stay.
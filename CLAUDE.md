# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Tangent AI — a China-ready SaaS that **sells finished business reports**, not AI tools.
A boss uploads messy Excel and a multi-agent "AI team" produces a one-page,
source-verified 经营周报 (weekly business report), delivered on a schedule. The
customer never writes a prompt, never picks a model, never sees a token. There are
**two surfaces**:

- **Fusion** (`/`) — public, no-login, multi-model chatbot: one question fans out to
  several models and one fused answer is reconciled from them. Model names are shown
  here (the open front door), bounded by per-IP rate limits + platform spend caps.
- **工作流** (sign-in required; the customer nav tab — formerly "Agents") — the
  report/analytics product: the AI team that turns Excel into a verified weekly report,
  delivered on a schedule. The whole flow is now ONE consolidated page, **数据分析+报告
  生成** (see Frontend below).

A newer direction (see memory + `docs/local-agents/`) is **locally-deployable agents**:
the same Flask app runs as a loopback desktop daemon (`tangent-agent`), inference
proxied through the cloud `/v1` gateway via per-device tokens. This is **backend +
daemon only**: the customer-facing UI for it (an 智能体市场 marketplace + a 桌面版与设备
device-pairing page) was built then **removed**, so there is currently NO customer UI —
the catalog/device endpoints (`/api/agents/catalog`, `/api/devices/*`) and
`agent_team/runner/` remain. Packaging/signing steps: `docs/desktop/DEPLOY.md`.

## Commands

```bash
# Setup (once)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # paste a real ANTHROPIC_API_KEY (or use China mode below)

# Run the web app (dev: SQLite, console SMS, mock payments — all zero-config)
python app.py                 # http://localhost:8000  (PORT=8001 python app.py to move it)

# Tests — the canonical command (no network, no real money; model calls faked but billed)
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest tests/test_phase9_payments.py            # one file
.venv/bin/python -m pytest tests/test_phase9_payments.py::test_name # one test
.venv/bin/python -m pytest -k fusion                               # by keyword

# Desktop daemon (local-agent path; installs the `tangent-agent` console script)
pip install -e .              # then: tangent-agent   (loopback server + tray icon)

# Production
docker compose up -d          # nginx → 4 gunicorn workers + scheduler worker + Postgres 16 + redis
alembic upgrade head          # migrations 0001–0010 (head = 0010_ar)
```

There is **no configured linter/formatter** (no ruff/black/flake8 config); match the
surrounding style. `pytest` is installed into the venv, not pinned in `requirements.txt`.

## Architecture

### The two big files
- **`app.py`** (~5300 lines, 110 routes) — the entire Flask web layer: page routes, the
  customer + founder `/api/*` JSON API, `/admin*` ops pages, payment notify webhooks,
  the Fusion endpoints, and the `/v1/*` OpenAI-compatible gateway. Jobs run in
  **background threads** (`JOBS` / `IMPORT_JOBS` dicts hold live in-memory `Memory`
  objects keyed by `job_id`); the live "watch the team work" feed polls `/api/status`.
- **`agent_team/`** — the engine, one concern per module. `config.py` (~66 KB) is the
  control panel: the **org chart** (`AGENTS`: ceo → doc_analyst/data_analyst/
  report_writer/media_producer/file_manager), model IDs, `PLANS`, pricing, product
  identity, and **every env knob** (each as a function like `database_url()`,
  `admin_emails()`, `platform_hourly_cap_yuan()`).

### The agent run (the core flow)
`agent_team/orchestrator.py` is the CEO: **PLAN → DELEGATE → SYNTHESIZE**. The CEO
writes a delegation plan, each specialist runs in turn reading shared `Memory`
(`memory.py`) so it builds on teammates' output, then the CEO assembles the final
deliverable. Everything streams to shared memory, which is what the live feed reads.

Two rules the agents must not break, enforced outside the LLM:
- **Trends are deterministic.** `trends.py` computes 环比/同比, streaks, and 2σ anomalies
  in pure Python and injects them under a verbatim "PRE-COMPUTED … MUST NOT recompute"
  header. The LLM never does arithmetic on metrics.
- **Every figure is traceable.** `verification.py` requires each number to carry a 【N】
  anchor backed by a 数据来源与核验 source table, with an auto-audit.

### Models & providers
`providers.py` → `ModelRouter` is a drop-in LLM client reaching every provider in the
catalog (Claude natively; OpenAI/Gemini/Grok/DeepSeek/Qwen/etc. via one
OpenAI-compatible adapter). All agents share one router instance. `RemoteRouter`
subclasses it to proxy inference through the cloud `/v1` gateway with a device token
(the desktop daemon's path). Cost is tallied per call in ¥ and billed to the account
via a `cost_listener` wrapper + `token_manager.py` (the CFO; enforces ¥ budgets and
aborts a run with `BudgetExceededError` before overspend).

Each non-Anthropic provider reads its key from the env var named in
`PROVIDERS[...]["env_key"]` — **the name must match exactly** (Qwen=`DASHSCOPE_API_KEY`
NOT `QWEN_API_KEY`; Kimi=`MOONSHOT_API_KEY` NOT `KIMI_API_KEY`). Its base_url is
**env-overridable** via `<PREFIX>_BASE_URL` (the `*_API_KEY` name with `_API_KEY` →
`_BASE_URL`) read by `config.provider_base_url()`: the catalog ships INTERNATIONAL
endpoints, so a mainland-console key 401s until you point it at the `.cn` host
(`DASHSCOPE_BASE_URL`, `MOONSHOT_BASE_URL`). The OpenAI-compatible adapter
(`_openai_chat`) strips a leading `<think>…</think>` block from replies so inline-CoT
reasoning models (e.g. `MiniMax-M3`) don't leak chain-of-thought into a deliverable.

### The company brain (persistence)
`db.py` + `models.py` (SQLAlchemy) + `metrics_store.py` are the per-account "企业大脑":
every metric from every run is persisted so later runs compare against history. Key
tables: `Company`, `Run`, `Metric`, `CompanyProfile`, `GlossaryTerm`,
`ReportPreference`, `ScheduledJob`, `Delivery`, `Feedback`, `Account`, `Device`,
`AgentCatalog`/`AgentInstall`/`AgentRun` (marketplace), `Order` (payments), audit/spend/
breaker tables. Schema changes go through **Alembic** (`migrations/versions/`,
`alembic upgrade head`) — do not rely on `create_all` for production columns.

### Frontend
`frontend/` is a vanilla-JS ES-module SPA (no framework/build step): `index.html` +
`style.css` + `js/` (router + nav live in `js/main.js`). `js/views/` are customer pages;
`js/founder/` are the founder-gated machinery views (team/models/costs/fusion/unified).
Served directly by Flask. The report product's pages are consolidated behind ONE host,
`js/views/workflow.js` — the **工作流 / 数据分析+报告生成** page — which mounts each
existing view module (workbench / report_new / reports / schedule / brain / account) into
an in-page tab, one at a time; the legacy routes (`/agents` `/new` `/reports` `/schedule`
`/brain` `/account`) still resolve, each pre-selecting its tab.

### Other notable modules
`industry_packs.py` + `templates_industry/*/pack.yaml` (editable per-industry KPI/section
packs) · `pdf_report.py` (branded CJK PDF) · `delivery.py` + `scheduler.py` (email/企业微信,
cron in Asia/Shanghai) · `payments.py` + `reconciliation.py` (Alipay RSA2 / WeChat APIv3 /
Mock provider) · `crypto.py` (Fernet encryption at rest) · `sms.py` `security.py`
`platform_guard.py` `alerts.py` (Phase 8 hardening) · `onboarding.py` `helpdocs.py`
`lifecycle.py` (Phase 10) · `deliverables.py` `devices.py` `workspace.py` `runner/`
(local-agent / marketplace).

### Two customer workflows that clone one shape (deterministic engine + 1-LLM writer)
Both are linear (NOT `Orchestrator` subclasses), keep their writer OUT of `AGENTS`, inject
verbatim 【N】-anchored engine facts, fold into the report quota (`QUOTA_RUN_KINDS`), and are
cloud-only (`_LOCAL_MODE_BLOCKED_PREFIXES`):
- **对账 / 平台结算核对** — `settlement_engine.py` + `settlement_orchestrator.py` +
  `settlement_templates.py` (`templates_platforms/*/pack.yaml`); routes `/api/recon/*`;
  view `recon.js`; tables `settlement_reports`/`settlement_exceptions`.
- **应收账款管理 + 催收 (AR)** — a NEW, additive sibling (2026-06-19), 对账 untouched:
  `ar_aging_engine.py` (payment→invoice application → aging buckets → DSO → 确定/疑似
  grading + bridge self-audit) + `dunning.py` (deterministic 催收 tier logic + no-double-dun
  re-run classifier) + `ar_orchestrator.py` + `ar_templates.py` (`templates_ar/*/pack.yaml`);
  routes `/api/ar/*`; view `ar.js`; tables `ar_reports`/`ar_flags`/`dunning_messages`.
  催收 drafts are NEVER auto-sent — only the human `/api/ar/dunning/<id>/send` route reaches
  'sent'. A future "workflow chassis" will extract what 对账 + AR share (CHASSIS-CANDIDATE).

## Conventions & gotchas

- **Founder-gating is server-side, by policy.** Customer surfaces must never expose
  models, tokens, or per-token ¥. The whole "machinery" (39-model storefront,
  `/api/tokens*`, `/api/models`, `/api/providers`, Fusion admin, `/admin*`) is gated by
  `_is_admin(customer)` (email in `ADMIN_EMAILS`) and returns 403 to everyone else.
  When adding machinery endpoints, gate them the same way — do not assume the hidden UI
  is sufficient.
- **Pricing is flat, never per-token.** Plans: 免费体验 2 份 / 单次 ¥29 / 基础版 ¥199/月 /
  企业版 ¥599/月. Subscribers are **quota-gated, not balance-gated**. Token ¥ is internal
  COGS only.
- **China / domestic mode.** Set `TEAM_MODEL_PRESET=domestic` + `DEEPSEEK_API_KEY` to run
  the entire team and all utility calls (metric extraction, audit, Fusion fuser) on a
  domestic model with **no Anthropic key** — the router builds its Claude client lazily.
  See `config.team_model_preset()` / `model_for(...)`. For a **heterogeneous per-agent
  mix** instead of one model, set per-agent overrides (团队与模型 UI → persisted to
  `memory_store/model_overrides.json`; `config.set_model_override` / `effective_model`,
  an override WINS over the preset) and move the utility calls off Claude with
  `EXTRACTION_MODEL` / `AUDIT_MODEL`. The current deployment runs deepseek-v4-pro
  (ceo/data_analyst) / qwen3.7-max (document_analyst/report_writer) / MiniMax-M3
  (records_manager/media_producer). Mainland keys need the `*_BASE_URL` endpoint
  overrides (see Models & providers), and each provider account must complete **实名认证**
  before its API will bill (a positive balance alone → `402 insufficient_balance`).
- **`LOCAL_MODE`** (set by the desktop daemon) hardens the loopback server: blocks
  admin/payment route prefixes, injects a per-launch CSRF token the SPA echoes on
  mutating calls. Entirely inert when off — never special-case it on the cloud path.
- **Tests are hermetic.** `tests/conftest.py` initializes flask-limiter then disables it,
  and no-ops `app.load_dotenv` so the developer's personal `.env` can't leak into the
  suite. Don't write tests that depend on the local `.env` or hit the network.
- **Local production guard.** Keep `APP_ENV=production` commented out locally — an empty
  `SECRET_KEY` refuses to boot.
- Handlers call `load_dotenv(override=True)` per request so live `.env` edits (e.g.
  newly added provider keys) apply without a restart.

## Where to read more

- `README.md` — full feature tour, the phase-by-phase build record, and a troubleshooting
  table (start here for any "how do I run X" question).
- `docs/ops.md` — the production runbook (deploy, backup/restore, monitoring, spend caps).
- `docs/product-brief.md` — what is sold and the three moats; `docs/ui-redesign-plan.md`
  — the customer-UI redesign plan + per-phase record.
- `docs/local-agents/` — the locally-deployable-agents plan and coding-agent prompts;
  `docs/desktop/DEPLOY.md` — the foolproof desktop packaging/signing guide (路线 A/B/C).
- `CHANGELOG.md` — rendered at `/changelog`; drives the 新功能 badge.

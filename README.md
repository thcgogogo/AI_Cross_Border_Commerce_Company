# AI Agent Team — 你的 AI 经营分析师

An **extensible "AI company"** that turns your business data into verified Chinese
operating reports — and a complete, China-ready SaaS around it.

A CEO agent plans each job and delegates to specialists (Document Analyst, Records
Manager, Data Analyst, Report Writer, Media Producer) who share one working memory.
Built up over ten phases, the product now: **remembers your company** (a per-account
企业大脑), computes **trends deterministically** (no LLM arithmetic), makes **every
number traceable** to its source, tailors reports by **industry template packs**,
**shows up on a schedule** (branded CJK PDF → email/企业微信), and runs as a real
business — SMS-verified signups, **Alipay/WeChat Pay** top-ups & subscriptions,
fapiao, a 5-step **onboarding wizard**, and a founder **ops dashboard**.

It also doubles as a **unified multi-model API storefront**: customers bundle any of
39 models across 11 providers (Claude native; OpenAI, Google, DeepSeek, Qwen, Kimi…
via one adapter) into one key behind an OpenAI-compatible endpoint. Every use —
agent runs, Fusion, the gateway — bills the same per-account **¥ balance**.

---

## Quick start

**You need:** Python 3.9+ and an Anthropic API key
(create one at https://console.anthropic.com/settings/keys — treat it like a password).

**Set up (once):**
```bash
cd /Users/brucetang/Company_Agent_Demo
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
open -e .env        # paste your real key over sk-ant-paste-your-key-here, save
```

**Run (every time):**
```bash
source .venv/bin/activate   # if this Terminal is fresh
python app.py
```
Open **http://localhost:8000**. Press **Control+C** in the Terminal to stop.

**First use:** click **Sign up** (top-right). Phone verification is on by default —
in local dev the SMS code is printed in the Terminal. New accounts start with
**¥200** demo credit, and the **新手引导** wizard opens automatically:

1. 公司信息 — pick your industry (selects the report template pack)
2. 上传一份数据文件 — or click **用示例数据体验** (a bundled 12-week dataset; no file needed)
3. KPI 列名确认 — guesses are pre-filled from your file's headers; confirm in one click
4. 生成第一份报告 — watch the live agent feed; read/download the branded PDF
5. 每周定时 — one click schedules every Monday 08:00 + email/企业微信 delivery

Everything else lives in **Menu** (top-left): Chat bot (the task form), Fusion,
Your Team (models + the ¥ Token Manager), Models & Pricing, Get Unified API
(dashboard, top-up, 订阅套餐, 订单与发票), and 企业大脑 (profile, KPI map, glossary,
preferences, 定时任务, report history). In-app docs: **/help** · updates: **/changelog**
· questions: the floating **联系支持** button.

---

## The ten build phases

| Phase | What it added | Where to see it |
|---|---|---|
| **1** | The **company brain** — every metric from every run is persisted per account | `data/company_brain.db` (auto-created) |
| **2** | Deterministic **trend engine**: 环比/同比, streaks, 2σ anomalies — pure Python, never LLM guesses | quoted verbatim inside every report |
| **3** | **Editable brain + learning loop**: profile, KPI column mappings, glossary, standing preferences, `UNKNOWN_TERMS` confirm, paid history import | Menu → **企业大脑** |
| **4** | **Industry packs** — required KPIs, 行业经验值 benchmarks, fixed section order, as editable YAML | `templates_industry/*/pack.yaml` + task-form dropdown |
| **5** | **Scheduled reports** (cron, Asia/Shanghai), branded **CJK PDF**, **email/企业微信 delivery**, balance-gated fires | 企业大脑 → 定时任务 / 报告历史 / 品牌 |
| **6** | **Verification trail** — every figure carries a 【N】 anchor backed by a 数据来源与核验 table + auto-audit; 👍/👎 **feedback loop** | the result page, the PDF, `/admin/feedback` |
| **7** | **Production deployment**: Postgres + Alembic migrations, Docker Compose (nginx/gunicorn/scheduler worker), `/healthz`, JSON logs, backups | `docker-compose.yml`, `migrations/`, [docs/ops.md](docs/ops.md) |
| **8** | **Security & abuse hardening**: SMS-code signup/reset, rate limits, per-key RPM, platform **spend circuit breaker** + alerts, **encryption at rest** (Fernet), audit log, secret-leak boot scan | `/admin/circuit-breaker`, docs/ops.md §7 |
| **9** | **Payments, subscriptions & fapiao**: Alipay (RSA2) / WeChat Pay (APIv3) with signature-first idempotent notifies, plans + quotas, user-confirmed renewals, admin refunds, nightly bill **reconciliation**, 发票 workflow — a Mock provider keeps it all runnable offline | dashboard 充值/订阅套餐/订单与发票; `/admin/orders`, `/admin/fapiao`, `/admin/reconciliation` |
| **10** | **Onboarding, support & ops**: the 5-step wizard (funnel-tracked, skippable, resumable), `/help` docs + 联系支持, `/changelog` + 新功能 badge, lifecycle emails (welcome / day-3 nudge / receipts), founder **ops dashboard** — signups, activation, retention cohorts, MRR, churn, per-account margin | the wizard on first login; `/help`, `/changelog`, **`/admin`** |

**Definition of done (Track 1), met:** a stranger can sign up, verify a phone, pay
with Alipay/WeChat Pay, request a fapiao, and receive a scheduled branded report in
WeChat every Monday — on a server restorable from last night's backup — while you
watch activation, MRR, churn, and per-account margin on one admin page.

---

## Production deployment

Local dev needs nothing beyond the Quick start (SQLite, console SMS, mock payments —
all zero-config). For a real server:

```bash
docker compose up -d      # nginx → gunicorn app + scheduler worker + PostgreSQL 16
```

Set the production env (`SECRET_KEY`, `DATABASE_URL`, `ADMIN_EMAILS`, `SMTP_*`,
`FERNET_KEYS`, payment merchant keys, spend caps — all documented in
[.env.example](.env.example)), run `alembic upgrade head`, and schedule
`scripts/backup.sh`. The full runbook — deploy, backup/restore, monitoring, and the
per-phase ops notes — is **[docs/ops.md](docs/ops.md)**.

---

## Tests

```bash
.venv/bin/python -m pytest -q     # 50 passed — acceptance tests for every phase
```

Each phase ships its own acceptance suite (`tests/test_phase8_security.py`,
`test_phase9_payments.py`, `test_phase10_onboarding.py`, …). No test hits the
network or spends real money — model calls are faked but **billed through the real
per-account path**, and payment tests use the HMAC-signed Mock provider.

---

## Controlling cost

- Every agent defaults to **Claude Opus** (best quality). Switch analysts to
  `SONNET` or `HAIKU` in `agent_team/config.py` — or live from Menu → Your Team —
  to cut spend substantially.
- The **Token Manager** (Your Team tab) enforces overall and per-task **¥ budgets**
  and stops a run before it can overshoot; account balances do the same per customer.
- Plans cap reports per month (free 10 / 基础 100 / 专业 500); the Phase 8 platform
  caps (`PLATFORM_HOURLY_CAP_YUAN` …) are the upstream kill-switch.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `No ANTHROPIC_API_KEY found` | Create `.env` from `.env.example` and paste your real key. |
| `401` invalid key | An old key/redirect in your shell can shadow `.env`: `unset ANTHROPIC_API_KEY`, and note the app auto-ignores a non-Anthropic `ANTHROPIC_BASE_URL`. No quotes/spaces around the key. |
| Signup asks for an SMS code | In local dev the code prints in the Terminal (console SMS provider). Set `PHONE_VERIFICATION=0` to disable entirely. |
| Run "stopped early" | A ¥ budget or your account balance ran out — raise it (Your Team tab) or top up. |
| Port 8000 busy | `PORT=8001 python app.py` |
| `ModuleNotFoundError` | `source .venv/bin/activate` then `pip install -r requirements.txt`. |
| Scheduled job didn't fire | The app (or the scheduler container) must be running; check cron validity, ¥ balance, plan quota, and that the timezone is Asia/Shanghai. |
| Chinese PDF shows □□□ | Restore the bundled `assets/fonts/NotoSansSC-Regular.ttf`. |
| Email not sending | Set the `SMTP_*` vars in `.env`; unset SMTP is skipped on purpose. |
| `/admin` says 403 | Add your login email to `ADMIN_EMAILS` in `.env`. Non-admins always get 403. |

---

## Project layout

```
Company_Agent_Demo/
├── app.py                  ← the website + API + admin pages (run this)
├── scheduler.py            ← standalone cron worker (production; in-process in dev)
├── agent_team/
│   ├── config.py           ← the ORG CHART + models + plans + all env knobs
│   ├── orchestrator.py     ← the CEO: plan → delegate → synthesize → verify
│   ├── base_agent.py / llm.py / providers.py / memory.py / file_tools.py
│   ├── token_manager.py    ← the CFO (¥ budgets)   customers.py ← accounts & balances
│   ├── db.py / models.py / metrics_store.py        ← the company brain (Phase 1+)
│   ├── trends.py(2) industry_packs.py(4) pdf_report.py / delivery.py / scheduler.py(5)
│   ├── verification.py(6)  crypto.py / sms.py / security.py / platform_guard.py / alerts.py(8)
│   ├── payments.py / reconciliation.py(9)
│   └── onboarding.py / helpdocs.py / lifecycle.py(10)
├── admin/queries.py        ← the ops dashboard's SQL (unit-tested)          (10)
├── migrations/             ← Alembic 0001–0004 (`alembic upgrade head`)      (7)
├── templates_industry/     ← industry packs as editable YAML                 (4)
├── docs/ops.md             ← the operations runbook   docs/help/ ← /help docs (10)
├── assets/                 ← bundled CJK font + 示例销售数据.csv
├── frontend/               ← the SPA (wizard, dashboard, billing, 企业大脑)
├── tests/                  ← per-phase acceptance suites (pytest)
├── CHANGELOG.md            ← rendered at /changelog (drives the 新功能 badge)
└── data/ uploads/ memory_store/  ← your local state (git-ignored)
```

**Privacy & safety:** everything runs on your machine/server. Uploads and report
PDFs are encrypted at rest; passwords and API keys are stored only as salted
hashes; SMS codes only as hashes; SMTP credentials and webhook URLs are never
logged; downloads require login + ownership checks; and your data is used only to
generate your own reports — never for training, never shown to other accounts.

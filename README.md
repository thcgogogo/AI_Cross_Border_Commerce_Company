# Tangent AI — 你的 AI 经营分析团队

**We sell finished business reports, delivered automatically — not AI tools, not
a platform, not API access.** A boss uploads messy Excel (or doesn't even do
that); every Monday 08:00 a one-page, source-verified 经营周报 arrives in
企业微信 and email, branded with their logo. The customer never writes a prompt,
never picks a model, never sees a token.

Inside, an extensible multi-agent "AI team" does the work: a CEO agent plans
each job and delegates to specialists (文档分析师, 资料管理员, 数据分析师,
报告撰写人, 媒体制作人) who share one working memory. The product
**remembers your company** (a per-account 企业大脑), computes **trends
deterministically** (no LLM arithmetic — "华东区已连续三周下滑，环比 -12.4%"
comes from a real engine), makes **every number traceable** to its source file
and rows, tailors reports by **industry template packs**, and runs as a real
China-ready SaaS — SMS-verified signups, **Alipay/WeChat Pay** subscriptions,
fapiao, a 6-step onboarding wizard, and a founder ops dashboard.

**Pricing is flat, never per-token** (免费体验 2 份 / 单次 ¥29 / 基础版 ¥199/月 /
企业版 ¥599/月). Token costs are internal COGS, watched by a founder-only cost
console — the customer-facing promise is 成本透明，绝不超支.

---

## Two surfaces: Fusion (home) + Agents

The home page (`/`) is **Fusion** — a public, no-login, multi-model chatbot: ask once,
several models answer, and one fused answer is reconciled from them (when one model is
wrong, the others catch it). It's the open front door; model names are shown, anonymous
visitors can try it, and it's bounded by a per-IP rate limit + the platform spend caps
(see [docs/ops.md](docs/ops.md) §7.7 — **the server pays, so set the caps before going
public**).

The report/analytics product described below now lives under the **Agents** section
(sign-in required): the AI team that turns your Excel into a source-verified weekly
经营周报. Everything in "The build record" and the moats applies to Agents.

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

**First use:** click **免费体验** (top-right) and sign up. Phone verification is
on by default — in local dev the SMS code is printed in the Terminal. New
accounts get **2 free reports** (an invisible internal cost cap protects the
platform; the customer never sees a ¥ balance), and the **新手引导** wizard
opens automatically:

1. 公司与行业 — pick your industry (selects the report template pack)
2. 上传数据 — or click **用示例数据体验** (a bundled 12-week dataset; no file needed)
3. 确认指标 — KPI column guesses are pre-filled from your file's headers
4. 导入历史数据 — optional: import 6–12 months of old spreadsheets so the very
   first report already has 与历史对比 trend context
5. 生成第一份报告 — watch the live agent feed; read/download the branded PDF
6. 开启每周自动送达 — 企业版: one click schedules every Monday 08:00 +
   email/企业微信 delivery (other plans see the ritual preview + upgrade path)

**Customer navigation:** 工作台 (next delivery countdown, latest report, quota,
data inbox) / 生成报告 / 历史报告 / 定时与送达 / 企业大脑 (profile, KPI map,
glossary, preferences, history import) / 账户与套餐 (plans, orders, fapiao).
In-app docs: **/help** · updates: **/changelog** · questions: the floating
**联系支持** button.

**Founder mode (founder/admin accounts only):** accounts listed in
`ADMIN_EMAILS` additionally see an **内部工具** nav section — 团队与模型
(per-agent model assignment), 成本控制台 (the ¥ Token Manager), 模型与定价,
Fusion, and 开发者接口. The last one is the legacy **unified multi-model API
storefront** (39 models, 11 providers behind one OpenAI-compatible key): the
code and `/v1/*` endpoints stay live, but the UI and every machinery API
(models, providers, pricing, token budgets) are founder-gated server-side —
customers can't reach them, by policy and by 403. The 运营后台 (`/admin*`)
pages hang off the same section.

---

## The build record

| Phase | What it added | Where to see it |
|---|---|---|
| **1** | The **company brain** — every metric from every run is persisted per account | `data/company_brain.db` (auto-created) |
| **2** | Deterministic **trend engine**: 环比/同比, streaks, 2σ anomalies — pure Python, never LLM guesses | quoted verbatim inside every report |
| **3** | **Editable brain + learning loop**: profile, KPI column mappings, glossary, standing preferences, `UNKNOWN_TERMS` confirm, history import | 企业大脑 |
| **4** | **Industry packs** — required KPIs, 行业经验值 benchmarks, fixed section order, as editable YAML | `templates_industry/*/pack.yaml` |
| **5** | **Scheduled reports** (cron, Asia/Shanghai), branded **CJK PDF**, **email/企业微信 delivery** | 定时与送达 |
| **6** | **Verification trail** — every figure carries a 【N】 anchor backed by a 数据来源与核验 table + auto-audit; 👍/👎 **feedback loop** | the report view, the PDF, `/admin/feedback` |
| **7** | **Production deployment**: Postgres + Alembic migrations, Docker Compose (nginx/gunicorn/scheduler worker), `/healthz`, JSON logs, backups | `docker-compose.yml`, `migrations/`, [docs/ops.md](docs/ops.md) |
| **8** | **Security & abuse hardening**: SMS-code signup/reset, rate limits, platform **spend circuit breaker** + alerts, **encryption at rest** (Fernet), audit log, secret-leak boot scan | `/admin/circuit-breaker`, docs/ops.md §7 |
| **9** | **Payments, subscriptions & fapiao**: Alipay (RSA2) / WeChat Pay (APIv3) with signature-first idempotent notifies, plans + quotas, user-confirmed renewals, admin refunds, nightly bill **reconciliation**, 发票 workflow — a Mock provider keeps it all runnable offline | 账户与套餐; `/admin/orders`, `/admin/fapiao`, `/admin/reconciliation` |
| **10** | **Onboarding, support & ops**: the onboarding wizard (funnel-tracked, skippable, resumable), `/help` docs + 联系支持, `/changelog` + 新功能 badge, lifecycle emails, founder **ops dashboard** | the wizard on first login; `/help`, `/changelog`, **`/admin`** |

After Track 1, a **customer-facing UI redesign** (10 more phases, June 2026)
rebuilt the product around the report-product positioning in
[docs/product-brief.md](docs/product-brief.md): the light "business document"
theme, the zero-prompt 生成报告 flow, the branded mobile web report, the visible
企业大脑 (stats + KPI sparklines), flat plans (B8: subscribers are quota-gated,
never balance-gated), 单次 ¥29 purchases, the 6-step wizard with history
import, and server-side founder-gating of every machinery endpoint. The full
plan and per-phase record: [docs/ui-redesign-plan.md](docs/ui-redesign-plan.md).

**Definition of done, met:** a stranger can sign up, verify a phone, complete
the wizard with 示例数据 and receive a finished, source-verified report without
typing a sentence; pay with Alipay/WeChat Pay; request a fapiao; and get a
scheduled branded report in WeChat every Monday — on a server restorable from
last night's backup — while the founder watches activation, MRR, churn, and
per-account margin on one admin page, with zero machinery (models, tokens,
per-token ¥) visible to any customer surface.

---

## Production deployment

Local dev needs nothing beyond the Quick start (SQLite, console SMS, mock payments —
all zero-config). For a real server:

```bash
docker compose up -d      # nginx → gunicorn app + scheduler worker + PostgreSQL 16
```

Set the production env (`SECRET_KEY`, `DATABASE_URL`, `ADMIN_EMAILS`, `SMTP_*`,
`FERNET_KEYS`, payment merchant keys, spend caps — all documented in
[.env.example](.env.example)), run `alembic upgrade head` (0001–0006), and schedule
`scripts/backup.sh`. The full runbook — deploy, backup/restore, monitoring, and the
per-phase ops notes — is **[docs/ops.md](docs/ops.md)**.

The compose stack runs **4 gunicorn workers behind nginx, plus a redis service** so
every rate limit (per-IP auth, per-account runs, per-key API) is counted **once
globally** instead of ~4× per worker. Set **`TRUST_PROXY`** to the number of proxy
hops in front (compose nginx = `1`) so per-IP limits and the audit log see the real
client IP, not the proxy's — both are wired by default in `docker-compose.yml`.
Leave it unset when the app is directly exposed (see [docs/ops.md](docs/ops.md) §7.6).

---

## Running in mainland China (DeepSeek — no Anthropic key)

The AI team defaults to Claude. For a mainland deployment — where `api.anthropic.com`
isn't reliably reachable and a foreign LLM raises 生成式AI 算法备案 compliance issues —
flip the **whole team and its internal calls onto DeepSeek V4** with two lines in `.env`:

```bash
TEAM_MODEL_PRESET=domestic     # every agent + metric extraction + the audit → deepseek-v4-flash
DEEPSEEK_API_KEY=sk-...        # the only model key you then need
```

No `ANTHROPIC_API_KEY` is required — the model router builds its Claude client lazily
and never touches it. Want Qwen instead, or `deepseek-v4-pro` for the report writer?
See [docs/launch-checklist.md](docs/launch-checklist.md) §8.

---

## Tests

```bash
.venv/bin/python -m pytest -q     # 209 passed — acceptance tests for every phase
```

Each phase ships its own acceptance suite (`tests/test_phase8_security.py`,
`test_phase9_payments.py`, `test_redesign_phase5.py` … `test_redesign_phase10.py`).
No test hits the network or spends real money — model calls are faked but **billed
through the real per-account path**, and payment tests use the HMAC-signed Mock
provider.

---

## Controlling cost (founder concerns — invisible to customers)

- Every agent defaults to the strongest model (best quality). Reassign analysts
  to cheaper tiers in `agent_team/config.py` — or live from 内部工具 → 团队与模型 —
  to cut spend substantially.
- The **成本控制台** (内部工具) enforces overall and per-task **¥ budgets** and
  stops a run before it can overshoot; free-trial accounts carry an invisible
  internal cost cap.
- Plans cap reports (免费体验 2 份终身 / 基础版 20/月 / 企业版大额度); the
  platform caps (`PLATFORM_HOURLY_CAP_YUAN` …) are the upstream kill-switch.
- Customers never see any of this — flat plan prices only.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `No ANTHROPIC_API_KEY found` | Create `.env` from `.env.example` and paste your real key. |
| `401` invalid key | An old key/redirect in your shell can shadow `.env`: `unset ANTHROPIC_API_KEY`, and note the app auto-ignores a non-Anthropic `ANTHROPIC_BASE_URL`. No quotes/spaces around the key. |
| Run needs `DEEPSEEK_API_KEY` | You set `TEAM_MODEL_PRESET=domestic` (China mode) but no DeepSeek key — add `DEEPSEEK_API_KEY` to `.env`. In this mode no Anthropic key is needed. |
| Signup asks for an SMS code | In local dev the code prints in the Terminal (console SMS provider). Set `PHONE_VERIFICATION=0` to disable entirely. |
| Run "stopped early" | A founder ¥ budget (成本控制台) or the free trial's internal cost cap cut it off — raise the budget, or upgrade the account's plan. |
| 「报告额度已用完」 | The plan quota (or the free trial's 2 reports) is spent — upgrade in 账户与套餐 or buy a 单次报告 (¥29). |
| Port 8000 busy | `PORT=8001 python app.py` |
| `ModuleNotFoundError` | `source .venv/bin/activate` then `pip install -r requirements.txt`. |
| Scheduled job didn't fire | The app (or the scheduler container) must be running; the account must be 企业版 (scheduled delivery is plan-gated); check cron validity, plan quota, and that the timezone is Asia/Shanghai. |
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
│   ├── config.py           ← the ORG CHART + models + plans + product identity + env knobs
│   ├── orchestrator.py     ← the CEO: plan → delegate → synthesize → verify
│   ├── base_agent.py / llm.py / providers.py / memory.py / file_tools.py
│   ├── token_manager.py    ← the CFO (¥ budgets)   customers.py ← accounts & plans
│   ├── db.py / models.py / metrics_store.py        ← the company brain (Phase 1+)
│   ├── trends.py(2) industry_packs.py(4) pdf_report.py / delivery.py / scheduler.py(5)
│   ├── verification.py(6)  crypto.py / sms.py / security.py / platform_guard.py / alerts.py(8)
│   ├── payments.py / reconciliation.py(9)
│   └── onboarding.py / helpdocs.py / lifecycle.py(10)
├── admin/queries.py        ← the ops dashboard's SQL (unit-tested)          (10)
├── migrations/             ← Alembic 0001–0006 (`alembic upgrade head`)
├── templates_industry/     ← industry packs as editable YAML                 (4)
├── docs/ops.md             ← the operations runbook   docs/help/ ← /help docs
├── docs/product-brief.md   ← what we sell & why it defends (the three moats)
├── docs/ui-redesign-plan.md← the customer-UI redesign plan + per-phase record
├── assets/                 ← bundled CJK font + 示例销售数据.csv
├── frontend/               ← the SPA: index.html + style.css + js/ ES modules
│   └── js/views/ js/founder/  ← customer pages vs. founder-gated machinery views
├── scripts/                ← backups, screenshot capture, brand-asset generation
├── tests/                  ← per-phase acceptance suites (pytest)
├── CHANGELOG.md            ← rendered at /changelog (drives the 新功能 badge)
└── data/ uploads/ memory_store/  ← your local state (git-ignored)
```

**Privacy & safety:** everything runs on your machine/server. Uploads and report
PDFs are encrypted at rest; passwords are stored only as salted hashes; SMS codes
only as hashes; SMTP credentials and webhook URLs are never logged; downloads
require login + ownership checks; and your data is used only to generate your own
reports — never for training, never shown to other accounts.

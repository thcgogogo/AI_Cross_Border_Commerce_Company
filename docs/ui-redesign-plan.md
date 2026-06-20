# UI Redesign — Execution Plan

**Goal:** turn the customer-facing UI from an "AI model platform" (统一智能体中转站) into the product
defined in [docs/product-brief.md](product-brief.md): **finished business reports, delivered
automatically** — the customer never sees the machinery, pricing is flat (never per-token), and the
three moats (customer memory, vertical depth, distribution & trust) are visible in the interface.

**How to use this document:** each phase is a self-contained work session with tasks, file
references, acceptance criteria, and verification steps. Run them in order; every phase leaves the
app working and the test suite green. Copy-paste kickoff prompts are in §10.

---

## 1. The gap — why the redesign

| The brief says | The current UI does | Where |
|---|---|---|
| "We sell finished reports — not AI tools, not a platform, not API access" | Hero sells API keys: "One API key for every model… **Get API Key** / **Explore Models and Pricing**" | `frontend/index.html:29-35` |
| "The customer never sees or touches that machinery" | Menu exposes **Your Team** (per-agent model dropdowns), **Models & Pricing** (39 models, 11 providers), **Fusion**, **Get Unified API** (key + model picker + token usage table) | `index.html:55-104`, `app.js` views |
| "Flat, value-based pricing. **Never per-token.** Token costs are internal COGS, invisible" | Model cards show ¥/1M tokens; dashboard shows ¥ balance, token in/out counts, top-up buttons | `app.js:1529-1611` (models), `app.js:1684-1783` (dashboard) |
| "The customer … will never write a prompt" | The main flow asks for a free-text goal in a "Chat bot" tab | `app.js:1202` (`chatbotDetailHTML`) |
| "周一早上，报告自己到" is the defining experience | Scheduling is buried as a sub-section inside 企业大脑 | `app.js:2859` (`brainScheduleSectionHTML`) |
| "Make the memory *visible* (a 企业大脑 page showing tracked KPIs and history)" | Brain page is configuration forms only — no stats, no trends, nothing emotionally concrete | `app.js:2648-3163` |
| Branded boss report in 汇报体, mobile link arrives in 企业微信 | Result is a markdown dump in a dark dev-tool theme; CSS is desktop-first | `app.js:706` (`reportBodyHTML`), `style.css` |
| 免费体验 = **2 reports**; 基础版 ¥199 ≈ 20 reports, 1 user; **企业版** ¥599 = 5 users + scheduled delivery + contract review | free = 10/月 + visible ¥200 credit; basic = 100/月, 3 seats; "专业版" = 500/月, 10 seats; nothing plan-gated | `agent_team/config.py:85,367-375` |
| 单次 ¥29/report | Doesn't exist (only ¥ balance top-up) | — |
| 合同审查 add-on (企业版, ~3/month, with disclaimer) | Doesn't exist | — |
| Brand name on every PDF footer | `PRODUCT_NAME = "统一智能体中转站"` hardcoded — a platform name, not a report brand | `agent_team/pdf_report.py:47` |

**What already matches the brief (keep, restyle only):** live agent activity feed ("persuasive
theater, kept"), per-section 👍/👎 + 存为长期偏好, 【N】 anchors + 数据来源与核验 appendix, UNKNOWN_TERMS
confirm banner, the 5-step onboarding wizard, branded CJK PDF, email/企业微信 delivery, fapiao,
phone-verified signup, history import (exists but buried).

---

## 2. Design principles

1. **Sell the result, not the machinery.** No customer-visible screen may show model names,
   provider names, token counts, per-token prices, or API keys. All of that moves to founder mode.
2. **The report is the hero artifact.** The web report view should look like the branded PDF —
   logo, brand color, fixed section order, sources appendix, footer — not like chat output.
3. **Zero-configuration surface.** The boss never types a prompt: report type + uploaded files are
   enough. Free-text is an optional "补充说明" accordion, never a requirement.
4. **Make the moats visible.** 企业大脑 opens with memory stats and KPI trends ("已记住 27 个指标 ·
   38 周历史"); the workbench counts down to Monday 08:00; every report shows its verification trail.
5. **Flat pricing only.** Customers see plans and report quotas ("本月已用 3/20"), never ¥ balance
   or token meters. The promise line 成本透明，绝不超支 appears on the pricing page.
6. **Chinese-first, boss-grade.** All customer copy in business Chinese; light, trustworthy,
   document-like visual language (the current dark dev theme stays for founder mode only).
7. **Mobile-first report viewing.** The link arrives in 企业微信 and opens on a phone. The report
   view, login, and history pages must be excellent at 390px wide.
8. **Keep the theater.** The live agent feed stays — with Chinese role names (数据分析师, 报告撰写人…)
   and friendly verbs, with model IDs/token lines stripped from the customer view.
9. **Config-driven extensibility.** Product name, plan definitions, agent display names, feature
   gates, and goal templates live in config — not scattered through the JS.

---

## 3. Target information architecture

```
Logged OUT                          Logged IN (customer)                Founder mode (+ is_admin)
─────────────                       ─────────────────────               ─────────────────────────
营销首页 (landing)                   工作台      (workbench/home)         内部工具 section in nav:
  · 周一早上，报告自己到               生成报告    (new report)              · 团队与模型   (old Your Team)
  · 示例报告 showcase                历史报告    (reports list + viewer)    · 成本控制台   (Token Manager)
  · 三步如何工作                      定时与送达  (schedule & delivery)      · 模型与定价   (old Models&Pricing)
  · 行业模板 strip                   企业大脑    (memory, KPI trends)       · Fusion
  · 数字可溯源 trust block            账户与套餐  (plan, orders, fapiao)     · 开发者接口   (old Unified API)
  · 定价（四档，flat）                ──────                               · 运营后台 → /admin pages
  · 安全与隐私                        帮助中心 / 更新日志 / 联系支持
  · 登录 / 免费体验 CTA               新手引导 (wizard, first login)
```

Moves: **定时任务** and **报告历史** move OUT of 企业大脑 into their own top-level pages.
Disappears from customer view (founder mode only): Your Team, Models & Pricing, Fusion, Unified
API/keys, Token Manager, ¥ balance & top-up, token usage table.

> **Update (2026-06-20):** "Fusion" above is the *founder* machinery view (single-shot, shows ¥ —
> still admin-only). A separate **customer-facing** Fusion chatbot was later added at `/fusion`: a
> freemium multi-model chat (3 free anonymous turns → sign in for free, persisted per-conversation
> memory). It is a deliberate customer surface and is NOT machinery. See the two-surfaces note in
> `README.md` / `CLAUDE.md`.

### Copy deck (old → new)

| Old | New (customer-facing) |
|---|---|
| 统一智能体中转站 / Unified Agent Gateway | `PRODUCT_NAME` from config (decision D1) + tagline 你的 AI 经营分析团队 |
| Menu / Chat bot | 生成报告 |
| Get API Key (hero CTA) | 免费体验（送 2 份报告） |
| Explore Models and Pricing (hero CTA) | 看一份示例报告 |
| Dashboard (account widget) | 工作台 |
| Top-up / balance ¥ | （隐藏）→ 套餐与购买 / 单次购买 |
| Token Manager | （founder）成本控制台; customer-facing promise: 成本透明，绝不超支 |
| Agent names (Document Analyst, …) | Chinese display names via config (`name_cn`): 文档分析师, 资料管理员, 数据分析师, 报告撰写人, 媒体制作人, 首席执行官 |
| Footer "Built with the Anthropic Claude API…" | 由 [产品名] AI 团队生成 · 每个数字可溯源 |
| Log in / Sign up / Email / Password (English labels) | 登录 / 注册 / 邮箱 / 密码 — all auth & form copy in Chinese |

### Screen specs

**3.1 营销首页 (logged out).** Sections in order: ① Hero — 「周一早上，报告自己到。」+ sub: 把杂乱的
Excel 交给你的 AI 经营分析团队；每周一 08:00，一页可转发的经营报告准时送到企业微信和邮箱。不用配置，不写提示词。
CTAs: 免费体验（送 2 份报告） / 看一份示例报告. ② 三步 strip: 上传数据 → AI 团队分析（多模型交叉核验）→
准时送达. ③ 示例报告 showcase — a real rendered sample (generated from `assets/示例销售数据.csv`),
shown as an embedded scrollable report frame or large screenshots, with the 数据来源与核验 appendix
visible. ④ 数字可溯源 trust block — explain anchors + auditor pass in one diagram. ⑤ 行业模板 strip
(from `/api/industries`). ⑥ 企业大脑 value prop — "它记住你的公司：指标、口径、术语、偏好". ⑦ 定价 —
four flat cards (免费体验 / 单次 ¥29 / 基础版 ¥199/月 / 企业版 ¥599/月) + 支持支付宝、微信支付，可开发票 +
成本透明，绝不超支. ⑧ 安全与隐私 strip (加密存储、仅用于生成你自己的报告、绝不用于训练). ⑨ FAQ + 联系支持.

**3.2 工作台 (logged-in home).** Cards: ① 下次送达 — "下一份周报：周一 08:00 → 企业微信 + 邮箱" with
countdown (needs `next_run_at`, task B5); if no schedule: setup CTA (or upgrade CTA per D3).
② 最新报告 — title, date, 查看/下载 (from `/api/reports`). ③ 本月额度 meter — "已生成 3 / 20 份"
(from `/api/plans`). ④ 数据收件箱 — files the next scheduled run will use + drag-drop to add
(latest upload batch). ⑤ 企业大脑 teaser — "已记住 N 个指标 · M 周历史" (task B6). ⑥ Primary action:
立即生成报告.

**3.3 生成报告.** Replaces "Chat bot". Report-type cards: 经营周报 (default) / 经营月报 / 自定义分析
(only this one reveals a textarea, labeled 补充说明（可选）). Industry comes from the company profile
(editable inline). Drag-drop file zone + 用示例数据 link. One big 开始生成 button. Internally the
frontend composes the goal text from a template per report type (templates in config, task B1) and
POSTs the existing `/api/run` — no backend pipeline change. While running: status pill + restyled
live feed (`app.js:448` `feedRowHTML`) with Chinese role names, friendly verbs, no model/token lines
for customers. On completion, navigate to the report view (3.4).

**3.4 报告视图 (result + `/reports/<id>`).** A document, not a chat answer: brand color top bar +
logo + report title + period; sections in the pack's fixed order; sticky/collapsible section TOC on
mobile; every 【N】 anchor tappable → opens the matching source row (bottom sheet on mobile);
数据来源与核验 as collapsible appendix with the audit status banner; per-section 👍/👎 + comment +
存为长期偏好 (existing `/api/feedback*`); UNKNOWN_TERMS confirm banner (existing); prominent
下载 PDF and 在企业微信中转发 hint (the forwarded PDF is the growth loop); footer 由 [产品名] AI 团队
生成 · 每个数字可溯源.

**3.5 历史报告.** Top-level list grouped by month: date, title, type badge (周报/月报/手动/导入),
status, 查看 / PDF (existing `/api/reports`, `/api/reports/<run_id>/download`). Row click → 3.4.
Empty state sells scheduling. Served at `/reports` and `/reports/<id>` (task B7) so the 企业微信
message can deep-link a mobile-readable web report, not just a PDF download.

**3.6 定时与送达.** ① Schedule card — preset 每周一 08:00 (brief default), frequency picker
每周/每月/高级（cron）, report type, data source: 最新上传 (default) / 固定文件 (existing
`/api/scheduled-jobs*`). Per-run budget field is machinery → hidden for customers (internal default;
visible in founder mode). ② 送达渠道 — 企业微信群机器人 (webhook paste + illustrated how-to +
发送测试消息 button, task B5) and 邮箱. ③ 数据收件箱 — "下次报告将使用这些文件" + upload zone.
④ Plan gating per D3: non-企业版 sees an explainer of the Monday ritual + upgrade CTA instead of
the editor.

**3.7 企业大脑.** ① Hero stats strip — 已记录指标 N 项 · 覆盖 M 周 · 术语 K 条 · 长期偏好 P 条, with the
line 这是只属于你公司的经营记忆，每份报告都会让它更聪明 (task B6). ② KPI 趋势 tiles — per tracked KPI:
latest value, 环比 arrow, streak note (e.g. 已连续 3 周下滑), client-rendered SVG sparkline from the
series endpoint (B6; engine functions already exist: `trends.py:38,68,218`). ③ Existing editors
restyled as tabs: 公司档案 / 指标定义 / 术语表 / 报告偏好 / 品牌与送达 / 历史数据导入 (import promoted,
with its cost-estimate step — existing `/api/company-brain/import*`). 定时任务 and 报告历史 sections
are removed from this page (moved to 3.5/3.6).

**3.8 账户与套餐.** ① Current plan card + quota meter + 续费/到期不续 (existing `/api/plans`,
`/api/subscription/*`). ② Plans grid — the four flat offers; purchase via existing
`/api/pay/create` QR panel; 单次 ¥29 purchase (task B4). ③ 订单与发票 — existing orders table +
fapiao request. ④ 成本透明，绝不超支 promise copy. ⑤ Security: 修改密码, 退出所有设备 (existing).
**Removed from customer view:** ¥ balance, top-up buttons, token usage table, model picker, API key
display.

**3.9 Founder mode.** If `/api/me` returns `is_admin` (task B2): nav gains an 内部工具 section with
the five machinery views (unchanged functionality, minimal restyle) + links to `/admin*` pages.
Customers never see it.

**3.10 新手引导 (rework, Phase 8).** Steps become: ① 公司与行业 → ② 上传数据/用示例数据 → ③ 确认指标
→ ④ **导入历史数据 (new, skippable)** — "导入过去 6–12 个月的表格，第一份报告就带趋势对比" (existing
import endpoints; internally cost-capped) → ⑤ 生成第一份报告 (live feed) → ⑥ 开启每周自动送达
(plan-gated per D3: free/基础版 sees a preview of the Monday experience + upgrade CTA).

---

## 4. Visual design system

**Theme:** customer UI moves to a light "business document" theme. Founder views may keep the
current dark theme (cheap differentiation between 前台/后台).

Proposed tokens (`style.css` `:root`, replacing the current dark set):

```css
--bg: #f6f7f9;          /* page */         --ink: #1a2233;        /* primary text */
--card: #ffffff;        /* surfaces */     --muted: #5d6b82;      /* secondary text */
--line: #e3e7ee;        /* borders */      --accent: #1f5eff;     /* actions (or customer brand color) */
--accent-ink: #ffffff;  --green: #0f9d58;  --amber: #c98a00;      --red: #d23f31;
--radius: 12px;         --shadow: 0 1px 2px rgba(16,24,40,.06), 0 8px 24px rgba(16,24,40,.08);
--font-cjk: -apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", sans-serif;
--font-num: "SF Pro Text", "Helvetica Neue", Arial, sans-serif;  /* tabular numerals for KPIs */
```

Type scale: base 15–16px, report body 16–17px/1.75 (CJK needs looser leading), KPI numerals large
with `font-variant-numeric: tabular-nums`. Components to build once and reuse: page header, card,
stat tile (value + delta arrow + label), quota meter, section TOC, source-anchor chip 【N】, feed
row, plan card, empty state, mobile bottom sheet. The report view also gets a print stylesheet.
Personalization: when the company has a `brand_color`, use it as `--accent` inside the report frame
(reinforces "this is *my* analyst").

---

## 5. Technical approach (frontend)

**Keep the stack** — vanilla JS SPA, no framework, no build step (the 4k-line `app.js` is fully
mapped and works; a framework rewrite adds risk and zero customer value). Restructure into native ES
modules (`<script type="module">`):

```
frontend/
  index.html            ← rewritten shell (minimal: header, <main id="view">, modals, FAB)
  style.css             ← new tokens + components (founder views may keep a dark scope class)
  js/main.js            ← boot, router, shell wiring
  js/api.js             ← all fetch calls (extracted from current app.js)
  js/state.js           ← session/account/profile cache
  js/components.js      ← shared HTML builders (cards, meters, feed rows, report frame)
  js/views/landing.js  workbench.js  report_new.js  report_view.js  reports.js
           schedule.js  brain.js  account.js  onboarding.js  auth.js
  js/founder/team.js  models.js  fusion.js  unified.js   ← ported old views, admin-gated
```

**Routing:** move from the overlay/detail-window pattern to a simple path router
(`/`, `/new`, `/reports`, `/reports/<id>`, `/schedule`, `/brain`, `/account`) using the History API.
Backend: add a catch-all that serves `index.html` for these paths (same pattern as the existing
`/company-brain` route in `app.py`). Keep modals for login/reset; keep 900ms polling
(`/api/status/<job_id>`) exactly as is.

**Migration safety:** Phase 0 puts the repo under git; each phase lands complete and is verified
before the next; the old `app.js` stays untouched on disk until Phase 2 replaces the shell, after
which old view functions are ported file-by-file (founder views are ports, not rewrites).

---

## 6. Backend alignment tasks (small, enumerated)

Each lands with a pytest in the matching phase. Nothing here touches the agent pipeline.

- **B1 — Product identity & copy knobs.** `PRODUCT_NAME`, `PRODUCT_TAGLINE` in
  `agent_team/config.py` (env-overridable); replace the hardcoded name in
  `agent_team/pdf_report.py:47`; expose via `/api/meta`; add `name_cn` per agent in the org chart
  config; add report-type goal templates (周报/月报) to config and expose via `/api/meta`.
- **B2 — `is_admin` flag.** `/api/me` and login responses include
  `is_admin: email ∈ ADMIN_EMAILS`. Frontend uses it to show founder mode. (UI gating only; real
  authorization on `/admin*` already exists server-side.)
- **B3 — Plan alignment.** In `config.py:367-375`: labels → 免费体验 / 基础版 / 企业版 (keep internal
  codes `free/basic/pro` — no data migration); quotas → free **2 lifetime** (new
  `lifetime_quota: true` flag honored by the quota gate), basic 20/月 + 1 seat, pro high-volume +
  5 seats; add `features: [...]` per plan (`scheduled_delivery`, `contract_review`,
  `priority_models`) and a helper `plan_has(code, feature)`.
- **B4 — 单次 ¥29.** New order type `single_report` through the existing payments machinery
  (`/api/pay/create`), granting +1 report credit; quota gate counts credits before refusing.
- **B5 — Scheduling alignment.** Gate schedule creation/fires on `plan_has(…, "scheduled_delivery")`
  per D3 with a friendly upgrade error; expose `next_run_at` per job (APScheduler knows it) in
  `/api/scheduled-jobs`; add `POST /api/delivery/test` (send a test message to the configured
  webhook/email).
- **B6 — Brain summary & series.** `GET /api/company-brain/summary` → `{metrics_count, kpi_count,
  weeks_covered, glossary_count, preferences_count, kpis: [{name, latest, delta_pct, streak,
  series: [...last 12 points]}]}` — read-only composition over `metrics_store` + `trends.py`
  (`compute_deltas:38`, `detect_streaks:68`, `_ordered_series:218`).
- **B7 — Web report deep link.** Serve the SPA at `/reports` and `/reports/<run_id>` (login +
  ownership, same pattern as `/company-brain`); `agent_team/delivery.py` includes this link in the
  企业微信/email message alongside the PDF.
- **B8 — Billing-model alignment (the one structural item).** Today every run bills tokens against
  the customer's ¥ balance, and scheduled fires are balance-gated — that *is* per-token billing and
  it would break flat-plan subscribers (¥199/月 customers would hit 402 when the invisible ¥200
  demo credit ran out). Change: for accounts with an active plan, runs are gated by **plan quota**
  (+ credits from B4), not customer balance; internal cost is still metered per run
  (`runs.total_cost_yuan`) for margin tracking; COGS protection stays via per-run budgets
  (Token Manager), `PLATFORM_*_CAP` circuit breaker, and a per-account internal cost cap for free
  trials (reuse `START_BALANCE_CNY` as an invisible cap, never displayed). Customer-visible ¥
  balance/top-up disappears (founder mode keeps it for the API storefront). **Touches the Phase 9
  billing tests — do this as its own session, update `tests/test_phase9_payments.py`
  deliberately, never silently.**
- **B9 — 合同审查 (later, Phase 9).** `mode=contract_review` on `/api/run` selecting a
  contract-review prompt pack; monthly cap (3) for plans with the `contract_review` feature;
  disclaimer constant 辅助审查，不构成法律意见 rendered in UI + PDF.

---

## 7. Phased execution

> Effort is in "focused sessions" (one Claude Code working session ≈ a coherent sitting).
> Every phase ends with: `pytest -q` green + the phase's manual checks via the running app.

### Phase 0 — Safety net (0.5)
1. Put the project under version control (it currently is **not** a git repo):
   `git init && git add -A && git commit -m "Baseline before UI redesign"` (`.gitignore` exists).
2. Baseline: `.venv/bin/python -m pytest -q` → record "50 passed".
3. Screenshot every current screen (landing, chat bot, team, models, unified, brain, wizard,
   result) into `docs/redesign/before/` for the after-comparison.
4. ✅ Brief copied to `docs/product-brief.md` (done).
**Acceptance:** committed baseline, tests green, screenshots saved.

### Phase 1 — Backend alignment I: identity, flags, plans (1)
Tasks: B1, B2, B3 (+ tests: meta exposes product name/templates; `is_admin` correct; quota gate
honors 2-lifetime free + new labels/quotas/features).
**Acceptance:** `/api/meta` carries product identity + goal templates; `/api/me` carries
`is_admin`; `/api/plans` shows 免费体验/基础版/企业版 with brief quotas; all tests green.

### Phase 2 — Design system & shell (1.5–2)
Rewrite `index.html` (new shell, Chinese-first, `lang="zh-CN"`, new `<title>`), new `style.css`
tokens/components (§4), `js/` module skeleton + router + ported `api.js`/auth (login/reset/signup
modals restyled in Chinese), nav with customer IA + founder-mode section gated on `is_admin`.
Old machinery views reachable only through founder nav (ported as-is into `js/founder/*`,
dark-scoped). Placeholder pages for views built in later phases.
**Acceptance:** app boots into new shell; login/signup/reset work; customer nav shows the six
customer items only; founder account sees 内部工具; old per-token screens unreachable for customers.

### Phase 3 — 营销首页 (1)
Build §3.1 including the sample report showcase: run the pipeline once on
`assets/示例销售数据.csv`, save the rendered report (PDF + screenshots/HTML) under
`frontend/assets/sample/`, embed it. Flat pricing section reads from `/api/plans`.
**Acceptance:** logged-out experience sells the report; zero machinery words on the page; CTAs:
免费体验 → signup, 看示例报告 → showcase.

### Phase 4 — Report experience (2)
生成报告 view (§3.3, zero-prompt templates), restyled live feed (Chinese roles via B1 `name_cn`,
machinery lines hidden for customers), report view as branded document (§3.4) reused for both
fresh runs and history, 历史报告 page (§3.5), B7 deep links + delivery message link.
**Acceptance:** demo-data run end-to-end without typing any text; feed shows no model IDs/token
lines (customer); finished report shows brand bar, TOC, tappable anchors, sources appendix,
feedback, UNKNOWN_TERMS banner, PDF download; `/reports/<id>` opens from a phone-width viewport
cleanly.

### Phase 5 — 定时与送达 + 工作台 (1–1.5)
Tasks: B5 backend bits; §3.6 schedule/delivery page; §3.2 workbench with next-delivery countdown,
quota meter, latest report, data inbox; 402/gating states per D3.
**Acceptance:** schedule 每周一 08:00 in two clicks (企业版 account); test-send button delivers;
workbench shows countdown + quota; non-企业版 sees the upgrade explainer (per D3).

### Phase 6 — 企业大脑 (1–1.5)
Tasks: B6 endpoint + §3.7 page (stats hero, KPI trend tiles with sparklines, restyled editors,
history import promoted).
**Acceptance:** after ≥2 runs on demo data, the page shows real stats and at least one KPI tile
with delta/streak/sparkline; profile/KPI/glossary/preferences edits still persist; import flow
(estimate → run → progress) works.

### Phase 7 — 账户与套餐 + billing alignment (1.5)
Tasks: B8 (own commit, tests updated deliberately), B4 单次 ¥29, §3.8 page.
**Acceptance:** subscribed account with exhausted demo credit can still run (quota-gated);
free account hard-stops after 2 reports with friendly upsell; 单次 ¥29 purchase via mock provider
adds one credit; no ¥ balance/token/model UI anywhere customer-visible; payments tests green.

### Phase 8 — 新手引导 rework (1)
§3.10: restyle + insert history-import step + plan-gated final step; funnel events still recorded
(`/api/onboarding/*`; new step gets its own event so `/admin` funnel stays meaningful).
**Acceptance:** fresh phone-verified signup → wizard → demo data → import (skippable) → first
report → schedule/upsell, all in Chinese, no free-text required; funnel rows appear in `/admin`.

### Phase 9 — 合同审查 (optional, when ready) (1.5–2)
B9 + a second service card on 生成报告 (upload contract → review report with disclaimer + cap meter
本月已用 1/3). Brief says keep it deliberately quiet — no landing-page promotion yet.
**Acceptance:** 企业版-only, capped at 3/月, disclaimer on screen and in PDF.

### Phase 10 — Polish & full verification (1)
Empty states everywhere, error catalogue (401 → login modal; 402 → upgrade/quota card; network →
retry), loading skeletons, responsive audit at 390/768/1280, print stylesheet, favicon/brand
assets, rewrite `docs/help/*` to match the new IA, add `CHANGELOG.md` entry (drives the 新功能
badge), capture `docs/redesign/after/` screenshots, run §11 master checklist.

**Total: ~11–13 sessions** (Phase 9 excluded: ~9.5–11.5).

---

## 8. Explicitly unchanged

Agent pipeline (`orchestrator.py`, agents, verification, PDF rendering), payment provider adapters,
scheduler internals, admin server-rendered pages (minimal/no restyle), the API storefront code
(`/v1/*` endpoints stay live; UI is founder-gated, not deleted — D4), all existing tests except the
deliberate Phase 7 billing updates.

---

## 9. Decisions for Bruce (defaults applied unless you say otherwise)

- **D1 — Product name.** 统一智能体中转站 is platform branding; the report footer needs a product
  name (「由 ___ AI 团队生成」). *Default: introduce the config knob with a placeholder; you supply
  the real name before Phase 3.*
- **D2 — Free tier.** Brief: 2 reports, phone-verified, hard internal cost cap. *Default: 2
  lifetime reports; the ¥200 demo credit becomes an invisible internal cost cap (B8).*
- **D3 — Who gets scheduling.** The brief lists scheduled auto-delivery under 企业版 only — but the
  Monday ritual is the product's defining experience, and gating it makes 基础版 a manual-upload
  product. *Default: follow the brief (企业版 only) with a strong upgrade path; alternative worth
  considering: email-only scheduling on 基础版, 企业微信 + multi-user on 企业版.*
- **D4 — API storefront fate.** *Default: keep code + endpoints, founder-mode UI only.* (Removal or
  a separate developer portal are both possible later.)
- **D5 — Theme.** *Default: light business theme for customers, dark retained for founder views.*
- **D6 — 单次 ¥29 timing.** *Default: Phase 7 (needs B8 anyway).*

---

## 10. Execution playbook (copy-paste)

Each phase = one Claude Code session inside `/Users/brucetang/Company_Agent_Demo`:

```
Read docs/ui-redesign-plan.md and docs/product-brief.md. Implement Phase <N> exactly as specified
(tasks, acceptance criteria). Keep all tests green; add the tests named in the phase. When done,
run the phase's verification steps yourself (start the app, check the UI) and show me the results.
```

Verify any phase by hand:

```bash
cd /Users/brucetang/Company_Agent_Demo
source .venv/bin/activate
python -m pytest -q          # must end "passed"
python app.py                # then open http://localhost:8000
```

After each successful phase:

```bash
git add -A && git commit -m "UI redesign Phase <N>: <one line>"
```

---

## 11. Master verification checklist (Definition of Done)

Mirrors the brief's working tests:

- [ ] A boss-persona user signs up (phone code), completes the wizard with 示例数据, and receives a
      finished report **without typing a single sentence**.
- [ ] No customer-visible screen contains model names, provider names, token counts, per-token ¥,
      or API keys (sweep every page with `preview_snapshot` and grep for: `token`, `Opus`,
      `DeepSeek`, `Qwen`, `/1M`, `API key`, `sk_`, `模型`).
- [ ] After two runs, 企业大脑 shows memory stats and ≥1 KPI trend tile, and the second report
      contains a correct 与历史对比 section (Moat 1 working test).
- [ ] Workbench shows the Monday 08:00 countdown; a scheduled fire delivers 企业微信/email with a
      link that opens the mobile web report (Moat 3 habit surface).
- [ ] Pricing page shows exactly: 免费体验 / 单次 ¥29 / 基础版 ¥199 / 企业版 ¥599 + 成本透明，绝不超支;
      free account stops after 2 reports with a friendly upsell.
- [ ] Report view at 390px wide: readable, TOC works, anchors tappable, PDF downloadable.
- [ ] PDF and web report both carry logo, brand color, sources appendix, and the
      由 [产品名] AI 团队生成 · 每个数字可溯源 footer.
- [ ] Founder account sees 内部工具 (team/models/fusion/API/token manager) and `/admin*`; a customer
      account sees none of it (and gets 403 on `/admin`).
- [ ] `python -m pytest -q` fully green.

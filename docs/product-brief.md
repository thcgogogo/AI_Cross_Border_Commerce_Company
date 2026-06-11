# Product Description & Strategic Moats
*Reference document — what this company does and why it stays defensible. Suitable as context for design/coding agents and future collaborators.*

---

## Part 1 — What the company does

### One-line definition
A vertical AI application company for Chinese SMBs: we sell **finished business reports, delivered automatically** — not AI tools, not a platform, not API access. Internally, a multi-agent "AI team" (orchestrator + specialist agents with shared memory, multi-model cross-checking, and a cost controller) does the work; the customer never sees or touches that machinery.

The customer is a business owner (10–200 employees) who will never write a prompt, configure a workflow, or learn a tool. Positioning sentence: 「他们卖工具和平台，我们卖结果。客户不搭建、不配置、不写提示词——周一早上，报告自己到。」

### Exact services delivered

1. **经营周报/月报 (weekly/monthly business reports) — the lead product.**
   The customer's raw business files (sales exports, order sheets, cost sheets — messy Excel/CSV is expected) are turned into a one-page-plus boss report: core conclusions, key metrics, comparison against the company's own history ("华东区已连续三周下滑，环比 -12.4%"), flagged anomalies, and three recommended actions — written in business Chinese (汇报体), structured by the customer's industry.

2. **合同审查 (contract review) — add-on inside 企业版, capped (e.g. 3 reviews/month).**
   Uploaded contracts are checked for risky clauses, missing protections, and unusual payment terms, in plain Chinese, with the explicit disclaimer 辅助审查，不构成法律意见. Deliberately capped and quiet at first; graduates to a standalone product only after accuracy is proven on real customer documents.

3. **历史数据导入 (onboarding history import).**
   At signup, up to ~6–12 months of past spreadsheets are ingested (metrics extraction only, cheap models) so the very first report already has trend context. This is an onboarding service and a retention mechanism in one.

### The form of the deliverable
A **branded PDF** carrying the customer's logo and brand color, with a fixed, industry-specific section order (e.g. 本周核心结论 → 销售与订单 → 费用与利润 → 与历史对比 → 风险与异常 → 下周建议行动), every number anchored to a **数据来源与核验** appendix (which file, which sheet, which rows; pre-computed trend numbers marked as machine-verified), and a footer: 「由 [产品名] AI 团队生成 · 每个数字可溯源」. The same report is viewable in the web app with per-section 👍/👎 feedback; the PDF is the artifact bosses forward.

### How it is delivered
- **Primary: scheduled and automatic.** A standing job (e.g. 每周一 08:00) runs the pipeline on the latest uploaded data and pushes the result to **企业微信** (core conclusions + secure download link) and **email** (PDF attached). The boss does nothing. This zero-effort arrival is the product's defining experience — "an employee who shows up," not "a tool you visit."
- **Secondary: on-demand.** Web app: drag files in, watch the live agent activity feed (persuasive theater, kept), download the result. All past reports live in 历史报告.

### How we charge
**Flat, value-based pricing. Never per-token.**
- 免费体验: 2 reports (phone-verified accounts, hard cost cap per account)
- 基础版 ¥199/月: ~20 reports, 1 user
- 企业版 ¥599/月: high-volume reports, 5 users, scheduled auto-delivery, contract-review add-on, priority models
- 单次 ¥29/report: the commitment-free bridge to subscription
- Payment via Alipay/WeChat Pay; 发票 available (mandatory for B2B credibility)

Token/model costs are **internal COGS, invisible to the customer**, targeted at <40% of revenue. Rationale, stated once: per-token billing sells anxiety, caps margin at a markup, and punishes our own efficiency gains; a flat price reads like a salary and anchors against the real alternative — a ¥8,000/月 junior analyst. The internal Token Manager survives as (a) our margin protector and (b) a customer-facing promise: 成本透明，绝不超支.

---

## Part 2 — The three moats

A moat is the reason a customer stays when a free or copycat alternative appears. Raw model intelligence is explicitly **not** a moat — we rent it from suppliers (GLM, Qwen, DeepSeek), and it gets cheaper and more available every quarter. These three are what we actually own.

### Moat 1 — Accumulated customer memory ("the company brain")

**What it is.** A per-customer, permanent store of everything the product has learned about that company: every extracted metric from every past report (with period, dimension, and source), the company profile (industry, fiscal conventions, KPI definitions, column-name mappings for their messy spreadsheets), a glossary of their internal terms (asked once, remembered forever), and their report-format preferences.

**The mechanism that makes it bite.** A deterministic (non-LLM) trend engine runs over the stored metrics before each report: week-over-week and year-over-year deltas, multi-week streaks, statistical anomalies. These pre-computed, hallucination-proof numbers are injected into the writing step, which is required to use them verbatim. The result is the sentence a chat window can never produce: a comparison against *this company's own history*.

**Why it defends.** Switching cost compounds weekly. A competitor — including a free chatbot — starts from zero on day one; by month six, leaving us means firing the only "employee" who remembers the company's whole year. Corollary obligations: front-load the moat by importing history at onboarding (so it exists in week one, not month six), make the memory *visible* (a 企业大脑 page showing tracked KPIs and history makes the cost of leaving emotionally concrete), and treat stored business data as sensitive — encryption, access control, and a plain data policy are part of the pitch, not overhead.

**Working test:** a new report automatically contains a correct "与历史对比" section without anyone asking for it.

### Moat 2 — Vertical depth ("knows their industry")

**What it is.** Per-industry template packs — required KPI lists, reference benchmark ranges (labeled 行业经验值，仅供参考), mandatory report skeletons, and industry-specific analysis instructions (e.g. for 跨境电商: break out per-platform performance, refund rate, platform-fee and ad-spend ratios). Stored as editable YAML content, not code, so it can be improved weekly without engineering.

**The mechanism that makes it bite.** A learning loop that generic AI structurally cannot run: every customer in a vertical reveals something — a metric they ask for, a format they prefer, a failure mode — and it is folded back into the shared template. Customer #30 in a vertical receives a product trained by the 29 before them. DeepSeek cannot run this loop because it does not know who its users are or what industry they're in.

**Why it defends.** A copycat must re-learn the vertical customer by customer, which takes the same calendar time it took us — there is no shortcut to accumulated specificity. Discipline rule that keeps the moat real: do not enter a new vertical until the current one is *embarrassingly good*, defined as: five owners in that industry, shown our report next to a raw chatbot's, all pick ours immediately and can say why.

**Working test:** that five-owner comparison, passed.

### Moat 3 — Distribution & trust ("owns the relationship")

**What it is.** The only moat that is mostly *not code*. Three layers: (a) **the habit** — the report arrives in the boss's 企业微信 every Monday at 8:00 with his logo on it; a ritual wired into the company's week is not "switched away from," it has to be torn out; (b) **earned trust in the numbers** — the verification trail (per-number source anchors, an auditor pass that flags any figure it cannot trace, the cross-checked-data footer) makes "we don't hallucinate" a demonstrable claim rather than a slogan, which matters because one fabricated number in a forwarded report kills an account; (c) **the channel** — 代记账/财税 firms and consultants, each already trusted by 100–500 SMBs, reselling via 20–30% recurring commission or white-label. One signed partner equals dozens of customers, and recruiting them early also pre-empts the realistic flank attack (a consultant rebuilding a lookalike on Coze for their own clients).

**Why it defends.** When features commoditize — and "upload Excel, get report" will be a free native feature of Chinese chat apps within a few years — the winner is whoever already owns the relationship and the habit, not whoever has the cleverest prompt. Platform giants will not do relationship sales to a 50-person company in Dongguan; our partners and our 4-hour support response effectively do. The self-marketing detail: every forwarded PDF carries the footer, so the deliverable itself recruits the next customer inside organizations we've never contacted.

**Working test:** a customer would notice — and complain — within hours if Monday's report failed to arrive; and at least one channel partner is actively reselling.

### How the moats relate (one paragraph, no redundancy)
They operate on different clocks. **Memory** defends each existing account, compounding privately per customer. **Vertical depth** defends the segment, compounding publicly across customers in an industry. **Distribution & trust** defends the business itself when technology stops being scarce. Features win the first 50 customers; memory keeps them; vertical depth makes us the obvious choice in a niche; the channel scales past the founder's own network. Pricing ties it together: because we charge for the employee rather than the electricity, every efficiency gain in the engine room widens margin instead of shrinking revenue.

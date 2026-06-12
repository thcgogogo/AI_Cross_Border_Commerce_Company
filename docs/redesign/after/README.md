# UI redesign — after (Phase 10)

The post-redesign screenshot record, paired with [`../before/`](../before/)
(the pre-redesign overlay UI captured in Phase 0). Captured from the running
app with [`scripts/capture_screens.py`](../../../scripts/capture_screens.py):

```bash
.venv/bin/python scripts/capture_screens.py --out docs/redesign/after \
    --email <is_admin + 企业版 account> --password … \
    --customer-email <plain free customer> --customer-password …
```

What each shot shows (numbering = capture order):

| Shot | Surface |
|---|---|
| `01-landing` | 营销首页 (logged out) — hero, 三步, sample report, pricing, trust |
| `02-workbench` | 工作台 — next-delivery countdown, latest report, quota, inbox, brain teaser |
| `03-report-new` | 生成报告 — zero-prompt type cards + drop zone |
| `04-reports` | 历史报告 — month-grouped archive |
| `05-schedule` | 定时与送达 — the 企业版 editors (jobs, channels, inbox) |
| `06-brain` | 企业大脑 — stats hero, KPI trend tiles, editor tabs |
| `07-account` | 账户与套餐 — plan card, flat pricing grid, orders, security |
| `08-report-view` | 报告视图 — branded document frame, TOC, anchors, sources |
| `09-wizard` | 新手引导 — the 6-step overlay (opened at 示例数据) |
| `10–14-founder-*` | 内部工具 — 团队与模型 / 成本控制台 / 模型与定价 / Fusion / 开发者接口 (founder-only) |
| `15-schedule-upsell` | D3 — what a non-企业版 customer sees instead of the editors |
| `16-account-free` | 免费体验 account page (lifetime quota meter) |
| `17-workbench-free` | Free workbench — upgrade CTA in the 下次送达 card |

The login account for shots 02–14 is `is_admin` **and** 企业版, so the founder
nav and the schedule editors render; shots 15–17 use a plain free customer for
the gated states. Exact counts/names may shift run to run — the table maps
slugs, the numeric prefix is just capture order.

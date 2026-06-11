# UI redesign — "before" record (Phase 0)

Captured 2026-06-10 against baseline commit `ed2e7a0` ("Baseline before UI redesign").

**Test baseline:** `.venv/bin/python -m pytest -q` → **50 passed** (after making the
suite hermetic against the local `.env` — see `tests/conftest.py` shim commit).

**Capture tool:** `scripts/capture_screens.py` (Playwright + installed Chrome, 1440×900,
full-page). Reuse the same script for `docs/redesign/after/` in Phase 10.

| File | Screen |
|---|---|
| 01-landing.png | 营销首页 (logged out) — API-key hero |
| 02-wizard.png | 新手引导 wizard (first login) |
| 03-chatbot.png | Chat bot (free-text goal form) |
| 04-fusion.png | Fusion |
| 05-team.png | Your Team (per-agent model dropdowns) |
| 06-models.png | Models & Pricing (per-token ¥/1M) |
| 07-unified.png | Get Unified API (key + token usage dashboard) |
| 08-brain.png | 企业大脑 (configuration forms) |
| 09-home-logged-in.png | Logged-in home (hero + account widget) |

**Not captured — result view:** the finished-report screen (`app.js` `reportBodyHTML`)
only renders from a live run snapshot; producing one requires a real (paid) pipeline
run, which the plan schedules for Phase 3 (sample report from `assets/示例销售数据.csv`).
Capture it then, before the Phase 4 restyle replaces it.

Screenshot login account (local throwaway, safe to delete):
`redesign.shots@local.test` in `memory_store/customers.json`.

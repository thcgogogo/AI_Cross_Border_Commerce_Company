"""Phase 10 — onboarding, support & ops dashboard acceptance tests.

The five checks mirror the Phase 10 spec's acceptance criteria, in order:

  1. A FRESH account reaches a finished first report through the wizard using
     the bundled demo data — without uploading anything (e2e over HTTP: start →
     公司信息 → 用示例数据体验 → KPI 确认 → 生成 (live run, billed) → 每周定时,
     ending with the Monday-08:00 job and a rendered PDF on disk).
  2. onboarding_events records each step, and the admin funnel reports the
     correct percentages on seeded data (unit-tested against hand-computed
     values, and visible on the rendered /admin page).
  3. The dashboard numbers (signups/day, activation, retention cohorts, MRR,
     churn, per-account economics + the >60% cost flag) match hand-computed
     values on a seeded dataset of 3 accounts with known orders/runs.
  4. Non-admin → /admin returns 403; the changelog 新功能 badge clears after
     viewing /changelog.
  5. Lifecycle emails: welcome on signup, the day-3 nudge for report-less
     accounts, and a subscription payment receipt — each sent EXACTLY once.

Isolation matches the Phase 8/9 suites: a throwaway SQLite DB per test, a fresh
CustomerStore wired into app.CUSTOMERS, storage dirs redirected into tmp, and a
network-free fake ModelRouter that bills through the real per-account path.
"""
import os
import sys
import time
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot is one-time & idempotent
from admin import queries  # noqa: E402
from agent_team import (  # noqa: E402
    config, db, helpdocs, lifecycle, metrics_store, orchestrator, payments)
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.models import (  # noqa: E402
    LIFECYCLE_NUDGE_DAY3, LIFECYCLE_RECEIPT, LIFECYCLE_WELCOME, RUN_SUCCEEDED,
    Account, Order, Run, Subscription)


@pytest.fixture
def fresh_db(tmp_path):
    """A throwaway SQLite file per test: events, orders, runs all start empty."""
    db.init_db(str(tmp_path / "brain.db"))
    yield


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    """Redirect uploads/reports/memory/output dirs into tmp so wizard staging and
    the rendered PDF never touch the real data/ tree."""
    monkeypatch.setattr(metrics_store, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(metrics_store, "REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(app, "MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path / "outputs"))
    yield


def _store(tmp_path, **accounts):
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    for email, password in accounts.items():
        store.create_customer([], name=email.split("@")[0], starting_balance=1000.0,
                              email=email, password=password)
    app.CUSTOMERS = store
    return store


def _login(store, email):
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return client


# A Chinese weekly report every fake model call returns, so the synthesized
# final report (and its PDF) carry CJK content the assertions can look for.
_CN_REPORT = (
    "# 经营周报\n\n## 核心结论\n- 本周销售额较上周增长 12.5%\n"
    "- 广告费占比稳定，退款率无异常\n\n## 数据分析\n整体稳中向好。\n"
    "## 下周建议\n关注独立站渠道的增长机会。\n"
)


class _BillingChineseRouter:
    """Network-free ModelRouter stand-in. Built BY the orchestrator (llm=None),
    so it receives the real _tally biller and every call charges the account."""

    def __init__(self, token_manager=None, cost_listener=None, **kwargs):
        self.cost_listener = cost_listener

    def complete(self, **kwargs):
        if self.cost_listener:
            self.cost_listener(config.HAIKU, 50_000, 8_000)
        return _CN_REPORT

    def complete_with_tools(self, **kwargs):
        return self.complete(**kwargs)


def _post_json(client, url, payload=None):
    return client.post(url, json=(payload or {}))


def _subscribe_pro(account_id):
    """An active 企业版 subscription for the wizard account. Step 5 (每周定时)
    is plan-gated per redesign D3 (Phase 5/B5) — a free account now gets the
    structured 402 upgrade explainer instead of silently creating the job."""
    now = datetime.utcnow()
    with db.session_scope() as s:
        s.add(Subscription(account_id=str(account_id), plan_code="pro",
                           status="active", current_period_start=now,
                           current_period_end=now + timedelta(days=30),
                           cancel_at_period_end=False,
                           created_at=now, updated_at=now))


# ---------------------------------------------------------------------------
# (1) Fresh account + demo data → finished first report through the wizard.
# ---------------------------------------------------------------------------
def test_wizard_demo_data_reaches_finished_first_report_e2e(
        fresh_db, tmp_storage, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "ModelRouter", _BillingChineseRouter)
    store = _store(tmp_path, **{"wizard@corp.com": "password1"})
    account_id = store.find_by_email("wizard@corp.com")["id"]
    start_balance = store.find_by_email("wizard@corp.com")["balance"]
    _subscribe_pro(account_id)   # D3 — step 5's scheduling is 企业版-only
    client = _login(store, "wizard@corp.com")

    # The wizard opens (funnel start) and resumes at step 1.
    r = _post_json(client, "/api/onboarding/start")
    assert r.status_code == 200
    assert r.get_json()["next_step"] == 1 and r.get_json()["done"] is False

    # Step 1 — 公司信息 selects the industry template pack.
    r = _post_json(client, "/api/onboarding/company",
                   {"company_name": "示例科技", "industry": "跨境电商"})
    assert r.status_code == 200

    # Step 2 — 用示例数据体验: NOTHING is uploaded; the bundled CSV is staged.
    r = _post_json(client, "/api/onboarding/demo-data")
    assert r.status_code == 200
    data = r.get_json()
    assert data["file_name"] == "示例销售数据.csv"
    assert "销售额" in data["headers"]
    # The KPI guesses are pre-filled from the file header (units included).
    guessed = {g["kpi_name_cn"]: g for g in data["kpi_guesses"]}
    assert guessed["销售额"]["unit"] == "¥" and guessed["订单数"]["unit"] == "单"

    # Step 3 — one-click confirm persists the standing KPI mappings.
    r = _post_json(client, "/api/onboarding/kpi-confirm",
                   {"kpi_mappings": data["kpi_guesses"]})
    assert r.status_code == 200
    company_id = metrics_store.company_id_for_email("wizard@corp.com")
    assert len(metrics_store.get_kpi_mappings(company_id)) == len(data["kpi_guesses"])

    # Step 4 — the wow moment: a real (faked-LLM) run with a live feed.
    r = _post_json(client, "/api/onboarding/generate")
    assert r.status_code == 200
    job_id = r.get_json()["job_id"]
    snap = None
    for _ in range(200):                       # ≤ 20s — the fake run takes ~1s
        snap = client.get("/api/status/%s" % job_id).get_json()
        if snap.get("status") in ("done", "error"):
            break
        time.sleep(0.1)
    assert snap and snap["status"] == "done"
    assert "核心结论" in (snap.get("final_report") or "")
    assert (snap.get("activity") or []), "the live agent feed must have events"

    # The run row finished SUCCEEDED, the branded PDF exists, the account was
    # billed through the real path, and the step-4 event landed (written by the
    # background thread right after the run closes — give it a moment).
    run_id = pdf = None
    state = {}
    for _ in range(100):
        runs = metrics_store.list_runs(company_id)
        state = metrics_store.onboarding_state(account_id)
        if runs and runs[0]["status"] == RUN_SUCCEEDED:
            run_id = runs[0]["id"]
            pdf = metrics_store.report_pdf_path(company_id, run_id)
            if os.path.isfile(pdf) and state["steps"]["4"] == "completed":
                break
        time.sleep(0.1)
    assert run_id and os.path.isfile(pdf) and os.path.getsize(pdf) > 0
    assert state["steps"]["4"] == "completed"
    assert store.find_by_email("wizard@corp.com")["balance"] < start_balance

    # Step 5 — ONE click creates the Monday-08:00 weekly job.
    r = _post_json(client, "/api/onboarding/schedule", {"channels": ["email"]})
    assert r.status_code == 200
    final_state = r.get_json()
    assert final_state["done"] is True
    jobs = metrics_store.list_scheduled_jobs(company_id)
    assert len(jobs) == 1
    # "0 8 * * mon" — named weekday on purpose: APScheduler's from_crontab
    # numbers day_of_week 0=Monday (non-standard), so numeric "1" fired Tuesday.
    assert jobs[0]["cron_expr"] == app.ONBOARDING_WEEKLY_CRON
    assert jobs[0]["active"] is True
    assert jobs[0]["delivery_channels"] == ["email"]

    # A second click must update, never duplicate (idempotent).
    _post_json(client, "/api/onboarding/schedule", {"channels": ["email", "wechat"]})
    jobs = metrics_store.list_scheduled_jobs(company_id)
    assert len(jobs) == 1 and jobs[0]["delivery_channels"] == ["email", "wechat"]


# ---------------------------------------------------------------------------
# (2) onboarding_events records each step; the funnel percentages are correct.
# ---------------------------------------------------------------------------
def test_onboarding_events_and_admin_funnel_percentages(
        fresh_db, tmp_path, monkeypatch):
    store = _store(tmp_path, **{"founder@corp.com": "password1"})

    # Seeded funnel: A completed everything; B completed 1, skipped 2; C only
    # opened the wizard. Started accounts (denominator) = 3.
    for step in range(0, 6):
        metrics_store.record_onboarding_event(
            "cus_a", step, "started" if step == 0 else "completed")
    metrics_store.record_onboarding_event("cus_b", 0, "started")
    metrics_store.record_onboarding_event("cus_b", 1, "completed")
    metrics_store.record_onboarding_event("cus_b", 2, "skipped")
    metrics_store.record_onboarding_event("cus_c", 0, "started")

    funnel = queries.onboarding_funnel()
    assert funnel["accounts_started"] == 3
    by_step = {s["step"]: s for s in funnel["steps"]}
    assert by_step[1] == {"step": 1, "label": "公司信息", "completed": 2, "pct": 66.7}
    assert by_step[2]["completed"] == 1 and by_step[2]["pct"] == 33.3  # skip ≠ completed
    for n in (3, 4, 5):
        assert by_step[n]["completed"] == 1 and by_step[n]["pct"] == 33.3

    # Every step is individually recorded (the raw event log).
    events_a = [(e["step"], e["event"])
                for e in metrics_store.list_onboarding_events("cus_a")]
    assert events_a == [(0, "started")] + [(n, "completed") for n in range(1, 6)]

    # …and the /admin page renders those exact percentages.
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")
    admin = _login(store, "founder@corp.com")
    page = admin.get("/admin")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "66.7%" in html and "33.3%" in html and "新手引导漏斗" in html


# ---------------------------------------------------------------------------
# (3) Dashboard numbers vs hand-computed values on a seeded 3-account dataset.
# ---------------------------------------------------------------------------
def test_dashboard_numbers_match_hand_computed_seed(fresh_db, tmp_path, monkeypatch):
    now = datetime(2026, 6, 10, 12, 0, 0)
    with db.session_scope() as s:
        s.add(Account(id="cus_a", email="a@x.com", name="A",
                      created_at=now - timedelta(days=20)))
        s.add(Account(id="cus_b", email="b@x.com", name="B",
                      created_at=now - timedelta(days=10)))
        s.add(Account(id="cus_c", email="c@x.com", name="C",
                      created_at=now - timedelta(days=2)))
    cid_a = metrics_store.get_or_create_company("a@x.com")
    cid_b = metrics_store.get_or_create_company("b@x.com")
    cid_c = metrics_store.get_or_create_company("c@x.com")
    with db.session_scope() as s:
        # A: activated (succeeded run 1 day after signup); ¥40 total model cost.
        s.add(Run(company_id=cid_a, goal_text="g", status="succeeded",
                  started_at=now - timedelta(days=19), total_cost_yuan=30.0))
        s.add(Run(company_id=cid_a, goal_text="g", status="succeeded",
                  started_at=now - timedelta(days=5), total_cost_yuan=10.0))
        # B: first run 9 days after signup → NOT activated; ¥50 cost.
        s.add(Run(company_id=cid_b, goal_text="g", status="succeeded",
                  started_at=now - timedelta(days=1), total_cost_yuan=50.0))
        # C: cost with ZERO revenue → must be flagged.
        s.add(Run(company_id=cid_c, goal_text="g", status="succeeded",
                  started_at=now - timedelta(days=1), total_cost_yuan=30.0))
        # Subscriptions: A active basic; B canceled THIS month (June 7).
        s.add(Subscription(account_id="cus_a", plan_code="basic", status="active",
                           current_period_start=now,
                           current_period_end=now + timedelta(days=30),
                           created_at=now - timedelta(days=40), updated_at=now))
        s.add(Subscription(account_id="cus_b", plan_code="pro", status="canceled",
                           current_period_start=now - timedelta(days=60),
                           current_period_end=now - timedelta(days=30),
                           created_at=now - timedelta(days=60),
                           updated_at=now - timedelta(days=3)))
        # Orders: A topped up ¥100 then ¥20 refunded → net 80; B paid ¥599 sub.
        s.add(Order(order_no="O1", account_id="cus_a", type="topup",
                    amount_yuan=100, provider="mock", status="paid",
                    refunded_yuan=20, created_at=now,
                    paid_at=now - timedelta(days=3)))
        s.add(Order(order_no="O2", account_id="cus_b", type="subscription",
                    plan_code="pro", amount_yuan=599, provider="mock",
                    status="paid", refunded_yuan=0, created_at=now,
                    paid_at=now - timedelta(days=40)))

    # Hand-computed: 3 signups in the 30d window. A activated (run 1 day after
    # signup) and C activated (run 1 day after signup); B's first run came 9
    # days in — outside the 7-day window. 2/3 = 66.67%.
    spd = queries.signups_per_day(days=30, now=now)
    assert len(spd) == 30 and sum(r["count"] for r in spd) == 3
    act = queries.activation_rate(days=30, now=now)
    assert (act["signups"], act["activated"], act["rate"]) == (3, 2, 0.6667)

    # MRR = the one ACTIVE basic sub × ¥199 (the canceled pro never counts).
    m = queries.mrr(now=now)
    assert m["total_yuan"] == 199.0 and m["active_subscriptions"] == 1
    assert m["by_plan"]["basic"]["count"] == 1

    # Churn = 1 canceled in June / 2 active at June 1st = 50%.
    ch = queries.churn_this_month(now=now)
    assert (ch["canceled"], ch["active_at_month_start"], ch["rate"]) == (1, 2, 0.5)

    # Economics (90d): B ¥599 vs ¥50 (8.35%, ok); A ¥80 vs ¥40 (50%, ok);
    # C ¥0 vs ¥30 → flagged (any cost over 60% of zero revenue).
    econ = {r["account_id"]: r for r in queries.account_economics(days=90, now=now)}
    assert econ["cus_b"]["revenue_yuan"] == 599.0
    assert econ["cus_b"]["cost_yuan"] == 50.0 and econ["cus_b"]["flagged"] is False
    assert econ["cus_a"]["revenue_yuan"] == 80.0    # 100 net of the ¥20 refund
    assert econ["cus_a"]["ratio"] == 0.5 and econ["cus_a"]["flagged"] is False
    assert econ["cus_c"]["revenue_yuan"] == 0.0 and econ["cus_c"]["flagged"] is True

    # Retention cohorts (4 weeks): A signed up in the week of 2026-05-18 and ran
    # in weeks 0 and 2 — exactly [1, 0, 1, 0] for that cohort row.
    coh = {c["week"]: c for c in queries.retention_cohorts(weeks=4, now=now)["cohorts"]}
    assert coh["2026-05-18"]["signups"] == 1
    assert coh["2026-05-18"]["active"] == [1, 0, 1, 0]
    # The current week's future cells are unmeasurable, never zero-faked.
    assert coh["2026-06-08"]["active"][1] is None


# ---------------------------------------------------------------------------
# (4) Non-admin → /admin 403; the changelog badge clears after viewing.
# ---------------------------------------------------------------------------
def test_admin_403_and_changelog_badge_clears(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")
    store = _store(tmp_path, **{"user@corp.com": "password1",
                                "founder@corp.com": "password1"})

    # Logged out AND logged-in-but-not-admin both get 403; the admin gets 200.
    assert app.app.test_client().get("/admin").status_code == 403
    user = _login(store, "user@corp.com")
    assert user.get("/admin").status_code == 403
    admin = _login(store, "founder@corp.com")
    page = admin.get("/admin")
    assert page.status_code == 200
    assert "运营看板" in page.get_data(as_text=True)

    # Fresh account → the newest CHANGELOG entry is unseen → badge ON.
    latest = helpdocs.latest_changelog_id()
    assert latest, "CHANGELOG.md must have at least one '## ' entry"
    st = user.get("/api/changelog/state").get_json()
    assert st["latest"] == latest and st["has_new"] is True

    # Viewing /changelog marks it seen — the badge clears.
    assert user.get("/changelog").status_code == 200
    st = user.get("/api/changelog/state").get_json()
    assert st["has_new"] is False and st["seen"] == latest

    # Support requests are logged (the floating 联系支持 form's endpoint).
    r = user.post("/api/support", json={"message": "发票抬头可以改吗？",
                                        "page": "/help"})
    assert r.status_code == 200
    logged = metrics_store.list_support_requests()
    assert logged and logged[0]["message"] == "发票抬头可以改吗？"
    assert logged[0]["account_email"] == "user@corp.com"


# ---------------------------------------------------------------------------
# (5) Lifecycle emails: welcome / day-3 nudge / receipt — each exactly once.
# ---------------------------------------------------------------------------
def test_lifecycle_welcome_nudge_and_receipt_send_once(
        fresh_db, tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(lifecycle.delivery, "send_email",
                        lambda to, subject, body, **kw: (sent.append(
                            {"to": to, "subject": subject, "body": body}) or (True, None)))
    monkeypatch.setenv("PHONE_VERIFICATION", "0")   # signup without SMS here
    store = _store(tmp_path)
    client = app.app.test_client()

    # Welcome: sign-up sends the 3-step quickstart exactly once.
    r = client.post("/api/signup", json={
        "name": "新客", "email": "fresh@corp.com", "password": "password1"})
    assert r.status_code == 200
    welcomes = [m for m in sent if "欢迎" in m["subject"]]
    assert len(welcomes) == 1 and welcomes[0]["to"] == "fresh@corp.com"
    account_id = app.CUSTOMERS.find_by_email("fresh@corp.com")["id"]
    assert not lifecycle.send_welcome_email(account_id, "fresh@corp.com")  # deduped

    # Day-3 nudge: an account 4 days old with NO finished report gets nudged
    # once (with the demo-data link); the next tick is a no-op.
    with db.session_scope() as s:
        acc = s.get(Account, account_id)
        acc.created_at = datetime.utcnow() - timedelta(days=4)
    summary = lifecycle.daily_lifecycle_tick()
    assert summary["nudged"] == [account_id]
    nudges = [m for m in sent if "还没生成第一份报告" in m["subject"]]
    assert len(nudges) == 1 and "?onboarding=demo" in nudges[0]["body"]
    assert lifecycle.daily_lifecycle_tick()["nudged"] == []
    assert len(metrics_store.list_lifecycle_events(
        account_id, kind=LIFECYCLE_NUDGE_DAY3)) == 1

    # Receipt: paying a subscription order emails ONE receipt; a replayed
    # notify (delivered again) must not send a second one.
    with client.session_transaction() as sess:
        sess["customer_id"] = account_id
    order_no = client.post("/api/pay/create", json={
        "type": "subscription", "plan_code": "basic"}).get_json()["order_no"]
    fields = payments.MockProvider().notify_fields(order_no, 199.0)
    assert client.post("/pay/notify/mock", data=fields).status_code == 200
    assert client.post("/pay/notify/mock", data=fields).status_code == 200  # replay
    receipts = [m for m in sent if "收据" in m["subject"]]
    assert len(receipts) == 1 and order_no in receipts[0]["body"]
    assert len(metrics_store.list_lifecycle_events(
        account_id, kind=LIFECYCLE_RECEIPT)) == 1
    # The welcome mark is still the only one of its kind.
    assert len(metrics_store.list_lifecycle_events(
        account_id, kind=LIFECYCLE_WELCOME)) == 1

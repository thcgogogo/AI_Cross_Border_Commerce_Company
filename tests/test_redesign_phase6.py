"""UI redesign Phase 6 — 企业大脑 (B6) + run-kind quota fix + import hardening
acceptance tests (plan §3.7, §6 B6, §7 Phase 6).

What this suite pins down:

  1. Migration 0005 (real `alembic upgrade head` against a simulated pre-0005
     SQLite): runs.kind arrives NOT NULL DEFAULT 'report'; legacy import rows
     (recognised by their exact goal_text) become kind='import'; legacy wizard
     rows with the broken Tuesday cron '0 8 * * 1' are normalized to
     '0 8 * * mon'.
  2. B1 behavior — kind='import' runs no longer eat the plan quota
     (count_runs_total / count_runs_this_month) and never appear in
     /api/reports (they used to show as dead has_content=false rows).
  3. B2 — the wizard schedule click dedupes by job NAME (a legacy-cron row
     used to slip the dedupe and create a second weekly job = double billing),
     and re-points the legacy cron to Monday.
  4. B3 — the import path: /api/run-grade upload guards (magic bytes), CJK
     filenames survive, and the ¥ figures (estimated_cost / per-file cost /
     progress cost) are FOUNDER-ONLY — customers get file count + a rough
     duration; the progress poll is login + ownership gated.
  5. B6 — GET /api/company-brain/summary: 401 logged out; clean zeros + [] on
     an empty brain; populated shape (counts, KPI ranking by period coverage,
     delta/streak/series-of-12, dimension fallback) from directly seeded rows.
  6. E1 — concurrent /api/uploads adds are serialized per company (both files
     survive), and the route is rate-limited.
  7. E2 — budget_cny (internal cost cap) is redacted from scheduled-jobs
     responses for non-admin sessions; founders keep it.
  8. E3 — branding refuses a wechat_webhook whose host isn't
     qyapi.weixin.qq.com (the delivery-test SSRF surface), "" still clears.

Same isolation pattern as the phase-2/4/5 suites: fresh CustomerStore wired
into app.CUSTOMERS, throwaway SQLite DB, sessions set directly on the client,
storage dirs redirected into tmp.
"""
import io
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot is one-time & idempotent
from agent_team import config, db, metrics_store, trends  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.models import (  # noqa: E402
    RUN_KIND_IMPORT, RUN_KIND_REPORT, RUN_SUCCEEDED, SUB_ACTIVE,
    Metric, Subscription)

OWNER = "owner@corp.com"
OTHER = "other@corp.com"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh DB + customer store + tmp storage dirs; returns shared handles."""
    db.init_db(str(tmp_path / "phase6.db"))
    monkeypatch.setattr(app, "MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setattr(app, "UPLOAD_DIR", str(tmp_path / "rawuploads"))
    monkeypatch.setattr(metrics_store, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(metrics_store, "REPORTS_DIR", str(tmp_path / "reports"))
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    store.create_customer([], name="Owner", starting_balance=1000.0,
                          email=OWNER, password="password1")
    store.create_customer([], name="Other", starting_balance=1000.0,
                          email=OTHER, password="password1")
    monkeypatch.setattr(app, "CUSTOMERS", store)
    company_id = metrics_store.get_or_create_company(OWNER, company_name="OwnerCo")
    return {"store": store, "company_id": company_id}


def _client_for(store, email):
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return client


def _subscribe(account_id, plan_code):
    now = datetime.utcnow()
    with db.session_scope() as s:
        sub = (s.query(Subscription)
               .filter(Subscription.account_id == str(account_id)).one_or_none())
        if sub is None:
            s.add(Subscription(account_id=str(account_id), plan_code=plan_code,
                               status=SUB_ACTIVE, current_period_start=now,
                               current_period_end=now + timedelta(days=30),
                               cancel_at_period_end=False,
                               created_at=now, updated_at=now))
        else:
            sub.plan_code = plan_code
            sub.status = SUB_ACTIVE
            sub.current_period_end = now + timedelta(days=30)


def _seed_metrics(cid, metric_name, values, labels=None, dimension=None,
                  name_cn="销售额", unit="CNY"):
    """Insert one Metric row per value, oldest first (test_trends.py pattern)."""
    rid = metrics_store.create_run(cid, f"seed {metric_name}")
    if labels is None:
        labels = [f"2026-W{10 + i}" for i in range(len(values))]
    with db.session_scope() as s:
        for label, value in zip(labels, values):
            s.add(Metric(company_id=cid, run_id=rid, period_label=label,
                         metric_name=metric_name, metric_name_cn=name_cn,
                         value=value, unit=unit, dimension=dimension,
                         source_file="seed.xlsx"))
    return rid


# ---------------------------------------------------------------------------
# 1. Migration 0005 — the real alembic upgrade against a pre-0005 schema
# ---------------------------------------------------------------------------
def test_migration_0005_adds_kind_and_normalizes_legacy_rows(tmp_path):
    """Simulate a database deployed before Phase 6 (runs WITHOUT kind, a legacy
    import row, a legacy Tuesday wizard cron), stamped at 0004, then run the
    real `alembic upgrade head` and inspect what arrived."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY, account_email VARCHAR(320) NOT NULL,
            company_name VARCHAR(255), industry VARCHAR(255),
            created_at DATETIME NOT NULL);
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, goal_text TEXT,
            started_at DATETIME NOT NULL, finished_at DATETIME,
            status VARCHAR(14) NOT NULL, total_cost_yuan FLOAT,
            report_path VARCHAR(255));
        CREATE TABLE scheduled_jobs (
            id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL,
            name_cn VARCHAR(255) NOT NULL, cron_expr VARCHAR(128) NOT NULL,
            industry_override VARCHAR(64), goal_template TEXT,
            source_mode VARCHAR(13) NOT NULL, fixed_file_paths TEXT,
            delivery_channels TEXT, budget_cny FLOAT, active BOOLEAN NOT NULL,
            last_run_at DATETIME, last_status VARCHAR(64),
            created_at DATETIME NOT NULL);
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
        INSERT INTO alembic_version VALUES ('0004_phase10_onboarding');
        INSERT INTO companies VALUES (1, 'legacy@corp.com', 'LegacyCo', NULL,
                                      '2026-01-01');
        INSERT INTO runs (company_id, goal_text, started_at, status)
            VALUES (1, '历史数据导入 (historical import)', '2026-01-02', 'succeeded');
        INSERT INTO runs (company_id, goal_text, started_at, status)
            VALUES (1, '生成上周经营周报', '2026-01-03', 'succeeded');
        INSERT INTO scheduled_jobs
            (company_id, name_cn, cron_expr, source_mode, active, created_at)
            VALUES (1, '每周经营周报', '0 8 * * 1', 'latest_upload', 1, '2026-01-01');
        INSERT INTO scheduled_jobs
            (company_id, name_cn, cron_expr, source_mode, active, created_at)
            VALUES (1, '月度复盘', '0 8 * * 1', 'latest_upload', 1, '2026-01-01');
    """)
    conn.commit()
    conn.close()

    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"})
    assert proc.returncode == 0, proc.stderr

    conn = sqlite3.connect(db_path)
    cols = {row[1]: row for row in conn.execute("PRAGMA table_info(runs)")}
    assert "kind" in cols
    notnull, default = cols["kind"][3], cols["kind"][4]
    assert notnull == 1
    assert "report" in str(default)

    kinds = dict(conn.execute("SELECT goal_text, kind FROM runs"))
    assert kinds["历史数据导入 (historical import)"] == "import"
    assert kinds["生成上周经营周报"] == "report"

    crons = dict(conn.execute("SELECT name_cn, cron_expr FROM scheduled_jobs"))
    assert crons["每周经营周报"] == "0 8 * * mon"   # normalized
    assert crons["月度复盘"] == "0 8 * * 1"          # other jobs untouched
    conn.close()


# ---------------------------------------------------------------------------
# 2. B1 — import runs don't eat quota and don't pollute 历史报告
# ---------------------------------------------------------------------------
def test_import_runs_excluded_from_quota_and_reports(env):
    cid = env["company_id"]
    r1 = metrics_store.create_run(cid, "生成上周经营周报")           # default kind
    metrics_store.create_run(cid, "历史数据导入 (historical import)",
                             kind=RUN_KIND_IMPORT)
    metrics_store.finish_run(r1, RUN_SUCCEEDED)

    assert metrics_store.count_runs_total(cid) == 1
    assert metrics_store.count_runs_this_month(cid) == 1

    runs = metrics_store.list_runs(cid)
    assert [r["kind"] for r in runs] == [RUN_KIND_REPORT]
    assert len(metrics_store.list_runs(cid, kind=None)) == 2  # opt-in sees all

    client = _client_for(env["store"], OWNER)
    rows = client.get("/api/reports").get_json()["reports"]
    assert [r["id"] for r in rows] == [r1]   # the import run never appears


def test_create_run_rejects_unknown_kind(env):
    rid = metrics_store.create_run(env["company_id"], "x", kind="nonsense")
    assert metrics_store.get_run(rid)["kind"] == RUN_KIND_REPORT


# ---------------------------------------------------------------------------
# 3. B2 — wizard schedule dedupes by NAME and heals the legacy cron
# ---------------------------------------------------------------------------
def test_wizard_schedule_dedupes_legacy_cron_row_by_name(env):
    cid = env["company_id"]
    _subscribe(env["store"].find_by_email(OWNER)["id"], "pro")
    # The pre-fix wizard wrote this row — numeric cron that fired TUESDAY.
    metrics_store.create_scheduled_job(
        cid, app.ONBOARDING_WEEKLY_JOB_NAME, "0 8 * * 1",
        goal_template="生成上周经营周报", delivery_channels=["email"])

    client = _client_for(env["store"], OWNER)
    resp = client.post("/api/onboarding/schedule", json={"channels": ["email"]})
    assert resp.status_code == 200

    jobs = [j for j in metrics_store.list_scheduled_jobs(cid)
            if j["name_cn"] == app.ONBOARDING_WEEKLY_JOB_NAME]
    assert len(jobs) == 1, "the legacy row must be reused, never duplicated"
    assert jobs[0]["cron_expr"] == app.ONBOARDING_WEEKLY_CRON  # healed to Monday


# ---------------------------------------------------------------------------
# 4. B3 — import path hardening + founder-only ¥
# ---------------------------------------------------------------------------
def _csv_file(name="六月销售明细.csv", body=b"date,sales\n2026-06-01,100\n"):
    return (io.BytesIO(body), name)


def test_import_estimate_strips_cost_for_customers(env):
    client = _client_for(env["store"], OWNER)
    resp = client.post("/api/company-brain/import/estimate",
                       data={"files": [_csv_file()]},
                       content_type="multipart/form-data")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["file_count"] == 1
    assert body["estimated_seconds"] > 0
    assert "estimated_cost" not in body
    assert all("cost" not in item for item in body["files"])
    # The CJK name survives the sanitizer — it echoes in the customer UI.
    assert body["files"][0]["name"] == "六月销售明细.csv"


def test_import_estimate_keeps_cost_for_founder(env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    client = _client_for(env["store"], OWNER)
    resp = client.post("/api/company-brain/import/estimate",
                       data={"files": [_csv_file()]},
                       content_type="multipart/form-data")
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["estimated_cost"] > 0
    assert all(item["cost"] > 0 for item in body["files"])


def test_import_estimate_rejects_executable_magic_bytes(env):
    client = _client_for(env["store"], OWNER)
    resp = client.post("/api/company-brain/import/estimate",
                       data={"files": [(io.BytesIO(b"MZ\x90\x00payload"), "report.csv")]},
                       content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "可执行程序" in resp.get_json()["error"]


def test_import_progress_poll_auth_ownership_and_cost_redaction(env, monkeypatch):
    cid = env["company_id"]
    app.IMPORT_JOBS["impjob1"] = {
        "job_id": "impjob1", "company_id": cid, "status": "running",
        "total": 2, "done": 1, "cost": 0.42,
        "files": [{"name": "a.csv", "status": "done", "metrics": 7},
                  {"name": "b.csv", "status": "working", "metrics": 0}],
    }
    try:
        # Anonymous → 401 (was: open to the world).
        assert app.app.test_client().get(
            "/api/company-brain/import/impjob1").status_code == 401
        # Another company's session → same 404 as a missing job (no oracle).
        other = _client_for(env["store"], OTHER)
        assert other.get("/api/company-brain/import/impjob1").status_code == 404
        # The owner sees progress but never the ¥ tally.
        owner = _client_for(env["store"], OWNER)
        body = owner.get("/api/company-brain/import/impjob1").get_json()
        assert body["done"] == 1 and body["total"] == 2
        assert "cost" not in body and "company_id" not in body
        assert body["files"][0]["metrics"] == 7
        # The founder keeps the running ¥.
        monkeypatch.setenv("ADMIN_EMAILS", OWNER)
        body = owner.get("/api/company-brain/import/impjob1").get_json()
        assert body["cost"] == 0.42
        # Unknown job id → plain 404 (in-memory registry, lost on restart).
        assert owner.get("/api/company-brain/import/zzz").status_code == 404
    finally:
        app.IMPORT_JOBS.pop("impjob1", None)


# ---------------------------------------------------------------------------
# 5. B6 — /api/company-brain/summary
# ---------------------------------------------------------------------------
def test_summary_requires_login():
    resp = app.app.test_client().get("/api/company-brain/summary")
    assert resp.status_code == 401


def test_summary_empty_brain_answers_clean_zeros(env):
    client = _client_for(env["store"], OWNER)
    body = client.get("/api/company-brain/summary").get_json()
    assert body == {"metrics_count": 0, "kpi_count": 0, "weeks_covered": 0,
                    "glossary_count": 0, "preferences_count": 0, "kpis": []}


def test_summary_populated_shape_ranking_and_series(env):
    cid = env["company_id"]
    # 5-period company-total series (the richest → first tile), with a streak.
    _seed_metrics(cid, "total_sales", [500, 400, 300, 200, 100],
                  name_cn="销售额", unit="CNY")
    # 3-period total series → second tile.
    _seed_metrics(cid, "refund_rate", [3.0, 2.0, 2.5],
                  name_cn="退款率", unit="%")
    # A per-dimension series — must NOT appear while totals exist.
    _seed_metrics(cid, "channel_sales", [10, 20, 30, 40, 50, 60],
                  dimension="channel=独立站", name_cn="渠道销售额")
    # 13 periods on one metric → series must clip to the last 12.
    _seed_metrics(cid, "orders", list(range(13)),
                  labels=[f"2026-W{i:02d}" for i in range(1, 14)],
                  name_cn="订单数", unit="单")
    metrics_store.add_glossary_term(cid, "GMV", "成交总额")
    metrics_store.add_report_preference(cid, "结论先行")

    client = _client_for(env["store"], OWNER)
    body = client.get("/api/company-brain/summary").get_json()

    assert body["metrics_count"] == 5 + 3 + 6 + 13
    assert body["kpi_count"] == 4          # distinct metric names
    assert body["glossary_count"] == 1
    assert body["preferences_count"] == 1
    # weeks: W10-14 ∪ W10-12 ∪ W10-15 ∪ W01..W13 — careful: seeds share labels.
    with db.session_scope() as s:
        expected_weeks = len({m.period_label for m in s.query(Metric).all()})
    assert body["weeks_covered"] == expected_weeks

    kpis = body["kpis"]
    # Only NULL-dimension totals are tiled; ranked by period coverage desc.
    assert [k["name"] for k in kpis] == ["订单数", "销售额", "退款率"]

    orders = kpis[0]
    assert len(orders["series"]) == 12               # last 12 of 13
    assert orders["series"][0]["label"] == "2026-W02"
    assert orders["series"][-1] == {"label": "2026-W13", "value": 12.0}

    sales = kpis[1]
    assert sales["latest"] == 100
    assert sales["unit"] == "CNY"
    assert sales["delta_pct"] == -50.0               # 100 vs 200
    assert sales["streak"] == {"direction": "down", "length": 4}
    assert [p["value"] for p in sales["series"]] == [500, 400, 300, 200, 100]

    refund = kpis[2]
    assert refund["latest"] == 2.5
    assert refund["delta_pct"] == 25.0
    assert refund["streak"] is None


def test_summary_falls_back_to_dimension_series(env):
    cid = env["company_id"]
    _seed_metrics(cid, "channel_sales", [10, 20, 30],
                  dimension="channel=独立站", name_cn="渠道销售额", unit="USD")
    client = _client_for(env["store"], OWNER)
    kpis = client.get("/api/company-brain/summary").get_json()["kpis"]
    assert len(kpis) == 1
    assert kpis[0]["name"] == "渠道销售额（独立站）"
    assert kpis[0]["latest"] == 30


# ---------------------------------------------------------------------------
# 6. E1 — the uploads lock: concurrent adds both survive
# ---------------------------------------------------------------------------
def test_concurrent_upload_adds_preserve_both_files(env, monkeypatch):
    """Two simultaneous /api/uploads adds used to race read-prev → write and
    silently lose one file. With the per-company lock both must survive. The
    patched latest_upload_dir widens the race window so an unlocked
    implementation fails this test reliably."""
    real_latest = metrics_store.latest_upload_dir

    def slow_latest(company_id):
        out = real_latest(company_id)
        time.sleep(0.15)          # hold the read result while the other thread runs
        return out

    monkeypatch.setattr(metrics_store, "latest_upload_dir", slow_latest)
    store = env["store"]
    results = {}

    def post(name):
        client = _client_for(store, OWNER)
        resp = client.post(
            "/api/uploads",
            data={"files": [(io.BytesIO(b"d,v\n1,2\n"), name)]},
            content_type="multipart/form-data")
        results[name] = resp.status_code

    threads = [threading.Thread(target=post, args=(n,))
               for n in ("一月.csv", "二月.csv")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(results.values()) == {200}

    monkeypatch.setattr(metrics_store, "latest_upload_dir", real_latest)
    client = _client_for(store, OWNER)
    names = {f["name"] for f in client.get("/api/uploads/latest").get_json()["files"]}
    assert names == {"一月.csv", "二月.csv"}, "the race lost a file"


def test_uploads_route_is_rate_limited():
    # flask-limiter marks decorated views; /api/uploads was the only
    # unthrottled write route before E1.
    assert any("uploads_modify" in name
               for name in app.limiter._marked_for_limiting), \
        "POST /api/uploads must carry a @limiter.limit"


# ---------------------------------------------------------------------------
# 7. E2 — budget_cny is founder-only in scheduled-jobs responses
# ---------------------------------------------------------------------------
def test_budget_cny_redacted_for_customers_kept_for_founder(env, monkeypatch):
    cid = env["company_id"]
    _subscribe(env["store"].find_by_email(OWNER)["id"], "pro")
    metrics_store.create_scheduled_job(
        cid, "每周经营周报", "0 8 * * mon", delivery_channels=["email"],
        budget_cny=3.5)

    client = _client_for(env["store"], OWNER)
    job = client.get("/api/scheduled-jobs").get_json()["jobs"][0]
    assert "budget_cny" not in job
    assert job["next_run_at"]            # the useful fields are intact

    monkeypatch.setenv("ADMIN_EMAILS", OWNER)
    job = client.get("/api/scheduled-jobs").get_json()["jobs"][0]
    assert job["budget_cny"] == 3.5


# ---------------------------------------------------------------------------
# 8. E3 — wechat webhook host allow-list
# ---------------------------------------------------------------------------
def test_branding_rejects_non_wechat_webhook_hosts(env):
    client = _client_for(env["store"], OWNER)
    for bad in ("https://evil.example.com/hook",
                "http://qyapi.weixin.qq.com/cgi-bin/webhook/send",   # not https
                "https://qyapi.weixin.qq.com@evil.com/x",            # userinfo trick
                "ftp://qyapi.weixin.qq.com/x"):
        resp = client.post("/api/company-brain/branding",
                           data={"wechat_webhook": bad},
                           content_type="multipart/form-data")
        assert resp.status_code == 400, bad
        assert "仅支持企业微信群机器人地址" in resp.get_json()["error"]

    good = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123"
    resp = client.post("/api/company-brain/branding",
                       data={"wechat_webhook": good},
                       content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.get_json()["branding"]["wechat_webhook"] == good

    # "" still clears the stored webhook.
    resp = client.post("/api/company-brain/branding",
                       data={"wechat_webhook": ""},
                       content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.get_json()["branding"]["wechat_webhook"] == ""

"""
test_scheduler_delivery.py — Phase 5 acceptance tests for the "定时任务" engine,
the branded CJK PDF, the email/WeChat delivery layer, and the report-download
security boundary.

Run from the project root:

    .venv/bin/python -m pytest tests/test_scheduler_delivery.py -v

These mirror the four acceptance tests from the Phase 5 spec, verbatim:

  (a) a scheduled job (cron "*/2 * * * *") with an uploaded file and a funded
      account RUNS through the same billing path as an interactive run — a PDF is
      produced, the balance drops, and an email delivery row is recorded (the
      email is actually received by a local aiosmtpd debug server);
  (b) the rendered PDF embeds the bundled CJK font (so Chinese renders, not tofu);
  (c) a ¥0 account does NOT run — no run row is created and a "余额不足" notice is
      logged as a skipped delivery for the audit;
  (d) a logged-in account fetching ANOTHER account's report URL gets 403.

Every DB-touching test runs against its OWN throwaway SQLite file and redirects
the uploads/reports dirs into tmp, so the real data/ tree is never touched. No
test hits the network or spends real money — the model router is always a local
fake that bills through the real per-account path.
"""

import os
import socket
import sys
import time

import pytest

# Make the project root importable no matter where pytest is launched from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot (db.init_db + migrate) is one-time & idempotent
from agent_team import (  # noqa: E402
    config,
    db,
    delivery,
    metrics_store,
    orchestrator,
    pdf_report,
)
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.models import (  # noqa: E402
    DELIVERY_SENT,
    DELIVERY_SKIPPED,
    RUN_SUCCEEDED,
    SOURCE_LATEST_UPLOAD,
)
from agent_team.scheduler import ReportScheduler  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def fresh_db(tmp_path):
    """Point the company brain at a throwaway SQLite file for this one test."""
    db.init_db(str(tmp_path / "brain.db"))
    yield


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    """Redirect the on-disk uploads/reports/memory/output dirs into tmp, so a run
    triggered by a test never writes into the real data/ tree."""
    monkeypatch.setattr(metrics_store, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(metrics_store, "REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(app, "MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path / "outputs"))
    yield


# A Chinese weekly report the fake router returns for every model call. It carries
# a 核心结论 section (the email/WeChat blurb is extracted from it) and enough CJK
# text that the rendered PDF must embed the bundled font.
_CN_REPORT = (
    "# 经营周报\n\n"
    "## 核心结论\n"
    "- 本周销售额较上周增长 12.5%\n"
    "- 毛利率维持在 35%，符合预期\n"
    "- KA 渠道动销加快，库存周转改善\n\n"
    "## 数据分析\n"
    "本周整体经营稳中向好，各业务线表现符合预期，无重大异常。\n\n"
    "## 下周建议\n"
    "持续关注重点渠道的增长机会，并补充华东区域的库存。\n"
)


class _BillingChineseRouter:
    """A network-free stand-in for ModelRouter on the scheduled-run path.

    It is built BY the orchestrator (so the executor must be called with llm=None),
    which means it receives the orchestrator's _tally wrapper as `cost_listener`.
    Every model call bills through that real per-account path (decrementing the
    balance, tallying the run's ¥ cost, and raising BudgetExceededError if the
    balance is spent). It returns a Chinese report for BOTH the plain and the
    tool-capable code paths, so the synthesized final report — and the rendered
    PDF — contain CJK text. It never actually invokes a tool (no real files)."""

    def __init__(self, token_manager=None, cost_listener=None, **kwargs):
        self.cost_listener = cost_listener
        self.calls = 0

    def complete(self, **kwargs):
        if self.cost_listener:
            # A chunky-but-affordable call billed through the real path.
            self.cost_listener(config.HAIKU, 100_000, 20_000)
        self.calls += 1
        return _CN_REPORT

    def complete_with_tools(self, **kwargs):
        # Tool-capable agents (e.g. the media producer) take this path. We bill the
        # same way but DON'T call any tool, so the test produces no real files.
        return self.complete(**kwargs)


def _free_port():
    """An ephemeral localhost port for the debug SMTP server to bind."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _fund_account(store_path, email, balance):
    store = CustomerStore(store_path=str(store_path))
    store.create_customer([], name="Acct", starting_balance=balance,
                          email=email, password="pw")
    return store


# ---------------------------------------------------------------------------
# (a) A funded scheduled job RUNS: PDF produced, balance dropped, email delivered.
# ---------------------------------------------------------------------------
def test_scheduled_job_runs_produces_pdf_bills_and_delivers(
        fresh_db, tmp_storage, tmp_path, monkeypatch):
    from aiosmtpd.controller import Controller

    cid = metrics_store.get_or_create_company("owner@corp.com", company_name="OwnerCo")

    store = _fund_account(tmp_path / "customers.json", "owner@corp.com", 50_000.0)
    cust = store.find_by_email("owner@corp.com")
    cust_id, start_balance = cust["id"], cust["balance"]
    monkeypatch.setattr(app, "CUSTOMERS", store)
    # The orchestrator imports ModelRouter at module scope, so patch it THERE; with
    # llm=None the orchestrator builds our fake with the real biller wired in.
    monkeypatch.setattr(orchestrator, "ModelRouter", _BillingChineseRouter)

    # "Upload a file" — drop a spreadsheet into the account's newest upload batch,
    # which is exactly what a SOURCE_LATEST_UPLOAD job reads at fire time.
    batch = metrics_store.new_upload_batch_dir(cid)
    with open(os.path.join(batch, "week.csv"), "w", encoding="utf-8") as f:
        f.write("周次,销售额\n2026-W21,1000\n2026-W22,1125\n")

    # A local debug SMTP (aiosmtpd) that captures whatever the delivery layer sends.
    class _Capture:
        def __init__(self):
            self.messages = []

        async def handle_DATA(self, server, session, envelope):
            self.messages.append(envelope)
            return "250 Message accepted"

    handler = _Capture()
    port = _free_port()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    try:
        monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("SMTP_PORT", str(port))
        monkeypatch.setenv("SMTP_FROM", "no-reply@test.local")
        for k in ("SMTP_USER", "SMTP_PASS", "SMTP_STARTTLS", "SMTP_SSL"):
            monkeypatch.delenv(k, raising=False)

        job_id = metrics_store.create_scheduled_job(
            cid, "每周经营周报", "*/2 * * * *",
            goal_template="生成上周经营周报",
            source_mode=SOURCE_LATEST_UPLOAD,
            delivery_channels=["email"])
        assert job_id

        # The exact cron string from the spec registers with a LIVE scheduler.
        sched = ReportScheduler(lambda job: None)
        sched.start()
        try:
            assert ("job-%s" % job_id) in sched.job_ids()
        finally:
            sched.shutdown()

        # Fire the job exactly as the scheduler does at trigger time: re-read the
        # row fresh (this is where account_email must survive), then execute.
        job = metrics_store.get_scheduled_job(job_id)
        assert job and job.get("account_email") == "owner@corp.com"
        result = app._execute_scheduled_job(job)

        assert result["ran"] is True
        assert result["status"] == RUN_SUCCEEDED
        run_id = result["run_id"]
        assert run_id

        # A branded PDF was produced on disk for this run.
        pdf_path = metrics_store.report_pdf_path(cid, run_id)
        assert os.path.isfile(pdf_path)
        assert os.path.getsize(pdf_path) > 0

        # The account was billed through the real path: balance dropped, stayed +.
        new_balance = store.get_by_id(cust_id)["balance"]
        assert new_balance < start_balance
        assert new_balance > 0

        # An email delivery row was recorded with SENT status...
        deliveries = metrics_store.list_deliveries(company_id=cid)
        email_rows = [d for d in deliveries if d["channel"] == "email"]
        assert email_rows, "expected an email delivery row"
        assert email_rows[0]["status"] == DELIVERY_SENT

        # ...and the debug SMTP actually received the message.
        for _ in range(50):
            if handler.messages:
                break
            time.sleep(0.02)
        assert len(handler.messages) == 1
    finally:
        controller.stop()


# ---------------------------------------------------------------------------
# (b) The rendered PDF embeds the bundled CJK font (Chinese renders, not tofu).
# ---------------------------------------------------------------------------
def test_pdf_embeds_cjk_font(tmp_path):
    out = str(tmp_path / "report.pdf")
    pdf_report.build_report_pdf(
        out_path=out,
        markdown_text=_CN_REPORT,
        company_name="测试科技有限公司",
        report_title="经营周报",
        period_label="报告期：2026-W22")

    assert os.path.isfile(out)
    with open(out, "rb") as f:
        data = f.read()

    assert data[:4] == b"%PDF"
    # FontFile2 is reportlab's marker for an EMBEDDED TrueType program; together
    # with our face name it proves the Chinese glyphs travel inside the PDF.
    assert b"FontFile2" in data
    assert b"NotoSansSC" in data
    # An embedded font subset makes a real (non-empty) document.
    assert len(data) > 5000


# ---------------------------------------------------------------------------
# (c) A ¥0 account does NOT run — no run row, and a "余额不足" skip is logged.
# ---------------------------------------------------------------------------
def test_zero_balance_skips_run_and_logs_notice(
        fresh_db, tmp_storage, tmp_path, monkeypatch):
    cid = metrics_store.get_or_create_company("broke@corp.com", company_name="BrokeCo")

    store = _fund_account(tmp_path / "customers.json", "broke@corp.com", 0.0)
    monkeypatch.setattr(app, "CUSTOMERS", store)
    # No SMTP configured → the best-effort notice send is a no-op; the audit row
    # (a skipped delivery carrying the 余额不足 reason) is what we assert.
    monkeypatch.delenv("SMTP_HOST", raising=False)

    job_id = metrics_store.create_scheduled_job(
        cid, "每周经营周报", "0 8 * * 1",
        goal_template="生成上周经营周报",
        delivery_channels=["email"])
    job = metrics_store.get_scheduled_job(job_id)
    result = app._execute_scheduled_job(job)

    assert result["ran"] is False
    assert result["status"] == "skipped_no_balance"
    assert result["run_id"] is None

    # The balance gate fires BEFORE a run row is opened — nothing was created.
    assert metrics_store.list_runs(cid) == []

    # A skipped delivery carrying the exact "余额不足" notice was logged.
    deliveries = metrics_store.list_deliveries(company_id=cid)
    assert any(d["status"] == DELIVERY_SKIPPED
               and d["error"] == delivery.INSUFFICIENT_BALANCE_MSG
               for d in deliveries)


# ---------------------------------------------------------------------------
# (d) Cross-account report download is forbidden (403); owner succeeds (200).
# ---------------------------------------------------------------------------
def test_cross_account_report_download_is_forbidden(
        fresh_db, tmp_storage, tmp_path, monkeypatch):
    # The OWNER account has a finished run with a PDF on disk.
    cid_owner = metrics_store.get_or_create_company("owner@a.com", company_name="ACo")
    run_id = metrics_store.create_run(cid_owner, "生成上周经营周报")
    assert run_id
    pdf_path = metrics_store.report_pdf_path(cid_owner, run_id)
    os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
    with open(pdf_path, "wb") as f:
        f.write(b"%PDF-1.4 owner-only report body")

    # Two real accounts: the owner and an intruder (a different logged-in account).
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    store.create_customer([], name="Owner", starting_balance=100.0,
                          email="owner@a.com", password="pw")
    store.create_customer([], name="Intruder", starting_balance=100.0,
                          email="intruder@b.com", password="pw")
    monkeypatch.setattr(app, "CUSTOMERS", store)

    client = app.app.test_client()

    # The intruder requesting the owner's report URL is forbidden — 403, no file.
    intruder = store.find_by_email("intruder@b.com")
    with client.session_transaction() as sess:
        sess["customer_id"] = intruder["id"]
    forbidden = client.get("/api/reports/%s/download" % run_id)
    assert forbidden.status_code == 403
    assert b"%PDF" not in forbidden.data

    # The owner can download their OWN report.
    owner = store.find_by_email("owner@a.com")
    with client.session_transaction() as sess:
        sess["customer_id"] = owner["id"]
    ok = client.get("/api/reports/%s/download" % run_id)
    assert ok.status_code == 200
    assert ok.data[:4] == b"%PDF"

    # An unauthenticated request is rejected outright (401).
    anon = app.app.test_client()
    assert anon.get("/api/reports/%s/download" % run_id).status_code == 401

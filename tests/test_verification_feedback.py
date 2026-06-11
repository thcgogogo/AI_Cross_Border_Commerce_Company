"""
test_verification_feedback.py — Phase 6 acceptance tests for the verification
trail (Moat 3) and the report-feedback loop.

Run from the project root:

    .venv/bin/python -m pytest tests/test_verification_feedback.py -v

These mirror the five acceptance tests from the Phase 6 spec, verbatim:

  (1) a mocked writer output with 2 anchors and a valid SOURCES_JSON block splits
      into a clean body (anchors kept, machine line dropped) + 2 source rows, and
      the PDF's 数据来源与核验 section is a 2-row table (header + 2);
  (2) a malformed SOURCES_JSON block degrades gracefully — the report body still
      renders, a "本次未能生成数据溯源表" banner is shown, and nothing raises;
  (3) the cheap-model auditor, given a report that contains a number absent from
      the verified set, produces the "请人工确认" warning section (and is silent —
      never deletes content — when everything checks out, or on a budget cut-off);
  (4) a 👎 + "以后…" comment is flagged as a preference candidate; one click saves
      it into report_preferences and the NEXT run's memory context contains it;
  (5) /admin/feedback (and its CSV export) reject non-admin accounts and serve the
      founder allow-listed by $ADMIN_EMAILS — across all companies, filterable.

Every DB-touching test runs against its OWN throwaway SQLite file (a pytest
tmp_path) via the `fresh_db` fixture, so the real data/company_brain.db is never
touched. No test hits the network or spends real money — the LLM is a local fake.
"""

import csv
import io
import json
import os
import sys

import pytest

# Make the project root importable no matter where pytest is launched from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot (db.init_db + migrate) is one-time & idempotent
from agent_team import config, db, metrics_store, pdf_report, verification  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.token_manager import BudgetExceededError  # noqa: E402
from reportlab.platypus import Paragraph, Table  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    """Point the company brain at a throwaway SQLite file for this one test.

    Runs per-test, AFTER the one-time `import app` at collection (which binds the
    engine to the production path), so it always wins and keeps tests isolated.
    """
    db.init_db(str(tmp_path / "brain.db"))
    yield


class _FakeAuditorLLM:
    """Network-free stand-in for the cheap auditor model. Returns a canned string
    (we feed it a JSON array) and records the kwargs of the last call so a test can
    assert the known-numbers list and the report body actually reached the model."""

    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.last = None

    def complete(self, **kwargs):
        self.calls += 1
        self.last = kwargs
        return self.response


# A writer-style report carrying exactly two inline anchors (【1】【2】). The
# second number is a trend figure, so its source row says computed_by "trend_engine".
_REPORT_BODY = (
    "# 经营周报\n\n"
    "## 核心结论\n"
    "- 本周销售额为 1,234,567 元【1】。\n"
    "- 环比增长 3.2%【2】，主要由 KA 渠道拉动。\n"
)
_SRC_ROWS = [
    {"anchor": 1, "claim_cn": "本周销售额 1,234,567 元", "source_file": "5月销售.xlsx",
     "source_detail": "Sheet '5月' rows 2-145, column 销售额", "computed_by": "data_analyst"},
    {"anchor": 2, "claim_cn": "环比增长 3.2%", "source_file": "5月销售.xlsx",
     "source_detail": "趋势引擎按周环比计算", "computed_by": "trend_engine"},
]


def _writer_output(body, rows):
    """Assemble a full Report-Writer output: body + the machine SOURCES_JSON line."""
    return body + "\n\n" + verification.SOURCES_MARKER + " " + json.dumps(rows, ensure_ascii=False)


def _brand_and_styles():
    # The public build_report_pdf() registers the CJK font first; a direct call to
    # the section/styles helpers must do the same or reportlab can't resolve it.
    pdf_report._ensure_font()
    brand = pdf_report._brand(pdf_report.DEFAULT_BRAND_COLOR)
    return brand, pdf_report._styles(brand)


# ---------------------------------------------------------------------------
# (1) 2 anchors + valid SOURCES_JSON → clean body + 2 rows, 2-row PDF table.
# ---------------------------------------------------------------------------
def test_two_anchors_valid_sources_render_table_and_pdf(fresh_db, tmp_path):
    text = _writer_output(_REPORT_BODY, _SRC_ROWS)
    body, sources, status = verification.split_report_and_sources(text)

    assert status == "ok"
    assert sources is not None and len(sources) == 2
    # The body the user sees keeps every inline anchor but drops the machine line.
    assert "【1】" in body and "【2】" in body
    assert verification.SOURCES_MARKER not in body
    # A trend figure must be attributed to the deterministic engine, not the analyst.
    by_anchor = {s["anchor"]: s for s in sources}
    assert by_anchor[1]["computed_by"] == "data_analyst"
    assert by_anchor[2]["computed_by"] == "trend_engine"
    # Each row carries exactly the five fields the result page / PDF render.
    for s in sources:
        assert {"anchor", "claim_cn", "source_file", "source_detail", "computed_by"} <= set(s)

    # --- PDF: the 数据来源与核验 section appends a table of 1 header + 2 rows. ---
    brand, styles = _brand_and_styles()
    story = []
    verification_status = status
    pdf_report._append_sources_section(story, styles, brand, sources, verification_status, body)
    tables = [f for f in story if isinstance(f, Table)]
    assert len(tables) == 1
    assert tables[0]._nrows == 3  # header row + 2 source rows
    # The section heading is present.
    heads = " ".join(getattr(f, "text", "") for f in story if isinstance(f, Paragraph))
    assert "数据来源与核验" in heads

    # --- And the full branded PDF builds to disk without error. ---
    out = str(tmp_path / "report.pdf")
    pdf_report.build_report_pdf(
        out_path=out, markdown_text=body, company_name="测试公司",
        sources=sources, sources_status=status)
    assert os.path.isfile(out) and os.path.getsize(out) > 0


# ---------------------------------------------------------------------------
# (2) Malformed SOURCES_JSON → body still renders, banner shown, no exception.
# ---------------------------------------------------------------------------
def test_malformed_sources_render_banner_no_exception(fresh_db, tmp_path):
    # A truncated / non-JSON block after the marker.
    text = _REPORT_BODY + "\n\n" + verification.SOURCES_MARKER + " [ {oops not json "
    body, sources, status = verification.split_report_and_sources(text)

    assert status == "malformed"
    assert sources is None
    # The report is intact — anchors preserved, machine line stripped.
    assert "【1】" in body and "【2】" in body
    assert verification.SOURCES_MARKER not in body

    # PDF: a banner (NOT a table) is appended, and the document still builds.
    brand, styles = _brand_and_styles()
    story = []
    pdf_report._append_sources_section(story, styles, brand, sources, status, body)
    assert not any(isinstance(f, Table) for f in story)
    banner = " ".join(getattr(f, "text", "") for f in story if isinstance(f, Paragraph))
    assert "本次未能生成数据溯源表" in banner

    out = str(tmp_path / "report.pdf")
    pdf_report.build_report_pdf(
        out_path=out, markdown_text=body, company_name="测试公司",
        sources=sources, sources_status=status)
    assert os.path.isfile(out) and os.path.getsize(out) > 0


def test_missing_sources_on_non_report_shows_nothing(fresh_db):
    """A deliverable with no anchors (e.g. a poem / slide outline) and no
    SOURCES_JSON must NOT trigger a scary banner — render nothing."""
    body, sources, status = verification.split_report_and_sources("一首小诗，没有任何数字。")
    assert status == "missing" and sources is None

    brand, styles = _brand_and_styles()
    story = []
    pdf_report._append_sources_section(story, styles, brand, sources, status, body)
    assert story == []  # neither a table nor a banner


# ---------------------------------------------------------------------------
# (3) Auditor flags a number absent from the verified set → "请人工确认" section.
# ---------------------------------------------------------------------------
def test_auditor_flags_unverified_number(fresh_db):
    report = "## 核心结论\n本周销售额 1,234,567 元，另有 9,999,999 元的神秘收入。"
    known = {"1,234,567", "1234567"}
    llm = _FakeAuditorLLM(json.dumps(["9,999,999"]))

    unverified = verification.audit_report(report, known, llm, model=config.HAIKU)
    assert unverified == ["9,999,999"]

    warning = verification.build_audit_warning(unverified)
    assert "请人工确认" in warning
    assert "9,999,999" in warning
    # It is a PREPENDABLE section (a markdown heading) — content is flagged, not deleted.
    assert warning.lstrip().startswith("#")

    # The model was actually consulted with both the known list and the report body.
    assert llm.calls == 1
    assert "9,999,999" in llm.last["user_content"]
    assert "1,234,567" in llm.last["user_content"]


def test_auditor_clean_report_produces_no_warning(fresh_db):
    llm = _FakeAuditorLLM("[]")
    unverified = verification.audit_report(
        "本周销售额 1,234,567 元【1】。", {"1,234,567"}, llm)
    assert unverified == []
    assert verification.build_audit_warning(unverified) == ""


def test_auditor_budget_cutoff_is_silent(fresh_db):
    """A budget cut-off during the OPTIONAL audit must never unwind a run whose
    report is already assembled — it degrades to "no warning"."""
    class _Boom:
        def complete(self, **kwargs):
            raise BudgetExceededError(config.AGENT_TEAM_BUDGET, "spent")

    assert verification.audit_report("神秘数字 42。", {"1"}, _Boom()) == []


# ---------------------------------------------------------------------------
# (4) 👎 + "以后…" → save preference → next run's memory context contains it.
# ---------------------------------------------------------------------------
def test_down_then_save_preference_reaches_next_run_context(fresh_db, tmp_path, monkeypatch):
    cid = metrics_store.get_or_create_company("boss@corp.com", company_name="BossCo")

    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    store.create_customer([], name="Boss", starting_balance=100.0,
                          email="boss@corp.com", password="pw")
    cust = store.find_by_email("boss@corp.com")
    monkeypatch.setattr(app, "CUSTOMERS", store)

    run_id = metrics_store.create_run(cid, "生成上周经营周报")
    assert run_id

    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = cust["id"]

    # A 👎 whose comment starts with "以后…" is surfaced as a preference candidate.
    r = client.post("/api/feedback", json={
        "run_id": run_id, "section_title": "退款", "vote": "down",
        "comment": "以后把退款放第一节",
    })
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["preference_candidate"] is True
    fid = data["id"]

    # One click promotes it to a STANDING report preference.
    r2 = client.post("/api/feedback/save-preference", json={"id": fid})
    assert r2.status_code == 200
    prefs = r2.get_json()["preferences"]
    assert any("以后把退款放第一节" in p["instruction"] for p in prefs)

    # The NEXT run's memory context now carries that instruction verbatim.
    ctx = metrics_store.load_memory_context(cid)
    assert "以后把退款放第一节" in ctx


def test_feedback_rejects_cross_account_run(fresh_db, tmp_path, monkeypatch):
    """A run id that belongs to ANOTHER company must never accept feedback from
    the logged-in account (the same security boundary as report downloads)."""
    mine = metrics_store.get_or_create_company("me@corp.com")
    other = metrics_store.get_or_create_company("rival@corp.com")
    other_run = metrics_store.create_run(other, "别人的报告")

    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    store.create_customer([], name="Me", starting_balance=10.0,
                          email="me@corp.com", password="pw")
    monkeypatch.setattr(app, "CUSTOMERS", store)

    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email("me@corp.com")["id"]

    r = client.post("/api/feedback", json={
        "run_id": other_run, "section_title": "核心结论", "vote": "up",
    })
    assert r.status_code == 403
    # Nothing was written for either company.
    assert metrics_store.list_feedback(company_id=mine) == []
    assert metrics_store.list_feedback(company_id=other) == []


# ---------------------------------------------------------------------------
# (5) /admin/feedback gate + CSV export across companies, filterable by vote.
# ---------------------------------------------------------------------------
def test_admin_feedback_rejects_non_admin(fresh_db, tmp_path, monkeypatch):
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    store.create_customer([], name="NormalUser", starting_balance=10.0,
                          email="user@corp.com", password="pw")
    store.create_customer([], name="Founder", starting_balance=10.0,
                          email="founder@corp.com", password="pw")
    monkeypatch.setattr(app, "CUSTOMERS", store)
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")

    client = app.app.test_client()

    # Logged OUT → 403.
    assert client.get("/admin/feedback").status_code == 403

    # Logged-in NON-admin → 403 (page + CSV).
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email("user@corp.com")["id"]
    assert client.get("/admin/feedback").status_code == 403
    assert client.get("/admin/feedback/export.csv").status_code == 403

    # The founder (in $ADMIN_EMAILS) → 200 and sees the dashboard.
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email("founder@corp.com")["id"]
    ok = client.get("/admin/feedback")
    assert ok.status_code == 200
    assert "反馈看板" in ok.get_data(as_text=True)


def test_admin_feedback_csv_export_and_vote_filter(fresh_db, tmp_path, monkeypatch):
    # Two different companies, mixed votes — the dashboard mines ACROSS companies.
    c1 = metrics_store.get_or_create_company("a@corp.com")
    c2 = metrics_store.get_or_create_company("b@corp.com")
    metrics_store.record_feedback(c1, None, "核心结论", "up", "很好")
    metrics_store.record_feedback(c2, None, "退款", "down", "以后把退款放第一节")

    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    store.create_customer([], name="Founder", starting_balance=10.0,
                          email="founder@corp.com", password="pw")
    monkeypatch.setattr(app, "CUSTOMERS", store)
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")

    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email("founder@corp.com")["id"]

    # Full CSV: correct header + both companies' feedback present.
    resp = client.get("/admin/feedback/export.csv")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    full = resp.get_data(as_text=True)
    rows = list(csv.reader(io.StringIO(full)))
    assert rows[0] == ["id", "created_at", "account_email", "company_id",
                       "run_id", "section_title", "vote", "comment"]
    assert "很好" in full and "以后把退款放第一节" in full
    assert "a@corp.com" in full and "b@corp.com" in full

    # ?vote=down filters to just the 👎 row.
    down = client.get("/admin/feedback/export.csv?vote=down").get_data(as_text=True)
    assert "以后把退款放第一节" in down
    assert "很好" not in down

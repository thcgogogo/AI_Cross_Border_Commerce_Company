"""
test_company_brain.py — Phase 3 acceptance tests for the "企业大脑" (Company Brain):
the KPI-mapping injection, the glossary learning loop (UNKNOWN_TERMS → confirm →
remembered), the historical-import billing path, and the logged-out guard.

Run from the project root:

    .venv/bin/python -m pytest tests/test_company_brain.py -v

Every DB-touching test runs against its OWN throwaway SQLite file (a pytest
tmp_path) via the `fresh_db` fixture, so the real data/company_brain.db is never
used and tests never interfere. No test hits the network or spends real money —
the LLM is always a local fake.
"""

import json
import os
import sys

import pytest

# Make the project root importable no matter where pytest is launched from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot (db.init_db + migrate) is one-time & idempotent
from agent_team import config, db, metrics_store  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.memory import SharedMemory  # noqa: E402
from agent_team.models import GlossaryTerm, Metric  # noqa: E402
from agent_team.orchestrator import Orchestrator  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    """Point the company brain at a throwaway SQLite file for this one test.

    Runs per-test, AFTER the one-time `import app` at collection (which binds the
    engine to the production path), so it always wins and keeps tests isolated.
    """
    db.init_db(str(tmp_path / "brain.db"))
    yield


class _FakeLLM:
    """Network-free stand-in for ModelRouter, used where only Orchestrator
    construction matters (its .complete is never reached in that path)."""

    def __init__(self, response="[]"):
        self.response = response
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        return self.response


class _FakeRouter:
    """ModelRouter replacement for the import test. It accepts the SAME wiring
    kwargs the real router gets (token_manager / cost_listener), BILLS every call
    through the injected cost_listener (the real per-account biller — so the
    account balance is actually decremented), and returns a canned metrics JSON
    whose period_label advances each call, so N files yield N distinct periods."""

    PERIODS = ["2026-W21", "2026-W22", "2026-W23", "2026-W24"]

    def __init__(self, token_manager=None, cost_listener=None, **kwargs):
        self.cost_listener = cost_listener
        self.calls = 0

    def complete(self, **kwargs):
        # A chunky-but-affordable call, billed through the real path (decrements
        # the balance, tallies job cost, would raise BudgetExceededError if spent).
        if self.cost_listener:
            self.cost_listener(config.HAIKU, 100_000, 20_000)
        label = self.PERIODS[self.calls % len(self.PERIODS)]
        self.calls += 1
        return json.dumps([{
            "metric_name": "total_sales", "metric_name_cn": "总销售额",
            "value": 1000 + self.calls, "unit": "CNY", "period_label": label,
            "dimension": None, "source_file": "import.xlsx", "source_detail": None,
        }])


# ---------------------------------------------------------------------------
# Test 1 — a saved KPI mapping reaches the Data Analyst's system prompt.
# ---------------------------------------------------------------------------
def test_kpi_mapping_reaches_data_analyst_prompt(fresh_db, tmp_path):
    """Saving a KPI mapping then starting a run injects the alias list AND the
    verbatim mapping instruction into the Data Analyst's prompt."""
    cid = metrics_store.get_or_create_company("kpi@corp.com", company_name="KpiCo")
    metrics_store.save_profile(cid, {"kpi_mappings": [
        {"kpi_name_cn": "毛利", "aliases": "毛利,毛利润,gross profit", "unit": "CNY"},
    ]})

    mem = SharedMemory("brain-test-kpi", store_dir=str(tmp_path / "mem"))
    orch = Orchestrator(mem, llm=_FakeLLM(), company_id=cid)

    prompt = orch.workers["data_analyst"].cfg["system_prompt"]
    assert "gross profit" in prompt          # the configured aliases are present
    assert "毛利" in prompt                    # the Chinese KPI name is present
    # The exact words the spec requires the Data Analyst to receive, verbatim.
    assert metrics_store.KPI_MAPPING_INSTRUCTION in prompt

    # Injection must never mutate the SHARED config — other workers stay clean.
    base = {w["id"]: w["system_prompt"] for w in config.get_workers()}
    assert metrics_store.KPI_MAPPING_INSTRUCTION not in base["data_analyst"]


# ---------------------------------------------------------------------------
# Test 2 — the learning loop: UNKNOWN_TERMS → confirm → next run remembers it.
# ---------------------------------------------------------------------------
def test_unknown_term_confirmed_is_remembered_next_run(fresh_db):
    """A Data Analyst output ending in UNKNOWN_TERMS parses to that term;
    confirming a definition writes a glossary row; the next run's memory context
    then contains the term."""
    cid = metrics_store.get_or_create_company("learn@corp.com")

    analysis = (
        "本周销售分析……KA渠道表现突出。\n\n"
        'UNKNOWN_TERMS: ["KA渠道"]'
    )
    terms = metrics_store.parse_unknown_terms(analysis)
    assert terms == ["KA渠道"]

    # The /api/glossary/confirm flow persists the user's answer(s).
    written = metrics_store.add_glossary_terms(cid, {"KA渠道": "Key Account 大客户渠道"})
    assert written == 1

    # A real glossary row now exists for the company...
    rows = metrics_store.list_glossary(cid)
    assert any(r["term"] == "KA渠道" and "Key Account" in r["definition"] for r in rows)

    # ...and the memory injected into the NEXT run includes it.
    ctx = metrics_store.load_memory_context(cid)
    assert "KA渠道" in ctx
    assert "Key Account" in ctx


# ---------------------------------------------------------------------------
# Test 3 — historical import: 3 files → 3 periods, billed through the real path.
# ---------------------------------------------------------------------------
def test_historical_import_creates_periods_and_bills_account(fresh_db, tmp_path, monkeypatch):
    """Importing 3 past spreadsheets creates metrics rows for 3 distinct
    period_labels and charges/deducts the balance through the existing billing
    path (CUSTOMERS.record_usage), with the job completing cleanly."""
    cid = metrics_store.get_or_create_company("import@corp.com", company_name="ImportCo")

    # A throwaway account store with a comfortable balance (no cutoff expected).
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    store.create_customer([], name="Importer", starting_balance=10_000.0,
                          email="import@corp.com", password="pw")
    cust = store.find_by_email("import@corp.com")
    cust_id, start_balance = cust["id"], cust["balance"]

    # Bill against the temp store, and have _run_import_job build our fake router
    # (with the REAL biller wired in) by leaving llm=None below.
    monkeypatch.setattr(app, "CUSTOMERS", store)
    monkeypatch.setattr(app, "ModelRouter", _FakeRouter)

    files = [("q1.xlsx", "raw 1"), ("q2.xlsx", "raw 2"), ("q3.xlsx", "raw 3")]
    job = {
        "job_id": "imp-1", "status": "running", "total": len(files), "done": 0,
        "cost": 0.0,
        "files": [{"name": n, "status": "pending", "metrics": 0} for n, _ in files],
    }

    app._run_import_job(job, cust_id, cid, files)  # llm=None → builds _FakeRouter

    # Three files → three distinct period_labels persisted to the brain.
    with db.session_scope() as s:
        periods = {m.period_label for m in s.query(Metric).all()}
    assert len(periods) == 3

    # The job finished and every file was processed.
    assert job["status"] == "done"
    assert job["done"] == 3

    # The account was charged through the real billing path: balance went down,
    # but stayed positive (we gave it plenty of headroom — no budget cutoff).
    assert job["cost"] > 0
    new_balance = store.get_by_id(cust_id)["balance"]
    assert new_balance < start_balance
    assert new_balance > 0


# ---------------------------------------------------------------------------
# Test 4 — logged-out users cannot access /company-brain (302 → /login).
# ---------------------------------------------------------------------------
def test_company_brain_requires_login():
    """A request with no session is redirected to the login page."""
    client = app.app.test_client()
    resp = client.get("/company-brain")
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")

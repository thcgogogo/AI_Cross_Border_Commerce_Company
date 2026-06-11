"""
test_memory_store.py — Acceptance tests for the persistent company brain.

Run from the project root:

    .venv/bin/python -m pytest tests/test_memory_store.py -v

Each test runs against its OWN isolated SQLite file (a pytest tmp_path), so the
real data/company_brain.db is never touched and tests never interfere.
"""

import json
import os
import sys

import pytest

# Make the project root importable no matter where pytest is launched from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import db, metrics_store  # noqa: E402
from agent_team.models import Company, GlossaryTerm, Metric, Run  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    """Point the company brain at a throwaway SQLite file for this one test."""
    db.init_db(str(tmp_path / "brain.db"))
    yield


class _FakeLLM:
    """Stand-in for ModelRouter: returns a canned string and counts calls, so the
    extraction tests never hit the network or spend money."""

    def __init__(self, response):
        self.response = response
        self.calls = 0
        self.last_kwargs = None

    def complete(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        return self.response


# ---------------------------------------------------------------------------
def test_two_runs_reuse_one_company(fresh_db):
    """Creating two runs for the same account reuses ONE companies row."""
    cid1 = metrics_store.get_or_create_company("owner@acme.com", company_name="Acme")
    run1 = metrics_store.create_run(cid1, "First weekly report")
    cid2 = metrics_store.get_or_create_company("owner@acme.com")  # same email again
    run2 = metrics_store.create_run(cid2, "Second weekly report")

    assert cid1 == cid2                # same company
    assert run1 and run2 and run1 != run2  # two distinct runs

    with db.session_scope() as s:
        assert s.query(Company).count() == 1
        assert s.query(Run).count() == 2


def test_email_is_case_insensitive(fresh_db):
    """The email key is normalised, so casing/whitespace can't fork a company."""
    a = metrics_store.get_or_create_company("Owner@Acme.com")
    b = metrics_store.get_or_create_company("  owner@acme.com  ")
    assert a == b
    with db.session_scope() as s:
        assert s.query(Company).count() == 1


# ---------------------------------------------------------------------------
def test_extract_three_metrics_inserts_three_rows(fresh_db):
    """A mocked LLM returning 3 metrics inserts 3 rows with correct values."""
    cid = metrics_store.get_or_create_company("data@corp.com")
    rid = metrics_store.create_run(cid, "Analyze week 23")

    payload = json.dumps([
        {"metric_name": "total_sales", "metric_name_cn": "总销售额", "value": 125000,
         "unit": "CNY", "period_label": "2026-W23", "dimension": None,
         "source_file": "sales.xlsx", "source_detail": "rows 2-145"},
        {"metric_name": "order_count", "metric_name_cn": "订单数", "value": 342,
         "unit": "orders", "period_label": "2026-W23", "dimension": None,
         "source_file": "sales.xlsx", "source_detail": None},
        {"metric_name": "growth_rate", "metric_name_cn": "增长率", "value": 12.5,
         "unit": "%", "period_label": "2026-W23", "dimension": "region=华东",
         "source_file": "sales.xlsx", "source_detail": None},
    ])
    llm = _FakeLLM(payload)

    n = metrics_store.extract_metrics_to_db("the analysis text", cid, rid, llm=llm)

    assert n == 3
    assert llm.calls == 1  # exactly ONE extra LLM call
    with db.session_scope() as s:
        rows = s.query(Metric).all()
        assert len(rows) == 3
        by_name = {m.metric_name: m for m in rows}
        assert by_name["total_sales"].value == 125000
        assert by_name["order_count"].value == 342
        # A percentage is stored as the bare number with unit "%".
        assert by_name["growth_rate"].value == 12.5
        assert by_name["growth_rate"].unit == "%"
        assert by_name["growth_rate"].dimension == "region=华东"


def test_extract_malformed_json_inserts_zero_and_does_not_raise(fresh_db):
    """Malformed JSON inserts 0 rows and NEVER raises (extraction is best-effort)."""
    cid = metrics_store.get_or_create_company("data@corp.com")
    rid = metrics_store.create_run(cid, "Analyze week 23")

    # Broken array: a stray comma + missing value — json.loads will reject it.
    llm = _FakeLLM('```json\n[{"metric_name": "x", "value": ,, }]\n```')

    n = metrics_store.extract_metrics_to_db("the analysis text", cid, rid, llm=llm)

    assert n == 0
    with db.session_scope() as s:
        assert s.query(Metric).count() == 0


def test_extract_handles_prose_with_no_json(fresh_db):
    """Plain prose (no JSON at all) also inserts 0 rows without raising."""
    cid = metrics_store.get_or_create_company("data@corp.com")
    rid = metrics_store.create_run(cid, "g")
    llm = _FakeLLM("I'm sorry, I could not find any numeric metrics in that text.")
    assert metrics_store.extract_metrics_to_db("x", cid, rid, llm=llm) == 0


# ---------------------------------------------------------------------------
def test_load_memory_context_recent_period_and_glossary(fresh_db):
    """With 10 periods of metrics, the context contains the most recent period's
    label and at least one glossary term."""
    cid = metrics_store.get_or_create_company("memory@corp.com", company_name="MemCo")

    # One glossary term.
    with db.session_scope() as s:
        s.add(GlossaryTerm(company_id=cid, term="GMV",
                           definition="gross merchandise value / 商品交易总额"))

    # 10 weekly periods, inserted oldest -> newest (W10 .. W19).
    labels = [f"2026-W{w}" for w in range(10, 20)]
    for label in labels:
        rid = metrics_store.create_run(cid, f"goal {label}")
        payload = json.dumps([
            {"metric_name": "total_sales", "metric_name_cn": "总销售额",
             "value": 1000 + int(label.split("W")[1]), "unit": "CNY",
             "period_label": label, "dimension": None,
             "source_file": "sales.xlsx", "source_detail": None},
        ])
        metrics_store.extract_metrics_to_db("analysis", cid, rid, llm=_FakeLLM(payload))

    ctx = metrics_store.load_memory_context(cid)

    assert labels[-1] in ctx          # most recent period (2026-W19) present
    assert "GMV" in ctx               # at least one glossary term present
    # The oldest period should have been dropped (only the newest 8 are kept).
    assert labels[0] not in ctx       # 2026-W10 trimmed


def test_build_memory_block_includes_verbatim_header(fresh_db):
    """The injected block leads with the exact Chinese COMPANY MEMORY header, and
    a brand-new company yields an empty block."""
    # Empty company -> empty block (nothing to inject).
    cid = metrics_store.get_or_create_company("blank@corp.com")
    assert metrics_store.build_memory_block(cid) == ""

    # Once it has data, the block starts with the verbatim header.
    rid = metrics_store.create_run(cid, "g")
    payload = json.dumps([
        {"metric_name": "total_sales", "metric_name_cn": "总销售额", "value": 5,
         "unit": "CNY", "period_label": "2026-W23", "dimension": None,
         "source_file": "s.xlsx", "source_detail": None},
    ])
    metrics_store.extract_metrics_to_db("a", cid, rid, llm=_FakeLLM(payload))
    block = metrics_store.build_memory_block(cid)
    assert block.startswith(metrics_store.MEMORY_HEADER)
    assert "公司历史记忆" in block


# ---------------------------------------------------------------------------
def test_migration_is_idempotent(fresh_db, tmp_path):
    """Migrating the JSON account store creates one company per account, and
    re-running it creates none (idempotent)."""
    customers_path = tmp_path / "customers.json"
    customers_path.write_text(json.dumps({
        "customers": [
            {"id": "cus_1", "email": "a@x.com", "company": "Alpha", "name": "Al"},
            {"id": "cus_2", "email": "b@x.com", "company": None, "name": "Bee"},
            {"id": "cus_3", "email": "", "name": "no-email"},  # skipped (no email)
        ],
    }), encoding="utf-8")

    created = metrics_store.migrate_accounts_to_companies(str(customers_path))
    assert created == 2  # the two with emails

    again = metrics_store.migrate_accounts_to_companies(str(customers_path))
    assert again == 0  # idempotent

    with db.session_scope() as s:
        assert s.query(Company).count() == 2

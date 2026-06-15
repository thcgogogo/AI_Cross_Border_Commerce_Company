"""
test_report_pricing_removed.py — WS10: report-delivery pricing discarded (2026-06-15).

The _plan_gate quota/credit/lock machinery is now DORMANT by default
(config.report_quota_enabled() is False), so any signed-in account can generate reports.
This proves the gate is open AND that the orthogonal subsystems — auth, the ¥ wallet, and
the platform breaker — are fully intact (they live on the /api/run path independent of the
gate). The gate body is kept for later re-enabling via REPORT_QUOTA_ENABLED=1.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402
from agent_team import config, db, metrics_store, platform_guard  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "x.db"))
    yield


@pytest.fixture(autouse=True)
def _gate_off(monkeypatch):
    monkeypatch.delenv("REPORT_QUOTA_ENABLED", raising=False)   # the product default


def test_report_quota_disabled_by_default():
    assert config.report_quota_enabled() is False


def test_plan_gate_open_for_free_account_over_old_quota(fresh_db, tmp_path):
    store = CustomerStore(store_path=str(tmp_path / "c.json"))
    store.create_customer([], name="u", starting_balance=0.0,   # spent ¥0 trial cap too
                          email="u@corp.com", password="pw123456")
    app.CUSTOMERS = store
    cust = store.find_by_email("u@corp.com")
    cid = metrics_store.get_or_create_company("u@corp.com")
    for i in range(3):                                          # 3 > the old free 2-lifetime quota
        rid = metrics_store.create_run(cid, f"report {i}")
        metrics_store.finish_run(rid, "succeeded")
    assert metrics_store.count_runs_total(cid) == 3
    # Gate OFF → allowed regardless of quota or a spent balance.
    ok, _msg, code = app._plan_gate(cust)
    assert ok is True and code is None


def test_auth_wallet_breaker_intact(fresh_db, tmp_path):
    store = CustomerStore(store_path=str(tmp_path / "c.json"))
    store.create_customer([config.HAIKU], name="u", starting_balance=50.0,
                          email="u@corp.com", password="pw123456")
    app.CUSTOMERS = store

    # (a) AUTH still works — orthogonal to the report gate.
    assert store.verify_login("u@corp.com", "pw123456")["email"] == "u@corp.com"
    assert store.verify_login("u@corp.com", "wrong-password") is None

    # (b) The ¥ WALLET still meters + decrements.
    cust = store.find_by_email("u@corp.com")
    before = cust["balance"]
    billed = store.record_usage(cust["id"], config.HAIKU, 500_000, 200_000)
    assert billed["balance"] < before

    # (c) The platform BREAKER still functions (returns a verdict).
    allowed, _reason = platform_guard.check(cust["id"], None)
    assert isinstance(allowed, bool)

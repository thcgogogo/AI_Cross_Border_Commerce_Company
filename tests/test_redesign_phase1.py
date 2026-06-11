"""UI redesign Phase 1 — backend alignment acceptance tests (plan §7, tasks B1/B2/B3).

Four checks mirror the phase's acceptance criteria:

  1. /api/meta exposes the product identity (PRODUCT_NAME / PRODUCT_TAGLINE,
     env-overridable) and the zero-prompt report goal templates (周报/月报).
  2. /api/me and the login response carry is_admin (email ∈ $ADMIN_EMAILS) —
     the flag the frontend uses to reveal founder mode.
  3. /api/plans shows the brief's flat plans: 免费体验 (2 reports, LIFETIME) /
     基础版 (20/月, 1 seat) / 企业版 (high-volume, 5 seats, gated features),
     with internal codes unchanged; config.plan_has() reads the feature flags.
  4. The run quota gate treats the free tier's 2 reports as a LIFETIME total:
     the 3rd run is refused (402) even when earlier runs were last month.

Same isolation pattern as the Phase 8/9 suites: a throwaway SQLite DB per test
and a fresh CustomerStore wired into app.CUSTOMERS.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot is one-time & idempotent
from agent_team import config, db, metrics_store  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.models import RUN_SUCCEEDED, Run  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    """A throwaway SQLite file per test — companies and runs start empty."""
    db.init_db(str(tmp_path / "brain.db"))
    yield


def _store(tmp_path, **accounts):
    """A fresh CustomerStore wired into the app, pre-loaded with {email: password}
    accounts (each with ¥100 demo credit so the balance gate stays open)."""
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    for email, password in accounts.items():
        store.create_customer([], name=email.split("@")[0], starting_balance=100.0,
                              email=email, password=password)
    app.CUSTOMERS = store
    return store


def _login(store, email):
    """A test client with `email`'s session cookie established."""
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return client


# ---------------------------------------------------------------------------
# (B1) /api/meta carries the product identity + the report goal templates.
# ---------------------------------------------------------------------------
def test_meta_exposes_product_identity_and_goal_templates(monkeypatch):
    client = app.app.test_client()
    meta = client.get("/api/meta").get_json()

    assert meta["product_name"] == config.product_name() != ""
    assert meta["product_tagline"] == config.product_tagline() != ""

    templates = meta["report_goal_templates"]
    assert [t["code"] for t in templates] == ["weekly", "monthly"]
    by_code = {t["code"]: t for t in templates}
    assert by_code["weekly"]["label"] == "经营周报"
    assert by_code["monthly"]["label"] == "经营月报"
    for t in templates:
        # A composed goal must be substantial enough to drive the pipeline and
        # must demand the boss-report structure, not just name the report type.
        assert len(t["goal"]) > 30
        assert "核心结论" in t["goal"]

    # The identity is env-overridable without a restart (redesign B1).
    monkeypatch.setenv("PRODUCT_NAME", "测试品牌")
    monkeypatch.setenv("PRODUCT_TAGLINE", "测试标语")
    meta2 = client.get("/api/meta").get_json()
    assert meta2["product_name"] == "测试品牌"
    assert meta2["product_tagline"] == "测试标语"


# ---------------------------------------------------------------------------
# (B2) /api/me and the login response carry is_admin.
# ---------------------------------------------------------------------------
def test_me_and_login_carry_is_admin_flag(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")
    store = _store(tmp_path, **{"founder@corp.com": "password1",
                                "customer@corp.com": "password1"})

    # The login response itself carries the flag (the frontend gates founder
    # mode off it immediately, before any /api/me refresh).
    client = app.app.test_client()
    r = client.post("/api/login", json={"email": "founder@corp.com",
                                        "password": "password1"})
    assert r.status_code == 200
    assert r.get_json()["customer"]["is_admin"] is True
    assert client.get("/api/me").get_json()["customer"]["is_admin"] is True

    client2 = app.app.test_client()
    r = client2.post("/api/login", json={"email": "customer@corp.com",
                                         "password": "password1"})
    assert r.status_code == 200
    assert r.get_json()["customer"]["is_admin"] is False
    assert client2.get("/api/me").get_json()["customer"]["is_admin"] is False


# ---------------------------------------------------------------------------
# (B3) /api/plans matches the brief: labels, quotas, seats, features.
# ---------------------------------------------------------------------------
def test_plans_catalog_matches_brief():
    client = app.app.test_client()
    plans = {p["code"]: p for p in client.get("/api/plans").get_json()["plans"]}

    free, basic, pro = plans["free"], plans["basic"], plans["pro"]
    assert free["label"] == "免费体验"
    assert free["monthly_report_quota"] == 2
    assert free["lifetime_quota"] is True
    assert free["monthly_price_yuan"] == 0.0

    assert basic["label"] == "基础版"
    assert basic["monthly_report_quota"] == 20
    assert basic["lifetime_quota"] is False
    assert basic["seats"] == 1
    assert basic["monthly_price_yuan"] == 199.0

    assert pro["label"] == "企业版"
    assert pro["seats"] == 5
    assert pro["monthly_price_yuan"] == 599.0
    assert pro["monthly_report_quota"] >= 100   # "high-volume reports"
    assert set(pro["features"]) == {"scheduled_delivery", "contract_review",
                                    "priority_models"}

    # The feature-gate helper the later phases (B5/B9) will call.
    assert config.plan_has("pro", "scheduled_delivery") is True
    assert config.plan_has("basic", "scheduled_delivery") is False
    assert config.plan_has("free", "contract_review") is False
    assert config.plan_has("nonsense-code", "scheduled_delivery") is False  # → free


# ---------------------------------------------------------------------------
# (B3) The quota gate honors the free tier's 2-LIFETIME-report trial.
# ---------------------------------------------------------------------------
def _record_succeeded_run(company_id, started_at=None):
    run_id = metrics_store.create_run(company_id, "测试报告")
    metrics_store.finish_run(run_id, RUN_SUCCEEDED)
    if started_at is not None:
        with db.session_scope() as s:
            s.query(Run).filter(Run.id == run_id).update({"started_at": started_at})
    return run_id


def test_free_quota_gate_is_two_lifetime_reports(fresh_db, tmp_path):
    store = _store(tmp_path, **{"trial@corp.com": "password1",
                                "fresh@corp.com": "password1"})
    client = _login(store, "trial@corp.com")
    company_id = metrics_store.get_or_create_company("trial@corp.com",
                                                     company_name="trial")

    # Two reports used — one of them LAST MONTH. A monthly meter would have
    # forgotten it; the lifetime trial must not.
    _record_succeeded_run(company_id,
                          started_at=datetime.utcnow() - timedelta(days=40))
    _record_succeeded_run(company_id)

    r = client.post("/api/run", data={"goal": "做个经营周报"})
    assert r.status_code == 402
    assert "免费体验额度已用完" in r.get_json()["error"]
    assert "2/2" in r.get_json()["error"]

    # /api/plans meters the same lifetime usage for the quota display.
    cur = client.get("/api/plans").get_json()["current"]
    assert (cur["plan_code"], cur["lifetime_quota"]) == ("free", True)
    assert cur["reports_used_this_month"] == 2
    assert cur["monthly_report_quota"] == 2

    # A free account with only ONE run ever still passes the gate — the empty
    # goal then 400s, which proves the plan gate let the request through.
    client2 = _login(store, "fresh@corp.com")
    company2 = metrics_store.get_or_create_company("fresh@corp.com",
                                                   company_name="fresh")
    _record_succeeded_run(company2)
    assert client2.post("/api/run", data={"goal": ""}).status_code == 400

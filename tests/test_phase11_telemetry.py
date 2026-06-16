"""
test_phase11_telemetry.py — WS09: device run telemetry (shared run lifecycle).

A paired desktop daemon reports a run SUMMARY back to the cloud so local + cloud runs
share one runs table. A device run is excluded from the quota meter, the ¥29 credit
consume, and the default 历史报告 filter (exactly like 'import'), and never renders a
cloud PDF (report_path stays NULL). cost_yuan is a summary only — never the breaker/wallet.

Harness mirrors test_redesign_phase7: throwaway SQLite, CustomerStore into app.CUSTOMERS,
a real paired device via the WS03/04 store.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402
from agent_team import db, metrics_store  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.models import RUN_KIND_DEVICE, RUN_KINDS  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "telemetry.db"))
    yield


def _company():
    return metrics_store.get_or_create_company("u@corp.com", company_name="U")


def _store(tmp_path, **accounts):
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    for email, password in accounts.items():
        store.create_customer([], name=email.split("@")[0], starting_balance=100.0,
                              email=email, password=password)
    app.CUSTOMERS = store
    return store


def _device_token(account_id):
    pc = app.DEVICES.create_pairing_code(account_id)
    return app.DEVICES.exchange_pairing_code(pc["pairing_code"])["device_token"]


# ---------------------------------------------------------------------------
# Model + metrics_store layer
# ---------------------------------------------------------------------------
def test_run_kinds_includes_device_run():
    # RUN_KINDS grows over time (e.g. "recon" was added for 对账); assert membership
    # rather than an exact tuple so a new kind doesn't spuriously fail this.
    assert {"report", "import", "device_run"} <= set(RUN_KINDS)
    assert RUN_KIND_DEVICE == "device_run"


def test_create_device_run_stamps_origin_and_device(fresh_db):
    cid = _company()
    rid = metrics_store.create_device_run(cid, "分析本地表格", device_id="dev_abc123")
    run = metrics_store.get_run(rid)
    assert run["kind"] == "device_run" and run["origin"] == "device"
    assert run["device_id"] == "dev_abc123" and run["status"] == "running"


def test_device_run_excluded_from_quota_counters(fresh_db):
    cid = _company()
    metrics_store.create_run(cid, "report 1")          # kind=report (default)
    metrics_store.create_run(cid, "report 2")
    metrics_store.create_device_run(cid, "device", device_id="dev_x")
    assert metrics_store.count_runs_total(cid) == 2     # the device run is NOT counted
    assert metrics_store.count_runs_this_month(cid) == 2


def test_device_run_finish_never_consumes_credit_and_no_pdf(fresh_db):
    cid = _company()
    rid = metrics_store.create_device_run(cid, "device", device_id="dev_x")
    metrics_store.finish_run(rid, "succeeded", total_cost_yuan=12.3, report_path=None)
    run = metrics_store.get_run(rid)
    assert run["status"] == "succeeded" and run["report_path"] == ""   # PDF never rendered
    # The kind=='report' guard means a device run can never consume a ¥29 credit.
    assert metrics_store.consume_report_credit_if_over_quota(rid) is False


def test_device_run_hidden_from_default_history_visible_with_kinds(fresh_db):
    cid = _company()
    metrics_store.create_run(cid, "a report")
    metrics_store.create_device_run(cid, "device", device_id="dev_x")
    default = metrics_store.list_runs(cid)                              # kind='report'
    assert all(r["kind"] == "report" for r in default) and len(default) == 1
    both = metrics_store.list_runs(cid, kind=("report", "device_run"))
    assert {r["kind"] for r in both} == {"report", "device_run"}
    everything = metrics_store.list_runs(cid, kind=None)
    assert len(everything) == 2


def test_agent_run_crud_roundtrip(fresh_db):
    cid = _company()
    rid = metrics_store.create_device_run(cid, "device", device_id="dev_x")
    ar_id = metrics_store.create_agent_run(rid, account_id="cus_a", device_id="dev_x",
                                           agent_id="data_analyst", manifest_hash="sha256:m")
    assert metrics_store.get_agent_run(ar_id)["status"] == "running"
    assert metrics_store.finish_agent_run(
        ar_id, "succeeded", cost_yuan=4.5, report_summary="ok",
        report_sha256="abc", report_bytes=2048, deliverable_kind="xlsx") is True
    ar = metrics_store.get_agent_run(ar_id)
    assert ar["status"] == "succeeded" and ar["cost_yuan"] == 4.5
    assert ar["deliverable_kind"] == "xlsx" and ar["report_bytes"] == 2048
    # Unknown status coerces to failed; missing row → False.
    metrics_store.finish_agent_run(ar_id, "bogus")
    assert metrics_store.get_agent_run(ar_id)["status"] == "failed"
    assert metrics_store.finish_agent_run(999999, "succeeded") is False


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
def test_run_start_finish_happy_path(fresh_db, tmp_path):
    store = _store(tmp_path, **{"u@corp.com": "pw123456"})
    acct = store.find_by_email("u@corp.com")["id"]
    token = _device_token(acct)
    hdr = {"Authorization": f"Bearer {token}"}
    gw = app.app.test_client()

    started = gw.post("/api/devices/run/start",
                      json={"agent_id": "data_analyst", "goal_text": "分析", "manifest_hash": "sha256:m"},
                      headers=hdr).get_json()
    run_id = started["run_id"]
    assert run_id and started["agent_run_id"]
    run = metrics_store.get_run(run_id)
    assert run["origin"] == "device" and run["kind"] == "device_run" and run["status"] == "running"

    fin = gw.post(f"/api/devices/run/{run_id}/finish",
                  json={"status": "succeeded", "cost_yuan": 8.8, "report_summary": "done",
                        "report_sha256": "deadbeef", "report_bytes": 4096, "deliverable_kind": "xlsx"},
                  headers=hdr)
    assert fin.status_code == 200
    after = metrics_store.get_run(run_id)
    assert after["status"] == "succeeded" and after["report_path"] == ""   # no cloud PDF


def test_run_start_requires_device_token(fresh_db, tmp_path):
    _store(tmp_path)
    r = app.app.test_client().post("/api/devices/run/start", json={"agent_id": "data_analyst"})
    assert r.status_code == 401


def test_run_finish_cross_device_is_denied(fresh_db, tmp_path):
    store = _store(tmp_path, **{"a@corp.com": "pw123456", "b@corp.com": "pw123456"})
    acct_a = store.find_by_email("a@corp.com")["id"]
    acct_b = store.find_by_email("b@corp.com")["id"]
    token_a, token_b = _device_token(acct_a), _device_token(acct_b)
    gw = app.app.test_client()

    run_id = gw.post("/api/devices/run/start", json={"agent_id": "data_analyst"},
                     headers={"Authorization": f"Bearer {token_a}"}).get_json()["run_id"]
    # Device B cannot finish device A's run.
    denied = gw.post(f"/api/devices/run/{run_id}/finish", json={"status": "succeeded"},
                     headers={"Authorization": f"Bearer {token_b}"})
    assert denied.status_code == 404
    assert metrics_store.get_run(run_id)["status"] == "running"   # untouched

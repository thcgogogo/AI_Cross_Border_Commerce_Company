"""跨境财务 /api/crossborder/* — the shared HTTP surface for the new engines.

One kind-parameterized surface (三流合一 / 退税 / 发票审核 / 无票免税). Hermetic: the
engines have NO LLM and hit no network, so a run executes deterministically in the
background thread — no model faking needed. Mirrors the 对账/AR route tests:
auth/ownership, unknown-kind 404, full threaded run → result → flags → classify,
customer-safe payload, and RUN_KIND_CROSSBORDER folding into the quota.
"""
import csv
import io
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402
from agent_team import db, metrics_store as ms  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.models import RUN_KIND_CROSSBORDER  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "brain.db"))
    yield


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(app, "MEMORY_DIR", str(tmp_path / "mem"))
    monkeypatch.setattr(app, "OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(ms, "REPORTS_DIR", str(tmp_path / "reports"))
    yield


def _store(tmp_path, **accounts):
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))
    for email, password in accounts.items():
        store.create_customer([], name=email.split("@")[0], starting_balance=100.0,
                              email=email, password=password)
    app.CUSTOMERS = store
    return store


def _login(store, email):
    client = app.app.test_client()
    with client.session_transaction() as sess:
        sess["customer_id"] = store.find_by_email(email)["id"]
    return client


def _csv(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return (io.BytesIO(buf.getvalue().encode("utf-8")), "f.csv")


def _threeflow_files(amount_invoice="1000"):
    """4 single-row flows on order O1, all consistent except the invoice amount,
    which the caller can vary to force a 金额不一致 flag."""
    return {
        "orders_file": _csv(["订单号", "卖方", "买方", "金额", "币种", "数量", "日期"],
                            [["O1", "甲公司", "海外客户", "1000", "USD", "10", "2026-06-10"]]),
        "receipts_file": _csv(["订单号", "收款主体", "收款金额", "币种", "收款日期"],
                              [["O1", "甲公司", "1000", "USD", "2026-06-20"]]),
        "customs_file": _csv(["报关单号", "订单号", "经营单位", "成交金额", "数量", "报关日期"],
                             [["BG1", "O1", "甲公司", "1000", "10", "2026-06-12"]]),
        "invoices_file": _csv(["发票号", "订单号", "销售方", "价税合计", "数量", "开票日期"],
                              [["FP1", "O1", "甲公司", amount_invoice, "10", "2026-06-11"]]),
    }


def _poll(client, job_id, tries=60):
    for _ in range(tries):
        snap = client.get(f"/api/status/{job_id}").get_json()
        if snap.get("status") in ("done", "error"):
            return snap
        time.sleep(0.1)
    return {"status": "timeout"}


# --- auth + unknown kind ----------------------------------------------------
def test_config_requires_login(fresh_db, tmp_path):
    _store(tmp_path, **{"a@co.com": "pw"})
    assert app.app.test_client().get("/api/crossborder/threeflow/config").status_code == 401


def test_unknown_kind_404(fresh_db, tmp_path):
    store = _store(tmp_path, **{"a@co.com": "pw"})
    c = _login(store, "a@co.com")
    assert c.get("/api/crossborder/nonsense/config").status_code == 404
    assert c.get("/api/crossborder/nonsense/reports").status_code == 404


def test_config_lists_roles(fresh_db, tmp_path):
    store = _store(tmp_path, **{"a@co.com": "pw"})
    c = _login(store, "a@co.com")
    cfg = c.get("/api/crossborder/threeflow/config").get_json()
    fields = {r["field"] for r in cfg["roles"]}
    assert {"orders_file", "receipts_file", "customs_file", "invoices_file"} <= fields
    assert any(r["required"] for r in cfg["roles"])


# --- full threaded run → result → flags → classify --------------------------
def test_threeflow_run_end_to_end(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    client = _login(store, "boss@co.com")
    # invoice amount 1200 ≠ others 1000 → forces a 金额不一致 flag
    data = {k: v for k, v in _threeflow_files(amount_invoice="1200").items()}
    data["period"] = "2026-06"
    res = client.post("/api/crossborder/threeflow/run", data=data,
                      content_type="multipart/form-data")
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    job_id, run_id = body["job_id"], body["run_id"]

    snap = _poll(client, job_id)
    assert snap["status"] == "done", snap

    out = client.get(f"/api/crossborder/threeflow/result/{run_id}").get_json()
    assert out["kind"] == "threeflow"
    assert out["summary"]["交易数"] == 1
    assert out["exception_count"] >= 1
    assert "final_report" in out and "三流合一" in out["final_report"]

    flags = client.get(f"/api/crossborder/threeflow/flags?run_id={run_id}").get_json()["flags"]
    assert any(f["cause"] == "金额不一致" for f in flags)

    fid = flags[0]["id"]
    done = client.post(f"/api/crossborder/flags/{fid}/classify",
                       json={"label": "已核实"}).get_json()
    assert done["ok"] and done["flag"]["status"] == "resolved"

    # the run is recorded under RUN_KIND_CROSSBORDER (folds into the report quota)
    run = ms.get_run(run_id)
    assert run["kind"] == RUN_KIND_CROSSBORDER

    # history lists it
    hist = client.get("/api/crossborder/threeflow/reports").get_json()["reports"]
    assert any(r["run_id"] == run_id for r in hist)


def test_run_rejects_missing_required_file(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    client = _login(store, "boss@co.com")
    files = _threeflow_files()
    del files["customs_file"]                      # required → 400
    res = client.post("/api/crossborder/threeflow/run", data=files,
                      content_type="multipart/form-data")
    assert res.status_code == 400
    assert "报关单" in res.get_json()["error"]


# --- ownership + customer-safe ----------------------------------------------
def test_result_ownership(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"a@co.com": "pw", "b@co.com": "pw"})
    a = _login(store, "a@co.com")
    files = _threeflow_files()
    files["period"] = "2026-06"
    body = a.post("/api/crossborder/threeflow/run", data=files,
                  content_type="multipart/form-data").get_json()
    _poll(a, body["job_id"])
    b = _login(store, "b@co.com")
    assert b.get(f"/api/crossborder/threeflow/result/{body['run_id']}").status_code in (403, 404)


def test_payload_carries_no_machinery(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    client = _login(store, "boss@co.com")
    files = _threeflow_files()
    files["period"] = "2026-06"
    body = client.post("/api/crossborder/threeflow/run", data=files,
                       content_type="multipart/form-data").get_json()
    _poll(client, body["job_id"])
    text = client.get(
        f"/api/crossborder/threeflow/result/{body['run_id']}").get_data(as_text=True).lower()
    for needle in ("model", "opus", "deepseek", "sk-", "per_mtok", "cost_yuan", "¥cogs"):
        assert needle not in text


# --- schedule endpoint (auto-delivery job creation, 企业版-gated) --------------
def _subscribe_pro(account_id):
    from datetime import datetime, timedelta

    from agent_team.models import SUB_ACTIVE, Subscription
    now = datetime.utcnow()
    with db.session_scope() as s:
        s.add(Subscription(account_id=str(account_id), plan_code="pro",
                           status=SUB_ACTIVE, current_period_start=now,
                           current_period_end=now + timedelta(days=30),
                           cancel_at_period_end=False, created_at=now, updated_at=now))


def test_schedule_requires_pro(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"free@co.com": "pw"})
    client = _login(store, "free@co.com")
    data = _threeflow_files()
    data.update({"name": "每周三流", "cron": "0 8 * * mon", "channels": "email"})
    res = client.post("/api/crossborder/threeflow/schedule", data=data,
                      content_type="multipart/form-data")
    assert res.status_code == 402
    assert res.get_json().get("code") == "upgrade_required"


def test_schedule_creates_crossborder_job(fresh_db, tmp_storage, tmp_path):
    import os as _os
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    ms.get_or_create_company("boss@co.com", company_name="boss")
    _subscribe_pro(store.find_by_email("boss@co.com")["id"])
    client = _login(store, "boss@co.com")
    data = _threeflow_files()
    data.update({"name": "每周三流核验", "cron": "0 8 * * mon", "channels": "email,wechat"})
    res = client.post("/api/crossborder/threeflow/schedule", data=data,
                      content_type="multipart/form-data")
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["ok"] and body["job_id"]
    job = [j for j in body["jobs"] if j["id"] == body["job_id"]][0]
    assert job["engine_kind"] == "threeflow"
    assert job["delivery_channels"] == ["email", "wechat"]
    fixed = job["fixed_file_paths"]                       # role→path, persisted + encrypted
    assert {f["role"] for f in fixed} >= {"orders", "receipts", "customs", "invoices"}
    assert all(_os.path.isfile(f["path"]) for f in fixed)


def test_schedule_bad_cron_400(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    _subscribe_pro(store.find_by_email("boss@co.com")["id"])
    client = _login(store, "boss@co.com")
    data = _threeflow_files()
    data.update({"name": "x", "cron": "not-a-cron", "channels": "email"})
    assert client.post("/api/crossborder/threeflow/schedule", data=data,
                       content_type="multipart/form-data").status_code == 400


def test_schedule_unknown_kind_404(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    _subscribe_pro(store.find_by_email("boss@co.com")["id"])
    client = _login(store, "boss@co.com")
    assert client.post("/api/crossborder/nonsense/schedule",
                       data={"cron": "0 8 * * mon", "channels": "email"},
                       content_type="multipart/form-data").status_code == 404


def test_schedule_missing_required_file_400(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    _subscribe_pro(store.find_by_email("boss@co.com")["id"])
    client = _login(store, "boss@co.com")
    data = _threeflow_files()
    del data["customs_file"]                              # required for threeflow
    data.update({"name": "x", "cron": "0 8 * * mon", "channels": "email"})
    res = client.post("/api/crossborder/threeflow/schedule", data=data,
                      content_type="multipart/form-data")
    assert res.status_code == 400 and "报关单" in res.get_json()["error"]


def test_delete_schedule_gcs_pinned_files(fresh_db, tmp_storage, tmp_path):
    import os as _os
    store = _store(tmp_path, **{"boss@co.com": "pw"})
    _subscribe_pro(store.find_by_email("boss@co.com")["id"])
    client = _login(store, "boss@co.com")
    data = _threeflow_files()
    data.update({"name": "x", "cron": "0 8 * * mon", "channels": "email"})
    body = client.post("/api/crossborder/threeflow/schedule", data=data,
                       content_type="multipart/form-data").get_json()
    job = [j for j in body["jobs"] if j["id"] == body["job_id"]][0]
    sched_dir = _os.path.dirname(job["fixed_file_paths"][0]["path"])
    assert _os.path.isdir(sched_dir) and "cbsched" in sched_dir
    # deleting the job GC's its pinned-file dir
    res = client.post("/api/scheduled-jobs/delete", json={"id": body["job_id"]})
    assert res.status_code == 200
    assert not _os.path.exists(sched_dir)


def test_cleanup_only_touches_cbsched(tmp_storage, tmp_path):
    import os as _os
    # a job pinned to a NON-cbsched path (like a report fixed_files job) must be untouched
    other = tmp_path / "uploads" / "userbatch"
    other.mkdir(parents=True)
    keep = other / "data.csv"
    keep.write_text("x")
    app._cleanup_sched_pinned_files({"fixed_file_paths": [{"role": "x", "path": str(keep)}]})
    assert keep.exists()

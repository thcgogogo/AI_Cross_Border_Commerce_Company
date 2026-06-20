"""应收账款 /api/ar/* — HTTP surface: auth, ownership, customer-safe, quota, send.

Hermetic (no network); the model call is faked but still bills the real per-account
path. Mirrors the 对账 route tests. SMS is out of scope; email send is mocked.
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
from agent_team import (ar_orchestrator, config, db, delivery,  # noqa: E402
                        metrics_store as ms)
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.models import RUN_KIND_AR  # noqa: E402


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


def _csv_bytes(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


class _FakeRouter:
    def __init__(self, token_manager=None, cost_listener=None, **k):
        self.cost_listener = cost_listener
        self.token_manager = token_manager

    def complete(self, **kw):
        if self.cost_listener:
            self.cost_listener(config.HAIKU, 60_000, 9_000)
        return "# 应收账款账龄报告\n\n## 核心结论\n- 应收 ¥5,000.00【1】。\n"

    def complete_with_tools(self, **kw):
        return self.complete(**kw)


# ---------------------------------------------------------------------------
# Auth / validation / packs
# ---------------------------------------------------------------------------
def test_ar_run_requires_login(fresh_db, tmp_path):
    _store(tmp_path)
    r = app.app.test_client().post("/api/ar/run", data={"format_key": "通用"})
    assert r.status_code == 401


def test_ar_run_rejects_missing_files(fresh_db, tmp_storage, tmp_path):
    store = _store(tmp_path, **{"a@corp.com": "pw"})
    r = _login(store, "a@corp.com").post(
        "/api/ar/run", data={"format_key": "通用"}, content_type="multipart/form-data")
    assert r.status_code == 400


def test_ar_packs_lists_generic_and_gates_anon(fresh_db, tmp_path):
    store = _store(tmp_path, **{"a@corp.com": "pw"})
    keys = [p["key"] for p in _login(store, "a@corp.com").get(
        "/api/ar/packs").get_json()["formats"]]
    assert "generic" in keys
    assert app.app.test_client().get("/api/ar/packs").status_code == 401


# ---------------------------------------------------------------------------
# Result ownership + customer-safe payload
# ---------------------------------------------------------------------------
def _seed_report(cid, account_id=None):
    from agent_team import ar_aging_engine as eng, ar_templates as t
    run_id = ms.create_run(cid, "应收", kind=RUN_KIND_AR)
    inv = [{"发票号": "INV1", "客户编号": "C1", "客户名称": "甲公司",
            "到期日": "2026-05-01", "金额": "5000"}]
    pay = [{"流水号": "P9", "客户编号": "C2", "收款日期": "2026-06-30", "收款金额": "100"}]
    res = eng.aging_from_records({"invoices": inv, "payments": pay, "customers": []},
                                 t.load_pack("通用"))
    ms.save_ar_report(cid, run_id, res, account_id=account_id, format_key="generic")
    return run_id


def test_ar_result_ownership_and_customer_safe(fresh_db, tmp_path):
    store = _store(tmp_path, **{"a@corp.com": "pw", "b@corp.com": "pw"})
    cid_a = ms.get_or_create_company("a@corp.com")
    run_id = _seed_report(cid_a)
    ra = _login(store, "a@corp.com").get(f"/api/ar/result/{run_id}")
    assert ra.status_code == 200
    body = ra.get_json()
    assert body["summary"]["逾期总额"] == 5000.0
    # customer-safe: no ¥ COGS / internal machinery fields
    assert "total_cost_yuan" not in body and "report_path" not in body
    raw = ra.get_data(as_text=True)
    assert "claude" not in raw.lower() and "sk-" not in raw and "per_mtok" not in raw
    rb = _login(store, "b@corp.com").get(f"/api/ar/result/{run_id}")
    assert rb.status_code == 403


def test_ar_flags_classify_and_ledger(fresh_db, tmp_path):
    store = _store(tmp_path, **{"a@corp.com": "pw", "b@corp.com": "pw"})
    cid_a = ms.get_or_create_company("a@corp.com")
    run_id = _seed_report(cid_a, account_id=store.find_by_email("a@corp.com")["id"])
    ca = _login(store, "a@corp.com")

    flags = ca.get(f"/api/ar/flags?run_id={run_id}").get_json()["flags"]
    inv1 = [f for f in flags if f["invoice_ref"] == "INV1"][0]
    # B cannot classify A's flag.
    rb = _login(store, "b@corp.com").post(
        f"/api/ar/flags/{inv1['id']}/classify", json={"label": "已收款"})
    assert rb.status_code == 404
    ok = ca.post(f"/api/ar/flags/{inv1['id']}/classify", json={"label": "已收款"})
    assert ok.status_code == 200 and ok.get_json()["flag"]["cause"] == "已收款"

    led = ca.get(f"/api/ar/ledger/{run_id}")
    assert led.status_code == 200
    assert "spreadsheetml" in led.headers["Content-Type"] and led.data[:2] == b"PK"


# ---------------------------------------------------------------------------
# Quota fold
# ---------------------------------------------------------------------------
def test_ar_folds_into_report_quota(fresh_db, monkeypatch):
    monkeypatch.setenv("REPORT_QUOTA_ENABLED", "1")
    cid = ms.get_or_create_company("boss@corp.com")
    ms.create_run(cid, "经营周报")
    ms.create_run(cid, "应收", kind=RUN_KIND_AR)
    assert ms.count_runs_this_month(cid) == 2 and ms.count_runs_total(cid) == 2


# ---------------------------------------------------------------------------
# Full threaded run via the client, then the blocking send + no-double-dun
# ---------------------------------------------------------------------------
def _wait_done(client, job_id, tries=80):
    snap = {}
    for _ in range(tries):
        snap = client.get(f"/api/status/{job_id}").get_json()
        if snap.get("status") in ("done", "error"):
            return snap
        time.sleep(0.05)
    return snap


def test_full_ar_run_then_human_send(fresh_db, tmp_storage, tmp_path, monkeypatch):
    monkeypatch.setattr(ar_orchestrator, "ModelRouter", _FakeRouter)
    monkeypatch.setattr(delivery, "send_email", lambda *a, **k: (True, None))
    store = _store(tmp_path, **{"boss@corp.com": "pw"})
    client = _login(store, "boss@corp.com")

    data = {
        "format_key": "通用", "period": "2026-06",
        "invoices_file": (io.BytesIO(_csv_bytes(
            ["发票号", "客户编号", "客户名称", "到期日", "金额"],
            [["INV1", "C1", "甲公司", "2026-05-01", "5000"]])), "inv.csv"),
        "payments_file": (io.BytesIO(_csv_bytes(
            ["流水号", "客户编号", "收款日期", "收款金额"],
            [["P9", "C2", "2026-06-30", "100"]])), "pay.csv"),
        "customers_file": (io.BytesIO(_csv_bytes(
            ["客户编号", "客户名称", "联系方式"],
            [["C1", "甲公司", "boss@c1.com"]])), "cus.csv"),
    }
    r = client.post("/api/ar/run", data=data, content_type="multipart/form-data")
    assert r.status_code == 200
    job_id, run_id = r.get_json()["job_id"], r.get_json()["run_id"]
    snap = _wait_done(client, job_id)
    assert snap["status"] == "done"
    # Non-admin status feed is customer-safe (no model names).
    assert all("claude" not in (a.get("message") or "").lower()
               for a in snap.get("activity", []))

    res = client.get(f"/api/ar/result/{run_id}").get_json()
    assert res["summary"]["逾期总额"] == 5000.0 and "账龄报告" in (res.get("final_report") or "")

    # A 催收 draft exists, NOT sent (no-auto-send invariant).
    msgs = client.get(f"/api/ar/dunning?run_id={run_id}").get_json()["messages"]
    draft = [m for m in msgs if m["customer_key"] == "C1"][0]
    assert draft["state"] == "draft" and draft["tier"] == "final_notice"

    # Human approve-then-send (the ONLY path to 'sent').
    assert client.post(f"/api/ar/dunning/{draft['id']}/approve").status_code == 200
    sent = client.post(f"/api/ar/dunning/{draft['id']}/send",
                       json={"channel": "email", "to": "boss@c1.com"})
    assert sent.status_code == 200 and sent.get_json()["message"]["state"] == "sent"
    # idempotent: a second send is refused (no double-send).
    assert client.post(f"/api/ar/dunning/{draft['id']}/send",
                       json={"channel": "email", "to": "boss@c1.com"}).status_code == 409


def test_send_email_requires_recipient_and_rejects_handoff(fresh_db, tmp_path):
    store = _store(tmp_path, **{"a@corp.com": "pw"})
    cid = ms.get_or_create_company("a@corp.com")
    ca = _login(store, "a@corp.com")
    # a normal draft: email send with no recipient -> 400
    d1 = ms.record_dunning_draft(cid, "C1", "reminder", draft_body="您好…", amount=100)
    assert ca.post(f"/api/ar/dunning/{d1['id']}/send",
                   json={"channel": "email"}).status_code == 400
    # a handoff item has no customer message -> send refused
    d2 = ms.record_dunning_draft(cid, "C9", "handoff", draft_body="", amount=99)
    assert ca.post(f"/api/ar/dunning/{d2['id']}/send",
                   json={"channel": "email", "to": "x@y.com"}).status_code == 400

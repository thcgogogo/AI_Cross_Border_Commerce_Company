"""Phase 9 — payments, subscriptions & fapiao acceptance tests (MockProvider).

The five checks mirror the Phase 9 spec's acceptance criteria, in order:

  1. An order paid via a mocked notify credits the balance EXACTLY ONCE even
     when the notify is delivered 3 times (idempotent created→paid).
  2. A tampered signature → 400, the order stays `created`, nothing credited.
  3. Subscription lifecycle: pay → active; the daily tick at T-3d creates ONE
     renewal order + notifies the user; left unpaid past expiry+grace → past_due
     and run-starts are locked; paying the renewal order → active again.
  4. Refunding a top-up deducts the balance; a refund is BLOCKED with a clear
     error when the balance was already spent below the refund amount.
  5. A fapiao request shows up in the admin CSV export with the correct 税号.

Same isolation pattern as the Phase 8 suite: a throwaway SQLite DB per test and
a fresh CustomerStore wired into app.CUSTOMERS. No real provider is configured
in the test env, so payments.get_provider() returns the MockProvider and the
/pay/notify/mock endpoint is live — its notifies are HMAC-signed, so the
signature-first handler logic is exercised for real.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402 — module-level boot is one-time & idempotent
from agent_team import db, metrics_store, payments  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402
from agent_team.models import (  # noqa: E402
    AUDIT_ADMIN, AUDIT_TOPUP, ORDER_CREATED, ORDER_PAID, ORDER_REFUNDED,
    SUB_PAST_DUE, Subscription)


@pytest.fixture(autouse=True)
def _enable_report_quota(monkeypatch):
    # Report-delivery pricing is opt-in since 2026-06-15 (REPORT_QUOTA_ENABLED, default
    # OFF). The subscription-lifecycle test asserts the past-due lock gate, so enable it.
    monkeypatch.setenv("REPORT_QUOTA_ENABLED", "1")


@pytest.fixture
def fresh_db(tmp_path):
    """A throwaway SQLite file per test: orders, subscriptions, fapiao requests
    and the audit log all start empty."""
    db.init_db(str(tmp_path / "brain.db"))
    yield


def _store(tmp_path, **accounts):
    """A fresh CustomerStore wired into the app, pre-loaded with {email: password}
    accounts (each starting with ¥100 demo credit)."""
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


def _pay(client, order_no, amount, times=1):
    """Deliver a properly SIGNED mock notify for an order, `times` times."""
    fields = payments.MockProvider().notify_fields(order_no, amount)
    responses = [client.post("/pay/notify/mock", data=fields) for _ in range(times)]
    return responses


# ---------------------------------------------------------------------------
# (1) A replayed paid-notify credits the balance exactly once.
# ---------------------------------------------------------------------------
def test_paid_notify_credits_exactly_once_when_delivered_3_times(fresh_db, tmp_path):
    store = _store(tmp_path, **{"buyer@corp.com": "password1"})
    client = _login(store, "buyer@corp.com")

    r = client.post("/api/pay/create", json={"type": "topup", "amount": 50})
    assert r.status_code == 200
    order = r.get_json()
    assert order["pay_url"].startswith("/pay/mock/")   # mock cashier in dev

    # The provider delivers the same signed notify 3 times.
    responses = _pay(client, order["order_no"], 50, times=3)
    assert [x.status_code for x in responses] == [200, 200, 200]
    assert responses[1].get_json()["replay"] is True   # replays are no-op successes

    # Credited exactly once: ¥100 start + ¥50, NOT ¥250.
    acct = store.find_by_email("buyer@corp.com")
    assert acct["balance"] == 150.0
    assert metrics_store.get_order(order["order_no"])["status"] == ORDER_PAID
    # The verified paid transition wrote ONE topup audit row.
    rows = [a for a in metrics_store.list_audit(action=AUDIT_TOPUP)
            if order["order_no"] in (a["detail"] or "")]
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# (2) A tampered signature → 400; the order stays created; nothing credited.
# ---------------------------------------------------------------------------
def test_tampered_signature_is_rejected_and_credits_nothing(fresh_db, tmp_path):
    store = _store(tmp_path, **{"victim@corp.com": "password1"})
    client = _login(store, "victim@corp.com")

    order_no = client.post("/api/pay/create",
                           json={"type": "topup", "amount": 80}).get_json()["order_no"]

    # (a) Amount inflated AFTER signing — the signature no longer matches.
    fields = payments.MockProvider().notify_fields(order_no, 80)
    fields["paid_amount"] = "8000.00"
    assert client.post("/pay/notify/mock", data=fields).status_code == 400

    # (b) Outright forged signature.
    fields = payments.MockProvider().notify_fields(order_no, 80)
    fields["sign"] = "deadbeef" * 8
    assert client.post("/pay/notify/mock", data=fields).status_code == 400

    # Nothing changed: order still created, balance untouched.
    assert metrics_store.get_order(order_no)["status"] == ORDER_CREATED
    assert store.find_by_email("victim@corp.com")["balance"] == 100.0


# ---------------------------------------------------------------------------
# (3) Subscription lifecycle: activate → T-3d renewal order → unpaid →
#     past_due lock → pay → active again.
# ---------------------------------------------------------------------------
def _set_period_end(account_id, when):
    """Rewind the subscription clock (tests can't wait 30 days)."""
    with db.session_scope() as s:
        sub = s.query(Subscription).filter(
            Subscription.account_id == account_id).one()
        sub.current_period_end = when


def test_subscription_lifecycle_renewal_pastdue_lock_then_pay(
        fresh_db, tmp_path, monkeypatch):
    sent = []

    def _spy_email(to, subject, body, **kwargs):
        sent.append({"to": to, "subject": subject})
        return True, None

    monkeypatch.setattr(payments.delivery, "send_email", _spy_email)

    store = _store(tmp_path, **{"sub@corp.com": "password1"})
    account_id = store.find_by_email("sub@corp.com")["id"]
    client = _login(store, "sub@corp.com")

    # Subscribe to basic (price comes from the server's PLANS — never the client).
    order = client.post("/api/pay/create",
                        json={"type": "subscription", "plan_code": "basic"}).get_json()
    assert order["amount"] == 199.0
    _pay(client, order["order_no"], 199.0)
    plan_code, sub, locked = metrics_store.effective_plan(account_id)
    assert (plan_code, locked) == ("basic", False)

    # Paying the subscription ALSO emails a Phase 10 receipt through the same
    # (spied) delivery module, so the renewal-notice assertions filter by
    # subject — this case is about the notice, not the receipt.
    def _notices():
        return [m for m in sent if m["subject"] == "订阅续费提醒"]

    # T-3d: the daily tick creates ONE renewal order and notifies the user.
    now = datetime.utcnow()
    _set_period_end(account_id, now + timedelta(days=2))
    summary = payments.daily_subscription_tick(now=now)
    assert len(summary["renewal_orders"]) == 1
    renewal_no = summary["renewal_orders"][0]
    assert metrics_store.get_order(renewal_no)["status"] == ORDER_CREATED
    assert len(_notices()) == 1 and _notices()[0]["to"] == "sub@corp.com"

    # A second tick the next day must NOT spam a duplicate order/notice.
    summary2 = payments.daily_subscription_tick(now=now + timedelta(days=1))
    assert summary2["renewal_orders"] == []
    assert len(_notices()) == 1

    # Unpaid past expiry + 3-day grace → past_due, and run-starts are LOCKED.
    _set_period_end(account_id, now - timedelta(days=4))
    summary3 = payments.daily_subscription_tick(now=now)
    assert account_id in summary3["past_due"]
    assert metrics_store.get_subscription(account_id)["status"] == SUB_PAST_DUE
    r = client.post("/api/run", data={"goal": "做个经营周报"})
    assert r.status_code == 402
    assert "订阅已逾期" in r.get_json()["error"]

    # Paying the renewal order restores service immediately.
    _pay(client, renewal_no, 199.0)
    plan_code, sub, locked = metrics_store.effective_plan(account_id)
    assert (plan_code, locked) == ("basic", False)
    assert metrics_store._parse_dt(sub["current_period_end"]) > datetime.utcnow()
    # Gates now pass — the empty goal 400 proves the plan gate let us through.
    assert client.post("/api/run", data={"goal": ""}).status_code == 400


# ---------------------------------------------------------------------------
# (4) Refunds: a top-up refund deducts the balance; blocked once the credit
#     was spent below the refund amount (clear error).
# ---------------------------------------------------------------------------
def test_refund_topup_deducts_balance_and_blocks_when_spent(
        fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")
    store = _store(tmp_path, **{"payer@corp.com": "password1",
                                "founder@corp.com": "password1"})
    client = _login(store, "payer@corp.com")
    admin = _login(store, "founder@corp.com")
    payer_id = store.find_by_email("payer@corp.com")["id"]

    # Pay a ¥50 top-up → balance 150.
    order_no = client.post("/api/pay/create",
                           json={"type": "topup", "amount": 50}).get_json()["order_no"]
    _pay(client, order_no, 50)
    assert store.get_by_id(payer_id)["balance"] == 150.0

    # Full refund by the admin → balance back to 100, order refunded, audit row.
    r = admin.post("/admin/orders", data={"action": "refund",
                                          "order_no": order_no, "amount": "50"})
    assert r.status_code == 200
    assert store.get_by_id(payer_id)["balance"] == 100.0
    assert metrics_store.get_order(order_no)["status"] == ORDER_REFUNDED
    assert any("refund" in (a["detail"] or "")
               for a in metrics_store.list_audit(action=AUDIT_ADMIN))

    # Second ¥50 top-up (balance 150), then the credit gets SPENT down to ¥30.
    order2 = client.post("/api/pay/create",
                         json={"type": "topup", "amount": 50}).get_json()["order_no"]
    _pay(client, order2, 50)
    store.deduct_balance(payer_id, 120)            # simulate ¥120 of usage
    assert store.get_by_id(payer_id)["balance"] == 30.0

    # Refunding ¥50 must now be BLOCKED with a clear error; nothing changes.
    r = admin.post("/admin/orders", data={"action": "refund",
                                          "order_no": order2, "amount": "50"})
    assert r.status_code == 400
    assert "已被消费" in r.get_data(as_text=True)
    assert store.get_by_id(payer_id)["balance"] == 30.0
    assert metrics_store.get_order(order2)["status"] == ORDER_PAID


# ---------------------------------------------------------------------------
# (5) A fapiao request appears in the admin CSV export with the correct 税号.
# ---------------------------------------------------------------------------
def test_fapiao_request_appears_in_admin_export_with_tax_no(
        fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "founder@corp.com")
    store = _store(tmp_path, **{"inv@corp.com": "password1",
                                "founder@corp.com": "password1"})
    client = _login(store, "inv@corp.com")
    admin = _login(store, "founder@corp.com")

    order_no = client.post("/api/pay/create",
                           json={"type": "topup", "amount": 200}).get_json()["order_no"]

    # 申请发票 on an UNPAID order is refused.
    tax_no = "91330106MA2AXY129K"
    r = client.post("/api/fapiao/request", json={
        "order_no": order_no, "title": "杭州示例科技有限公司", "tax_no": tax_no})
    assert r.status_code == 400

    _pay(client, order_no, 200)
    r = client.post("/api/fapiao/request", json={
        "order_no": order_no, "title": "杭州示例科技有限公司", "tax_no": tax_no})
    assert r.status_code == 200

    # Duplicate request for the same order is refused.
    r = client.post("/api/fapiao/request", json={
        "order_no": order_no, "title": "杭州示例科技有限公司", "tax_no": tax_no})
    assert r.status_code == 400

    # The bookkeeper's CSV export carries the row with the correct 税号.
    csv_resp = admin.get("/admin/fapiao/export.csv")
    assert csv_resp.status_code == 200
    body = csv_resp.get_data(as_text=True)
    assert tax_no in body
    assert order_no in body
    assert "杭州示例科技有限公司" in body

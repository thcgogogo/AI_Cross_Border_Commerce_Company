"""
reconciliation.py — nightly provider-bill vs orders comparison (Phase 9).

Money paths must be AUDITABLE: every night we take each provider's settlement
bill (对账单, a downloaded CSV) and compare it row-by-row against our `orders`
table. Anything that doesn't line up becomes a mismatch on /admin/reconciliation
AND fires an admin alert — a silently missing or mis-amounted payment is exactly
the failure mode reconciliation exists to catch.

WHERE BILLS COME FROM: real bill download needs per-merchant credentials and a
fetch job; at this stage ops (or a later fetcher) drops the day's CSVs into
BILLS_DIR as  <provider>-<YYYY-MM-DD>.csv  (China-local bill date), e.g.

    data/bills/alipay-2026-06-09.csv
    data/bills/wechat-2026-06-09.csv

A missing file is recorded as "skipped", never an error. One tolerant parser per
provider (Alipay bills are GBK with # comment lines; WeChat APIv3 bills prefix
every value with a backtick); a plain  order_no,amount,txn_id  CSV also parses,
which is what the mock provider / tests use.

Mismatch kinds:
    missing_order    the bill has a trade we have no order for
    amount_mismatch  bill amount ≠ our stored order amount
    status_mismatch  bill says collected, our order isn't paid
    missing_in_bill  our PAID order (that day) absent from the bill
"""

import csv
import io
import logging
import os
from datetime import datetime, timedelta

from . import alerts, db, metrics_store
from .models import ORDER_PAID, ORDER_REFUNDED

log = logging.getLogger("agent_team.reconciliation")

# China-local bill dates: providers cut bills on Beijing days, we store UTC.
_CHINA_UTC_OFFSET = timedelta(hours=8)


def bills_dir():
    """Where ops drops the daily bill CSVs (override with $BILLS_DIR)."""
    return (os.environ.get("BILLS_DIR") or "").strip() \
        or os.path.join(db.PROJECT_ROOT, "data", "bills")


# ---------------------------------------------------------------------------
# Parsers — one per provider, all tolerant: junk in → [] out, never an exception.
# ---------------------------------------------------------------------------
def _decode(raw):
    """Bill bytes → text. Alipay bills are GBK; everything else is UTF-8."""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, AttributeError):
            continue
    return raw.decode("utf-8", "replace")


def _amount(s):
    try:
        return round(float(str(s).replace("¥", "").replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return None


def _pick(header, *names):
    """Index of the first matching column name in `header`, or None."""
    for i, col in enumerate(header):
        c = (col or "").strip()
        for name in names:
            if name in c:
                return i
    return None


def parse_provider_bill(provider, raw):
    """Parse one bill CSV into [{"order_no", "amount", "txn_id"}].

    Recognizes the provider's Chinese headers (商户订单号 / 订单金额 / 交易号)
    and the plain order_no,amount,txn_id form. Skips # comment lines and the
    summary block under them. WeChat's backtick value prefix is stripped."""
    text = _decode(raw)
    rows = []
    try:
        reader = csv.reader(io.StringIO(text))
        header, idx = None, None
        for cells in reader:
            if not cells or (cells[0] or "").lstrip().startswith("#"):
                continue
            cells = [(c or "").strip().lstrip("`").strip() for c in cells]
            if header is None:
                header = cells
                no_i = _pick(header, "商户订单号", "商家订单号", "order_no")
                amt_i = _pick(header, "订单金额", "应结订单金额", "交易金额", "amount")
                txn_i = _pick(header, "支付宝交易号", "微信支付订单号", "交易号", "txn_id")
                if no_i is None or amt_i is None:
                    return []  # not a bill we understand
                idx = (no_i, amt_i, txn_i)
                continue
            no_i, amt_i, txn_i = idx
            if len(cells) <= max(no_i, amt_i):
                continue
            order_no = cells[no_i]
            amount = _amount(cells[amt_i])
            if not order_no or amount is None:
                continue
            rows.append({
                "order_no": order_no,
                "amount": amount,
                "txn_id": (cells[txn_i] if txn_i is not None and txn_i < len(cells)
                           else ""),
            })
    except Exception:  # noqa: BLE001 — a malformed bill must not kill the tick
        log.exception("bill parse failed for provider %s", provider)
        return []
    return rows


# ---------------------------------------------------------------------------
# The comparison
# ---------------------------------------------------------------------------
def _day_bounds_utc(bill_date):
    """[start, end) in UTC for a China-local "YYYY-MM-DD" bill date."""
    day = datetime.fromisoformat(bill_date)
    start = day - _CHINA_UTC_OFFSET
    return start, start + timedelta(days=1)


def reconcile_bill(provider, bill_date, rows):
    """Compare parsed bill `rows` against our orders; persist the recon report
    (upserted per provider+day) and alert the admin when mismatches exist.
    Returns the report dict."""
    mismatches, matched, seen = [], 0, set()
    for r in rows:
        seen.add(r["order_no"])
        order = metrics_store.get_order(r["order_no"])
        if order is None or order["provider"] != provider:
            mismatches.append({"kind": "missing_order", "order_no": r["order_no"],
                               "detail": "账单中存在但系统无此订单（¥%.2f）" % r["amount"]})
        elif abs(order["amount_yuan"] - r["amount"]) >= 0.005:
            mismatches.append({"kind": "amount_mismatch", "order_no": r["order_no"],
                               "detail": "账单 ¥%.2f ≠ 系统 ¥%.2f"
                                         % (r["amount"], order["amount_yuan"])})
        elif order["status"] not in (ORDER_PAID, ORDER_REFUNDED):
            mismatches.append({"kind": "status_mismatch", "order_no": r["order_no"],
                               "detail": "账单已收款但系统订单状态为 %s" % order["status"]})
        else:
            matched += 1

    start, end = _day_bounds_utc(bill_date)
    for order in metrics_store.list_paid_orders_between(provider, start, end):
        if order["order_no"] not in seen:
            mismatches.append({"kind": "missing_in_bill", "order_no": order["order_no"],
                               "detail": "系统已收款（¥%.2f）但账单中缺失"
                                         % order["amount_yuan"]})

    report = metrics_store.save_recon_report(provider, bill_date, len(rows),
                                             matched, mismatches)
    if mismatches:
        lines = "\n".join("- [%s] %s：%s" % (m["kind"], m["order_no"], m["detail"])
                          for m in mismatches[:20])
        more = "" if len(mismatches) <= 20 else "\n…共 %d 笔差异" % len(mismatches)
        try:
            alerts.send_admin_alert(
                "对账发现差异（%s · %s）" % (provider, bill_date),
                "%s 的 %s 账单与系统订单存在 %d 笔差异：\n%s%s\n\n"
                "详见 /admin/reconciliation。"
                % (provider, bill_date, len(mismatches), lines, more))
        except Exception:  # noqa: BLE001 — alerting must never mask the report
            log.exception("admin alert failed after reconciliation mismatch")
    return report


def run_reconciliation(days=2, now=None):
    """Scan BILLS_DIR for each configured provider × the last `days` China-local
    dates (yesterday first) and reconcile every bill file found. Idempotent —
    results upsert per provider+day. Returns a list of per-(provider, date)
    outcome dicts. This is both the nightly tick (days=2) and the manual
    "扫描账单" button on /admin/reconciliation (days=7)."""
    from . import payments  # local import — payments has no reason to import us
    now = now or datetime.utcnow()
    china_today = (now + _CHINA_UTC_OFFSET).date()
    outcomes = []
    for provider in payments.available_providers():
        for back in range(1, int(days) + 1):
            bill_date = (china_today - timedelta(days=back)).isoformat()
            path = os.path.join(bills_dir(), "%s-%s.csv" % (provider, bill_date))
            if not os.path.isfile(path):
                outcomes.append({"provider": provider, "bill_date": bill_date,
                                 "status": "skipped_no_bill"})
                continue
            try:
                with open(path, "rb") as f:
                    raw = f.read()
                report = reconcile_bill(provider, bill_date,
                                        parse_provider_bill(provider, raw))
                outcomes.append({"provider": provider, "bill_date": bill_date,
                                 "status": "ok",
                                 "mismatch_count": report["mismatch_count"]})
            except Exception as e:  # noqa: BLE001 — one bad file must not stop the rest
                log.exception("reconciliation failed for %s %s", provider, bill_date)
                outcomes.append({"provider": provider, "bill_date": bill_date,
                                 "status": "error: %s" % type(e).__name__})
    return outcomes


def nightly_reconciliation_tick():
    """The scheduler's nightly entrypoint (no args — APScheduler calls it bare)."""
    outcomes = run_reconciliation(days=2)
    done = [o for o in outcomes if o["status"] == "ok"]
    if done:
        log.info("reconciliation: %d bill(s) processed.", len(done))
    return outcomes

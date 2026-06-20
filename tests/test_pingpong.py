"""跨境财务 sub-tab 1 — PingPong (收款服务商) adapter.

The money-chain leg 平台 → PingPong → 结汇 → 境内银行. PingPong's 对账单 is a transaction
ledger; the `pingpong` adapter normalizes 收款 into per-transaction CNY orders, separates
退款, surfaces 未结汇 as 在途 + the FX 点差, and ignores 结汇/提现 (the bank role owns them).
Reuses the 对账 engine via the reconcile_records adapter seam. PROVISIONAL format.
"""
import os
import sys
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import crossborder_adapters as cba  # noqa: E402
from agent_team import settlement_engine as engine  # noqa: E402
from agent_team import settlement_templates as templates  # noqa: E402

RATE = Decimal("7.15")  # matches templates_platforms/pingpong/pack.yaml fx.rate


def _row(ttype, amount, *, order_ref="", txn_no="T", fee="0", currency="USD",
         date="2026-06-10"):
    return {"交易时间": date, "交易类型": ttype, "交易号": txn_no, "关联订单号": order_ref,
            "金额": amount, "币种": currency, "手续费": fee, "状态": "成功"}


def _dataset():
    return [
        _row("平台收款", "1000", order_ref="P-1", txn_no="T1", fee="20"),
        _row("收款入账", "500", order_ref="P-2", txn_no="T2", fee="10"),
        _row("退款", "100", order_ref="P-1", txn_no="T3"),
        _row("结汇提现", "7000", txn_no="T4", currency="CNY"),    # bank role owns this
        _row("未结汇余额", "200", txn_no="BAL"),
        _row("某种未知类型", "999", txn_no="T9"),                 # unknown → skipped
    ]


def test_adapter_normalizes_income_to_cny():
    pack = templates.load_pack("pingpong")
    records, _ = cba.pingpong_preprocess(_dataset(), pack)
    by = {(r["merchant_order_no"], r["status"]): r for r in records}
    assert by[("P-1", "SUCCESS")]["gross_amount"] == "7150.00"   # 1000 * 7.15
    assert by[("P-1", "SUCCESS")]["fee"] == "143.00"            # 20 * 7.15
    assert by[("P-2", "SUCCESS")]["gross_amount"] == "3575.00"  # 500 * 7.15
    assert by[("P-1", "REFUND")]["refund_amount"] == "715.00"   # 100 * 7.15
    # 结汇/提现 + unknown rows are NOT emitted as orders
    assert all(r["batch_no"] == "PINGPONG" for r in records)
    assert not any(r["gross_amount"] == "50050.00" for r in records)  # the 7000 结汇 ignored


def test_adapter_surfaces_reserve_and_fx():
    pack = templates.load_pack("pingpong")
    _, extras = cba.pingpong_preprocess(_dataset(), pack)
    assert extras["reserve"] == Decimal("1430.00")     # 200 未结汇 * 7.15
    assert extras["reserve_count"] == 1
    assert extras["fx_spread"] == Decimal("42.90")     # (7150 + 3575) * 0.004
    assert extras["orders"] == 2
    assert extras["currency_in"] == "USD"


def test_unknown_type_skipped():
    pack = templates.load_pack("pingpong")
    records, _ = cba.pingpong_preprocess(_dataset(), pack)
    # the 999 unknown-type row contributes no order
    assert not any(r.get("gross_amount") == str((Decimal("999") * RATE).quantize(Decimal("0.01")))
                   for r in records)


def test_end_to_end_reserve_keeps_residual_clean():
    pack = templates.load_pack("pingpong")
    # settled = Σnet(10725) − fee(214.50) − refund(715) = 9795.50; reserve held 1430 →
    # bank 结汇到账 = 9795.50 − 1430 = 8365.50, so 未解释差异 nets to 0.
    bank = [{"收入金额": "8365.50", "交易日期": "2026-06-20"}]
    out = engine.reconcile_records({"platform": _dataset(), "bank": bank}, pack,
                                   period_label="2026-06")
    s = out["summary"]
    assert s["实收"] == 9795.5
    assert s["到账"] == 8365.5
    assert s["在途金额"] == 1430.0                     # 未结汇 folded into 在途
    assert s["未解释差异"] == 0.0
    kinds = {a["kind"]: a["amount"] for a in out["attributions"]}
    assert kinds["reserve_held"] == 1430.0
    assert kinds["fx_spread"] == 42.9


# --- review fixes: reserve is a snapshot (not summed) + per-currency conversion ---
def test_reserve_is_latest_snapshot_not_summed():
    pack = templates.load_pack("pingpong")
    rows = [
        _row("平台收款", "1000", order_ref="P1", txn_no="T1"),
        _row("未结汇余额", "300", txn_no="B1", date="2026-06-10"),
        _row("未结汇余额", "420", txn_no="B2", date="2026-06-20"),   # later snapshot wins
    ]
    _, extras = cba.pingpong_preprocess(rows, pack)
    assert extras["reserve"] == Decimal("3003.00")   # 420×7.15 (latest), NOT (300+420)×7.15
    assert extras["reserve_count"] == 2


def test_cny_native_row_not_double_converted():
    pack = templates.load_pack("pingpong")
    rows = [_row("平台收款", "100", order_ref="P1", txn_no="T1", currency="CNY", fee="2")]
    records, _ = cba.pingpong_preprocess(rows, pack)
    sale = [r for r in records if r["status"] == "SUCCESS"][0]
    assert sale["gross_amount"] == "100.00"          # CNY passes through at 1, not ×7.15
    assert sale["fee"] == "2.00"


def test_per_currency_rate_override():
    pack = dict(templates.load_pack("pingpong"))
    pack["fx"] = {**pack["fx"], "rates": {"EUR": "7.80"}}
    records, _ = cba.pingpong_preprocess(
        [_row("平台收款", "100", order_ref="P1", currency="EUR")], pack)
    assert [r for r in records if r["status"] == "SUCCESS"][0]["gross_amount"] == "780.00"


def test_in_dropdown_and_bridge_clean():
    keys = {p["key"] for p in templates.available_platforms()}
    assert "pingpong" in keys
    bank = [{"收入金额": "8365.50", "交易日期": "2026-06-20"}]
    out = engine.reconcile_records({"platform": _dataset(), "bank": bank},
                                   templates.load_pack("pingpong"))
    assert out["bridge"]["ok"] is True

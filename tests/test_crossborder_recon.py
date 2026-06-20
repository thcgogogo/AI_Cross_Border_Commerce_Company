"""跨境财务 sub-tab 1 — 对账 EXTENDED for cross-border (Amazon FF V2 + FX + reserve).

The 对账 engine is REUSED, not rewritten: a pack-named pre-normalization adapter
aggregates Amazon's multi-row-per-order Settlement Flat File V2 into one synthetic
per-order CNY row, and reconcile() folds the reserve (held → 在途) + FX 点差 in. The
core reconcile() signature + every domestic pack stay byte-stable (proven by the
unchanged test_settlement_recon.py). These tests pin the new behavior.

Pure, deterministic, hermetic — no LLM, no IO, no network. PROVISIONAL: the FX rate
+ amount-type taxonomy are the documented FF V2 model, not a real seller export.
"""
import os
import sys
from datetime import date
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import crossborder_adapters as cba  # noqa: E402
from agent_team import settlement_engine as engine  # noqa: E402
from agent_team import settlement_templates as templates  # noqa: E402

RATE = Decimal("7.20")  # matches templates_platforms/amazon/pack.yaml fx.rate


def _ffv2_row(order_id, ttype, atype, adesc, amount, *, currency="USD",
              date="2026-06-10", settlement_id="S-1"):
    return {
        "order-id": order_id, "transaction-type": ttype, "amount-type": atype,
        "amount-description": adesc, "amount": amount, "currency": currency,
        "posted-date": date, "settlement-id": settlement_id,
    }


def _amazon_dataset():
    """One settlement: order A-001 (sale of 100, FBA fee 10, referral 15) + a cross-
    period refund of 30 on A-001; order A-002 (sale 50, FBA fee 5); a 40 reserve hold.
    All in USD; the adapter converts at 7.20 and surfaces reserve as 在途."""
    rows = [
        _ffv2_row("A-001", "Order", "ItemPrice", "Principal", "100.00"),
        _ffv2_row("A-001", "Order", "ItemFees", "FBAPerUnitFulfillmentFee", "-10.00"),
        _ffv2_row("A-001", "Order", "ItemFees", "Commission", "-15.00"),
        _ffv2_row("A-002", "Order", "ItemPrice", "Principal", "50.00"),
        _ffv2_row("A-002", "Order", "ItemFees", "FBAPerUnitFulfillmentFee", "-5.00"),
        _ffv2_row("A-001", "Refund", "ItemPrice", "Principal", "-30.00"),
        _ffv2_row("", "", "current reserve amount", "current reserve amount balance", "-40.00"),
    ]
    return rows


# --- the adapter in isolation (deterministic aggregation + FX + reserve) ---------
def test_adapter_aggregates_multirow_order_to_cny():
    pack = templates.load_pack("amazon")
    records, extras = cba.amazon_ffv2_preprocess(_amazon_dataset(), pack)
    by = {(r["merchant_order_no"], r["status"]): r for r in records}
    # A-001 sale: one row, components summed, converted at 7.20
    sale = by[("A-001", "SUCCESS")]
    assert sale["gross_amount"] == "720.00"      # 100 * 7.20
    assert sale["fee"] == "72.00"                # 10 * 7.20
    assert sale["commission"] == "108.00"        # 15 * 7.20
    assert sale["refund_amount"] == "0"          # refund is its own row
    assert sale["currency"] == "CNY"
    # refund emitted as a SEPARATE row (mirrors WeChat/Alipay)
    assert by[("A-001", "REFUND")]["refund_amount"] == "216.00"   # 30 * 7.20
    assert by[("A-002", "SUCCESS")]["gross_amount"] == "360.00"   # 50 * 7.20


def test_adapter_surfaces_reserve_and_fx():
    pack = templates.load_pack("amazon")
    _, extras = cba.amazon_ffv2_preprocess(_amazon_dataset(), pack)
    assert extras["reserve"] == Decimal("288.00")        # 40 held * 7.20
    assert extras["reserve_count"] == 1
    assert extras["fx_spread"] == Decimal("5.40")        # (720+360) * 0.005
    assert extras["orders"] == 2
    assert extras["currency_in"] == "USD"


def test_adapter_no_rate_passes_through():
    # A pack with no fx.rate must still aggregate (rate → 1), never crash.
    pack = {"ffv2_columns": {}, "ffv2_taxonomy": templates.load_pack("amazon")["ffv2_taxonomy"]}
    records, extras = cba.amazon_ffv2_preprocess(_amazon_dataset(), pack)
    assert extras["fx_rate"] == Decimal("1")
    assert {r["merchant_order_no"] for r in records} >= {"A-001", "A-002"}


# --- end-to-end through reconcile_records (the seam) ----------------------------
def _run(bank_arrived="360.00", bank_date="2026-06-20"):
    pack = templates.load_pack("amazon")
    bank = [{"收入金额": bank_arrived, "交易日期": bank_date}]
    return engine.reconcile_records(
        {"platform": _amazon_dataset(), "bank": bank}, pack, period_label="2026-06")


def test_end_to_end_summary_and_residual():
    out = _run()
    s = out["summary"]
    assert s["应收"] == 1080.0                  # 720 + 360
    assert s["实收"] == 648.0                   # 1080 - 108 fee - 216 refund - 108 commission
    assert s["到账"] == 360.0
    assert s["在途金额"] == 288.0               # reserve folded into 在途
    assert s["总手续费"] == 108.0
    assert s["总佣金"] == 108.0
    assert s["总退款"] == 216.0
    # reserve absorbs the gap → clean books, no 未解释差异
    assert s["未解释差异"] == 0.0


def test_reserve_keeps_residual_clean_vs_naive():
    # Without reserve modeling, 实收(648) − 到账(360) would look like a 288 hole;
    # the reserve attribution explains it exactly, so 未解释差异 is 0.
    out = _run()
    assert out["summary"]["未解释差异"] == 0.0
    kinds = {a["kind"]: a["amount"] for a in out["attributions"]}
    assert kinds["reserve_held"] == 288.0
    assert kinds["fx_spread"] == 5.4
    assert kinds["commission"] == 108.0


def test_bridge_balances_on_clean_cross_border_data():
    out = _run()
    assert out["bridge"]["ok"] is True          # 应收 − 优惠 == Σ应结
    assert out["bridge"]["imbalance"] == 0.0


def test_provenance_carries_reserve_and_fx_anchors():
    out = _run()
    claims = [r["claim_cn"] for r in out["source_rows"]]
    assert "账户准备金(reserve)" in claims
    assert "汇兑点差(FX)" in claims
    # every cross-border source row is still engine-computed (no LLM provenance)
    assert all(r["computed_by"] == "recon_engine" for r in out["source_rows"])
    # the facts block the writer must quote carries the 【N】 anchors
    assert any("账户准备金" in f and "【" in f for f in out["facts_block"])


# --- regression: per-order settle amount is net of commission (review finding) --
def _success_txn(**kw):
    base = {"source": "platform", "status": "SUCCESS", "gross_amount": Decimal("0"),
            "net_amount": Decimal("0"), "fee": Decimal("0"), "commission": Decimal("0"),
            "coupon_amount": Decimal("0"), "refund_amount": Decimal("0"),
            "_fee_known": True, "event_time": date(2026, 6, 10), "batch_no": "B1",
            "merchant_order_no": "X"}
    base.update(kw)
    return base


def test_commission_order_l3_matches_true_net():
    # gross 700, fee 56, commission 105 → true net to bank = 539. The per-order settle
    # amount must net commission too, or the L3 batch (644) never matches the bank (539).
    plat = [_success_txn(merchant_order_no="C1", gross_amount=Decimal("700"),
                         net_amount=Decimal("700"), fee=Decimal("56"),
                         commission=Decimal("105"))]
    bank = [{"source": "bank", "net_amount": Decimal("539"), "event_time": date(2026, 6, 30)}]
    out = engine.reconcile(plat, bank, [], pack=templates.load_pack("amazon"))
    assert out["buckets"]["L3"]["已匹配"] == 1        # batch 539 matches bank 539
    assert out["summary"]["未解释差异"] == 0.0


def test_commission_in_transit_not_inflated_no_phantom_negative():
    # an in-transit commission order: 在途 must be the true net (539), not 644, and the
    # residual must not go negative by the commission.
    plat = [_success_txn(merchant_order_no="C2", gross_amount=Decimal("700"),
                         net_amount=Decimal("700"), fee=Decimal("56"),
                         commission=Decimal("105"), event_time=date(2026, 6, 29))]
    bank = [{"source": "bank", "net_amount": Decimal("0"), "event_time": date(2026, 6, 10)}]
    out = engine.reconcile(plat, bank, [], pack=templates.load_pack("amazon"))
    assert out["summary"]["在途金额"] == 539.0        # true net, not 644
    assert out["summary"]["未解释差异"] == 0.0         # no phantom negative


# --- regression: domestic packs are untouched by the cross-border additions -----
def test_domestic_pack_has_no_crossborder_attributions():
    pack = templates.load_pack("wechat_pay")
    platform = [{"交易时间": "2026-06-10", "商户订单号": "D1", "交易状态": "SUCCESS",
                 "订单金额": "100.00", "应结订单金额": "100.00", "手续费": "0.60"}]
    bank = [{"交易日期": "2026-06-12", "收入金额": "99.40"}]
    out = engine.reconcile_records({"platform": platform, "bank": bank}, pack)
    kinds = {a["kind"] for a in out["attributions"]}
    # the domestic pack declares none of the cross-border kinds → none appear
    assert "reserve_held" not in kinds and "fx_spread" not in kinds and "commission" not in kinds
    # and no cross-border provenance leaked in
    claims = [r["claim_cn"] for r in out["source_rows"]]
    assert "账户准备金(reserve)" not in claims and "汇兑点差(FX)" not in claims

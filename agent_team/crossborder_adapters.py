"""
crossborder_adapters.py — cross-border platform ingest adapters for 对账.

Some 出口跨境电商 settlement statements are NOT one-row-per-order. Amazon's
Settlement Report Flat File V2 spreads ONE order across MANY rows — one per
fee/charge/tax component — in the marketplace's LOCAL currency. The deterministic
settlement_engine expects one normalized row per order, in CNY. These adapters sit
at the `reconcile_records` PRE-normalization seam: given the raw multi-row export
they (1) aggregate components into one synthetic per-order record keyed by the
canonical column names the pack's column_map then maps, (2) convert to CNY at a
configured rate, and (3) surface 准备金/reserve (held, not paid out → 在途) and the
FX 点差 as separate, deterministic figures the engine attributes.

Pure Python — no LLM, no IO, no DB — same discipline as settlement_engine. Adapters
are DATA-DRIVEN from the pack (the FF V2 column names, the amount-type taxonomy, and
the FX rate all live in pack.yaml / per-account overrides), so onboarding or retuning
a platform is a YAML edit, not code.

PROVISIONAL: grounded in the DOCUMENTED Amazon FF V2 structure + a single per-period
FX rate. Confirm the real column names, the amount-type → bucket taxonomy, the reserve
semantics, and the FX/point-spread basis against an actual settlement export before
treating any number as authoritative.

  preprocess(raw_rows, pack) -> (records, extras)
    records : list of synthetic per-order dicts (canonical-keyed, CNY) for normalize_rows
    extras  : {"fx_spread","fx_rate","fx_point_spread","reserve","reserve_count",
               "currency_in","orders"} — the cross-border figures reconcile() folds in
"""
import re
from decimal import Decimal, InvalidOperation

_ZERO = Decimal("0")
_CENT = Decimal("0.01")


def _dec(x):
    """Parse a cell into a Decimal. Tolerates $/¥ signs, thousands separators,
    parentheses-for-negative, backtick prefix, blanks → 0."""
    if x is None:
        return _ZERO
    if isinstance(x, Decimal):
        return x
    if isinstance(x, (int, float)):
        try:
            return Decimal(str(x))
        except InvalidOperation:
            return _ZERO
    s = str(x).strip().lstrip("`").strip()
    if not s or s in ("-", "--", "N/A", "无"):
        return _ZERO
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("¥", "").replace("￥", "").replace("$", "") \
         .replace(",", "").replace(" ", "")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return _ZERO
    return -d if neg else d


def _norm(s):
    return str(s or "").strip().lower()


def _parse_iso_date(x):
    """Best-effort YYYY-MM-DD out of a cell (Amazon posted-date is ISO-ish). "" → None."""
    s = str(x or "").strip().lstrip("`").strip()
    if not s:
        return None
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if not m:
        return None
    from datetime import date
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


# Bucket vocabulary the taxonomy classifies each FF V2 component row into.
BUCKET_PRINCIPAL = "principal"    # 商品本金 (Principal / ItemPrice) — the revenue side
BUCKET_FEE = "fee"                # 平台/FBA/仓储/物流 手续费
BUCKET_COMMISSION = "commission"  # 佣金 (referral fee)
BUCKET_TAX = "tax"                # 代扣代缴税 (MarketplaceFacilitatorTax) — passthrough
BUCKET_PROMO = "promo"            # 促销/优惠 (→ 优惠/coupon)
BUCKET_REFUND = "refund"          # 退款/退货
BUCKET_RESERVE = "reserve"        # 账户准备金 (held, not paid out → 在途)
BUCKET_OTHER = "other"
# Order matters: more specific buckets win (commission/tax/reserve before the broad
# "fee" / "principal" catch-alls).
_BUCKET_PRIORITY = (BUCKET_RESERVE, BUCKET_REFUND, BUCKET_COMMISSION, BUCKET_TAX,
                    BUCKET_PROMO, BUCKET_FEE, BUCKET_PRINCIPAL)

# Transaction-type substrings that mark a whole component as a refund/return.
_REFUND_TTYPES = ("refund", "retrocharge", "return", "退款", "退货", "返还")

_DEFAULT_COLS = {
    "order_id": "order-id", "settlement_id": "settlement-id",
    "transaction_type": "transaction-type", "amount_type": "amount-type",
    "amount_description": "amount-description", "amount": "amount",
    "currency": "currency", "posted_date": "posted-date",
}


def amazon_ffv2_preprocess(raw_rows, pack):
    """Aggregate an Amazon Settlement Flat File V2 (many rows per order) into one
    synthetic CNY record per order, plus reserve/FX extras. Pure; deterministic."""
    pack = pack or {}
    cols = {**_DEFAULT_COLS, **(pack.get("ffv2_columns") or {})}
    taxonomy = pack.get("ffv2_taxonomy") or {}
    fxc = pack.get("fx") or {}
    rate = _dec(fxc.get("rate"))
    if rate <= _ZERO:
        rate = Decimal("1")                    # no-rate → passthrough (still aggregates)
    spread = _dec(fxc.get("point_spread"))

    def to_cny(x):
        return (x * rate).quantize(_CENT)

    def classify(rec):
        ttype = _norm(rec.get(cols["transaction_type"]))
        if any(t in ttype for t in _REFUND_TTYPES):
            return BUCKET_REFUND
        hay = _norm(rec.get(cols["amount_type"])) + " " + _norm(rec.get(cols["amount_description"]))
        for bucket in _BUCKET_PRIORITY:
            for kw in (taxonomy.get(bucket) or []):
                k = _norm(kw)
                if k and k in hay:
                    return bucket
        return BUCKET_OTHER

    orders = {}            # order_id -> per-order component sums (signed, local currency)
    reserve_signed = _ZERO
    reserve_count = 0
    stmt_fee = _ZERO       # statement-level (no order-id) non-reserve charges
    currency_in = ""
    settlement_id = ""

    for rec in (raw_rows or []):
        if not isinstance(rec, dict):
            continue
        amt = _dec(rec.get(cols["amount"]))
        bucket = classify(rec)
        cur = str(rec.get(cols["currency"]) or "").strip()
        if cur and not currency_in:
            currency_in = cur
        sid = str(rec.get(cols["settlement_id"]) or "").strip()
        if sid and not settlement_id:
            settlement_id = sid

        if bucket == BUCKET_RESERVE:
            reserve_signed += amt              # a hold is negative; a release positive
            reserve_count += 1
            continue
        oid = str(rec.get(cols["order_id"]) or "").strip()
        if not oid:
            # statement-level non-reserve charge (storage / subscription / ad fees…)
            if bucket in (BUCKET_FEE, BUCKET_COMMISSION, BUCKET_TAX, BUCKET_OTHER):
                stmt_fee += amt                # signed (a debit is negative)
            continue
        o = orders.setdefault(oid, {"principal": _ZERO, "fee": _ZERO, "commission": _ZERO,
                                    "promo": _ZERO, "refund": _ZERO, "tax": _ZERO, "date": None})
        if bucket == BUCKET_PRINCIPAL:
            o["principal"] += amt
        elif bucket == BUCKET_COMMISSION:
            o["commission"] += amt
        elif bucket == BUCKET_PROMO:
            o["promo"] += amt
        elif bucket == BUCKET_TAX:
            o["tax"] += amt                    # passthrough; not in settled
        elif bucket == BUCKET_REFUND:
            o["refund"] += amt
        else:                                  # FEE and OTHER → a deduction
            o["fee"] += amt
        d = _parse_iso_date(rec.get(cols["posted_date"]))
        if d and (o["date"] is None or d > o["date"]):
            o["date"] = d

    # --- build synthetic per-order CNY records ---
    # Refunds are emitted as SEPARATE rows (mirroring how WeChat/Alipay export a
    # distinct refund line), so a sale-with-partial-refund keeps its full 应收 and
    # the engine's refund handling subtracts the return cleanly.
    records = []
    gross_cny_total = _ZERO
    for oid, o in orders.items():
        date_iso = o["date"].isoformat() if o["date"] else ""
        has_sale = any(o[k] != _ZERO for k in ("principal", "fee", "commission", "promo"))
        if has_sale:
            gross = o["principal"]             # 订单金额 (revenue, positive)
            coupon = abs(o["promo"])           # 优惠
            net = gross - coupon               # 应结订单金额 (so 桥式 应收−优惠==应结)
            gross_cny_total += to_cny(gross)
            records.append({
                "merchant_order_no": oid, "status": "SUCCESS",
                "gross_amount": str(to_cny(gross)), "net_amount": str(to_cny(net)),
                "fee": str(to_cny(abs(o["fee"]))), "commission": str(to_cny(abs(o["commission"]))),
                "coupon_amount": str(to_cny(coupon)), "refund_amount": "0",
                "currency": "CNY", "event_time": date_iso, "batch_no": settlement_id,
            })
        if o["refund"] != _ZERO:
            records.append({
                "merchant_order_no": oid, "status": "REFUND",
                "gross_amount": "0", "net_amount": "0", "fee": "0", "commission": "0",
                "coupon_amount": "0", "refund_amount": str(to_cny(abs(o["refund"]))),
                "currency": "CNY", "event_time": date_iso, "batch_no": settlement_id,
            })

    # statement-level fees → one synthetic order so they still reduce 实收
    if stmt_fee != _ZERO:
        records.append({
            "merchant_order_no": "(平台月度费用)", "status": "SUCCESS",
            "gross_amount": "0", "net_amount": "0", "fee": str(to_cny(abs(stmt_fee))),
            "commission": "0", "coupon_amount": "0", "refund_amount": "0",
            "currency": "CNY", "event_time": "", "batch_no": settlement_id,
        })

    # reserve held this period = the net NEGATIVE movement (a release just lands in
    # the bank as more 到账). PROVISIONAL — confirm reserve semantics against a real file.
    held = -reserve_signed if reserve_signed < _ZERO else _ZERO
    extras = {
        "fx_spread": (gross_cny_total * spread).quantize(_CENT),
        "fx_rate": rate, "fx_point_spread": spread,
        "reserve": (held * rate).quantize(_CENT), "reserve_count": reserve_count,
        "currency_in": currency_in or "USD", "orders": len(orders),
    }
    return records, extras


# ===========================================================================
# PingPong (收款服务商) — the money-chain leg 平台 → PingPong → 结汇 → 境内银行.
# ===========================================================================
# A PingPong account statement (对账单) is a TRANSACTION LEDGER (one row per movement),
# not multi-row-per-order: 收款 (platform→PingPong, USD), 结汇/提现 (PingPong→境内银行,
# CNY), 退款, 手续费, and an 未结汇余额 (held, not yet converted → 在途). This adapter
# normalizes the 收款 income into per-transaction CNY orders, separates refunds, surfaces
# 未结汇 as reserve(在途) + the FX 点差, and ignores the 结汇/提现 rows (the bank role carries
# the 结汇到账). reconcile then answers: did PingPong's 收款 all reach the 境内银行?
#
# PROVISIONAL: the column names + transaction-type taxonomy are the DOCUMENTED PingPong
# 对账单 model, not a real seller export — confirm against an actual statement.
PP_INCOME = "income"      # 收款 (platform → PingPong)
PP_SETTLE = "settle"      # 结汇 / 提现 (PingPong → 境内银行) — bank role carries this leg
PP_REFUND = "refund"      # 退款 / 退单
PP_RESERVE = "reserve"    # 未结汇余额 / 账户余额 (held → 在途)
PP_FEE = "fee"            # standalone 手续费 rows
_PP_PRIORITY = (PP_RESERVE, PP_REFUND, PP_SETTLE, PP_FEE, PP_INCOME)

_PP_DEFAULT_COLS = {
    "txn_time": "交易时间", "txn_type": "交易类型", "txn_no": "交易号",
    "order_ref": "关联订单号", "amount": "金额", "currency": "币种",
    "fee": "手续费", "status": "状态",
}


def pingpong_preprocess(raw_rows, pack):
    """Aggregate a PingPong 对账单 (transaction ledger) into per-收款 CNY order rows, plus
    reserve(未结汇)/FX extras. Pure; deterministic."""
    pack = pack or {}
    cols = {**_PP_DEFAULT_COLS, **(pack.get("pingpong_columns") or {})}
    taxonomy = pack.get("pingpong_taxonomy") or {}
    fxc = pack.get("fx") or {}
    rate = _dec(fxc.get("rate"))
    if rate <= _ZERO:
        rate = Decimal("1")                    # no-rate → passthrough (still aggregates)
    spread = _dec(fxc.get("point_spread"))

    # Per-row FX (PingPong 对账单 is multi-currency): a row already in the base currency
    # (CNY) passes through at 1; a configured per-currency rate (fx.rates) wins; else the
    # single 结汇 rate (the USD framing) covers empty/other currencies.
    base_cur = str(fxc.get("base_currency") or "CNY").strip().upper()
    fx_rates = {}
    for k, v in (fxc.get("rates") or {}).items():
        dv = _dec(v)
        if dv > _ZERO:
            fx_rates[str(k).strip().upper()] = dv

    def rate_for(cur):
        c = (cur or "").strip().upper()
        if c == base_cur:
            return Decimal("1")
        if c in fx_rates:
            return fx_rates[c]
        return rate

    def classify(rec):
        t = _norm(rec.get(cols["txn_type"]))
        for bucket in _PP_PRIORITY:
            for kw in (taxonomy.get(bucket) or []):
                k = _norm(kw)
                if k and k in t:
                    return bucket
        return None                            # unknown type → skip (don't guess)

    records = []
    gross_cny_total = _ZERO
    reserve_count = 0
    reserve_best = None       # (date|None, cny_value) of the chosen 未结汇 SNAPSHOT
    currency_in = ""
    for rec in (raw_rows or []):
        if not isinstance(rec, dict):
            continue
        bucket = classify(rec)
        if bucket is None or bucket == PP_SETTLE:
            continue                           # 结汇/提现 + unknown rows: bank role owns them
        amt = _dec(rec.get(cols["amount"]))
        fee = _dec(rec.get(cols["fee"]))
        cur = str(rec.get(cols["currency"]) or "").strip()
        if cur and not currency_in:
            currency_in = cur
        r = rate_for(cur)

        def to_cny(x, _r=r):                   # _r binds this row's rate (no late-binding)
            return (x * _r).quantize(_CENT)

        ref = (str(rec.get(cols["order_ref"]) or "").strip()
               or str(rec.get(cols["txn_no"]) or "").strip())
        d = _parse_iso_date(rec.get(cols["txn_time"]))
        date_iso = d.isoformat() if d else ""

        if bucket == PP_RESERVE:
            # 未结汇/账户余额 is a SNAPSHOT (a balance), NOT a flow — keep the latest by date
            # (or the last-seen undated row); NEVER sum repeated snapshots (that would
            # inflate 在途 and drive 未解释差异 negative).
            reserve_count += 1
            val = to_cny(abs(amt))
            if reserve_best is None:
                reserve_best = (d, val)
            else:
                bd = reserve_best[0]
                if d is not None and (bd is None or d >= bd):
                    reserve_best = (d, val)
                elif d is None and bd is None:
                    reserve_best = (d, val)    # last undated snapshot wins
            continue
        if bucket == PP_FEE:
            # a standalone fee row → a synthetic fee-only "order" so it reduces 实收
            records.append({
                "merchant_order_no": ref or "(手续费)", "status": "SUCCESS",
                "gross_amount": "0", "net_amount": "0", "fee": str(to_cny(abs(amt) or abs(fee))),
                "commission": "0", "coupon_amount": "0", "refund_amount": "0",
                "currency": "CNY", "event_time": date_iso, "batch_no": "PINGPONG",
            })
            continue
        if bucket == PP_REFUND:
            records.append({
                "merchant_order_no": ref or "(退款)", "status": "REFUND",
                "gross_amount": "0", "net_amount": "0", "fee": "0", "commission": "0",
                "coupon_amount": "0", "refund_amount": str(to_cny(abs(amt))),
                "currency": "CNY", "event_time": date_iso, "batch_no": "PINGPONG",
            })
            continue
        # PP_INCOME — one success order per 收款 (gross net of its own 手续费)
        gross = abs(amt)
        gross_cny_total += to_cny(gross)
        records.append({
            "merchant_order_no": ref or "(收款)", "status": "SUCCESS",
            "gross_amount": str(to_cny(gross)), "net_amount": str(to_cny(gross)),
            "fee": str(to_cny(abs(fee))), "commission": "0", "coupon_amount": "0",
            "refund_amount": "0", "currency": "CNY", "event_time": date_iso,
            "batch_no": "PINGPONG",
        })

    extras = {
        "fx_spread": (gross_cny_total * spread).quantize(_CENT),
        "fx_rate": rate, "fx_point_spread": spread,
        "reserve": (reserve_best[1] if reserve_best else _ZERO),
        "reserve_count": reserve_count,
        "currency_in": currency_in or "USD",
        "orders": sum(1 for r in records if r["status"] == "SUCCESS"),
    }
    return records, extras


# adapter name (pack["adapter"]) -> preprocess callable.
_REGISTRY = {
    "amazon_ffv2": amazon_ffv2_preprocess,
    "pingpong": pingpong_preprocess,
}


def get_adapter(pack):
    """Return the preprocess callable named by pack['adapter'], or None. The seam the
    engine calls: a pack with no `adapter` key reconciles exactly as before."""
    name = (pack or {}).get("adapter")
    return _REGISTRY.get(name) if name else None

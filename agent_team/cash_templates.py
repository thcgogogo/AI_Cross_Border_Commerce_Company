"""
cash_templates.py — PROVISIONAL default assumptions for the 现金位置/资金跑道 capstone.

The capstone projects a forward cash runway from the 跨境财务 hub's reconciled position plus
the seller's own plans. The hub knows the AMOUNTS (到账/在途/可退税额/reserve) but NOT the
forward TIMING of reserve release, 退税 receipt, or 结汇 — those are platform- and seller-
specific. This module supplies SAFE, EDITABLE defaults for every such timing lag + the FX rate
+ the cash buffer + the horizon, so a brand-new account gets a usable (if conservative) runway
before it has tuned anything. EVERY value here is PROVISIONAL: it is surfaced in the UI as an
editable assumption, and the real value should come from the seller's own history once enough
runs exist (see plan §13). Defaults must NEVER be silent — the engine labels each number's
source (history / pack / PROVISIONAL).

Pure data + pure helpers — no DB, no LLM, no network, no IO.
"""
from .metrics_store import CROSS_BORDER_DEFAULT_MODE, CROSS_BORDER_MODES

# --- universal PROVISIONAL defaults ----------------------------------------
# All lags are in CALENDAR DAYS measured from the relevant event date.
DEFAULT_LAGS = {
    "settlement_lag_days": 7,    # 平台结算 → 银行到账 (Amazon ~T+7; per-platform, refine from 对账)
    "reserve_release_days": 14,  # 平台准备金 reserve held → released into available
    "rebate_lag_days": 60,       # 出口退税 申报 → 到账 (varies widely by 综服平台/地区)
    "fx_settle_days": 3,         # 收款服务商 收汇 → 结汇到账 (USD in PSP → RMB in bank)
    "supplier_terms_days": 30,   # default supplier 账期 if a PO carries none
    "freight_days": 21,          # 头程 freight transit (informational; outflow timing)
}

DEFAULT_FX_RATE = 7.20           # USD → CNY, conservative + EDITABLE (never bury FX risk)
DEFAULT_BUFFER_CNY = 50000.0     # cash safety threshold the crunch alert + solver fire against
DEFAULT_HORIZON_WEEKS = 13       # ~一个季度 forward roll
DEFAULT_STALE_DAYS = 14          # settlement data older than this flags the position as stale
DEFAULT_FORECAST_BAND = 0.3      # ±band around the seller's weekly-sales estimate (Phase 2, low-confidence)

# Which forward INFLOWS apply per 贸易方式. Settlement (回款) is universal; 出口退税 only
# applies to the export modes that file for it. The capstone is universal but pulls only the
# mode-appropriate inflows (plan §architecture). PROVISIONAL — confirm per real account.
_MODE_REBATE = {
    "9810": True,    # 海外仓 — files 退税
    "general": True,  # 一般贸易 — files 退税
    "9710": True,    # B2B 直出 — files 退税
    "9610": False,   # 综试区零售 — typically 无票免税, not 退税
    "1210": True,    # 保税 — 退税 on the export leg
}


def _mode(mode):
    return mode if mode in CROSS_BORDER_MODES else CROSS_BORDER_DEFAULT_MODE


def mode_includes_rebate(mode):
    """True when 出口退税 is a relevant forward inflow for this 贸易方式. PROVISIONAL."""
    return bool(_MODE_REBATE.get(_mode(mode), True))


def default_assumptions(mode=None):
    """The PROVISIONAL assumption pack for a 贸易方式 — the engine's fallback when the seller
    has set nothing. Returns a fresh dict (callers may mutate)."""
    return {
        "fx_rate": DEFAULT_FX_RATE,
        "buffer_cny": DEFAULT_BUFFER_CNY,
        "horizon_weeks": DEFAULT_HORIZON_WEEKS,
        "stale_days": DEFAULT_STALE_DAYS,
        "forecast_band": DEFAULT_FORECAST_BAND,
        "lags": dict(DEFAULT_LAGS),
        "include_rebate": mode_includes_rebate(mode),
        "_provisional": True,   # every value above is a default, not seller-confirmed
    }


def default_manual_inputs():
    """Empty skeleton for the inputs the hub cannot know (the seller fills these in). All
    amounts in their own currency; `currency` defaults to CNY when omitted. `due_date`/`week`
    are ISO dates the engine schedules against."""
    return {
        "opening_balances": {},  # {CNY, USD} current spendable cash (the hub can't see the bank balance)
        "restock": [],          # [{label, amount, currency, due_date}]  planned 备货 (v1: manual)
        "supplier_payments": [],  # [{label, amount, currency, due_date}]
        "ad_budget": [],        # [{label, amount, currency, week}]  recurring/one-off ad spend
        "fixed_costs": [],      # [{label, amount, currency, day_of_month}]  工资/房租/税
        "duties": [],           # [{label, amount, currency, due_date}]  destination duty/VAT/tariff
        "financing": [],        # [{label, amount, currency, due_date, kind: advance|repayment}]
        "weekly_sales": None,   # Phase 2: seller's est. weekly sales→cash run-rate → forecast band
    }


def default_prefs(mode=None):
    """Delivery + cadence prefs for the scheduled report (the interactive view needs none)."""
    return {
        "trade_mode": _mode(mode),
        "cadence": "weekly",          # weekly (default) | daily
        "delivery_channels": [],      # ["email","wechat"]
    }


def merged_assumptions(mode=None, overrides=None):
    """The effective assumptions: PROVISIONAL defaults with the seller's saved overrides applied
    on top (shallow for scalars, one level deep for `lags`). `_provisional` flips False once the
    seller has overridden anything, so the UI can stop labeling untouched values."""
    base = default_assumptions(mode)
    ov = overrides or {}
    if not ov:
        return base
    lags = dict(base["lags"])
    lags.update({k: v for k, v in (ov.get("lags") or {}).items() if v is not None})
    base.update({k: v for k, v in ov.items()
                 if k not in ("lags", "_provisional") and v is not None})
    base["lags"] = lags
    base["_provisional"] = False
    return base

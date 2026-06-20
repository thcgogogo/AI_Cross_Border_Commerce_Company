"""
settlement_templates.py — Platform-format templates for 对账 (平台结算核对).

A "platform pack" is a per-platform bundle of CONTENT (not code, and never customer
data) living on disk under `templates_platforms/<key>/pack.yaml`. It tells the
deterministic settlement engine how to read ONE platform's settlement statement:

  - column_map          raw column header  ->  canonical NormalizedTxn field, per
                        source role (platform / bank / ledger)
  - status_map          raw status value   ->  canonical SUCCESS / REFUND / REVOKED
  - fee_rate            default 费率 used to sanity-check 手续费 (per-account override)
  - settlement_lag_days default T+N lag for the cross-period bank match (overridable)
  - attribution_rules   ordered list of {cause, kind, params} the engine walks to
                        explain each gap; adding a cause that fits an existing
                        engine `kind` is a YAML edit, no code change.

Same two design rules as industry_packs.py:

  1. HOT-EDITABLE — load_pack() re-reads the YAML on every call (no caching).
  2. FAIL LOUDLY AT BOOT — validate_all_packs() raises PlatformPackError naming the
     file + missing key, so a broken pack can never silently degrade a 对账 run.

This module holds ONLY the shared, cross-account parsing schema. A specific
customer's 费率 / lag / column-name overrides live in the company brain
(company_profile.profile_json["recon"]) and are merged in via apply_overrides().
"""

import os

import yaml

# templates_platforms/ lives at the project root (one level above this package).
PACKS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "templates_platforms"))

# The neutral fallback pack — used for an unknown platform and for bank/ledger
# sources whose columns are supplied via per-account overrides.
GENERIC_KEY = "generic"

# The canonical NormalizedTxn fields a column_map may target (the engine's shared
# transaction shape). Documented here so packs + overrides reference one vocabulary.
CANONICAL_FIELDS = (
    "platform_txn_no", "merchant_order_no", "batch_no", "event_time", "direction",
    "gross_amount", "net_amount", "fee", "commission", "refund_amount",
    "coupon_amount", "status", "currency", "counterparty",
)

# The source roles a customer uploads (and that column_map keys on).
SOURCE_PLATFORM = "platform"   # 平台结算单 / 对账单
SOURCE_BANK = "bank"           # 银行流水
SOURCE_LEDGER = "ledger"       # 账本 / ERP 导出
SOURCE_ROLES = (SOURCE_PLATFORM, SOURCE_BANK, SOURCE_LEDGER)

# Every platform pack.yaml MUST define these keys (validated loudly at boot).
REQUIRED_KEYS = ("display_name_cn", "column_map", "attribution_rules")

# Map a Chinese platform LABEL (what the customer picks in the dropdown) to a pack
# key. Anything not here that isn't already a real key resolves to GENERIC_KEY.
PLATFORM_LABEL_TO_KEY = {
    "微信支付": "wechat_pay",
    "微信": "wechat_pay",
    "支付宝": "alipay",
    "通用": GENERIC_KEY,
    "其他": GENERIC_KEY,
}

# Header the platform block leads with when injected into the writer's prompt.
PLATFORM_HEADER = "=== 结算平台规则 (PLATFORM SETTLEMENT RULES — context only) ==="


class PlatformPackError(Exception):
    """A platform pack is missing, unreadable, or malformed. Raised at boot."""


# ---------------------------------------------------------------------------
# Paths & resolution
# ---------------------------------------------------------------------------
def _pack_dir(key):
    return os.path.join(PACKS_DIR, key)


def _pack_path(key):
    return os.path.join(_pack_dir(key), "pack.yaml")


def list_pack_keys():
    """Every subfolder of templates_platforms/ that contains a pack.yaml, sorted."""
    if not os.path.isdir(PACKS_DIR):
        return []
    return [name for name in sorted(os.listdir(PACKS_DIR))
            if os.path.isfile(_pack_path(name))]


def resolve_platform(platform_value):
    """Resolve a platform value to a pack key, falling back to GENERIC_KEY.

    Accepts (in priority order): an exact pack key on disk, a known Chinese label
    (PLATFORM_LABEL_TO_KEY), else generic."""
    v = (platform_value or "").strip()
    if not v:
        return GENERIC_KEY
    if os.path.isfile(_pack_path(v)):
        return v
    if v in PLATFORM_LABEL_TO_KEY:
        return PLATFORM_LABEL_TO_KEY[v]
    return GENERIC_KEY


# ---------------------------------------------------------------------------
# Loading & validation
# ---------------------------------------------------------------------------
def _read_yaml(path):
    if not os.path.isfile(path):
        raise PlatformPackError(f"Platform pack not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise PlatformPackError(f"{path}: invalid YAML — {e}")
    if not isinstance(data, dict):
        raise PlatformPackError(
            f"{path}: a pack must be a YAML mapping, got {type(data).__name__}.")
    return data


def _validate(data, path):
    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        raise PlatformPackError(
            f"{path} is missing required key(s): {', '.join(missing)}.")
    if not isinstance(data.get("column_map"), dict):
        raise PlatformPackError(f"{path}: 'column_map' must be a mapping.")
    if not isinstance(data.get("attribution_rules"), list):
        raise PlatformPackError(f"{path}: 'attribution_rules' must be a list.")
    return data


def load_pack(platform_value):
    """Read + validate the pack for a platform value, FRESH from disk every call
    (hot-editable). Unknown/empty -> the generic pack. Raises PlatformPackError if
    the resolved pack file is missing or malformed."""
    key = resolve_platform(platform_value)
    path = _pack_path(key)
    data = _validate(_read_yaml(path), path)
    data["_key"] = key
    return data


def validate_all_packs():
    """Validate EVERY platform pack on disk. Call once at app start: raises
    PlatformPackError if any pack is malformed, and requires the generic fallback."""
    keys = list_pack_keys()
    if GENERIC_KEY not in keys:
        raise PlatformPackError(
            f"Required fallback pack '{GENERIC_KEY}' not found under {PACKS_DIR}.")
    for key in keys:
        path = _pack_path(key)
        _validate(_read_yaml(path), path)


def available_platforms():
    """[{key, display_name_cn}] for the 对账 platform dropdown. Best-effort: never
    crashes the UI on a malformed pack (the loud gate is validate_all_packs)."""
    out = []
    for key in list_pack_keys():
        try:
            data = _read_yaml(_pack_path(key))
            label = (data.get("display_name_cn") or "").strip() or key
        except PlatformPackError:
            label = key
        out.append({"key": key, "display_name_cn": label})
    return out


# ---------------------------------------------------------------------------
# Per-account overrides (from company_profile.profile_json["recon"])
# ---------------------------------------------------------------------------
def apply_overrides(pack, overrides):
    """Return a shallow copy of `pack` with this account's overrides folded in.

    `overrides` is the per-platform dict the company brain stores, e.g.
    {"fee_rate": 0.0038, "settlement_lag_days": 2,
     "column_map": {"platform": {"net_amount": "应结金额"}}}.
    Scalars replace; column_map merges per source role (override wins). The shared
    pack on disk is never mutated."""
    if not overrides:
        return dict(pack)
    merged = dict(pack)
    for scalar in ("fee_rate", "settlement_lag_days", "amount_tolerance"):
        if overrides.get(scalar) is not None:
            merged[scalar] = overrides[scalar]
    # Cross-border: per-account FX (rate / 点差) + reserve params fold into the pack's
    # `fx` block, so a seller's real settlement rate beats the pack default. PROVISIONAL.
    fx_ov = overrides.get("fx")
    if isinstance(fx_ov, dict):
        base_fx = dict(pack.get("fx") or {})
        base_fx.update(fx_ov)
        merged["fx"] = base_fx
    ov_cols = overrides.get("column_map")
    if isinstance(ov_cols, dict):
        base_cols = {role: dict(cols) for role, cols in (pack.get("column_map") or {}).items()}
        for role, cols in ov_cols.items():
            if isinstance(cols, dict):
                base_cols.setdefault(role, {}).update(cols)
        merged["column_map"] = base_cols
    return merged


# ---------------------------------------------------------------------------
# Accessors the engine uses
# ---------------------------------------------------------------------------
def column_map_for(pack, role):
    """The {canonical_field: raw_column} mapping for one source role, or {}."""
    return dict((pack.get("column_map") or {}).get(role) or {})


def status_map_for(pack):
    return dict(pack.get("status_map") or {})


def fee_rate(pack):
    """The default 费率 (a fraction, e.g. 0.006), or None if the pack doesn't set one."""
    v = pack.get("fee_rate")
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def settlement_lag_days(pack, default=1):
    try:
        return int(pack.get("settlement_lag_days", default))
    except (TypeError, ValueError):
        return default


def attribution_rules(pack):
    """The ordered list of {cause, kind, params} the engine walks. Skips malformed
    entries and any without a `kind`."""
    out = []
    for r in (pack.get("attribution_rules") or []):
        if isinstance(r, dict) and r.get("kind"):
            out.append({
                "cause": str(r.get("cause") or r.get("kind")).strip(),
                "kind": str(r.get("kind")).strip(),
                "params": dict(r.get("params") or {}),
            })
    return out


def build_platform_block(pack):
    """A short, context-only block describing the platform's fee structure for the
    writer's prompt (it explains gaps; it never computes them). "" when empty."""
    if not pack:
        return ""
    name = (pack.get("display_name_cn") or pack.get("_key") or "").strip()
    lines = []
    rate = fee_rate(pack)
    if rate is not None:
        lines.append(f"- 默认支付手续费费率：约 {rate * 100:.2f}%（实际以结算单为准）。")
    lines.append(f"- 结算到账通常为 T+{settlement_lag_days(pack)} 日。")
    causes = [r["cause"] for r in attribution_rules(pack)]
    if causes:
        lines.append("- 本平台已知差异类型：" + "、".join(causes) + "。")
    if not name and not lines:
        return ""
    head = f"{PLATFORM_HEADER}\n结算平台：{name}" if name else PLATFORM_HEADER
    return head + "\n" + "\n".join(lines)

"""
ar_templates.py — Account-format templates for 应收账款管理 (AR aging + 催收).

The 应收 sibling of settlement_templates.py. An "AR pack" is a per-format bundle of
CONTENT (not code, never customer data) under `templates_ar/<key>/pack.yaml`. It tells
the deterministic AR engine how to read ONE customer's three source files:

  - column_map          raw column header  ->  canonical field, per source role
                        (invoices / payments / customers)
  - default_terms_days  fallback 账期 used to derive a due date when none is given
  - aging_boundaries    the day cutoffs for the overdue buckets (default 30/60/90)
  - dispute_keywords    note substrings that mark an invoice 疑似 (disputed)
  - dunning             escalation thresholds + cadence lock (read by dunning.py)
  - amount_tolerance    the fen tolerance for self-audit identities

Same two design rules as settlement_templates / industry_packs:
  1. HOT-EDITABLE — load_pack() re-reads the YAML on every call (no caching).
  2. FAIL LOUDLY AT BOOT — validate_all_packs() raises ARPackError naming the file +
     missing key, so a broken pack can never silently degrade an AR run.

A specific customer's 账期 / credit-limit / column-name overrides + learned grading
rules live in the company brain (company_profile.profile_json["ar"]) and are merged in
via apply_overrides(). This module is independent of the 对账 module by design (the two
share no code today; a later chassis extraction will dedupe what is genuinely identical).
"""

import os

import yaml

# templates_ar/ lives at the project root (one level above this package).
PACKS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "templates_ar"))

# The neutral fallback pack — used for an unknown format and for files whose columns
# are supplied via per-account overrides.
GENERIC_KEY = "generic"

# The canonical fields a column_map may target, per role (the engine's shared shape).
CANONICAL_FIELDS = {
    "invoices": ("invoice_no", "customer_id", "customer_name", "issue_date",
                 "due_date", "terms_days", "gross_amount", "currency", "note"),
    "payments": ("payment_no", "customer_id", "pay_date", "amount", "invoice_ref"),
    "customers": ("customer_id", "customer_name", "terms_days", "credit_limit",
                  "contact"),
}

# The source roles a customer uploads (and that column_map keys on).
ROLE_INVOICES = "invoices"   # 应收 / 已开票台账
ROLE_PAYMENTS = "payments"   # 回款 / 已收记录
ROLE_CUSTOMERS = "customers"  # 客户主数据 (账期 / 信用额度 / 联系方式)
SOURCE_ROLES = (ROLE_INVOICES, ROLE_PAYMENTS, ROLE_CUSTOMERS)

# Every AR pack.yaml MUST define these keys (validated loudly at boot). Note: unlike
# 对账 there is no attribution_rules requirement — AR attributions are aging-bucket
# roll-ups, not pack-declared causes.
REQUIRED_KEYS = ("display_name_cn", "column_map")

# Map a Chinese format LABEL (what the customer might pick) to a pack key.
FORMAT_LABEL_TO_KEY = {
    "通用": GENERIC_KEY,
    "其他": GENERIC_KEY,
    "标准应收台账": GENERIC_KEY,
}

# Defaults used when a pack does not override them.
DEFAULT_TERMS_DAYS = 30
DEFAULT_AGING_BOUNDARIES = (30, 60, 90)
DEFAULT_DISPUTE_KEYWORDS = ("争议", "纠纷", "质量", "退货", "暂缓", "待确认", "对账", "投诉")

# Header the context block leads with when injected into the writer's prompt.
AR_PLATFORM_HEADER = "=== 应收规则 (AR RULES — context only) ==="


class ARPackError(Exception):
    """An AR pack is missing, unreadable, or malformed. Raised at boot."""


# ---------------------------------------------------------------------------
# Paths & resolution
# ---------------------------------------------------------------------------
def _pack_dir(key):
    return os.path.join(PACKS_DIR, key)


def _pack_path(key):
    return os.path.join(_pack_dir(key), "pack.yaml")


def list_pack_keys():
    """Every subfolder of templates_ar/ that contains a pack.yaml, sorted."""
    if not os.path.isdir(PACKS_DIR):
        return []
    return [name for name in sorted(os.listdir(PACKS_DIR))
            if os.path.isfile(_pack_path(name))]


def resolve_format(format_value):
    """Resolve a format value to a pack key, falling back to GENERIC_KEY.

    Accepts (in priority order): an exact pack key on disk, a known Chinese label
    (FORMAT_LABEL_TO_KEY), else generic."""
    v = (format_value or "").strip()
    if not v:
        return GENERIC_KEY
    if os.path.isfile(_pack_path(v)):
        return v
    if v in FORMAT_LABEL_TO_KEY:
        return FORMAT_LABEL_TO_KEY[v]
    return GENERIC_KEY


# ---------------------------------------------------------------------------
# Loading & validation
# ---------------------------------------------------------------------------
def _read_yaml(path):
    if not os.path.isfile(path):
        raise ARPackError(f"AR pack not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ARPackError(f"{path}: invalid YAML — {e}")
    if not isinstance(data, dict):
        raise ARPackError(
            f"{path}: a pack must be a YAML mapping, got {type(data).__name__}.")
    return data


def _validate(data, path):
    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        raise ARPackError(
            f"{path} is missing required key(s): {', '.join(missing)}.")
    if not isinstance(data.get("column_map"), dict):
        raise ARPackError(f"{path}: 'column_map' must be a mapping.")
    return data


def load_pack(format_value):
    """Read + validate the pack for a format value, FRESH from disk every call
    (hot-editable). Unknown/empty -> the generic pack. Raises ARPackError if the
    resolved pack file is missing or malformed."""
    key = resolve_format(format_value)
    path = _pack_path(key)
    data = _validate(_read_yaml(path), path)
    data["_key"] = key
    return data


def validate_all_packs():
    """Validate EVERY AR pack on disk. Call once at app start: raises ARPackError if
    any pack is malformed, and requires the generic fallback."""
    keys = list_pack_keys()
    if GENERIC_KEY not in keys:
        raise ARPackError(
            f"Required fallback pack '{GENERIC_KEY}' not found under {PACKS_DIR}.")
    for key in keys:
        path = _pack_path(key)
        _validate(_read_yaml(path), path)


def available_formats():
    """[{key, display_name_cn}] for the AR format dropdown. Best-effort: never crashes
    the UI on a malformed pack (the loud gate is validate_all_packs)."""
    out = []
    for key in list_pack_keys():
        try:
            data = _read_yaml(_pack_path(key))
            label = (data.get("display_name_cn") or "").strip() or key
        except ARPackError:
            label = key
        out.append({"key": key, "display_name_cn": label})
    return out


# ---------------------------------------------------------------------------
# Per-account overrides (from company_profile.profile_json["ar"]["<key>"])
# ---------------------------------------------------------------------------
def apply_overrides(pack, overrides):
    """Return a shallow copy of `pack` with this account's overrides folded in.

    `overrides` is the per-format dict the company brain stores, e.g.
    {"default_terms_days": 45, "amount_tolerance": 0.01,
     "column_map": {"invoices": {"gross_amount": "含税金额"}},
     "dunning": {"cadence_lock_days": 10}}.
    Scalars replace; column_map + dunning merge per key (override wins). The shared
    pack on disk is never mutated."""
    if not overrides:
        return dict(pack)
    merged = dict(pack)
    for scalar in ("default_terms_days", "amount_tolerance", "aging_boundaries"):
        if overrides.get(scalar) is not None:
            merged[scalar] = overrides[scalar]
    ov_cols = overrides.get("column_map")
    if isinstance(ov_cols, dict):
        base_cols = {role: dict(cols)
                     for role, cols in (pack.get("column_map") or {}).items()}
        for role, cols in ov_cols.items():
            if isinstance(cols, dict):
                base_cols.setdefault(role, {}).update(cols)
        merged["column_map"] = base_cols
    ov_dun = overrides.get("dunning")
    if isinstance(ov_dun, dict):
        base_dun = dict(pack.get("dunning") or {})
        base_dun.update(ov_dun)
        merged["dunning"] = base_dun
    return merged


# ---------------------------------------------------------------------------
# Accessors the engine uses
# ---------------------------------------------------------------------------
def column_map_for(pack, role):
    """The {canonical_field: raw_column} mapping for one source role, or {}."""
    return dict((pack.get("column_map") or {}).get(role) or {})


def status_map_for(pack):
    return dict(pack.get("status_map") or {})


def default_terms_days(pack, default=DEFAULT_TERMS_DAYS):
    try:
        return int(pack.get("default_terms_days", default))
    except (TypeError, ValueError):
        return default


def aging_boundaries(pack, default=DEFAULT_AGING_BOUNDARIES):
    """Ascending day cutoffs for the overdue buckets, e.g. (30, 60, 90). Always 3."""
    raw = pack.get("aging_boundaries")
    if isinstance(raw, (list, tuple)) and len(raw) == 3:
        try:
            vals = tuple(int(x) for x in raw)
            if vals[0] < vals[1] < vals[2]:
                return vals
        except (TypeError, ValueError):
            pass
    return tuple(default)


def dispute_keywords(pack):
    """Note substrings that mark an invoice 疑似 (disputed). Pack-overridable."""
    raw = pack.get("dispute_keywords")
    if isinstance(raw, (list, tuple)) and raw:
        return tuple(str(k).strip() for k in raw if str(k).strip())
    return DEFAULT_DISPUTE_KEYWORDS


def amount_tolerance(pack, default=0.01):
    try:
        v = pack.get("amount_tolerance")
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Dunning accessors (read by dunning.py — escalation thresholds + cadence lock)
# ---------------------------------------------------------------------------
def dunning_config(pack):
    """The pack's dunning block (thresholds + cadence), with safe defaults filled in.

    PROVISIONAL defaults pending the owner's real dunning policy:
      day_bands: reminder ≤15, urge 16–45, final_notice 46–90, handoff >90
      amount_large = 10000, amount_legal = 100000
      cadence_lock_days = 7 (per-tier overridable)
    """
    d = dict(pack.get("dunning") or {})
    out = {
        "reminder_max_dpd": int(d.get("reminder_max_dpd", 15)),
        "urge_max_dpd": int(d.get("urge_max_dpd", 45)),
        "final_max_dpd": int(d.get("final_max_dpd", 90)),
        "amount_large": float(d.get("amount_large", 10000)),
        "amount_legal": float(d.get("amount_legal", 100000)),
        "cadence_lock_days": int(d.get("cadence_lock_days", 7)),
        # Optional per-tier cadence override, e.g. {"final_notice": 10}.
        "cadence_lock_days_by_tier": dict(d.get("cadence_lock_days_by_tier") or {}),
        # Per-channel length caps (chars), PROVISIONAL.
        "length_caps": dict(d.get("length_caps") or {
            "email": 400, "wechat": 200, "sms": 70}),
    }
    return out


def cadence_lock_days(pack, tier=None):
    cfg = dunning_config(pack)
    by_tier = cfg["cadence_lock_days_by_tier"]
    if tier and tier in by_tier:
        try:
            return int(by_tier[tier])
        except (TypeError, ValueError):
            pass
    return cfg["cadence_lock_days"]


def build_ar_block(pack):
    """A short, context-only block describing the AR rules for the writer's prompt
    (it explains figures; it never computes them). "" when empty."""
    if not pack:
        return ""
    name = (pack.get("display_name_cn") or pack.get("_key") or "").strip()
    lines = [
        f"- 默认账期：{default_terms_days(pack)} 天（仅在发票未给到期日时用于推算）。",
        "- 账龄分桶：" + "、".join(_bucket_labels(aging_boundaries(pack))) + "（天，逾期）。",
    ]
    head = f"{AR_PLATFORM_HEADER}\n应收格式：{name}" if name else AR_PLATFORM_HEADER
    return head + "\n" + "\n".join(lines)


def _bucket_labels(boundaries):
    b0, b1, b2 = boundaries
    return [f"0-{b0}", f"{b0 + 1}-{b1}", f"{b1 + 1}-{b2}", f"{b2}+"]

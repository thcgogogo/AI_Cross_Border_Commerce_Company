"""
rebate_engine.py — DETERMINISTIC 出口退税核算 (export VAT rebate estimation).

For 出口企业 the available 退税 is bounded by two realities: the input VAT actually
collected on 进项专票, and the 退税率 (which varies by HS-code, never a guarantee). This
engine pools the 进项税额, applies a PROVISIONAL 退税率 to each 报关 FOB amount, and
estimates the 可退税额 capped by the input-VAT pool — surfacing every gap a human (会计)
must close before any 申报. It is an EVIDENCE ASSEMBLER, not a 申报/报税 tool — it
estimates and flags, decides no tax, files nothing, takes no action; it produces the
worklist (进项不足 / 收汇缺失 / 退税率缺失) and a 退税额 estimate, nothing more.

Deterministic checks:
  1. 进项不足      Σ理论退税额 > Σ进项税额 → 可退税额 is input-VAT-constrained (退税额受限).
  2. 收汇缺失      an export whose `ref` has no settled 收汇/结汇 receipt — 收汇 is a
                   precondition for 退税; if no receipts file is supplied at all, ONE
                   summary-level note is emitted instead of per-export spam.
  3. 退税率缺失    an export row with no 退税率 fell back to the pack default rate.
  4. 进项税额为0   an input invoice carrying a 税率 but a 税额 of 0 (data-quality flag).

Pure Python — no LLM, no DB, no network, no file IO in the compute core (only
`run_rebate` reads files). Money as Decimal internally, floats-via-_f2 out, JSON-
serializable. Returns the SAME dict shape as settlement_engine (summary / exceptions /
bridge / source_rows / facts_block) so it rides the existing report-writer +
verification + persistence rails.

Grounding (2026): 出口退税率 is a four-tier structure 13% / 9% / 6% / 0%; the general rule is
退税率 = 该货物的增值税适用税率 (标准 13%) unless 财政部/税务总局 lowered or cancelled it (财政部
税务总局公告2026年第11号). The exact rate is per 出口商品 HS-code via the annually-issued 出口退税率
文库 (现行 2026A 版, 税总货劳函〔2026〕27号), so it is NEVER a single guaranteed value. The FOB ×
退税率 form here is the 生产企业「免抵退」 model; 外贸企业 use 购进价 × 退税率 — the input-VAT-pool cap
(可退税额 = min(理论, 进项)) approximates that. The pack `default_rebate_rate` (0.13, the
standard-rate case) and the column names must still be confirmed per product/account.
"""
from decimal import Decimal

from .settlement_engine import (_amt_match, _clean, _f2, _fmt, _money,
                                _parse_date)

_ZERO = Decimal("0")
COMPUTED_BY = "rebate_engine"             # provenance tag (mirrors threeflow_engine)
AMOUNT_TOLERANCE = Decimal("0.01")
# PROVISIONAL default 退税率 — 13% is a common rate but it varies by HS-code; never a
# guarantee. A real per-account pack / per-row 退税率 overrides this.
DEFAULT_REBATE_RATE = Decimal("0.13")

# Three input roles. input_invoices(进项专票) + exports(报关单) are the core; receipts
# (收汇/结汇) is an OPTIONAL precondition. exports/receipts key on a shared `ref`
# (订单号/报关关联号), the identifier that threads 出口 to 收汇.
ROLE_INPUT_INVOICES = "input_invoices"
ROLE_EXPORTS = "exports"
ROLE_RECEIPTS = "receipts"
ROLE_CN = {ROLE_INPUT_INVOICES: "进项专票", ROLE_EXPORTS: "报关/出口",
           ROLE_RECEIPTS: "收汇/结汇"}

# Canonical fields per role (mapped from raw headers via the pack column_map).
_FIELDS = {
    ROLE_INPUT_INVOICES: ("invoice_no", "seller", "amount", "tax_amount", "rate",
                          "date", "product"),
    ROLE_EXPORTS: ("customs_no", "ref", "fob_amount", "rebate_rate", "product", "date"),
    ROLE_RECEIPTS: ("ref", "amount", "settled", "date"),
}
_MONEY_FIELDS = ("amount", "tax_amount", "fob_amount", "settled")
_RATE_FIELDS = ("rate", "rebate_rate")
_DATE_FIELDS = ("date",)

# Default generic column_map (Chinese headers) + tolerances + PROVISIONAL rate. A real
# per-account pack / override replaces these. PROVISIONAL.
DEFAULT_PACK = {
    "display_name_cn": "通用 / 出口退税",
    "amount_tolerance": 0.01,
    "default_rebate_rate": 0.13,          # PROVISIONAL — varies by HS-code, not a guarantee
    "column_map": {
        ROLE_INPUT_INVOICES: {"invoice_no": "发票号", "seller": "销售方",
                              "amount": "价税合计", "tax_amount": "税额", "rate": "税率",
                              "date": "开票日期", "product": "货物名称"},
        ROLE_EXPORTS: {"customs_no": "报关单号", "ref": "订单号", "fob_amount": "出口额",
                       "rebate_rate": "退税率", "product": "货物名称", "date": "报关日期"},
        ROLE_RECEIPTS: {"ref": "订单号", "amount": "收汇金额", "settled": "已结汇金额",
                        "date": "结汇日期"},
    },
}


def _tolerance(pack):
    try:
        v = pack.get("amount_tolerance")
        return Decimal(str(v)) if v is not None else AMOUNT_TOLERANCE
    except Exception:  # noqa: BLE001
        return AMOUNT_TOLERANCE


def _default_rebate_rate(pack):
    try:
        v = pack.get("default_rebate_rate")
        return Decimal(str(v)) if v is not None else DEFAULT_REBATE_RATE
    except Exception:  # noqa: BLE001
        return DEFAULT_REBATE_RATE


def _col(pack, role):
    return dict((pack.get("column_map") or {}).get(role) or {})


def _rate(raw):
    """Parse a rate cell tolerantly. Accepts 0.13, '13%', '13' (→0.13). Returns Decimal
    or None when absent/unparseable. Pure."""
    s = _clean(raw)
    if not s:
        return None
    pct = s.endswith("%")
    s = s.rstrip("%").strip()
    try:
        v = Decimal(s)
    except Exception:  # noqa: BLE001
        return None
    if pct or v > 1:
        v = v / Decimal("100")
    return v


def normalize_role(records, role, pack):
    """Map raw row dicts for one role to canonical dicts via the pack column_map. Pure."""
    col = _col(pack, role)
    fields = _FIELDS.get(role, ())
    out = []
    for i, rec in enumerate(records or []):
        if not isinstance(rec, dict):
            continue
        row = {"_role": role, "_row": i + 1}
        for f in fields:
            raw = rec.get(col.get(f)) if col.get(f) else None
            if f in _MONEY_FIELDS:
                row[f] = _money(raw)
            elif f in _RATE_FIELDS:
                row[f] = _rate(raw)
            elif f in _DATE_FIELDS:
                row[f] = _parse_date(raw)
            else:
                row[f] = _clean(raw)
        out.append(row)
    return out


def check(records_by_role, pack=None, *, period_label=None, context=None):
    """Estimate the export VAT rebate from normalized records. `records_by_role` holds
    already-normalized canonical dicts per role. Returns the engine result dict. Pure.

    `context` (optional) carries AGGREGATE figures the orchestrator read from UPSTREAM
    跨境财务 sub-tabs so the seller needn't re-upload: `input_tax_pool` (有效进项税额 from
    发票审核) fills the input-VAT pool when 进项专票 weren't uploaded; `recon_received`
    (到账 from 对账) corroborates 收汇 when 收汇/结汇 weren't uploaded. Uploaded data always
    wins; the engine stays pure (the DB reads happen in the orchestrator)."""
    pack = pack or DEFAULT_PACK
    tol = _tolerance(pack)
    default_rate = _default_rebate_rate(pack)
    ctx = context or {}

    inputs = records_by_role.get(ROLE_INPUT_INVOICES) or []
    exports = records_by_role.get(ROLE_EXPORTS) or []
    receipts = records_by_role.get(ROLE_RECEIPTS) or []

    exceptions = []

    def _ex(ref, cause, severity, amount, detail):
        exceptions.append({
            "ref": ref, "cause": cause, "severity": severity,
            "amount": _f2(amount), "detail": detail,
        })

    # 1. 进项税额合计 — the available input-VAT pool
    sum_input_tax = _ZERO
    for inv in inputs:
        tax = inv.get("tax_amount", _ZERO)
        sum_input_tax += tax
        # data-quality: a 税率 present but 税额 == 0 is suspect
        if inv.get("rate") is not None and tax <= _ZERO:
            _ex(inv.get("invoice_no") or "", "进项税额为0", "中", _ZERO,
                f"进项专票「{inv.get('invoice_no') or '?'}」有税率 {_fmt_rate(inv.get('rate'))}"
                f" 但税额为 0（请核对税额列）")
    # cross-sub-tab: borrow the input-VAT pool from 发票审核 when no 进项专票 uploaded
    input_from_upstream = False
    if not inputs and ctx.get("input_tax_pool") is not None:
        sum_input_tax = _money(ctx["input_tax_pool"])
        input_from_upstream = True

    # 收汇/结汇 precondition: the set of refs with settled > 0
    settled_refs = set()
    for rc in receipts:
        ref = rc.get("ref")
        if ref and rc.get("settled", _ZERO) > _ZERO:
            settled_refs.add(ref)
    has_receipts = bool(receipts)

    # 2./3./5. per-export: 出口额, 理论退税额, 退税率缺失, 收汇缺失
    sum_fob = _ZERO
    sum_theoretical = _ZERO
    n_export = 0
    n_settled = 0
    for ex in exports:
        n_export += 1
        fob = ex.get("fob_amount", _ZERO)
        sum_fob += fob
        rate = ex.get("rebate_rate")
        used_default = rate is None
        rate = rate if rate is not None else default_rate
        theoretical = fob * rate
        sum_theoretical += theoretical
        ref = ex.get("ref")
        customs = ex.get("customs_no")
        label = customs or ref or ""

        if used_default:
            _ex(label, "退税率缺失(用默认)", "中", theoretical,
                f"报关「{label or '?'}」未提供退税率，按默认 {_fmt_rate(default_rate)} 估算"
                f"（退税率随商品HS编码而定，须核实）")

        # 收汇/结汇 precondition (only when receipts were supplied; else one summary note)
        if has_receipts and ref:
            if ref in settled_refs:
                n_settled += 1
            else:
                _ex(ref, "收汇缺失", "高", fob,
                    f"报关「{label or '?'}」无已结汇收汇记录（收汇/结汇为退税前提，"
                    f"未收汇不可退税）")
        elif has_receipts and not ref:
            # no ref to match on; treat as unmatched
            _ex(label, "收汇缺失", "高", fob,
                f"报关「{label or '?'}」无订单号无法关联收汇（收汇/结汇为退税前提，"
                f"未收汇不可退税）")

    # cross-sub-tab: corroborate 收汇 from 对账's 到账 when no 收汇/结汇 uploaded. This is a
    # SOFT, magnitude-aware corroboration — it must NOT erase the precondition caveat on the
    # strength of any positive 到账. Only a 到账 that COVERS the export total fully suppresses
    # the caveat; a partial 到账 keeps a (softer) note and surfaces the coverage.
    recon_recv = (_money(ctx["recon_received"])
                  if ctx.get("recon_received") is not None else None)
    recon_present = (not has_receipts) and recon_recv is not None and recon_recv > _ZERO
    recon_full = recon_present and (recon_recv + tol) >= sum_fob
    if exports and not has_receipts:
        if not recon_present:
            # graceful: one summary-level note, don't spam per export
            _ex("", "收汇未提供", "中", _ZERO,
                "未提供收汇/结汇数据；收汇为退税前提，退税额估算未经收汇核验")
        elif not recon_full:
            _ex("", "收汇部分佐证", "中", sum_fob - recon_recv,
                f"对账到账 ¥{_fmt(recon_recv)} 低于出口额 ¥{_fmt(sum_fob)}，"
                f"未收汇部分请核实（收汇为退税前提）")

    # 4. 可退税额(受进项约束) = min(理论退税额合计, 进项税额合计)
    refundable = min(sum_theoretical, sum_input_tax)
    input_constrained = sum_theoretical > sum_input_tax and not _amt_match(
        sum_theoretical, sum_input_tax, tol)
    if input_constrained:
        _ex("", "进项不足", "高", sum_theoretical - sum_input_tax,
            f"理论退税额 ¥{_fmt(sum_theoretical)} 超过进项税额 ¥{_fmt(sum_input_tax)}，"
            f"可退税额受进项税额限制为 ¥{_fmt(refundable)}")

    # 收汇状态 short string — show the 到账 coverage vs 出口额 so a tiny 到账 can't read
    # as fully collected.
    if has_receipts:
        settle_status = f"已收汇{n_settled}/总{n_export}"
    elif recon_present:
        settle_status = f"对账佐证 ¥{_fmt(recon_recv)}/出口额 ¥{_fmt(sum_fob)}"
    else:
        settle_status = "未提供"

    # what the cross-sub-tab data flow brought in (so the seller needn't re-upload)
    borrowed = []
    if input_from_upstream:
        borrowed.append(f"进项税额(发票审核) ¥{_fmt(sum_input_tax)}")
    if recon_present:
        borrowed.append(f"收汇到账(对账) ¥{_fmt(recon_recv)}")

    # bridge self-audit: lhs=理论退税额合计, rhs=可退税额(受进项约束). ok = not 进项-constrained
    # (理论 ≤ 进项, within tolerance) — i.e. the estimate is NOT capped down by the pool.
    bridge_ok = (not input_constrained) and _amt_match(sum_theoretical, refundable, tol)
    bridge = {
        "ok": bool(bridge_ok),
        "lhs": _f2(sum_theoretical), "rhs": _f2(refundable),
        "imbalance": _f2(sum_theoretical - refundable),
        "理论退税额": _f2(sum_theoretical), "可退税额": _f2(refundable),
        "进项税额": _f2(sum_input_tax),
    }

    summary = {
        "出口额合计": _f2(sum_fob),
        "进项税额合计": _f2(sum_input_tax),
        "理论退税额": _f2(sum_theoretical),
        "可退税额": _f2(refundable),
        "收汇状态": settle_status,
        "上游带入": "；".join(borrowed),
        "交易数": n_export,
        "异常数": len(exceptions),
        "period_label": period_label,
    }
    source_rows, facts = _build_provenance(
        summary, bridge, input_from_upstream=input_from_upstream,
        recon_corroborated=recon_present)
    return {
        "summary": summary, "exceptions": exceptions, "bridge": bridge,
        "source_rows": source_rows, "facts_block": facts,
    }


def _fmt_rate(rate):
    """Format a Decimal rate as a percent string, e.g. 0.13 → '13%'. Pure, display-only."""
    if rate is None:
        return "?"
    try:
        pct = (Decimal(str(rate)) * Decimal("100")).normalize()
        return f"{pct}%"
    except Exception:  # noqa: BLE001
        return str(rate)


def _build_provenance(summary, bridge, *, input_from_upstream=False,
                      recon_corroborated=False):
    rows, facts = [], []
    anchor = [0]

    def add(claim_cn, value_str, detail):
        anchor[0] += 1
        n = anchor[0]
        rows.append({"anchor": n, "claim_cn": claim_cn, "source_file": "出口退税数据",
                     "source_detail": detail, "computed_by": COMPUTED_BY})
        facts.append(f"{claim_cn} {value_str}【{n}】（{detail}）")
        return n

    add("出口额合计", f"¥{_fmt(summary['出口额合计'])}", "报关单出口额(FOB)求和")
    add("进项税额合计", f"¥{_fmt(summary['进项税额合计'])}",
        "由发票审核带入(有效进项税额)" if input_from_upstream else "进项专票税额求和(可退税进项池)")
    add("理论退税额", f"¥{_fmt(summary['理论退税额'])}",
        "Σ(出口额×退税率)，退税率随HS编码而定为暂定值")
    add("可退税额", f"¥{_fmt(summary['可退税额'])}",
        "min(理论退税额, 进项税额)，受进项税额约束")
    add("收汇状态", summary['收汇状态'],
        "由对账『到账』佐证(收汇为退税前提)" if recon_corroborated else "收汇/结汇为退税前提")
    return rows, facts


def check_records(records_by_role, pack=None, *, period_label=None, context=None):
    """Normalize raw per-role records, then estimate the rebate. The testable seam:
    `records_by_role` = {role: [raw row dicts keyed by the source's headers]}."""
    pack = pack or DEFAULT_PACK
    normalized = {role: normalize_role(records_by_role.get(role), role, pack)
                  for role in _FIELDS}
    return check(normalized, pack, period_label=period_label, context=context)


def run_rebate(files_by_role, pack=None, *, period_label=None, context=None):
    """Read each uploaded file into raw records, then estimate the rebate. `files_by_role`
    = {role: (path, display_name)}. Reuses settlement_engine's encrypted table reader.
    `context` carries upstream aggregates (see check) the orchestrator read from the brain."""
    from .settlement_engine import _read_table
    records = {}
    for role, spec in (files_by_role or {}).items():
        path = spec[0] if isinstance(spec, (tuple, list)) else spec
        records[role] = _read_table(path)
    return check_records(records, pack, period_label=period_label, context=context)

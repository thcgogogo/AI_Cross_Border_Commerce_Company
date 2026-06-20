"""
invoice_audit_engine.py — DETERMINISTIC 进项发票审核 (input-VAT invoice validity/completeness).

A boss has a pile of 进项发票 (input/purchase VAT invoices) and needs to know which are
GOOD for 退税/成本/合规 抵扣 and which are 问题票 a 会计 must chase. This engine audits a
batch of already-structured invoice rows and flags every票面/合规 problem — wrong 抬头,
作废/红冲, 普票不可抵扣, 税率异常, 重复, 超查验时限(5年), 查验未通过, 税额不自洽 — then rolls up
有效进项税额 and (if 销项 given) the 进销差. It is an EVIDENCE ASSEMBLER, not a 申报/认证/
抵扣 tool: it decides no tax, certifies nothing, files nothing, sends nothing; it surfaces
the 问题票 worklist for a human (会计) to act on.

The 发票 OCR and 真伪查验 happen UPSTREAM — the engine receives rows that already carry an
optional `verify_result` field straight from a 3rd-party 查验 SaaS. NO OCR, NO 查验 network
call happens here; `verify_result` is read as plain data.

Eight deterministic per-invoice checks (each firing adds an exception, keyed by 发票号):
  1. 抬头/税号不符    buyer_name/buyer_taxno != 本公司 (购方主体不符 → 不属于本公司进项).
  2. 作废/红冲        票面状态 作废/红冲 → 异常凭证, 暂不允许抵扣 (国家税务总局公告2019年第38号).
  3. 普票不可抵扣    type=普票 (一般纳税人 进项抵扣 需 专票; 普票 informational, 不计入抵扣).
  4. 税率异常        rate ∉ 法定税率集.
  5. 重复发票号      duplicate (发票代码,发票号) OR duplicate 发票号 (重复入账风险).
  6. 超查验时限(5年)  开票超过 5 年 → 全国增值税发票查验平台已无法在线查验 (info, 请人工核实).
  7. 查验未通过      verify_result 提供且 != 一致 (真伪/状态存疑, 请核实).
  8. 税额与价税不符  税额 ≈ 价税合计 × 税率/(1+税率) within tolerance.

Grounding (2026): the fixed 认证/抵扣 deadline (former 180→360 天) was ABOLISHED by 国家税务总局
公告2019年第45号 (2017-01-01 及以后开具的专票, 自 2020-03-01 起 取消认证确认/稽核比对/申报抵扣
期限) — so this engine does NOT flag a "超认证期限". What still blocks抵扣: 作废/红冲/异常凭证
(公告2019年第38号) + 票面信息/税率/重复. 真伪查验 is via the 国家税务总局全国增值税发票查验平台
(inv-veri.chinatax.gov.cn, 公告2016年第87号): inputs 发票代码+号码+开票日期+(专票:不含税金额 /
普票:校验码后6位), 每张发票每日限 5 次, 可查近 5 年 — hence the 5-year 超查验时限 info flag.

Pure Python — no LLM, no DB, no network, no file IO in the core (only run_invoice_audit
reads files). Money as Decimal, floats out, JSON-serializable. Returns the SAME dict shape
as settlement_engine (summary / exceptions / bridge / source_rows / facts_block).

PROVISIONAL: the 税率集 ({0.13,0.09,0.06,0.03,0.01,0}), the column names, the 本公司 抬头/税号,
and the 作废/红冲/查验 status vocabulary are the documented model — confirm against real files.
"""
from decimal import Decimal

from .settlement_engine import (_amt_match, _clean, _f2, _fmt, _money,
                                 _parse_date)

_ZERO = Decimal("0")
COMPUTED_BY = "invoice_audit_engine"      # provenance tag (mirrors threeflow_engine)
AMOUNT_TOLERANCE = Decimal("0.01")

# 法定增值税税率/征收率集合 — PROVISIONAL. 13%/9%/6% 一般税率, 3%/1% 征收率/小规模, 0 零税率.
# A rate outside this set is flagged 税率异常 for a human to confirm.
VALID_RATES = {Decimal("0.13"), Decimal("0.09"), Decimal("0.06"),
               Decimal("0.03"), Decimal("0.01"), Decimal("0")}

# 票面状态: anything in this set means the invoice is 不可抵扣 / 不计入有效进项.
VOID_STATUSES = {"作废", "红冲", "已红冲", "invalid", "INVALID", "已作废"}

# The roles. `invoices` is the audited batch; `sales` (销项/出口) is OPTIONAL and only
# feeds the 进销差. `company` (本公司期望购方主体) is config carried on the pack, not a
# table of rows.
ROLE_INVOICES = "invoices"
ROLE_SALES = "sales"
ROLE_CN = {ROLE_INVOICES: "进项发票", ROLE_SALES: "销项"}

# 专票/普票 vocabulary — only 专票 (增值税专用发票) is抵扣-eligible.
_SPECIAL_TYPES = {"专票", "专用发票", "增值税专用发票", "special"}

# Canonical fields per role (mapped from raw headers via the pack column_map).
_FIELDS = {
    ROLE_INVOICES: ("invoice_no", "invoice_code", "type", "buyer_name", "buyer_taxno",
                    "seller", "amount", "tax_amount", "rate", "date", "status",
                    "verify_result"),
    ROLE_SALES: ("ref", "tax_amount", "amount", "date"),
}
_MONEY_FIELDS = ("amount", "tax_amount")
_RATE_FIELDS = ("rate",)
_DATE_FIELDS = ("date",)

# Default generic column_map (Chinese headers) + tolerances + PROVISIONAL params. A real
# per-account pack / override replaces these.
DEFAULT_PACK = {
    "display_name_cn": "通用 / 进项发票审核",
    "amount_tolerance": 0.01,
    # The 认证/抵扣 DEADLINE was abolished (公告2019年第45号). This is now the 查验平台's real
    # limit: invoices older than ~5 years can't be re-verified online → an INFO flag only.
    "verify_window_days": 1825,
    # 本公司期望购方主体 — used to check 抬头/税号 == 本公司. Override per account.
    "company": {"name": "", "taxno": ""},
    "column_map": {
        ROLE_INVOICES: {
            "invoice_no": "发票号", "invoice_code": "发票代码", "type": "发票类型",
            "buyer_name": "购方名称", "buyer_taxno": "购方税号", "seller": "销售方",
            "amount": "价税合计", "tax_amount": "税额", "rate": "税率",
            "date": "开票日期", "status": "票面状态", "verify_result": "查验结果",
        },
        ROLE_SALES: {"ref": "凭证号", "tax_amount": "销项税额",
                     "amount": "金额", "date": "日期"},
    },
}


def _tolerance(pack):
    try:
        v = pack.get("amount_tolerance")
        return Decimal(str(v)) if v is not None else AMOUNT_TOLERANCE
    except Exception:  # noqa: BLE001
        return AMOUNT_TOLERANCE


def _window_days(pack):
    try:
        v = pack.get("verify_window_days")
        return int(v) if v is not None else 1825
    except Exception:  # noqa: BLE001
        return 1825


def _company(pack):
    c = dict(pack.get("company") or {})
    return _clean(c.get("name")), _clean(c.get("taxno"))


def _col(pack, role):
    return dict((pack.get("column_map") or {}).get(role) or {})


def _parse_rate(raw):
    """Parse a 税率 cell into a Decimal fraction. Tolerates '13%', '0.13', '13', ''.
    A bare integer/large number is read as a percent (13 -> 0.13); a value <= 1 is read
    as already-fractional (0.13 -> 0.13). Blank/unparseable -> None (so we don't false-
    flag a missing rate as 异常)."""
    if raw is None:
        return None
    s = _clean(raw)
    if not s:
        return None
    pct = s.endswith("%")
    s = s.rstrip("%").strip()
    if not s:
        return None
    d = _money(s)
    if pct:
        return d / Decimal("100")
    # bare number: > 1 means it was written as a percent (e.g. 13), else fractional.
    return d / Decimal("100") if d > Decimal("1") else d


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
                row[f] = _parse_rate(raw)
            elif f in _DATE_FIELDS:
                row[f] = _parse_date(raw)
            else:
                row[f] = _clean(raw)
        out.append(row)
    return out


def _is_special(inv):
    """专票 (增值税专用发票) — the only抵扣-eligible type. Empty type defaults to 专票
    (most 进项审核 batches are专票); only an explicit 普票 is treated as 普票."""
    t = (inv.get("type") or "").strip()
    if not t:
        return True
    return t in _SPECIAL_TYPES


def _is_void(inv):
    return (inv.get("status") or "").strip() in VOID_STATUSES


def _recompute_tax(inv):
    """税额 implied by 价税合计 and 税率: 税额 = 价税合计 × 税率 / (1 + 税率).
    Returns None when rate is missing (can't recompute)."""
    rate = inv.get("rate")
    amt = inv.get("amount", _ZERO)
    if rate is None:
        return None
    denom = Decimal("1") + rate
    if denom == _ZERO:
        return _ZERO
    return amt * rate / denom


def check(records_by_role, pack=None, *, period_label=None):
    """Audit the normalized invoice batch. `records_by_role` holds already-normalized
    canonical dicts per role. Returns the engine result dict. Pure."""
    pack = pack or DEFAULT_PACK
    tol = _tolerance(pack)
    window = _window_days(pack)
    co_name, co_taxno = _company(pack)
    has_company = bool(co_name or co_taxno)

    invoices = list(records_by_role.get(ROLE_INVOICES) or [])
    sales = list(records_by_role.get(ROLE_SALES) or [])

    # The "as of" anchor for the 时限 check: the latest invoice date in the batch (or the
    # pack's as_of if provided). PROVISIONAL — a real认证 window anchors on认证/申报日.
    as_of = pack.get("as_of") if isinstance(pack.get("as_of"), object) else None
    if not isinstance(as_of, (type(None),)) and as_of is not None:
        as_of = _parse_date(as_of)
    dates = [inv.get("date") for inv in invoices if inv.get("date")]
    if as_of is None and dates:
        as_of = max(dates)

    # duplicate detection (count each occurrence AFTER the first as a 重复).
    code_no_seen = {}      # (invoice_code, invoice_no) -> count seen so far
    no_seen = {}           # invoice_no -> count seen so far

    exceptions = []
    n_special_valid = 0
    n_general = 0
    n_void = 0
    n_dup = 0
    n_verify_fail = 0
    valid_input_tax = _ZERO
    lhs_tax = _ZERO        # Σ stated 税额 (all invoices)
    rhs_tax = _ZERO        # Σ recomputed 税额 (all invoices, where rate known)

    def _ex(ref, cause, severity, amount, detail):
        exceptions.append({
            "ref": ref, "cause": cause, "severity": severity,
            "amount": _f2(amount), "detail": detail,
        })

    for inv in invoices:
        ref = inv.get("invoice_no") or inv.get("invoice_code") or ""
        amt = inv.get("amount", _ZERO)
        tax = inv.get("tax_amount", _ZERO)
        rate = inv.get("rate")
        is_special = _is_special(inv)
        is_void = _is_void(inv)
        is_dup = False
        verify_ok = True

        lhs_tax += tax
        recomputed = _recompute_tax(inv)
        if recomputed is not None:
            rhs_tax += recomputed

        # 1. 抬头/税号不符
        if has_company:
            bn = inv.get("buyer_name") or ""
            bt = inv.get("buyer_taxno") or ""
            bad_name = bool(co_name) and bn != co_name
            bad_taxno = bool(co_taxno) and bt != co_taxno
            if bad_name or bad_taxno:
                parts = []
                if bad_name:
                    parts.append(f"抬头「{bn}」≠ 本公司「{co_name}」")
                if bad_taxno:
                    parts.append(f"税号「{bt}」≠ 本公司「{co_taxno}」")
                _ex(ref, "抬头/税号不符", "高", amt, "；".join(parts))

        # 2. 作废/红冲
        if is_void:
            n_void += 1
            _ex(ref, "作废/红冲发票", "高", amt,
                f"票面状态「{inv.get('status')}」，不可抵扣、不计入有效进项")

        # 3. 普票不可抵扣 (informational)
        if not is_special:
            n_general += 1
            _ex(ref, "普票不可抵扣进项", "中", tax,
                f"发票类型「{inv.get('type')}」非专票，税额不计入可抵扣进项（仅作成本参考）")

        # 4. 税率异常
        if rate is not None and rate not in VALID_RATES:
            _ex(ref, "税率异常", "中", tax,
                f"税率 {(rate * 100).normalize()}% 不在法定税率集（请核实）")

        # 5. 重复发票号
        key = (inv.get("invoice_code") or "", inv.get("invoice_no") or "")
        code_no_seen[key] = code_no_seen.get(key, 0) + 1
        no_key = inv.get("invoice_no") or ""
        if no_key:
            no_seen[no_key] = no_seen.get(no_key, 0) + 1
        dup_code_no = key[1] and code_no_seen[key] > 1
        dup_no = no_key and no_seen[no_key] > 1
        if dup_code_no or dup_no:
            is_dup = True
            n_dup += 1
            _ex(ref, "重复发票号", "高", amt,
                f"发票号「{no_key}」第 {no_seen.get(no_key, 1)} 次出现，重复入账风险")

        # 6. 超查验时限(5年) — info only. The 认证/抵扣 deadline is abolished (公告2019年第45号);
        # this flags invoices the 全国增值税发票查验平台 can no longer verify online (~5 年).
        d = inv.get("date")
        if d and as_of is not None:
            age = (as_of - d).days
            if age > window:
                _ex(ref, "超查验时限(请核实)", "低", amt,
                    f"开票日 {d.isoformat()}，票龄 {age} 天 > {window} 天（查验平台仅可查近5年；"
                    f"认证抵扣期限已取消，此为查验提示）")

        # 7. 查验未通过
        vr = (inv.get("verify_result") or "").strip()
        if vr and vr != "一致":
            verify_ok = False
            n_verify_fail += 1
            _ex(ref, "查验未通过(请核实)", "高", amt,
                f"第三方查验结果「{vr}」，真伪/状态存疑")

        # 8. 税额与价税不符
        if recomputed is not None and amt > _ZERO:
            if abs(tax - recomputed) > tol:
                _ex(ref, "税额与价税不符", "中", tax - recomputed,
                    f"票面税额 ¥{_fmt(tax)} vs 价税×税率/(1+税率)推算 ¥{_fmt(recomputed)}")

        # aggregate: 有效进项专票 = 专票 且 状态正常 且 非重复 且 查验通过(一致/未查)
        if is_special and not is_void and not is_dup and verify_ok:
            n_special_valid += 1
            valid_input_tax += tax

    n_total = len(invoices)
    n_exceptions = len(exceptions)

    # bridge self-audit: does the stated 税额 roll up to the价税×税率 recomputation?
    # Identity: Σ票面税额 ?= Σ(价税合计 × 税率 / (1+税率)). 偏差大 → 票面数据自相矛盾。
    bridge_ok = _amt_match(lhs_tax, rhs_tax, tol) if (lhs_tax or rhs_tax) else True
    bridge = {
        "ok": bool(bridge_ok),
        "lhs": _f2(lhs_tax), "rhs": _f2(rhs_tax),
        "imbalance": _f2(lhs_tax - rhs_tax),
        "票面税额合计": _f2(lhs_tax), "推算税额合计": _f2(rhs_tax),
    }

    summary = {
        "发票总数": n_total,
        "有效进项专票数": n_special_valid,
        "有效进项税额": _f2(valid_input_tax),
        "普票数": n_general,
        "作废红冲数": n_void,
        "重复数": n_dup,
        "查验未通过数": n_verify_fail,
        "异常数": n_exceptions,
        "period_label": period_label,
    }

    # 进销差 only when 销项 supplied: 有效进项税额 − Σ销项税额.
    if sales:
        sum_output_tax = sum((s.get("tax_amount", _ZERO) for s in sales), _ZERO)
        summary["进销差"] = _f2(valid_input_tax - sum_output_tax)
    else:
        sum_output_tax = _ZERO

    source_rows, facts = _build_provenance(summary, bridge, bool(sales), sum_output_tax)
    return {
        "summary": summary, "exceptions": exceptions, "bridge": bridge,
        "source_rows": source_rows, "facts_block": facts,
    }


def _build_provenance(summary, bridge, has_sales, sum_output_tax):
    rows, facts = [], []
    anchor = [0]

    def add(claim_cn, value_str, detail):
        anchor[0] += 1
        n = anchor[0]
        rows.append({"anchor": n, "claim_cn": claim_cn, "source_file": "进项发票数据",
                     "source_detail": detail, "computed_by": COMPUTED_BY})
        facts.append(f"{claim_cn} {value_str}【{n}】（{detail}）")
        return n

    add("受审发票总数", f"{summary['发票总数']} 张", "进项发票批次行数")
    add("有效进项专票", f"{summary['有效进项专票数']} 张",
        "专票 且 状态正常 且 非重复 且 查验通过")
    add("有效进项税额", f"¥{_fmt(summary['有效进项税额'])}", "有效进项专票税额求和")
    add("问题票", f"{summary['异常数']} 项",
        f"作废红冲 {summary['作废红冲数']} / 普票 {summary['普票数']} / "
        f"重复 {summary['重复数']} / 查验未通过 {summary['查验未通过数']}")
    if has_sales:
        add("进销差", f"¥{_fmt(summary['进销差'])}",
            f"有效进项税额 − 销项税额 ¥{_fmt(sum_output_tax)}")
    return rows, facts


def check_records(records_by_role, pack=None, *, period_label=None):
    """Normalize raw per-role records, then audit. The testable seam:
    `records_by_role` = {role: [raw row dicts keyed by the source's headers]}."""
    pack = pack or DEFAULT_PACK
    normalized = {role: normalize_role(records_by_role.get(role), role, pack)
                  for role in _FIELDS}
    return check(normalized, pack, period_label=period_label)


def run_invoice_audit(files_by_role, pack=None, *, period_label=None):
    """Read each uploaded file into raw records, then audit. `files_by_role` =
    {role: (path, display_name)}. Reuses settlement_engine's encrypted table reader."""
    from .settlement_engine import _read_table
    records = {}
    for role, spec in (files_by_role or {}).items():
        path = spec[0] if isinstance(spec, (tuple, list)) else spec
        records[role] = _read_table(path)
    return check_records(records, pack, period_label=period_label)

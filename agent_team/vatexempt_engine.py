"""
vatexempt_engine.py — DETERMINISTIC 无票免税 + 核定征收 (9610 综试区零售出口) 证据归集引擎.

9610 综试区零售出口 (跨境电商 B2C 小包) 常无法取得对应的销项发票，国家给予两项配套政策：
  1. 无票免税  —  对综试区内未取得有效进货凭证的零售出口货物，免征增值税。
  2. 核定征收  —  对符合条件的综试区企业，所得税按"核定应税所得率"方式征收。
本引擎按"综服平台出口数据登记 + 资格 + 适用窗口"判定每笔出口是否符合无票免税，汇总符合出口额，
再以 PROVISIONAL 的核定应税所得率估算企业所得税；并核对成本票据对所得税扣除的覆盖度。

它是一个 EVIDENCE ASSEMBLER（证据归集器），不是 申报/报税 工具 —— 它判定资格、估算口径、列出
异常清单交由人（会计/税务）核实与申报；它本身不申报、不缴税、不替企业做任何决定。

政策依据（2026 grounded — 仍须由专业税务人员对照企业实际核实）：
  * 无票免税：财税[2018]103号《关于跨境电子商务综合试验区零售出口货物税收政策的通知》(自
    2018-10-01 起执行)。同时满足三项条件即试行 免征增值税、消费税：(1) 在综试区注册并在注册地
    跨境电商线上综合服务平台登记出口日期/货物名称/计量单位/数量/单价/金额；(2) 经综试区所在地
    海关办理电商出口申报；(3) 出口货物不属于已取消出口退(免)税的货物。**通知未设任何时间窗口/
    日落条款** —— 因此本引擎不再做"适用窗口"判定（原 36 个月窗口无法律依据，已移除）。
  * 核定征收：国家税务总局公告2019年第36号《关于跨境电子商务综合试验区零售出口企业所得税核定
    征收有关问题的公告》(自 2020-01-01 起施行)：综试区零售出口企业 **应税所得率统一按 4% 确定**。
    本引擎据此估算所得税；与无票免税互为配套（无票→无进货发票→所得税改核定）。

关键纳税口径 NUANCE（务必牢记，已写入 facts）：
    无票免税仅免 增值税、消费税(VAT/消费税)，并不替代企业所得税(CIT)的成本扣除。
    —— 即便出口额符合无票免税，企业所得税仍需以核定(4%应税所得率)方式计算，成本票据仍影响所得税。

POLICY-SENSITIVE（仍需核实）：cit_rate=0.25 为法定税率（小微/优惠情形并不适用）；
cost_coverage_target=0.6 为纯经验阈值（非法定）；qualified 须以税务登记/备案为准；
assessed_income_rate=0.04 虽有依据，但具体企业是否适用核定征收仍须确认。

Pure Python — no LLM, no DB, no network, no arithmetic by the model. Decimal internally,
floats out. Returns the SAME dict shape as settlement_engine (summary / exceptions /
bridge / source_rows / facts_block) so it rides the existing report-writer + verification
+ persistence rails.
"""
from decimal import Decimal

from .settlement_engine import (_amt_match, _clean, _f2, _fmt, _money,
                                _parse_date)

_ZERO = Decimal("0")
COMPUTED_BY = "vatexempt_engine"          # provenance tag (mirrors settlement/threeflow)
AMOUNT_TOLERANCE = Decimal("0.01")

# The two document roles. exports(综服平台出口数据登记) is REQUIRED — every 出口 row carries a
# 综服平台登记标志, 出口额, 出口日期. cost_invoices(成本票据/普票) is OPTIONAL, only used to gauge
# the 所得税 成本扣除 coverage; it never affects the 无票免税(增值税) eligibility judgement.
ROLE_EXPORTS = "exports"
ROLE_COSTS = "cost_invoices"
ROLE_CN = {ROLE_EXPORTS: "综服平台出口登记", ROLE_COSTS: "成本票据"}

# Canonical fields per role (mapped from raw headers via the pack column_map).
_FIELDS = {
    ROLE_EXPORTS: ("ref", "amount", "registered", "date"),
    ROLE_COSTS: ("invoice_no", "amount", "date"),
}
_MONEY_FIELDS = ("amount",)
_DATE_FIELDS = ("date",)
_BOOL_FIELDS = ("registered",)

# Truthy / falsy tokens for the 综服平台登记 flag. PROVISIONAL — confirm the real column's
# actual vocabulary (是/否/已登记/未登记/Y/N…) against a real export.
_TRUTHY = {"是", "1", "true", "yes", "y", "已登记", "登记", "t", "对"}
_FALSY = {"否", "0", "false", "no", "n", "未登记", "未", "f", "无", ""}


def _truthy(raw):
    """Parse the 是否综服平台登记 flag. "是"/"1"/"true" → True; "否"/"0"/""/unrecognized
    → False (FAIL CLOSED). A non-zero numeric counts as 登记. Tolerant + deterministic.
    PROVISIONAL token set."""
    s = _clean(raw)
    if s is None:
        return False
    t = str(s).strip().lower()
    if t in _TRUTHY:
        return True
    if t in _FALSY:
        return False
    # Unknown non-empty token: a non-zero numeric counts as 登记; anything else FAILS
    # CLOSED. 综服平台登记 is an eligibility precondition for 无票免税 (财税[2018]103号 条件一),
    # so an unrecognized registration value must NOT silently auto-qualify the export —
    # treat it as 未登记 until a human confirms the column's real vocabulary.
    try:
        return Decimal(t) != _ZERO
    except Exception:  # noqa: BLE001
        return False


# Default generic column_map (Chinese headers) + tolerances + PROVISIONAL policy params.
# A real per-account pack / override replaces these. PROVISIONAL.
DEFAULT_PACK = {
    "display_name_cn": "通用 / 9610 综试区无票免税",
    "amount_tolerance": 0.01,
    "qualified": True,            # 本企业是否具备综试区无票免税资格 (以税务登记/备案为准)
    "assessed_income_rate": 0.04,  # 核定应税所得率 4% — 国家税务总局公告2019年第36号 (grounded)
    "cit_rate": 0.25,             # 企业所得税法定税率 (小微/优惠情形不适用)
    "cost_coverage_target": 0.6,  # 成本票据覆盖度目标 (经验阈值, 非法定)
    # NOTE: 财税[2018]103号 sets NO time window for 无票免税 → no window_months param.
    "column_map": {
        ROLE_EXPORTS: {"ref": "出口单号", "amount": "出口额",
                       "registered": "综服平台登记", "date": "出口日期"},
        ROLE_COSTS: {"invoice_no": "发票号", "amount": "金额", "date": "开票日期"},
    },
}


def _tolerance(pack):
    try:
        v = pack.get("amount_tolerance")
        return Decimal(str(v)) if v is not None else AMOUNT_TOLERANCE
    except Exception:  # noqa: BLE001
        return AMOUNT_TOLERANCE


def _dec(pack, key, default):
    """Read a Decimal policy param from the pack, tolerant of None/strings."""
    try:
        v = pack.get(key)
        return Decimal(str(v)) if v is not None else Decimal(str(default))
    except Exception:  # noqa: BLE001
        return Decimal(str(default))


def _col(pack, role):
    return dict((pack.get("column_map") or {}).get(role) or {})


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
            elif f in _DATE_FIELDS:
                row[f] = _parse_date(raw)
            elif f in _BOOL_FIELDS:
                row[f] = _truthy(raw)
            else:
                row[f] = _clean(raw)
        out.append(row)
    return out


def check(records_by_role, pack=None, *, period_label=None):
    """Judge each 出口 row for 无票免税 eligibility, sum the符合出口额, estimate核定征收所得税,
    and gauge成本票据 coverage for the所得税 deduction. `records_by_role` holds already-
    normalized canonical dicts per role. Returns the engine result dict. Pure.

    Eligibility (per export) = registered(综服平台登记) AND pack[qualified]. Per 财税[2018]103号
    无票免税 has NO time window. 无票免税 only affects增值税/消费税 — it never substitutes for the
    企业所得税 成本扣除, hence the separate成本票据 coverage check + the facts NUANCE."""
    pack = pack or DEFAULT_PACK
    tol = _tolerance(pack)
    qualified = bool(pack.get("qualified", True))
    rate = _dec(pack, "assessed_income_rate", 0.04)
    cit_rate = _dec(pack, "cit_rate", 0.25)
    cost_target = _dec(pack, "cost_coverage_target", 0.6)

    exports = list(records_by_role.get(ROLE_EXPORTS) or [])
    costs = list(records_by_role.get(ROLE_COSTS) or [])

    exceptions = []
    n_eligible = 0
    n_ineligible = 0
    sum_exports = _ZERO
    sum_eligible = _ZERO

    def _ex(ref, cause, severity, amount, detail):
        exceptions.append({
            "ref": ref, "cause": cause, "severity": severity,
            "amount": _f2(amount), "detail": detail,
        })

    # one summary-level资格 exception when the whole企业 lacks无票免税 qualification
    if not qualified:
        _ex(None, "综试区资格存疑", "高", _ZERO,
            "本企业综试区无票免税资格存疑(请核实税务登记/备案)，全部出口均不计入符合出口额")

    for r in exports:
        ref = r.get("ref")
        amt = r.get("amount", _ZERO)
        sum_exports += amt
        registered = bool(r.get("registered"))

        if not registered:
            n_ineligible += 1
            _ex(ref, "未登记综服平台", "高", amt,
                "无票免税需综服平台出口数据登记(财税[2018]103号 条件一)")
            continue
        if not qualified:
            # already flagged at summary level; count as ineligible silently per-row
            n_ineligible += 1
            continue

        n_eligible += 1
        sum_eligible += amt

    # 核定征收 estimate (PROVISIONAL): 核定应税所得 = 符合出口额 × 核定应税所得率;
    #                                估算企业所得税 = 核定应税所得 × 企业所得税率.
    assessed_income = sum_eligible * rate
    estimated_cit = assessed_income * cit_rate

    # 成本票据 coverage for the所得税 扣除 (NOT the增值税 无票免税 judgement).
    sum_costs = sum(((r.get("amount", _ZERO)) for r in costs), _ZERO)
    if costs:
        need = sum_eligible * cost_target
        if sum_costs < need:
            _ex(None, "成本票据不足(影响所得税)", "中",
                need - sum_costs,
                f"成本票据 ¥{_fmt(_f2(sum_costs))} < 符合出口额 ¥{_fmt(_f2(sum_eligible))} × "
                f"{cost_target}(目标，PROVISIONAL)；无票免税仅免增值税，所得税仍需成本扣除")
    else:
        _ex(None, "成本票据未提供", "中", _ZERO,
            "未提供成本票据，无法评估企业所得税成本扣除；无票免税仅免增值税，不替代所得税成本扣除")

    n_total = len(exports)

    # bridge self-audit: 符合无票免税出口额 must never exceed 出口额合计 (a subset sum). Equal
    # only when every export is eligible. ok = 符合 ≤ 合计 within tolerance — a data-integrity
    # invariant; a violation means a parsing/aggregation bug, not a tax finding.
    imbalance = sum_eligible - sum_exports
    bridge_ok = (sum_eligible <= sum_exports + tol)
    bridge = {
        "ok": bool(bridge_ok),
        "lhs": _f2(sum_eligible), "rhs": _f2(sum_exports),
        "imbalance": _f2(imbalance),
        "符合无票免税出口额": _f2(sum_eligible), "出口额合计": _f2(sum_exports),
    }

    summary = {
        "出口额合计": _f2(sum_exports),
        "符合无票免税出口额": _f2(sum_eligible),
        "不符合数": n_ineligible,
        "成本票据合计": _f2(sum_costs),
        "核定应税所得": _f2(assessed_income),
        "估算企业所得税": _f2(estimated_cit),
        "交易数": n_total,
        "异常数": len(exceptions),
        "period_label": period_label,
    }
    source_rows, facts = _build_provenance(summary, bridge, pack)
    return {
        "summary": summary, "exceptions": exceptions, "bridge": bridge,
        "source_rows": source_rows, "facts_block": facts,
    }


def _build_provenance(summary, bridge, pack):
    rows, facts = [], []
    anchor = [0]
    rate = pack.get("assessed_income_rate", 0.04)
    cit_rate = pack.get("cit_rate", 0.25)

    def add(claim_cn, value_str, detail):
        anchor[0] += 1
        n = anchor[0]
        rows.append({"anchor": n, "claim_cn": claim_cn, "source_file": "出口/成本数据",
                     "source_detail": detail, "computed_by": COMPUTED_BY})
        facts.append(f"{claim_cn} {value_str}【{n}】（{detail}）")
        return n

    add("出口额合计", f"¥{_fmt(summary['出口额合计'])}", "综服平台出口额求和")
    add("符合无票免税出口额", f"¥{_fmt(summary['符合无票免税出口额'])}",
        "已登记综服平台且具备资格的出口额求和(财税[2018]103号)")
    add("不符合数", f"{summary['不符合数']} 笔", "未登记综服平台/无资格的出口笔数")
    add("核定应税所得", f"¥{_fmt(summary['核定应税所得'])}",
        f"符合出口额 × 核定应税所得率 {rate}（国家税务总局公告2019年第36号，统一4%）")
    add("估算企业所得税", f"¥{_fmt(summary['估算企业所得税'])}",
        f"核定应税所得 × 企业所得税率 {cit_rate}（PROVISIONAL，仅估算）")
    # the load-bearing纳税口径 NUANCE — always present in the facts.
    add("纳税口径提示", "无票免税仅免增值税，不替代企业所得税的成本扣除",
        "增值税(VAT)免征 ≠ 企业所得税(CIT)免征；成本票据仍影响所得税成本扣除")
    return rows, facts


def check_records(records_by_role, pack=None, *, period_label=None):
    """Normalize raw per-role records, then run the eligibility/estimate核验. The testable
    seam: `records_by_role` = {role: [raw row dicts keyed by the source's headers]}."""
    pack = pack or DEFAULT_PACK
    normalized = {role: normalize_role(records_by_role.get(role), role, pack)
                  for role in _FIELDS}
    return check(normalized, pack, period_label=period_label)


def run_vatexempt(files_by_role, pack=None, *, period_label=None):
    """Read each uploaded file into raw records, then run the核验. `files_by_role` =
    {role: (path, display_name)}. Reuses settlement_engine's encrypted table reader."""
    from .settlement_engine import _read_table
    records = {}
    for role, spec in (files_by_role or {}).items():
        path = spec[0] if isinstance(spec, (tuple, list)) else spec
        records[role] = _read_table(path)
    return check_records(records, pack, period_label=period_label)

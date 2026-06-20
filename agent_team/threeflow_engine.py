"""
threeflow_engine.py — DETERMINISTIC 三流合一 / 四单合一 合规核验 (跨境财务 sub-tab 3).

For 出口跨境电商 the "flows" of one export must agree, or it is a 金税四期 / 骗税 risk:
订单(合同) ↔ 收款/结汇 ↔ 报关 ↔ 发票 (+ 物流). This engine cross-checks them per shared
transaction reference and flags every inconsistency. It is an EVIDENCE ASSEMBLER, not a
申报/报税 tool — it decides no tax, files nothing, takes no action; it surfaces the 不一致
worklist for a human (会计) to act on.

Four deterministic checks per reference:
  1. 缺流          a required flow is missing (e.g. 有报关无收汇 → 退税/收汇风险).
  2. 金额不一致    order ≈ receipt ≈ customs ≈ invoice amounts (within tolerance).
  3. 主体不一致    seller(订单) == payee(收款) == exporter(报关) == seller(发票); a
                   payee ≠ exporter is the 代理出口/买单出口 "实际货主存疑" red flag.
  4. 数量不一致    order ≈ customs ≈ logistics quantities.

Pure Python — no LLM, no DB, no arithmetic by the model. Returns the SAME dict shape as
settlement_engine (summary / exceptions / bridge / source_rows / facts_block) so it rides
the existing report-writer + verification + persistence rails. PROVISIONAL: the document
column names + tolerances are the documented model, not a real seller's export — confirm
the real columns, the entity-name normalization, and 代理出口 detection against real files.
"""
from decimal import Decimal

from .settlement_engine import (_amt_match, _clean, _f2, _fmt, _money,
                                _parse_date)

_ZERO = Decimal("0")
COMPUTED_BY = "threeflow_engine"          # provenance tag (mirrors recon_engine)
AMOUNT_TOLERANCE = Decimal("0.01")

# The five document flows. orders/receipts/customs/invoices are REQUIRED for a complete
# 四单合一; logistics(物流) is optional (corroborates 数量). All keyed on a shared `ref`
# (订单号/合同号/报关关联号), the one identifier that threads the flows together.
ROLE_ORDERS = "orders"
ROLE_RECEIPTS = "receipts"
ROLE_CUSTOMS = "customs"
ROLE_INVOICES = "invoices"
ROLE_LOGISTICS = "logistics"
REQUIRED_ROLES = (ROLE_ORDERS, ROLE_RECEIPTS, ROLE_CUSTOMS, ROLE_INVOICES)
ROLE_CN = {ROLE_ORDERS: "订单", ROLE_RECEIPTS: "收款/结汇", ROLE_CUSTOMS: "报关",
           ROLE_INVOICES: "发票", ROLE_LOGISTICS: "物流"}

# Canonical fields per role (mapped from raw headers via the pack column_map).
_FIELDS = {
    ROLE_ORDERS: ("ref", "entity", "counterparty", "amount", "currency", "qty", "date"),
    ROLE_RECEIPTS: ("ref", "payee", "amount", "currency", "date"),
    ROLE_CUSTOMS: ("customs_no", "ref", "exporter", "amount", "qty", "date"),
    ROLE_INVOICES: ("invoice_no", "ref", "seller", "amount", "qty", "date"),
    ROLE_LOGISTICS: ("waybill_no", "ref", "qty", "date"),
}
_MONEY_FIELDS = ("amount",)
_NUM_FIELDS = ("qty",)
_DATE_FIELDS = ("date",)

# Default generic column_map (Chinese headers) + tolerances. A real per-account pack /
# override replaces these. PROVISIONAL.
DEFAULT_PACK = {
    "display_name_cn": "通用 / 出口四单",
    "amount_tolerance": 0.01,
    "qty_tolerance": 0,
    "column_map": {
        ROLE_ORDERS: {"ref": "订单号", "entity": "卖方", "counterparty": "买方",
                      "amount": "金额", "currency": "币种", "qty": "数量", "date": "日期"},
        ROLE_RECEIPTS: {"ref": "订单号", "payee": "收款主体", "amount": "收款金额",
                        "currency": "币种", "date": "收款日期"},
        ROLE_CUSTOMS: {"customs_no": "报关单号", "ref": "订单号", "exporter": "经营单位",
                       "amount": "成交金额", "qty": "数量", "date": "报关日期"},
        ROLE_INVOICES: {"invoice_no": "发票号", "ref": "订单号", "seller": "销售方",
                        "amount": "价税合计", "qty": "数量", "date": "开票日期"},
        ROLE_LOGISTICS: {"waybill_no": "运单号", "ref": "订单号", "qty": "件数",
                         "date": "发货日期"},
    },
}


def _tolerance(pack):
    try:
        v = pack.get("amount_tolerance")
        return Decimal(str(v)) if v is not None else AMOUNT_TOLERANCE
    except Exception:  # noqa: BLE001
        return AMOUNT_TOLERANCE


def _col(pack, role):
    return dict((pack.get("column_map") or {}).get(role) or {})


def normalize_role(records, role, pack):
    """Map raw row dicts for one flow to canonical dicts via the pack column_map. Pure."""
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
            elif f in _NUM_FIELDS:
                row[f] = _money(raw)           # qty parsed like a number, tolerant
            elif f in _DATE_FIELDS:
                row[f] = _parse_date(raw)
            else:
                row[f] = _clean(raw)
        out.append(row)
    return out


def _entities(flows):
    """The distinct, non-empty 主体 names across a transaction's flows, tagged by role."""
    pairs = []
    o = flows.get(ROLE_ORDERS)
    if o and o.get("entity"):
        pairs.append(("订单卖方", o["entity"]))
    r = flows.get(ROLE_RECEIPTS)
    if r and r.get("payee"):
        pairs.append(("收款主体", r["payee"]))
    c = flows.get(ROLE_CUSTOMS)
    if c and c.get("exporter"):
        pairs.append(("报关经营单位", c["exporter"]))
    inv = flows.get(ROLE_INVOICES)
    if inv and inv.get("seller"):
        pairs.append(("发票销售方", inv["seller"]))
    return pairs


def _amounts(flows):
    """[(label, amount)] for flows that carry a non-zero amount."""
    out = []
    for role in (ROLE_ORDERS, ROLE_RECEIPTS, ROLE_CUSTOMS, ROLE_INVOICES):
        f = flows.get(role)
        if f and f.get("amount", _ZERO) > _ZERO:
            out.append((ROLE_CN[role], f["amount"]))
    return out


def _qtys(flows):
    out = []
    for role in (ROLE_ORDERS, ROLE_CUSTOMS, ROLE_LOGISTICS):
        f = flows.get(role)
        if f and f.get("qty", _ZERO) > _ZERO:
            out.append((ROLE_CN[role], f["qty"]))
    return out


def check(records_by_role, pack=None, *, period_label=None, context=None):
    """Cross-check the normalized flows by shared ref. `records_by_role` holds already-
    normalized canonical dicts per role. Returns the engine result dict. Pure.

    `context` (optional) carries the 订单 leg imported from 对账 (the run's persisted
    per-order list) so the seller needn't re-upload 订单: when no 订单 are uploaded and
    `context["orders_from_recon"]` is present, those become the 订单 flow (ref + amount;
    entity unknown). Uploaded 订单 always win; the read is period-scoped in the orchestrator."""
    pack = pack or DEFAULT_PACK
    tol = _tolerance(pack)
    ctx = context or {}

    # cross-sub-tab: import the 订单 leg from 对账 when 订单 not uploaded.
    rbr = dict(records_by_role)
    orders_imported = 0
    if not (rbr.get(ROLE_ORDERS)):
        imp = ctx.get("orders_from_recon") or []
        built = [{
            "_role": ROLE_ORDERS, "ref": _clean(o.get("ref")), "entity": "",
            "counterparty": "", "amount": _money(o.get("amount")), "currency": "",
            "qty": _ZERO, "date": _parse_date(o.get("date")),
        } for o in imp if _clean(o.get("ref"))]
        if built:
            rbr[ROLE_ORDERS] = built
            orders_imported = len(built)

    # index each flow's rows by ref (first row wins per ref+role; dup refs noted)
    by_ref = {}            # ref -> {role: row}
    order_of_ref = []
    for role in (ROLE_ORDERS, ROLE_RECEIPTS, ROLE_CUSTOMS, ROLE_INVOICES, ROLE_LOGISTICS):
        for row in (rbr.get(role) or []):
            ref = row.get("ref")
            if not ref:
                continue
            slot = by_ref.setdefault(ref, {})
            if ref not in order_of_ref:
                order_of_ref.append(ref)
            slot.setdefault(role, row)

    exceptions = []
    n_consistent = 0
    n_entity_mismatch = 0
    n_missing = 0
    n_amount_mismatch = 0
    sum_order = _ZERO
    sum_customs = _ZERO

    def _ex(ref, cause, severity, amount, detail):
        exceptions.append({
            "ref": ref, "cause": cause, "severity": severity,
            "amount": _f2(amount), "detail": detail,
        })

    for ref in order_of_ref:
        flows = by_ref[ref]
        issues = 0

        o = flows.get(ROLE_ORDERS)
        c = flows.get(ROLE_CUSTOMS)
        if o:
            sum_order += o.get("amount", _ZERO)
        if c:
            sum_customs += c.get("amount", _ZERO)

        # 1. 缺流 — a required flow is absent
        missing = [ROLE_CN[r] for r in REQUIRED_ROLES if r not in flows]
        if missing:
            issues += 1
            n_missing += 1
            biggest = max((a for _, a in _amounts(flows)), default=_ZERO)
            _ex(ref, "缺流", "高", biggest,
                f"缺少：{ '、'.join(missing) }（四单不齐，"
                f"{'收汇缺失→退税/收汇风险' if ROLE_CN[ROLE_RECEIPTS] in missing else '合规存疑'}）")

        # 2. 金额不一致
        amts = _amounts(flows)
        if len(amts) >= 2:
            lo = min(a for _, a in amts)
            hi = max(a for _, a in amts)
            if not _amt_match(lo, hi, tol):
                issues += 1
                n_amount_mismatch += 1
                _ex(ref, "金额不一致", "高", hi - lo,
                    "；".join(f"{lbl} ¥{_fmt(a)}" for lbl, a in amts))

        # 3. 主体不一致 (三流合一 的核心)
        ents = _entities(flows)
        distinct = {v for _, v in ents}
        if len(distinct) >= 2:
            issues += 1
            n_entity_mismatch += 1
            payee = (flows.get(ROLE_RECEIPTS) or {}).get("payee")
            exporter = (flows.get(ROLE_CUSTOMS) or {}).get("exporter")
            agency = bool(payee and exporter and payee != exporter)
            cause = "代理出口/实际货主存疑" if agency else "主体不一致(三流不合一)"
            _ex(ref, cause, "高", _ZERO,
                "；".join(f"{lbl}「{v}」" for lbl, v in ents))

        # 4. 数量不一致
        qtys = _qtys(flows)
        if len(qtys) >= 2:
            qlo = min(q for _, q in qtys)
            qhi = max(q for _, q in qtys)
            if (qhi - qlo) > _ZERO:
                issues += 1
                _ex(ref, "数量不一致", "中", _ZERO,
                    "；".join(f"{lbl} {q}" for lbl, q in qtys))

        if issues == 0:
            n_consistent += 1

    n_total = len(order_of_ref)
    n_inconsistent = n_total - n_consistent

    # bridge self-audit: do the order totals and the customs declared totals roll up?
    bridge_ok = _amt_match(sum_order, sum_customs, tol) if (sum_order or sum_customs) else True
    bridge = {
        "ok": bool(bridge_ok),
        "lhs": _f2(sum_order), "rhs": _f2(sum_customs),
        "imbalance": _f2(sum_order - sum_customs),
        "订单总额": _f2(sum_order), "报关总额": _f2(sum_customs),
    }

    summary = {
        "交易数": n_total, "一致": n_consistent, "不一致": n_inconsistent,
        "缺流": n_missing, "金额不一致": n_amount_mismatch, "主体不一致": n_entity_mismatch,
        "订单总额": _f2(sum_order), "报关总额": _f2(sum_customs),
        "上游带入": (f"订单(对账) {orders_imported} 笔" if orders_imported else ""),
        "period_label": period_label,
    }
    source_rows, facts = _build_provenance(summary, bridge, orders_imported=orders_imported)
    return {
        "summary": summary, "exceptions": exceptions, "bridge": bridge,
        "source_rows": source_rows, "facts_block": facts,
    }


def _build_provenance(summary, bridge, *, orders_imported=0):
    rows, facts = [], []
    anchor = [0]

    def add(claim_cn, value_str, detail):
        anchor[0] += 1
        n = anchor[0]
        rows.append({"anchor": n, "claim_cn": claim_cn, "source_file": "四单数据",
                     "source_detail": detail, "computed_by": COMPUTED_BY})
        facts.append(f"{claim_cn} {value_str}【{n}】（{detail}）")
        return n

    add("核验交易数", f"{summary['交易数']} 笔",
        ("订单由对账带入，关联收款/报关/发票/物流" if orders_imported
         else "按订单号关联订单/收款/报关/发票/物流"))
    add("三流一致", f"{summary['一致']} 笔", "四单齐全且金额/主体/数量一致")
    add("不一致", f"{summary['不一致']} 笔",
        f"含 缺流 {summary['缺流']} / 金额不一致 {summary['金额不一致']} / 主体不一致 {summary['主体不一致']}")
    add("报关总额", f"¥{_fmt(summary['报关总额'])}", "报关成交金额求和")
    return rows, facts


def check_records(records_by_role, pack=None, *, period_label=None, context=None):
    """Normalize raw per-role records, then cross-check. The testable seam:
    `records_by_role` = {role: [raw row dicts keyed by the source's headers]}."""
    pack = pack or DEFAULT_PACK
    normalized = {role: normalize_role(records_by_role.get(role), role, pack)
                  for role in _FIELDS}
    return check(normalized, pack, period_label=period_label, context=context)


def run_threeflow(files_by_role, pack=None, *, period_label=None, context=None):
    """Read each uploaded file into raw records, then cross-check. `files_by_role` =
    {role: (path, display_name)}. Reuses settlement_engine's encrypted table reader.
    `context` carries the 订单 leg the orchestrator imported from 对账 (see check)."""
    from .settlement_engine import _read_table
    records = {}
    for role, spec in (files_by_role or {}).items():
        path = spec[0] if isinstance(spec, (tuple, list)) else spec
        records[role] = _read_table(path)
    return check_records(records, pack, period_label=period_label, context=context)

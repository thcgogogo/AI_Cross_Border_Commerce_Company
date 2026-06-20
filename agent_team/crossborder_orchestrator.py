"""
crossborder_orchestrator.py — the 跨境财务 evidence-engine run driver + registry.

A deliberately LINEAR, NO-LLM driver for the new 跨境财务 sub-tabs (三流合一 / 退税核算 /
发票审核 / 无票免税). Each engine computes EVERY number deterministically, so — unlike 对账/AR —
there is no writer LLM at all: the report is rendered straight from the engine's 【N】-anchored
facts + exception worklist (zero hallucination surface). ONE `REGISTRY` maps each `engine_kind`
to its upload roles, run callable, report title, and summary cards; ONE `CrossBorderRunner`
persists + finishes the run (folding into the shared report quota via RUN_KIND_CROSSBORDER).

Evidence assembler, NOT a 申报/报税 tool. Every tax figure is PROVISIONAL (see each engine).
"""
from . import (invoice_audit_engine, metrics_store, rebate_engine,
               threeflow_engine, vatexempt_engine, workflow_chassis)
from .models import RUN_FAILED, RUN_SUCCEEDED

KIND_THREEFLOW = "threeflow"
KIND_REBATE = "rebate"
KIND_INVOICE = "invoice"
KIND_VATEXEMPT = "vatexempt"


# ---- cross-sub-tab data flow (§6) ----------------------------------------------
# Some sub-tabs build on EARLIER ones' persisted results so the seller needn't re-upload.
# A context_builder reads aggregate figures from the brain (graceful None) and hands them
# to the engine; the engine treats uploaded data as authoritative and only borrows to
# fill gaps. (三流←对账's per-order 收款 leg is deferred — 对账 persists only aggregates.)
def _rebate_context(company_id, period_label):
    """退税 borrows: the input-VAT pool from 发票审核 (有效进项税额) when 进项专票 weren't
    uploaded, and 收汇 corroboration from 对账 (到账). Returns a context dict or None.

    A borrow MUST be period-scoped: with no period the brain readers fall back to the
    NEWEST report of ANY period, which would silently borrow a stale, cross-period pool
    (and cap the headline 可退税额 by it). So a period-less run borrows nothing."""
    if not (period_label or "").strip():
        return None
    ctx = {}
    try:
        rec = metrics_store.read_reconciled_received(company_id, period_label)
        if rec and rec.get("到账") is not None:
            ctx["recon_received"] = rec["到账"]
    except Exception:  # noqa: BLE001
        pass
    try:
        inv = metrics_store.latest_crossborder_summary(company_id, KIND_INVOICE, period_label)
        if inv and inv.get("有效进项税额") is not None:
            ctx["input_tax_pool"] = inv["有效进项税额"]
    except Exception:  # noqa: BLE001
        pass
    return ctx or None


def _threeflow_context(company_id, period_label):
    """三流合一 imports the 订单 leg from 对账's persisted per-order list when 订单 weren't
    uploaded. Period-scoped (nothing without a period, to avoid a stale cross-period import)."""
    if not (period_label or "").strip():
        return None
    try:
        orders = metrics_store.latest_settlement_orders(company_id, period_label)
    except Exception:  # noqa: BLE001
        orders = []
    return {"orders_from_recon": orders} if orders else None

# kind -> config. roles: (role, form_field, required, cn_label). summary_cards:
# (label, summary_key, warn_when_positive). report_title titles the markdown + PDF.
REGISTRY = {
    KIND_THREEFLOW: {
        "label": "三流合一核验",
        "report_title": "三流合一 / 四单合一核验报告",
        "run": threeflow_engine.run_threeflow,
        "context_builder": _threeflow_context,
        "roles": [("orders", "orders_file", False, "订单/合同（可由对账带入）"),
                  ("receipts", "receipts_file", True, "收款/结汇"),
                  ("customs", "customs_file", True, "报关单"),
                  ("invoices", "invoices_file", True, "发票"),
                  ("logistics", "logistics_file", False, "物流/运单")],
        "summary_cards": [("交易数", "交易数", False), ("一致", "一致", False),
                          ("不一致", "不一致", True), ("主体不一致", "主体不一致", True)],
    },
    KIND_REBATE: {
        "label": "出口退税核算",
        "report_title": "出口退税核算报告（测算）",
        "run": rebate_engine.run_rebate,
        "context_builder": _rebate_context,
        # 进项专票 + 收汇 are OPTIONAL — they can be borrowed from 发票审核 / 对账 (§6).
        "roles": [("exports", "exports_file", True, "报关单/出口"),
                  ("input_invoices", "input_invoices_file", False, "进项专票（可由发票审核带入）"),
                  ("receipts", "receipts_file", False, "收汇/结汇（可由对账带入）")],
        "summary_cards": [("出口额合计", "出口额合计", False),
                          ("进项税额合计", "进项税额合计", False),
                          ("可退税额", "可退税额", False), ("异常数", "异常数", True)],
    },
    KIND_INVOICE: {
        "label": "进项发票审核",
        "report_title": "进项发票审核报告",
        "run": invoice_audit_engine.run_invoice_audit,
        "roles": [("invoices", "invoices_file", True, "进项发票"),
                  ("sales", "sales_file", False, "销项/出口")],
        "summary_cards": [("发票总数", "发票总数", False),
                          ("有效进项专票数", "有效进项专票数", False),
                          ("有效进项税额", "有效进项税额", False), ("异常数", "异常数", True)],
    },
    KIND_VATEXEMPT: {
        "label": "无票免税 + 核定征收",
        "report_title": "无票免税 + 核定征收测算报告",
        "run": vatexempt_engine.run_vatexempt,
        "roles": [("exports", "exports_file", True, "出口数据(综服平台)"),
                  ("cost_invoices", "cost_invoices_file", False, "成本票据(普票)")],
        "summary_cards": [("出口额合计", "出口额合计", False),
                          ("符合无票免税出口额", "符合无票免税出口额", False),
                          ("核定应税所得", "核定应税所得", False), ("异常数", "异常数", True)],
    },
}


def is_kind(kind):
    return kind in REGISTRY


def public_config(kind):
    """Customer-safe form/render config for a sub-tab (roles + cards + labels). No
    machinery — the run callables/engine internals are NOT serialized."""
    cfg = REGISTRY[kind]
    return {
        "kind": kind, "label": cfg["label"], "report_title": cfg["report_title"],
        "roles": [{"role": r, "field": f, "required": req, "label": cn}
                  for (r, f, req, cn) in cfg["roles"]],
        "summary_cards": [{"label": lbl, "key": key, "warn": warn}
                          for (lbl, key, warn) in cfg["summary_cards"]],
    }


def _fmt(v):
    try:
        return f"¥{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def render_report(kind, result):
    """Deterministic markdown straight from the engine output — NO LLM. Title + 核心结论
    (the 【N】-anchored facts) + a 待人工核查 table. The 数据来源与核验 table is appended by
    the result view / PDF from source_rows."""
    cfg = REGISTRY[kind]
    summary = result.get("summary") or {}
    facts = result.get("facts_block") or []
    exceptions = result.get("exceptions") or []
    lines = [f"# {cfg['report_title']}", ""]
    pl = summary.get("period_label")
    if pl:
        lines += [f"期间：{pl}", ""]
    lines += ["## 核心结论", ""] + [f"- {f}" for f in facts]
    bridge = result.get("bridge") or {}
    if not bridge.get("ok", True):
        lines += ["", f"> ⚠️ 自检未平：{_fmt(bridge.get('lhs'))} vs {_fmt(bridge.get('rhs'))}"
                  f"（差 {_fmt(bridge.get('imbalance'))}），请人工复核数据源。"]
    if exceptions:
        # every cell is sanitized — `ref`/`cause` are read verbatim from the seller's
        # upload (_clean strips only whitespace/backtick), so a 单据号 like "PO|2026|001"
        # or one with an embedded newline would otherwise corrupt the GFM table.
        def _cell(v):
            s = str(v) if v not in (None, "") else "—"
            return s.replace("|", "／").replace("\n", " ").replace("\r", " ")
        lines += ["", f"## 待人工核查（{len(exceptions)} 项）", "",
                  "| 单据 | 问题 | 级别 | 金额 | 说明 |", "|---|---|---|---|---|"]
        for e in exceptions[:100]:
            amt = _fmt(e.get("amount")) if e.get("amount") else "—"
            lines.append(f"| {_cell(e.get('ref'))} | {_cell(e.get('cause'))} | "
                         f"{_cell(e.get('severity'))} | {_cell(amt)} | {_cell(e.get('detail'))} |")
    else:
        lines += ["", "本期单证核验一致，无需人工处理 ✓"]
    lines += ["", "> 本报告为合规证据整理与提示，非申报/报税结论；税务口径以主管税务机关为准。"]
    return "\n".join(lines)


class CrossBorderRunner:
    """Linear, no-LLM runner: compute (engine) → render markdown → persist → finish."""

    def __init__(self, memory, kind, files_by_role, *, period_label=None,
                 company_id=None, run_id=None, account_id=None):
        self.memory = memory
        self.kind = kind
        self.files_by_role = files_by_role or {}
        self.period_label = period_label
        self.company_id = company_id
        self.run_id = run_id
        self.account_id = account_id
        self.memory.set_company_id(company_id)
        self.memory.set_run_id(run_id)

    def run(self):
        cfg = REGISTRY[self.kind]
        try:
            self.memory.set_status("running")
            self.memory.log("ceo", f"{cfg['label']}已就位，开始核验本期单证。", type="thinking")
            # cross-sub-tab data flow: borrow upstream aggregates from the brain (§6).
            extra = {}
            builder = cfg.get("context_builder")
            if builder and self.company_id:
                ctx = builder(self.company_id, self.period_label)
                if ctx:
                    extra["context"] = ctx
                    self.memory.log("ceo", "已自动带入上游核验结果（发票审核 / 对账）。",
                                    type="info")
            result = cfg["run"](self.files_by_role, period_label=self.period_label, **extra)
            s = result.get("summary") or {}
            n_exc = s.get("异常数", len(result.get("exceptions") or []))
            self.memory.log("ceo", f"核验完成：{cfg['label']}，待人工核查 {n_exc} 项。",
                            type="info")
            if not (result.get("bridge", {}).get("ok", True)):
                self.memory.log("ceo", "注意：自检未平，将在报告中提示人工复核。",
                                type="warning")
            final = render_report(self.kind, result)
            self.memory.set_sources(result.get("source_rows") or [], "ok")
            self._persist(result)
            self.memory.set_final_report(final)        # flips memory status -> done
            self.memory.log("ceo", f"{cfg['report_title']}已生成。", type="success")
            self._finish_run(RUN_SUCCEEDED)
        except ValueError as e:
            self.memory.set_error(str(e))
            self.memory.log("ceo", f"核验未能完成：{e}", type="error")
            self._finish_run(RUN_FAILED)
        except Exception as e:
            self.memory.set_error(f"{type(e).__name__}: {e}")
            self.memory.log("ceo", f"核验中断：{e}", type="error")
            self._finish_run(RUN_FAILED)
            raise

    def _persist(self, result):
        if not (self.company_id and self.run_id and result):
            return
        try:
            metrics_store.save_crossborder_report(
                self.company_id, self.run_id, self.kind, result,
                account_id=self.account_id, period_label=self.period_label)
        except Exception as e:
            self.memory.log("ceo", f"结果保存时出现问题（{e}）。", type="info")

    def _finish_run(self, status):
        # record the run + render the branded PDF (the shared linear-workflow finish tail).
        # cost is 0 — these engines have no LLM.
        cfg = REGISTRY[self.kind]
        workflow_chassis.finish_workflow_run(
            self.memory, self.company_id, self.run_id, status, cost_yuan=0.0,
            report_title=cfg["report_title"], period_label=self.period_label)

"""
cash_position_orchestrator.py — The 现金位置 / 资金跑道 run driver (the 跨境财务 capstone).

A deliberately LINEAR sibling of settlement_orchestrator (copies the cost / billing / lifecycle
wiring, NOT a subclass, shares no PLAN→DELEGATE→SYNTHESIZE machinery). The capstone is a pure
CONSUMER of the 跨境财务 hub: it reads the hub's already-persisted reconciled outputs READ-ONLY
(latest_settlement_summary + latest_crossborder_summary) — it does NOT re-ingest, re-reconcile,
or modify any hub code. The flow is:

  1. ASSEMBLE   read the hub outputs + the seller's editable settings (assumptions + manual
                inputs the hub cannot know), merging cash_templates PROVISIONAL defaults.
  2. PROJECT    the deterministic cash_position_engine rolls the forward runway (NO LLM) — it
                owns every number (position / runway / crunch / data-completeness).
  3. EXPLAIN    ONE writer LLM call words the engine's pre-computed facts into a one-page
                《现金位置与资金跑道》 it must quote verbatim (decision-support, never advice).
  4. SELF-AUDIT verification.audit_report flags any number the writer introduced.
  5. FINISH     record the run + ¥ cost, persist the result, render the branded PDF.

Every figure is the engine's; the LLM never computes, schedules, or alters a number — the
reliability story is identical to 对账/trends.py. The interactive affordability solver + scenarios
are a SEPARATE synchronous path (Phase 2), not this batch driver.
"""
from datetime import date

from . import (cash_position_engine, cash_templates, config, crossborder_orchestrator,
               metrics_store, verification, workflow_chassis)
from .models import RUN_FAILED, RUN_STOPPED_BUDGET, RUN_SUCCEEDED
from .providers import ModelRouter
from .token_manager import BudgetExceededError

# Verbatim header the engine's pre-computed facts are injected under — same contract as
# settlement_orchestrator.RECON_HEADER: the writer must use these numbers as-is.
CASH_HEADER = (
    "=== 系统现金核算结果 (PRE-COMPUTED — these numbers are exact, you MUST use them verbatim "
    "with their 【N】 anchors and MUST NOT recompute, reschedule, or alter them) ==="
)


def inventory_restock_plan(company_id, period_label=None):
    """The 库存补货 → 现金 integration seam (plan Phase 3). When an inventory/replenishment module
    exists, return its forward 备货 plan as a list of replenishment-plan rows the cash engine
    consumes — [{label, amount|qty+unit_cost, currency, due_date|order_date, deposit_pct?,
    freight_days?}] — so the loop closes without the seller re-keying it. Returns None today: no
    inventory data source is integrated yet (see plan §13 / NOT-DO #7), so 备货 stays seller-entered
    and CCC/库存周转 remain deferred. This stub is the single, documented plug-in point."""
    return None


def assemble_cash_inputs(company_id, period_label=None):
    """Read the seller's saved settings + the 跨境财务 hub outputs (READ-ONLY) into the
    (assumptions, hub, manual) tuple the deterministic engine consumes. Shared by the batch
    orchestrator (the scheduled/on-demand REPORT) and the synchronous solver/scenario routes,
    so both compute from exactly the same position. Touches no hub code."""
    settings = (metrics_store.get_cash_settings(company_id) if company_id else None) or {}
    prefs = settings.get("prefs") or {}
    assumptions = cash_templates.merged_assumptions(prefs.get("trade_mode"),
                                                    settings.get("assumptions"))
    manual = dict(cash_templates.default_manual_inputs())
    manual.update(settings.get("manual_inputs") or {})
    # Integration SEAM (plan Phase 3 — closing the loop): a future 库存补货 (inventory/replenishment)
    # module can auto-generate the 备货 plan so the seller needn't hand-enter it. The plan merges
    # into manual["restock"] (same replenishment-plan row shape the engine already consumes:
    # amount|qty×unit_cost + order/due date + optional 定金/头程). None today — no inventory module
    # or inventory data source exists yet (the engine's CCC/库存周转 stays deferred for the same
    # reason), so this is a graceful no-op until one ships.
    try:
        plan = inventory_restock_plan(company_id, period_label)
    except Exception:
        plan = None
    if plan:
        manual["restock"] = list(manual.get("restock") or []) + list(plan)
    hub = {"settlement": None, "rebate": None}
    if company_id:
        try:
            hub["settlement"] = metrics_store.latest_settlement_summary(
                company_id, period_label=period_label)
        except Exception:
            hub["settlement"] = None
        if assumptions.get("include_rebate", True):
            try:
                hub["rebate"] = metrics_store.latest_crossborder_summary(
                    company_id, crossborder_orchestrator.KIND_REBATE,
                    period_label=period_label)
            except Exception:
                hub["rebate"] = None
    return assumptions, hub, manual


class CashPositionOrchestrator:
    def __init__(self, memory, *, period_label=None, as_of=None, llm=None,
                 token_manager=None, cost_listener=None, company_id=None, run_id=None,
                 account_id=None):
        self.memory = memory
        self.period_label = period_label
        self.as_of = as_of or date.today()
        self.token_manager = token_manager
        self.company_id = company_id
        self.run_id = run_id
        self.account_id = account_id
        self._run_cost = 0.0
        self._result = None

        def _tally(model, input_tokens, output_tokens):
            try:
                self._run_cost += config.cost_cny(model, input_tokens, output_tokens)
            except Exception:
                pass
            if cost_listener:
                cost_listener(model, input_tokens, output_tokens)

        if llm is not None:
            self.llm = llm
            self.llm.cost_listener = _tally
            if token_manager is not None:
                self.llm.token_manager = token_manager
        else:
            self.llm = ModelRouter(token_manager=token_manager, cost_listener=_tally)

        self.memory.set_company_id(company_id)
        self.memory.set_run_id(run_id)

    # ----------------------------------------------------------------------
    def run(self):
        try:
            self.memory.set_status("running")
            self.memory.log("ceo", "现金位置团队已就位，开始汇总你的真实可动用资金与资金跑道。",
                            type="thinking")
            result = self._compute()                # deterministic engine
            final = self._write_report(result)      # one LLM call + self-audit
            self.memory.set_sources(result.get("source_rows") or [], "ok")
            self.memory.set_final_report(final)
            self._persist(result)
            self.memory.log("ceo", "现金位置与资金跑道报告已生成。", type="success")
            self._finish_run(RUN_SUCCEEDED, result)
        except BudgetExceededError as e:
            self._handle_cutoff(e)
            self._finish_run(RUN_STOPPED_BUDGET, self._result)
        except ValueError as e:
            self.memory.set_error(str(e))
            self.memory.log("ceo", f"现金位置未能完成：{e}", type="error")
            self._finish_run(RUN_FAILED, None)
        except Exception as e:
            self.memory.set_error(f"{type(e).__name__}: {e}")
            self.memory.log("ceo", f"现金位置中断：{e}", type="error")
            self._finish_run(RUN_FAILED, None)
            raise

    # ----------------------------------------------------------------------
    def _compute(self):
        """Assemble the hub outputs (READ-ONLY) + the seller's settings, then run the
        deterministic engine. Logs friendly progress to the live feed."""
        self.memory.log("ceo", "正在读取对账与退税核算结果（只读引用，不重新核对）…", type="info")
        assumptions, hub, manual = assemble_cash_inputs(self.company_id, self.period_label)

        if not hub["settlement"]:
            self.memory.log("ceo", "尚未找到对账结果：现金位置将仅基于期初余额，并在报告中标注数据缺口。",
                            type="warning")
        self.memory.log("ceo", "现金引擎正在按周推演可动用资金、在途回款与最早现金紧张点…",
                        type="thinking")

        result = cash_position_engine.project_cash(
            as_of=self.as_of, assumptions=assumptions, hub=hub, manual=manual,
            period_label=self.period_label)
        self._result = result
        s = result.get("summary", {})
        crunch = result.get("crunch")
        crunch_cn = (f"最早现金紧张 {crunch['date']}（缺口 ¥{crunch['shortfall']:,.2f}）"
                     if crunch else "区间内未跌破现金缓冲")
        self.memory.log(
            "ceo",
            f"推演完成：可动用 ¥{s.get('可动用现金', 0):,.2f}，锁定/在途 ¥{s.get('锁定在途合计', 0):,.2f}，"
            f"{crunch_cn}。", type="info")
        if not result.get("bridge", {}).get("ok", True):
            self.memory.log("ceo", "注意：现金桥式自检未平，将在报告中提示人工复核。", type="warning")
        return result

    # ----------------------------------------------------------------------
    def _write_report(self, result):
        """One writer LLM call over the engine's pre-computed facts, then the deterministic
        completeness banner + the verification number-audit. Falls back to a fully-deterministic
        report if the writer call fails for any non-budget reason."""
        cfg = config.cash_writer()
        user = self._writer_prompt(result)
        try:
            prose = self.llm.complete(
                system_prompt=cfg["system_prompt"],
                user_content=user,
                model=config.cash_writer_model(),
                effort=cfg.get("effort", "low"),
                max_tokens=cfg.get("max_tokens", 4000),
                budget=config.AGENT_TEAM_BUDGET,
            )
        except BudgetExceededError:
            raise
        except Exception as e:
            self.memory.log("ceo", f"报告撰写改用系统结果直接生成（{e}）。", type="info")
            prose = self._fallback_body(result)

        banner = self._completeness_banner(result)

        warning = ""
        try:
            unverified = verification.audit_report(
                prose, self._known_numbers(result), self.llm,
                budget=config.AGENT_TEAM_BUDGET)
            warning = verification.build_audit_warning(unverified)
            if unverified:
                self.memory.log(
                    "ceo",
                    f"自动核验发现 {len(unverified)} 个未匹配数字，已加注人工确认提示。",
                    type="warning")
        except Exception:
            warning = ""

        return banner + warning + prose

    def _completeness_banner(self, result):
        """A never-hidden, deterministic banner (prepended in Python) for stale / missing hub
        data + an unbalanced cash bridge — so the seller never trusts a stale number."""
        parts = []
        dc = result.get("data_completeness") or {}
        if dc.get("stale"):
            parts.append(f"对账数据可能已过期（数据截至 {dc.get('settlements_through') or '未知'}），"
                         "请先更新回款核对。")
        for m in (dc.get("missing") or []):
            parts.append(m)
        bridge = result.get("bridge") or {}
        if not bridge.get("ok", True):
            parts.append(f"现金桥式自检未平（差 ¥{bridge.get('imbalance', 0):,.2f}），请人工复核。")
        if not parts:
            return ""
        return "## ⚠️ 数据完整度提示\n\n" + "\n".join(f"- {p}" for p in parts) + "\n\n"

    def _writer_prompt(self, result):
        s = result.get("summary", {})
        facts = "\n".join(result.get("facts_block") or [])
        dc = result.get("data_completeness", {})
        a = result.get("assumptions", {})
        lags = a.get("lags", {})
        crunch = result.get("crunch")
        crunch_line = (
            f"最早现金紧张：{crunch['date']}，缺口 ¥{crunch['shortfall']:,.2f}"
            if crunch else f"未来 {a.get('horizon_weeks')} 周内未跌破现金缓冲")
        missing = "；".join(dc.get("missing") or []) or "（无）"
        return (
            f"数据截至：{dc.get('settlements_through') or '（未指定）'}\n"
            f"现金缓冲：¥{s.get('现金缓冲', 0):,.2f}；汇率假设 USD→CNY {a.get('fx_rate')}；"
            f"时滞假设(天)：到账 T+{lags.get('settlement_lag_days')}、"
            f"准备金释放 {lags.get('reserve_release_days')}、退税 {lags.get('rebate_lag_days')}\n\n"
            f"{CASH_HEADER}\n{facts}\n\n"
            f"可动用 ¥{s.get('可动用现金', 0):,.2f}；锁定/在途 ¥{s.get('锁定在途合计', 0):,.2f}；"
            f"预计退税 ¥{s.get('预计退税', 0):,.2f}\n{crunch_line}\n"
            f"数据完整度提示：{missing}\n\n"
            "请据此撰写《现金位置与资金跑道》。务必逐字引用上面的数字与【N】锚点，不要自行计算或改写任何数字；"
            "明确区分『已承诺/在途』与『预测』，呈现数据完整度，且仅作决策支持——不提供财务/税务建议、不代为操作。"
        )

    def _fallback_body(self, result):
        """A fully-deterministic report assembled from the engine facts — NO LLM."""
        s = result.get("summary", {})
        lines = ["# 现金位置与资金跑道", "", "## 核心结论", ""]
        lines += [f"- {f}" for f in (result.get("facts_block") or [])]
        lines += ["", f"可动用现金 ¥{s.get('可动用现金', 0):,.2f}，锁定/在途 "
                  f"¥{s.get('锁定在途合计', 0):,.2f}（卖家常高估可动用现金）。"]
        lines += ["", "> 本报告为决策支持，非财务/税务/投资建议；系统不代为下单、结汇、付款或融资。"]
        return "\n".join(lines)

    def _known_numbers(self, result):
        """Every figure the engine produced, in a few string forms, so the auditor treats them
        as backed (matches the writer's likely renderings)."""
        nums = set()
        vals = []
        for blk in (result.get("summary"), result.get("bridge")):
            vals += [v for v in (blk or {}).values() if isinstance(v, (int, float))]
        pos = result.get("position") or {}
        vals.append(pos.get("available_now"))
        for k in ("reserve", "in_transit", "unsettled_fx", "total"):
            vals.append((pos.get("locked_now") or {}).get(k))
        for f in (result.get("runway") or {}).get("pending_arrivals") or []:
            vals.append(f.get("amount"))
        crunch = result.get("crunch") or {}
        vals += [crunch.get("shortfall"), crunch.get("available")]
        for v in vals:
            if v is None:
                continue
            try:
                nums.add(f"{float(v):,.2f}")
                nums.add(f"{float(v):.2f}")
                nums.add(str(v))
            except (TypeError, ValueError):
                continue
        return nums

    # ----------------------------------------------------------------------
    def _persist(self, result):
        if not (self.company_id and self.run_id and result):
            return
        try:
            metrics_store.save_cash_report(
                self.company_id, self.run_id, result, account_id=self.account_id,
                period_label=self.period_label
                or (result.get("summary") or {}).get("period_label"))
        except Exception as e:
            self.memory.log("ceo", f"现金位置结果保存时出现问题（{e}）。", type="info")

    def _finish_run(self, status, result=None):
        workflow_chassis.finish_workflow_run(
            self.memory, self.company_id, self.run_id, status,
            cost_yuan=self._run_cost, report_title="现金位置与资金跑道",
            period_label=self.period_label, period_prefix="数据截至：")

    def _handle_cutoff(self, err):
        """Budget ran out during the writer call: assemble a deterministic report from the engine
        result with NO further API call (the numbers already exist)."""
        self.memory.log(
            "ceo",
            f"预算已达上限（{err}）。改用系统核算结果直接生成现金位置摘要，不再调用模型。",
            type="warning")
        result = self._result
        body = (self._completeness_banner(result) + self._fallback_body(result)) if result \
            else "_现金位置未能完成。_"
        if result:
            self.memory.set_sources(result.get("source_rows") or [], "ok")
        self.memory.set_final_report(body)
        if result:
            self._persist(result)
        self.memory.log("ceo", "已生成现金位置摘要（因预算提前结束）。", type="success")

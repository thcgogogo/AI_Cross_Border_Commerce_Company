"""
settlement_orchestrator.py — The 对账 / 平台结算核对 run driver.

A deliberately LINEAR sibling of orchestrator.Orchestrator (it copies the cost /
billing / lifecycle wiring but is NOT a subclass and shares none of the report
team's PLAN→DELEGATE→SYNTHESIZE machinery). 对账 is far more linear than the 经营
周报, and the engine already produces every number, so the flow is:

  1. RECONCILE   the deterministic settlement_engine matches + attributes (NO LLM).
  2. EXPLAIN     ONE writer LLM call turns the engine's pre-computed facts into a
                 one-page 对账报告 it must quote verbatim (it can only word prose).
  3. SELF-AUDIT  the engine's bridge-balance flag is prepended deterministically;
                 verification.audit_report flags any number the writer introduced.
  4. FINISH      record the run + ¥ cost, persist the result + worklist, render the PDF.

Everything streams to shared memory (the live "watch the team work" feed). Every
figure is the engine's; the LLM never computes, matches, or alters a number — that is
the entire reliability story, identical to the weekly report's trends.py moat.
"""

from . import (config, metrics_store, settlement_engine, settlement_templates,
               verification, workflow_chassis)
from .models import RUN_FAILED, RUN_STOPPED_BUDGET, RUN_SUCCEEDED
from .providers import ModelRouter
from .token_manager import BudgetExceededError

# Verbatim header the engine's pre-computed facts are injected under — same contract
# as orchestrator.TRENDS_HEADER: the writer must use these numbers as-is.
RECON_HEADER = (
    "=== 系统核对结果 (PRE-COMPUTED — these numbers are exact, you MUST use them "
    "verbatim with their 【N】 anchors and MUST NOT recompute or alter them) ==="
)


class SettlementOrchestrator:
    def __init__(self, memory, files_by_role, *, platform_key=None, period_label=None,
                 llm=None, token_manager=None, cost_listener=None,
                 company_id=None, run_id=None, account_id=None):
        self.memory = memory
        self.files_by_role = files_by_role or {}
        self.platform_key = platform_key
        self.period_label = period_label
        self.token_manager = token_manager
        self.company_id = company_id
        self.run_id = run_id
        self.account_id = account_id
        self._run_cost = 0.0
        self._result = None
        self._pack = None  # the resolved+overridden pack, cached by _reconcile

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
            self.memory.log("ceo", "对账团队已就位，开始核对本期结算。", type="thinking")
            result = self._reconcile()                 # deterministic engine
            final = self._write_report(result)         # one LLM call + self-audit
            self.memory.set_sources(result.get("source_rows") or [], "ok")
            self.memory.set_final_report(final)
            self._persist(result)
            self.memory.log("ceo", "对账报告已生成。", type="success")
            self._finish_run(RUN_SUCCEEDED, result)
        except BudgetExceededError as e:
            self._handle_cutoff(e)
            self._finish_run(RUN_STOPPED_BUDGET, self._result)
        except ValueError as e:
            # A customer-facing problem (unreadable / unrecognized upload).
            self.memory.set_error(str(e))
            self.memory.log("ceo", f"对账未能完成：{e}", type="error")
            self._finish_run(RUN_FAILED, None)
        except Exception as e:
            self.memory.set_error(f"{type(e).__name__}: {e}")
            self.memory.log("ceo", f"对账中断：{e}", type="error")
            self._finish_run(RUN_FAILED, None)
            raise

    # ----------------------------------------------------------------------
    def _reconcile(self):
        """Load the platform pack (+ this account's overrides + learned rules) and run
        the deterministic engine. Logs friendly progress to the live feed."""
        pack = settlement_templates.load_pack(self.platform_key)
        key = pack.get("_key")
        if self.company_id:
            try:
                pack = settlement_templates.apply_overrides(
                    pack, metrics_store.platform_overrides(self.company_id, key))
            except Exception:
                pass
        self._pack = pack  # cache so _writer_prompt reuses it (no 2nd disk read)
        learned = []
        if self.company_id:
            try:
                learned = metrics_store.learned_recon_rules(self.company_id)
            except Exception:
                learned = []

        self.memory.log("ceo", "资料管理员正在归一化平台结算单、银行流水与账本…", type="info")
        self.memory.log("ceo", "对账引擎正在按『订单 → 结算 → 银行』多层匹配并归因差异…",
                        type="thinking")
        result = settlement_engine.run_settlement(
            self.files_by_role, pack, learned_rules=learned,
            period_label=self.period_label)
        self._result = result
        s = result.get("summary", {})
        b = result.get("buckets", {}).get("L1", {})
        self.memory.log(
            "ceo",
            f"匹配完成：订单层已匹配 {b.get('已匹配', 0)} 笔；"
            f"应收 ¥{s.get('应收', 0):,.2f}，到账 ¥{s.get('到账', 0):,.2f}，"
            f"未解释差异 ¥{s.get('未解释差异', 0):,.2f}（{s.get('未解释笔数', 0)} 笔）。",
            type="info")
        if not (result.get("bridge", {}).get("ok", True)):
            self.memory.log("ceo", "注意：桥式核对未平，将在报告中提示人工复核。",
                            type="warning")
        return result

    # ----------------------------------------------------------------------
    def _write_report(self, result):
        """One writer LLM call over the engine's pre-computed facts, then the
        deterministic bridge banner + the verification number-audit. Falls back to a
        fully-deterministic report if the writer call fails for any non-budget reason
        (a budget cut propagates to _handle_cutoff)."""
        cfg = config.settlement_writer()
        user = self._writer_prompt(result)
        try:
            prose = self.llm.complete(
                system_prompt=cfg["system_prompt"],
                user_content=user,
                model=config.settlement_writer_model(),
                effort=cfg.get("effort", "low"),
                max_tokens=cfg.get("max_tokens", 4000),
                budget=config.AGENT_TEAM_BUDGET,
            )
        except BudgetExceededError:
            raise
        except Exception as e:
            self.memory.log("ceo", f"报告撰写改用系统结果直接生成（{e}）。", type="info")
            prose = self._fallback_body(result)

        # Deterministic, never-hidden bridge-balance banner (prepended in Python).
        bridge = result.get("bridge", {})
        banner = ""
        if not bridge.get("ok", True):
            banner = (f"## ⚠️ 桥式核对未平\n\n本期『应收 − 优惠』与平台应结金额相差 "
                      f"¥{bridge.get('imbalance', 0):,.2f}，结算单内部金额不一致，请人工复核。\n\n")

        # Verification number-audit: flag any figure the writer introduced that the
        # engine didn't produce (best-effort; reuses the report-flow auditor).
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

    def _writer_prompt(self, result):
        s = result.get("summary", {})
        facts = "\n".join(result.get("facts_block") or [])
        attrs = "\n".join(f"- {a['human_cn']}" for a in (result.get("attributions") or [])
                          if a.get("amount"))
        excs = result.get("exceptions") or []
        n_unexpl = s.get("未解释笔数", 0)
        platform = ((self._pack or {}).get("display_name_cn")
                    or self.platform_key or "结算平台")
        return (
            f"结算平台：{platform}\n对账期间：{self.period_label or '（未指定）'}\n\n"
            f"{RECON_HEADER}\n{facts}\n\n"
            f"已解释的差异归因：\n{attrs or '（无）'}\n\n"
            f"异常清单：共 {len(excs)} 条，其中未解释 {n_unexpl} 条需人工核查。\n\n"
            "请据此撰写《对账报告》。务必逐字引用上面的数字与【N】锚点，不要自行计算或改写任何数字。"
        )

    def _fallback_body(self, result):
        """A fully-deterministic report assembled from the engine facts — NO LLM.
        Used when the writer call fails (so a model hiccup never loses the report)."""
        s = result.get("summary", {})
        lines = ["# 对账报告", "", "## 核心结论", ""]
        lines += [f"- {f}" for f in (result.get("facts_block") or [])]
        attrs = [a["human_cn"] for a in (result.get("attributions") or []) if a.get("amount")]
        if attrs:
            lines += ["", "## 差异归因", ""] + [f"- {a}" for a in attrs]
        lines += ["", f"未解释差异 ¥{s.get('未解释差异', 0):,.2f}"
                  f"（{s.get('未解释笔数', 0)} 笔），请在系统异常清单中逐条核对。"]
        return "\n".join(lines)

    def _known_numbers(self, result):
        """Every figure the engine produced, in a few string forms, so the auditor
        treats them as backed (matches the writer's likely renderings)."""
        nums = set()
        s = result.get("summary", {})
        vals = [v for v in s.values() if isinstance(v, (int, float))]
        vals += [a.get("amount") for a in (result.get("attributions") or [])]
        b = result.get("bridge", {})
        vals += [v for v in b.values() if isinstance(v, (int, float))]
        for v in vals:
            if v is None:
                continue
            nums.add(f"{v:,.2f}")
            nums.add(f"{v:.2f}")
            nums.add(str(v))
        return nums

    # ----------------------------------------------------------------------
    def _persist(self, result):
        if not (self.company_id and self.run_id and result):
            return
        try:
            metrics_store.save_settlement_report(
                self.company_id, self.run_id, result, account_id=self.account_id,
                platform=(result.get("summary") or {}).get("platform"),
                period_label=self.period_label)
        except Exception as e:
            self.memory.log("ceo", f"对账结果保存时出现问题（{e}）。", type="info")

    def _finish_run(self, status, result=None):
        # record the run + render the branded PDF (the shared linear-workflow finish tail).
        workflow_chassis.finish_workflow_run(
            self.memory, self.company_id, self.run_id, status,
            cost_yuan=self._run_cost, report_title="对账报告",
            period_label=self.period_label, period_prefix="对账期间：")

    def _handle_cutoff(self, err):
        """Budget ran out during the writer call: assemble a deterministic report from
        the engine result with NO further API call (the numbers already exist)."""
        self.memory.log(
            "ceo",
            f"预算已达上限（{err}）。改用系统核对结果直接生成对账摘要，不再调用模型。",
            type="warning")
        result = self._result
        body = self._fallback_body(result) if result else "_对账未能完成。_"
        if result:
            self.memory.set_sources(result.get("source_rows") or [], "ok")
        self.memory.set_final_report(body)
        if result:
            self._persist(result)
        self.memory.log("ceo", "已生成对账摘要（因预算提前结束）。", type="success")

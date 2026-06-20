"""
ar_orchestrator.py — The 应收账款管理 + 催收 run driver.

A deliberately LINEAR sibling of settlement_orchestrator (it copies the cost / billing /
lifecycle wiring but is NOT a subclass and shares none of the report team's machinery).
AR is linear like 对账, and the engine already produces every number, so the flow is:

  1. COMPUTE   the deterministic ar_aging_engine applies payments + ages + grades (NO LLM).
  2. EXPLAIN   ONE writer LLM call turns the engine's pre-computed facts into a one-page
               《应收账款账龄报告》 it must quote verbatim (it can only word prose).
  3. SELF-AUDIT  the engine's bridge flag is prepended deterministically;
               verification.audit_report flags any number the writer introduced.
  4. PERSIST + FINISH  save the result + flag worklist, record cost, render the PDF.
  5. 催收 DRAFTS (best-effort tail)  one LLM call per NEW/ESCALATED overdue account drafts
               a dunning message with engine numbers INJECTED — never auto-sent. Budget
               cuts stop drafting gracefully; the aging report is the primary deliverable.

Every figure is the engine's; the LLM never computes, decides overdue, or alters a
number — the entire reliability story, identical to 对账 / the weekly report's moat.
"""

from datetime import datetime

from . import (ar_aging_engine, ar_templates, config, dunning, metrics_store,
               verification, workflow_chassis)
from .models import (DUNNING_TIER_HANDOFF, RUN_FAILED, RUN_STOPPED_BUDGET,
                     RUN_SUCCEEDED)
from .providers import ModelRouter
from .token_manager import BudgetExceededError

# Verbatim header the aging facts are injected under (same contract as RECON_HEADER).
AR_HEADER = (
    "=== 系统应收核算结果 (PRE-COMPUTED — these numbers are exact, you MUST use them "
    "verbatim with their 【N】 anchors and MUST NOT recompute or alter them) ==="
)

# Verbatim header the per-account 催收 facts are injected under.
DUNNING_HEADER = (
    "=== 催收事实数据 (PRE-COMPUTED — 以下金额、到期日、发票号、逾期天数均由系统计算，"
    "你必须逐字引用并保留【N】锚点，不得改写、四舍五入或新增任何数字) ==="
)


class AROrchestrator:
    def __init__(self, memory, files_by_role, *, format_key=None, period_label=None,
                 llm=None, token_manager=None, cost_listener=None,
                 company_id=None, run_id=None, account_id=None):
        self.memory = memory
        self.files_by_role = files_by_role or {}
        self.format_key = format_key
        self.period_label = period_label
        self.token_manager = token_manager
        self.company_id = company_id
        self.run_id = run_id
        self.account_id = account_id
        self._run_cost = 0.0
        self._result = None
        self._pack = None

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
            self.memory.log("ceo", "应收账款团队已就位，开始核算本期应收与账龄。",
                            type="thinking")
            result = self._compute()                    # deterministic engine
            final = self._write_report(result)          # one LLM call + self-audit
            self.memory.set_sources(result.get("source_rows") or [], "ok")
            self.memory.set_final_report(final)
            self._persist(result)
            self.memory.log("ceo", "应收账款账龄报告已生成。", type="success")
            self._draft_dunning(result)                 # best-effort 催收 tail (no send)
            self._finish_run(RUN_SUCCEEDED, result)
        except BudgetExceededError as e:
            self._handle_cutoff(e)
            self._finish_run(RUN_STOPPED_BUDGET, self._result)
        except ValueError as e:
            self.memory.set_error(str(e))
            self.memory.log("ceo", f"应收核算未能完成：{e}", type="error")
            self._finish_run(RUN_FAILED, None)
        except Exception as e:
            self.memory.set_error(f"{type(e).__name__}: {e}")
            self.memory.log("ceo", f"应收核算中断：{e}", type="error")
            self._finish_run(RUN_FAILED, None)
            raise

    # ----------------------------------------------------------------------
    def _compute(self):
        """Load the AR pack (+ this account's overrides + learned rules), read the
        reconciled 实收 cross-check from the brain when available, and run the engine."""
        pack = ar_templates.load_pack(self.format_key)
        key = pack.get("_key")
        if self.company_id:
            try:
                pack = ar_templates.apply_overrides(
                    pack, metrics_store.ar_format_overrides(self.company_id, key))
            except Exception:
                pass
        self._pack = pack
        learned, recon_totals = [], None
        if self.company_id:
            try:
                learned = metrics_store.learned_ar_rules(self.company_id)
            except Exception:
                learned = []
            try:
                recon_totals = metrics_store.read_reconciled_received(
                    self.company_id, self.period_label)
            except Exception:
                recon_totals = None

        self.memory.log("ceo", "资料管理员正在归一化发票、回款与客户主数据…", type="info")
        if recon_totals:
            self.memory.log("ceo", "已读取本期对账『到账』结果，将与应收回款交叉核对。",
                            type="info")
        self.memory.log("ceo", "应收引擎正在核销回款、计算账龄并分级…", type="thinking")
        result = ar_aging_engine.run_ar_aging(
            self.files_by_role, pack, recon_totals=recon_totals, learned_rules=learned,
            period_label=self.period_label)
        self._result = result
        s = result.get("summary", {})
        self.memory.log(
            "ceo",
            f"核算完成：应收账款总额 ¥{s.get('应收账款总额', 0):,.2f}，"
            f"逾期 ¥{s.get('逾期总额', 0):,.2f}（{s.get('逾期发票数', 0)} 张），"
            f"待人工核查 {s.get('疑似条目数', 0)} 项。",
            type="info")
        if not (result.get("bridge", {}).get("ok", True)):
            self.memory.log("ceo", "注意：应收核算未平，将在报告中提示人工复核。",
                            type="warning")
        return result

    # ----------------------------------------------------------------------
    def _write_report(self, result):
        cfg = config.ar_writer()
        user = self._writer_prompt(result)
        try:
            prose = self.llm.complete(
                system_prompt=cfg["system_prompt"], user_content=user,
                model=config.ar_writer_model(), effort=cfg.get("effort", "low"),
                max_tokens=cfg.get("max_tokens", 4000), budget=config.AGENT_TEAM_BUDGET)
        except BudgetExceededError:
            raise
        except Exception as e:
            self.memory.log("ceo", f"报告撰写改用系统结果直接生成（{e}）。", type="info")
            prose = self._fallback_body(result)

        bridge = result.get("bridge", {})
        banner = ""
        if not bridge.get("ok", True):
            banner = (f"## ⚠️ 应收核算未平\n\n本期『应收账款总额』与『已核销 + 未结』相差 "
                      f"¥{bridge.get('imbalance', 0):,.2f}，请人工复核数据源。\n\n")

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
                          if a.get("count"))
        fmt = ((self._pack or {}).get("display_name_cn") or self.format_key or "应收台账")
        return (
            f"应收格式：{fmt}\n核算期间：{self.period_label or '（未指定）'}\n\n"
            f"{AR_HEADER}\n{facts}\n\n"
            f"账龄分布：\n{attrs or '（无逾期）'}\n\n"
            f"待人工核查（疑似）：{s.get('疑似条目数', 0)} 项。\n\n"
            "请据此撰写《应收账款账龄报告》。务必逐字引用上面的数字与【N】锚点，"
            "不要自行计算或改写任何数字，也不要更改是否逾期的判定。"
        )

    def _fallback_body(self, result):
        """A fully-deterministic report assembled from the engine facts — NO LLM."""
        s = result.get("summary", {})
        lines = ["# 应收账款账龄报告", "", "## 核心结论", ""]
        lines += [f"- {f}" for f in (result.get("facts_block") or [])]
        attrs = [a["human_cn"] for a in (result.get("attributions") or []) if a.get("count")]
        if attrs:
            lines += ["", "## 账龄分布", ""] + [f"- {a}" for a in attrs]
        lines += ["", f"待人工核查（疑似）{s.get('疑似条目数', 0)} 项，请在系统中逐条确认后再催收。"]
        return "\n".join(lines)

    def _known_numbers(self, result):
        nums = set()
        s = result.get("summary", {})
        vals = [v for v in s.values() if isinstance(v, (int, float))]
        vals += [a.get("amount") for a in (result.get("attributions") or [])]
        vals += [a.get("count") for a in (result.get("attributions") or [])]
        b = result.get("bridge", {})
        vals += [v for v in b.values() if isinstance(v, (int, float))]
        vals += [c.get("outstanding") for c in (result.get("customers") or [])]
        for v in vals:
            if v is None:
                continue
            nums.add(f"{v:,.2f}")
            nums.add(f"{v:.2f}")
            nums.add(str(v))
        return nums

    # ----------------------------------------------------------------------
    # 催收 drafting (best-effort tail; NEVER sends — human approve-then-send only)
    # ----------------------------------------------------------------------
    def _draft_dunning(self, result):
        if not (self.company_id and self.run_id):
            return
        pack = self._pack or {}
        now = datetime.utcnow()
        # group overdue invoices by customer
        overdue_by_cust = {}
        for inv in result.get("invoices") or []:
            if inv.get("overdue"):
                overdue_by_cust.setdefault(inv.get("customer_id"), []).append(inv)
        drafted = handoff = locked = 0
        try:
            for cust in result.get("customers") or []:
                cid = cust.get("customer_id")
                invs = overdue_by_cust.get(cid) or []
                if not invs:
                    continue
                max_dpd, overdue_total, _n = dunning.summarize_customer(invs)
                last = metrics_store.last_dunning(self.company_id, cid)
                decision = dunning.assign_tier(
                    max_dpd, overdue_total, (last or {}).get("tier"),
                    cust.get("grade"), pack)
                outcome = dunning.classify_outcome(decision, last, pack, now=now)

                if outcome["outcome"] == dunning.OUTCOME_HANDOFF:
                    metrics_store.record_dunning_draft(
                        self.company_id, cid, DUNNING_TIER_HANDOFF, run_id=self.run_id,
                        account_id=self.account_id, grade=cust.get("grade"),
                        draft_body="", days_overdue=max_dpd, amount=overdue_total,
                        facts=[{"note": outcome["reason_cn"]}])
                    handoff += 1
                    continue
                if not outcome["should_draft"]:
                    locked += 1
                    continue

                facts = dunning.build_account_facts(cust, invs, outcome["tier"])
                body, audit_warning = self._write_dunning_draft(facts, pack)
                metrics_store.record_dunning_draft(
                    self.company_id, cid, outcome["tier"], run_id=self.run_id,
                    account_id=self.account_id, grade=cust.get("grade"),
                    facts=facts["source_rows"], draft_body=body,
                    audit_warning=audit_warning, days_overdue=max_dpd,
                    amount=overdue_total)
                drafted += 1
        except BudgetExceededError as e:
            self.memory.log("ceo", f"催收草稿生成因预算提前结束（{e}）。已生成的草稿仍可发送。",
                            type="warning")
        self.memory.log(
            "ceo",
            f"已生成 {drafted} 封催收草稿、{handoff} 项转人工/法务，"
            f"{locked} 个客户在催收间隔内暂不重复。所有草稿均需人工确认后才会发送。",
            type="success")

    def _write_dunning_draft(self, facts, pack):
        """One LLM call wording a 催收 draft over injected engine numbers, then a
        number-audit. Returns (body, reviewer_only_audit_warning). Raises
        BudgetExceededError up to _draft_dunning (which stops the loop)."""
        cfg = config.dunning_writer(facts["tier"])
        cap = ar_templates.dunning_config(pack)["length_caps"].get("email", 400)
        user = (
            f"催收对象：{facts['customer_name']}\n催收级别：{facts['tier_cn']}\n\n"
            f"{DUNNING_HEADER}\n" + "\n".join(facts["facts_block"]) + "\n\n"
            f"请撰写一封发给该客户的催款消息，控制在约 {cap} 字以内。"
            "务必逐字引用上面的金额/到期日/发票号与【N】锚点，不要新增或改写任何数字。"
        )
        try:
            body = self.llm.complete(
                system_prompt=cfg["system_prompt"], user_content=user,
                model=config.dunning_writer_model(), effort=cfg.get("effort", "low"),
                max_tokens=cfg.get("max_tokens", 800), budget=config.AGENT_TEAM_BUDGET)
        except BudgetExceededError:
            raise
        except Exception:
            body = self._fallback_draft(facts)
        warning = ""
        try:
            unverified = verification.audit_report(
                body, set(facts.get("known_numbers") or []), self.llm,
                budget=config.AGENT_TEAM_BUDGET)
            warning = verification.build_audit_warning(unverified)
        except BudgetExceededError:
            raise
        except Exception:
            warning = ""
        return body, warning

    def _fallback_draft(self, facts):
        """Deterministic 催收 draft from the injected facts — NO LLM."""
        lines = [f"尊敬的 {facts['customer_name']}：", "",
                 "贵司以下应收账款已逾期，烦请尽快安排付款："]
        lines += [f"- {f}" for f in facts["facts_block"]]
        lines += ["", "如已付款，请忽略本提醒。感谢配合。", "", "此致"]
        return "\n".join(lines)

    # ----------------------------------------------------------------------
    def _persist(self, result):
        if not (self.company_id and self.run_id and result):
            return
        try:
            metrics_store.save_ar_report(
                self.company_id, self.run_id, result, account_id=self.account_id,
                format_key=(self._pack or {}).get("_key"),
                period_label=self.period_label)
        except Exception as e:
            self.memory.log("ceo", f"应收结果保存时出现问题（{e}）。", type="info")

    def _finish_run(self, status, result=None):
        # record the run + render the branded PDF (the shared linear-workflow finish tail).
        workflow_chassis.finish_workflow_run(
            self.memory, self.company_id, self.run_id, status,
            cost_yuan=self._run_cost, report_title="应收账款账龄报告",
            period_label=self.period_label, period_prefix="核算期间：")

    def _handle_cutoff(self, err):
        self.memory.log(
            "ceo",
            f"预算已达上限（{err}）。改用系统核算结果直接生成应收摘要，不再调用模型。",
            type="warning")
        result = self._result
        body = self._fallback_body(result) if result else "_应收核算未能完成。_"
        if result:
            self.memory.set_sources(result.get("source_rows") or [], "ok")
        self.memory.set_final_report(body)
        if result:
            self._persist(result)
        self.memory.log("ceo", "已生成应收摘要（因预算提前结束）。", type="success")

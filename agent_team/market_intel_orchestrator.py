"""
market_intel_orchestrator.py — the per-customer 跨境市场情报日报 run driver (PHASE B).

A LINEAR sibling of settlement_orchestrator (copies the cost/billing/lifecycle wiring; NOT
an Orchestrator subclass, shares none of the report team's PLAN→DELEGATE machinery). The
EXPENSIVE web search already happened ONCE in market_intel_engine.collect_daily_intel; this
driver only slices the shared pool to ONE customer and words the prose:

  1. PERSONALIZE  market_intel_engine.personalize_for_watchlist filters the recency-gated
                  pool to this watchlist, assigns 【N】 anchors, and builds the masthead +
                  the date-sorted 截止日期 table + the 数据来源 table (all deterministic, NO LLM).
  2. WRITE        ONE writer LLM call returns prose for 今日要点/政策/成本/需求/竞争态势 (JSON), which
                  Python drops into a deterministic 8-section skeleton. A complete
                  deterministic fallback is used if the call fails / returns bad JSON.
  3. AUDIT        a deterministic 【N】 anchor-integrity check (Python): any anchor the writer
                  emitted that the engine didn't produce is flagged via build_audit_warning.
  4. FINISH       persist the report + record the run + render the branded PDF (shared tail).

Python owns the clock, the recency filter, the tables, and the anchor set; the LLM only words
prose around dated, anchored items it is handed. Nothing here touches the shared engine modules.
"""
import datetime
import json
import re

from . import (config, market_intel_engine, metrics_store, verification,
               workflow_chassis)

_ANCHOR_NUM_RE = re.compile(r"【\s*(\d+)\s*】")   # captures the integer inside 【N】
from .models import RUN_FAILED, RUN_STOPPED_BUDGET, RUN_SUCCEEDED
from .providers import ModelRouter
from .token_manager import BudgetExceededError

_NARRATIVE_KEYS = ("highlights", "policy", "cost", "demand", "competition")


class MarketIntelOrchestrator:
    def __init__(self, memory, pool, watchlist, *, company_name=None, period_label=None,
                 llm=None, token_manager=None, cost_listener=None,
                 company_id=None, run_id=None, account_id=None):
        self.memory = memory
        self.pool = pool or {}
        self.watchlist = watchlist or {}
        self.company_name = company_name or ""
        self.period_label = period_label or (pool or {}).get("period_label")
        self.token_manager = token_manager
        self.company_id = company_id
        self.run_id = run_id
        self.account_id = account_id
        self._run_cost = 0.0
        self._personalized = None

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
            self.memory.log("ceo", "市场情报团队已就位，正在为本店生成跨境市场情报日报。",
                            type="thinking")
            p = self._personalize()                      # deterministic slice + anchors
            final = self._write_report(p)                # one LLM call + assemble + audit
            # Section 8 lives in the report BODY (a markdown table) — set EMPTY structured
            # sources so the shared PDF renderer skips its own table (no double / no banner).
            self.memory.set_sources([], "ok")
            self.memory.set_final_report(final)
            self._persist(p, final)
            self.memory.log("ceo", "跨境市场情报日报已生成。", type="success")
            self._finish_run(RUN_SUCCEEDED)
        except BudgetExceededError as e:
            self._handle_cutoff(e)
            self._finish_run(RUN_STOPPED_BUDGET)
        except ValueError as e:
            self.memory.set_error(str(e))
            self.memory.log("ceo", f"跨境市场情报日报未能完成：{e}", type="error")
            self._finish_run(RUN_FAILED)
        except Exception as e:
            self.memory.set_error(f"{type(e).__name__}: {e}")
            self.memory.log("ceo", f"跨境市场情报日报中断：{e}", type="error")
            self._finish_run(RUN_FAILED)
            raise

    # ----------------------------------------------------------------------
    def _personalize(self):
        # Anchor the masthead coverage + the 截止日期 window to WHEN the pool was collected
        # (not the run's wall-clock), so an on-demand run mid-week stays consistent with the
        # week the pool actually covers.
        as_of = None
        try:
            as_of = datetime.date.fromisoformat((self.pool or {}).get("collected_at") or "")
        except ValueError:
            as_of = None
        p = market_intel_engine.personalize_for_watchlist(
            self.pool, self.watchlist, self.company_name, today=as_of)
        self._personalized = p
        self.memory.log(
            "ceo",
            f"已按本店关注列表筛选出 {p['kept_count']} 条情报，"
            f"其中 {p['deadline_count']} 条带时间节点。",
            type="info")
        return p

    # ----------------------------------------------------------------------
    def _write_report(self, p):
        """One writer call for the four narrative sections (JSON), assembled into the
        fixed 8-section skeleton with the deterministic tables. Falls back to the
        engine's deterministic prose if the call fails (non-budget) or returns bad JSON."""
        det = p.get("det_sections") or {}
        narrative = dict(det)   # start from the deterministic baseline
        try:
            raw = self.llm.complete(
                system_prompt=config.market_intel_writer()["system_prompt"],
                user_content=self._writer_prompt(p),
                model=config.market_intel_writer_model(),
                effort=config.market_intel_writer().get("effort", "low"),
                max_tokens=config.market_intel_writer().get("max_tokens", 6000),
                budget=config.AGENT_TEAM_BUDGET,
                web_search=False,   # collection already happened; never search at write time
            )
            parsed = _extract_json_object(raw)
            for k in _NARRATIVE_KEYS:
                v = (parsed.get(k) or "").strip() if isinstance(parsed, dict) else ""
                if v:
                    narrative[k] = v
        except BudgetExceededError:
            raise
        except Exception as e:
            self.memory.log("ceo", f"情报归纳改用系统结果直接生成（{e}）。", type="info")

        # --- Deterministic 【N】 anchor-integrity check (Python owns verification). Any
        #     anchor in the writer's prose that the engine didn't produce is flagged.
        anchor_set = p.get("anchor_set") or set()
        prose_blob = "\n".join(narrative.get(k, "") for k in _NARRATIVE_KEYS)
        used = {int(m) for m in _ANCHOR_NUM_RE.findall(prose_blob)}
        orphans = sorted(used - anchor_set)
        warning = ""
        if orphans:
            warning = verification.build_audit_warning(
                [f"【{a}】" for a in orphans])
            self.memory.log(
                "ceo", f"自动核验发现 {len(orphans)} 个未匹配溯源锚点，已加注人工确认提示。",
                type="warning")

        return warning + self._assemble(p, narrative)

    def _assemble(self, p, narrative):
        """Build the final 8-section report: deterministic framing + tables (Python) with
        the narrative prose dropped in. Section order matches the product spec (picture 1).
        The 竞争态势 narrative always carries the deterministic 竞品价格 note (from the engine)."""
        sec = dict(market_intel_engine._SECTION_TITLES)
        det = p.get("det_sections") or {}
        # 竞品价格 note must survive even if the LLM omitted it — append it when missing.
        competition = narrative.get("competition") or det.get("competition") or "无重大变动"
        note = market_intel_engine._COMPETITION_PRICE_NOTE
        if note not in competition:
            competition = competition + "\n\n_" + note + "_"
        parts = [
            f"# {sec['masthead']}", p["masthead_md"],
            f"\n## {sec['highlights']}", narrative.get("highlights") or "无重大变动",
            f"\n## {sec['deadlines']}", p["deadlines_md"],
            f"\n## {sec['policy']}", narrative.get("policy") or "无重大变动",
            f"\n## {sec['cost']}", narrative.get("cost") or "无重大变动",
            f"\n## {sec['demand']}", narrative.get("demand") or "无重大变动",
            f"\n## {sec['competition']}", competition,
            f"\n## {sec['sources']}", p["sources_md"],
        ]
        return "\n".join(parts)

    def _writer_prompt(self, p):
        return (
            f"本期覆盖范围：\n{p['masthead_md']}\n\n"
            "=== 系统采集结果 (PRE-COMPUTED, 已按发布日期核验时效 — 只能在这些条目内写作，"
            "逐字保留【N】锚点，不得新增或改写) ===\n"
            f"{p['facts_block']}\n\n"
            "请据此输出 JSON（键：highlights / policy / cost / demand / competition）。"
            "务必保留【N】锚点，不要引入任何上面没有的情报、日期或来源。")

    # ----------------------------------------------------------------------
    def _persist(self, p, final):
        if not (self.company_id and self.run_id):
            return
        payload = {
            "period_label": self.period_label,
            "masthead_md": p.get("masthead_md"),
            "source_rows": p.get("source_rows") or [],
            "highlight_count": p.get("highlight_count", 0),
            "deadline_count": p.get("deadline_count", 0),
            "kept_count": p.get("kept_count", 0),
            "report_markdown": final,
        }
        try:
            metrics_store.save_mi_report(
                self.company_id, self.run_id, payload,
                account_id=self.account_id, period_label=self.period_label)
        except Exception as e:
            self.memory.log("ceo", f"跨境市场情报日报保存时出现问题（{e}）。", type="info")

    def _finish_run(self, status):
        workflow_chassis.finish_workflow_run(
            self.memory, self.company_id, self.run_id, status,
            cost_yuan=self._run_cost, report_title="跨境市场情报日报",
            period_label=self.period_label, period_prefix="覆盖周期：")

    def _handle_cutoff(self, err):
        """Budget ran out during the writer call: assemble the fully-deterministic report
        from the personalized engine result — NO further API call (the data already exists)."""
        self.memory.log(
            "ceo",
            f"预算已达上限（{err}）。改用系统采集结果直接生成跨境市场情报日报，不再调用模型。",
            type="warning")
        p = self._personalized
        if p:
            body = self._assemble(p, dict(p.get("det_sections") or {}))
            self.memory.set_sources([], "ok")
            self.memory.set_final_report(body)
            self._persist(p, body)
        else:
            self.memory.set_final_report("_跨境市场情报日报未能完成。_")
        self.memory.log("ceo", "已生成跨境市场情报日报（因预算提前结束）。", type="success")


# ---------------------------------------------------------------------------
def _extract_json_object(text):
    """Tolerantly pull a JSON object out of a model reply (handles ```json fences / prose
    around it). Returns {} on failure — the caller then keeps the deterministic baseline."""
    if not text:
        return {}
    s = str(text).strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        obj = json.loads(s[start:end + 1])
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}

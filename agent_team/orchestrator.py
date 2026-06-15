"""
orchestrator.py — The CEO that runs the whole company.

The flow mirrors how a real team works:

  1. PLAN      The CEO reads the goal + the files and writes a delegation plan
               (which specialist does what, in what order).
  2. DELEGATE  Each specialist runs in turn, reading shared memory so it can
               build on whatever teammates produced before it.
  3. SYNTHESIZE The CEO assembles everyone's work into one final, polished
               deliverable for the user.

Everything is written to shared memory as it happens, which is what the
website's live "watch the team work" feed reads from.
"""

import json
import os
import re
from datetime import datetime

from . import config, crypto, industry_packs, metrics_store, trends, verification
from .base_agent import Agent
from .models import RUN_FAILED, RUN_STOPPED_BUDGET, RUN_SUCCEEDED
from .providers import ModelRouter
from .token_manager import BudgetExceededError

# Verbatim header the pre-computed trends/anomalies are injected under, so the
# Report Writer treats these numbers as ground truth it must not recompute.
TRENDS_HEADER = (
    "=== 系统计算的趋势与异常 (PRE-COMPUTED — these numbers are exact, you MUST "
    "use them verbatim and MUST NOT recompute or alter them) ==="
)


class Orchestrator:
    def __init__(self, memory, llm=None, token_manager=None, cost_listener=None,
                 company_id=None, run_id=None, industry_key=None):
        self.memory = memory
        self.token_manager = token_manager
        # company_id / run_id tie this job to the persistent "company brain" so
        # agents can read what previous runs learned and we can record this run.
        self.company_id = company_id
        self.run_id = run_id
        # The industry value for this run (a pack key, a profile label, or empty).
        # app.py resolves the per-run override vs. the company profile and passes
        # the winner here; load_pack() maps it to a pack (unknown/empty -> generic).
        self.industry_key = industry_key
        self._run_cost = 0.0  # ¥ tallied across every model call, for finish_run

        # Wrap the biller so we ALSO accumulate this run's ¥ cost. The wrapper
        # still calls (and re-raises from) the real biller, so a spent balance
        # cuts the run off via BudgetExceededError exactly as before.
        def _tally(model, input_tokens, output_tokens):
            try:
                self._run_cost += config.cost_cny(model, input_tokens, output_tokens)
            except Exception:
                pass
            if cost_listener:
                cost_listener(model, input_tokens, output_tokens)

        # ModelRouter is a drop-in replacement for LLMClient that can reach EVERY
        # provider in the catalog (Claude natively; OpenAI/Gemini/Grok/DeepSeek/
        # Qwen/MiniMax/Kimi via one OpenAI-compatible adapter). Every Agent shares
        # this one instance, so an agent assigned to any model "just works".
        # cost_listener (optional) bills each call to the logged-in account.
        if llm is not None:
            # A router injected by the caller (e.g. the desktop daemon's RemoteRouter,
            # which proxies inference through the cloud /v1 gateway). Route ITS cost
            # through this run's wrappers too, so the ¥ tally (for finish_run) and the
            # per-account biller still fire exactly as on the default path.
            self.llm = llm
            self.llm.cost_listener = _tally
            if token_manager is not None:
                self.llm.token_manager = token_manager
        else:
            self.llm = ModelRouter(token_manager=token_manager, cost_listener=_tally)

        # Surface the persistent-brain id + DB run id on the run so the snapshot/UI
        # can use them (the result page attaches per-section feedback to run_id).
        self.memory.set_company_id(company_id)
        self.memory.set_run_id(run_id)

        # Pull this company's long-term memory (best-effort) and inject it into
        # the CEO and Report Writer so they compare current data against the past.
        memory_block = self._load_company_memory()

        # The company's KPI column-mappings (Phase 3) are injected into the Data
        # Analyst so it maps messy/aliased spreadsheet headers to the right KPIs
        # BEFORE analyzing (best-effort; "" when the company defined no mappings).
        kpi_block = self._load_kpi_mapping_block()

        # The industry pack (Phase 4) tailors this run to a vertical. Read FRESH
        # from disk every run so founders can hot-edit pack YAML between runs (no
        # restart). Best-effort: a malformed/unknown pack degrades to nothing and
        # never blocks a run — the loud gate is validate_all_packs() at app boot.
        pack = self._load_industry_pack()
        analyst_pack_block = industry_packs.build_analyst_block(pack)  # KPIs+benchmarks+extra
        writer_pack_block = industry_packs.build_writer_block(pack)    # skeleton+benchmarks+extra

        # Copy the CEO config so augmenting its prompt never mutates shared config.
        self.ceo_cfg = dict(config.get_ceo())
        if memory_block:
            self.ceo_cfg["system_prompt"] = self.ceo_cfg["system_prompt"] + "\n\n" + memory_block

        # Build one Agent per worker. The Report Writer ALWAYS gets a private copy
        # of its config (never the shared dict): it receives the memory block now,
        # and we mutate its system_prompt again at run time to inject this run's
        # pre-computed trends (which only exist after the Data Analyst runs).
        self.workers = {}
        for w in config.get_workers():
            cfg = w
            if w["id"] == "report_writer":
                cfg = dict(w)  # copy so we never mutate the shared config dict
                # base prompt + company memory + this run's industry pack (mandatory
                # section order, benchmarks, writer guidance). Memory first, pack last.
                extra = [b for b in (memory_block, writer_pack_block) if b]
                if extra:
                    cfg["system_prompt"] = "\n\n".join([w["system_prompt"], *extra])
                if memory_block:
                    # Debug line proving the Report Writer received the memory block
                    # (shows up in the live "watch the team work" feed).
                    self.memory.log(
                        "report_writer",
                        f"Received COMPANY MEMORY block ({len(memory_block)} chars). "
                        f"First 200 chars: {memory_block[:200]}",
                        type="info",
                    )
                if writer_pack_block:
                    self.memory.log(
                        "report_writer",
                        f"Received INDUSTRY PACK block ({len(writer_pack_block)} chars). "
                        f"First 200 chars: {writer_pack_block[:200]}",
                        type="info",
                    )
            elif w["id"] == "data_analyst" and (kpi_block or analyst_pack_block):
                cfg = dict(w)  # copy so we never mutate the shared config dict
                # base prompt + company KPI mappings + this run's industry pack
                # (required KPIs, benchmarks, analyst guidance).
                extra = [b for b in (kpi_block, analyst_pack_block) if b]
                cfg["system_prompt"] = "\n\n".join([w["system_prompt"], *extra])
                if kpi_block:
                    # Debug line proving the Data Analyst received the alias list
                    # (the acceptance test asserts the aliases appear in this prompt).
                    self.memory.log(
                        "data_analyst",
                        f"Received KPI MAPPING block ({len(kpi_block)} chars). "
                        f"First 200 chars: {kpi_block[:200]}",
                        type="info",
                    )
                if analyst_pack_block:
                    self.memory.log(
                        "data_analyst",
                        f"Received INDUSTRY PACK block ({len(analyst_pack_block)} chars). "
                        f"First 200 chars: {analyst_pack_block[:200]}",
                        type="info",
                    )
            self.workers[w["id"]] = Agent(cfg, memory, self.llm)

        # Capture the Report Writer's prompt AFTER memory injection so the trends
        # block can be appended just-in-time without dropping the memory block.
        rw = self.workers.get("report_writer")
        self._report_writer_base_prompt = rw.cfg["system_prompt"] if rw else None
        self._trends_block = ""
        self._trends_computed = False
        # Raw anomaly dicts from the trend engine, kept so the Phase 6 auditor can
        # treat their exact figures as "known numbers" (computed_by=trend_engine).
        self._anomalies = []

        self._logged_warnings = set()

    # ----------------------------------------------------------------------
    def run(self):
        try:
            self.memory.set_status("running")
            ceo_name = self.ceo_cfg["name"]
            self.memory.log("ceo", f"{ceo_name} (CEO) received the goal and is planning.",
                            type="thinking")

            plan = self._make_plan()
            self.memory.log("ceo", f"Plan ready: delegating {len(plan)} task(s).",
                            type="info")

            for step in plan:
                self._run_step(step)
                self._log_token_warnings()
                # After the Data Analyst finishes, mine its output for numeric
                # metrics and store them in the persistent company brain, THEN
                # compute exact trends/anomalies for the Report Writer to use.
                if step["agent"] == "data_analyst":
                    self._extract_metrics_after_analysis()
                    self._compute_trends_block()
                    self._capture_unknown_terms()

            self.memory.log("ceo", "All specialists done. Assembling the final deliverable.",
                            type="thinking")
            final = self._synthesize()
            final = self._apply_verification_trail(final)
            self.memory.set_final_report(final)
            self._log_token_warnings()
            self.memory.log("ceo", "Final deliverable is ready.", type="success")
            self._finish_run(RUN_SUCCEEDED)
        except BudgetExceededError as e:
            self._handle_cutoff(e)
            self._finish_run(RUN_STOPPED_BUDGET)
        except Exception as e:
            self.memory.set_error(f"{type(e).__name__}: {e}")
            self.memory.log("ceo", f"The run stopped due to an error: {e}", type="error")
            self._finish_run(RUN_FAILED)
            raise

    # ----------------------------------------------------------------------
    def _run_step(self, step):
        # Just before the Report Writer runs, append this run's pre-computed
        # trends/anomalies to its prompt (computed after the Data Analyst ran).
        if step["agent"] == "report_writer":
            self._inject_trends_into_report_writer()
        agent = self.workers[step["agent"]]
        task_id = self.memory.add_task(step["agent"], step["instruction"])
        self.memory.update_task(task_id, status="in_progress")
        self.memory.log("ceo", f"Delegating to {agent.label}.", type="handoff")
        try:
            result = agent.run(step["instruction"])
            self.memory.update_task(task_id, status="done", result=result)
        except Exception as e:
            self.memory.update_task(task_id, status="error", result=str(e))
            raise

    # ----------------------------------------------------------------------
    # Persistent company-memory hooks (no-ops when there's no company/run id)
    # ----------------------------------------------------------------------
    def _load_company_memory(self):
        """Best-effort fetch of the persistent company-memory block (verbatim
        header + body). Returns "" for a brand-new company or on any error —
        memory must never block a run from starting."""
        if not self.company_id:
            return ""
        try:
            return metrics_store.build_memory_block(self.company_id) or ""
        except Exception as e:
            self.memory.log("ceo", f"Company memory unavailable ({e}); continuing without it.",
                            type="info")
            return ""

    def _load_kpi_mapping_block(self):
        """Best-effort fetch of this company's KPI column-mapping block (the verbatim
        mapping instruction + alias lines). Returns "" for a company with no mappings
        or on any error — mappings must never block a run from starting."""
        if not self.company_id:
            return ""
        try:
            return metrics_store.build_kpi_mapping_block(self.company_id) or ""
        except Exception as e:
            self.memory.log("data_analyst",
                            f"KPI mappings unavailable ({e}); continuing without them.",
                            type="info")
            return ""

    def _load_industry_pack(self):
        """Best-effort load of this run's industry pack, FRESH from disk (hot-editable).
        Unknown/empty industry values resolve to the generic pack. Returns None only if
        even that can't be read — a broken pack must never block a run from starting (the
        loud, fail-at-boot gate is industry_packs.validate_all_packs() in app.py)."""
        try:
            pack = industry_packs.load_pack(self.industry_key)
            self.memory.log(
                "ceo",
                f"Industry pack for this run: {pack.get('display_name_cn') or pack.get('_key')} "
                f"[{pack.get('_key')}].",
                type="info",
            )
            return pack
        except Exception as e:
            self.memory.log("ceo", f"Industry pack unavailable ({e}); continuing without it.",
                            type="info")
            return None

    def _capture_unknown_terms(self):
        """Parse the Data Analyst's UNKNOWN_TERMS list out of its output and record
        it on the run, so the result page can ask the user to define each term (the
        "AI团队有 N 个名词想和您确认" banner). Best-effort — never fails the run."""
        analysis = self.memory.get_artifact("data_analyst")
        if not analysis:
            return
        try:
            terms = metrics_store.parse_unknown_terms(analysis)
        except Exception:
            return
        if terms:
            self.memory.set_unknown_terms(terms)
            self.memory.log(
                "data_analyst",
                f"Flagged {len(terms)} term(s) to confirm with the user: {', '.join(terms)}",
                type="info",
            )

    def _extract_metrics_after_analysis(self):
        """Mine the Data Analyst's output for numeric metrics into the company
        brain. Best-effort — a budget cut-off still propagates, but any other
        failure is logged and swallowed so it never fails the run."""
        if not (self.company_id and self.run_id):
            return
        analysis = self.memory.get_artifact("data_analyst")
        if not analysis:
            return

        def _log(msg):
            self.memory.log("data_analyst", msg, type="info")

        metrics_store.extract_metrics_to_db(
            analysis, self.company_id, self.run_id, llm=self.llm, log=_log)

    # ----------------------------------------------------------------------
    # Deterministic trend/anomaly hooks (pure math; no LLM, no network)
    # ----------------------------------------------------------------------
    def _compute_trends_block(self):
        """Run the exact (non-LLM) trend engine over the company brain and cache
        the formatted block. Best-effort: any failure leaves the block empty and
        is logged, never failing the run."""
        self._trends_computed = True
        if not self.company_id:
            return
        try:
            anomalies = trends.detect_anomalies(self.company_id)
        except Exception as e:
            self.memory.log("data_analyst",
                            f"Trend computation unavailable ({e}); continuing without it.",
                            type="info")
            return
        self._anomalies = anomalies or []
        self._trends_block = self._format_trends_block(anomalies)
        self.memory.log(
            "data_analyst",
            f"Computed {len(anomalies)} pre-computed trend/anomaly fact(s) for the report.",
            type="info",
        )

    def _format_trends_block(self, anomalies):
        """Render anomaly dicts under the verbatim header. Empty list -> "" so the
        Report Writer prompt is left untouched when there's nothing to report."""
        if not anomalies:
            return ""
        lines = [TRENDS_HEADER]
        for a in anomalies:
            lines.append(
                f"- {a['human_cn']}  [kind={a['kind']}, wow_pct={a['wow_pct']}]"
            )
        return "\n".join(lines)

    def _inject_trends_into_report_writer(self):
        """Append the cached trends block to the Report Writer's prompt, just
        before it runs. Computes the block lazily if the Data Analyst step never
        ran (e.g. a plan that goes straight to the Report Writer)."""
        if not self._trends_computed:
            self._compute_trends_block()
        rw = self.workers.get("report_writer")
        if not (rw and self._trends_block):
            return
        base = self._report_writer_base_prompt or rw.cfg["system_prompt"]
        rw.cfg["system_prompt"] = base + "\n\n" + self._trends_block
        self.memory.log(
            "report_writer",
            f"Received PRE-COMPUTED trends block ({len(self._trends_block)} chars). "
            f"First 200 chars: {self._trends_block[:200]}",
            type="info",
        )

    def _finish_run(self, status):
        """Record this run's final status, ¥ cost, and report id in the brain,
        then render the branded PDF. Both steps are best-effort — bookkeeping or
        PDF trouble must never fail the run."""
        if not self.run_id:
            return
        try:
            metrics_store.finish_run(
                self.run_id, status,
                total_cost_yuan=round(self._run_cost, 6),
                report_path=self.memory.job_id,
            )
        except Exception:
            pass  # bookkeeping must never fail the run
        self._render_report_pdf()

    def _render_report_pdf(self):
        """Render the final report markdown into a branded, CJK-correct PDF at
        data/reports/<company_id>/<run_id>.pdf. Best-effort: a missing report,
        missing company, or any rendering error is logged and swallowed. Runs for
        both a finished run and a budget-cut partial (both set a final report)."""
        if not (self.company_id and self.run_id):
            return
        snap = self.memory.snapshot()
        final = (snap.get("final_report") or "").strip()
        if not final:
            return
        try:
            from . import pdf_report

            branding = metrics_store.get_branding(self.company_id)
            profile = metrics_store.get_profile(self.company_id)
            company_name = ((profile.get("company_name") or "").strip()
                            or "本公司")

            logo = branding.get("logo_path") or None
            if logo and not os.path.isabs(logo):
                logo = os.path.join(metrics_store.PROJECT_ROOT, logo)
            if logo and not os.path.isfile(logo):
                logo = None

            goal = (snap.get("goal") or "").strip()
            title = (goal.splitlines()[0][:40] if goal else "") or "AI 工作报告"
            period = "报告生成时间：" + datetime.now().strftime("%Y-%m-%d %H:%M")

            out = metrics_store.report_pdf_path(self.company_id, self.run_id)
            pdf_report.build_report_pdf(
                out_path=out,
                markdown_text=final,
                company_name=company_name,
                report_title=title,
                period_label=period,
                logo_path=logo,
                brand_color=branding.get("brand_color") or pdf_report.DEFAULT_BRAND_COLOR,
                # Phase 6 — the verification trail (数据来源与核验) + the marketing
                # footer domain (read from .env; omitted when unset).
                sources=snap.get("sources") or [],
                sources_status=snap.get("sources_status"),
                product_domain=(os.environ.get("PRODUCT_DOMAIN") or "").strip(),
            )
            crypto.encrypt_file_in_place(out)  # at-rest; no-op when FERNET unset
            self.memory.log("ceo", f"已生成品牌 PDF 报告（{os.path.basename(out)}）。",
                            type="success")
        except Exception as e:
            self.memory.log("ceo", f"PDF 生成已跳过（{e}）。", type="info")

    # ----------------------------------------------------------------------
    def _make_plan(self):
        """Ask the CEO to write a delegation plan; fall back to a sane default."""
        def capabilities(w):
            tags = []
            if "web_search" in (w.get("tools") or []):
                tags.append("can search the live web for current facts/news")
            return f"  ({'; '.join(tags)})" if tags else ""

        roster = "\n".join(
            f"  - id: {w['id']}  | {w['title']}{capabilities(w)}" for w in config.get_workers()
        )
        docs = self.memory.documents()
        doc_summary = (
            "\n".join(f"  - {d['name']} ({d['ext']}, {d['chars']:,} chars)" for d in docs)
            if docs else "  (no files were uploaded)"
        )

        prompt = (
            f"The user's goal:\n{self.memory.goal()}\n\n"
            f"Files the user uploaded:\n{doc_summary}\n\n"
            f"Your available specialists:\n{roster}\n\n"
            "Write a short delegation plan. Choose which specialists to use and in what "
            "order, and give each a concrete instruction tailored to this goal and these "
            "files. Skip specialists that aren't useful for this goal. If the goal needs "
            "current facts, recent news, or fact-checking, delegate that part to a specialist "
            "marked as able to search the live web, and tell them explicitly to verify it online.\n\n"
            "Respond with ONLY a JSON object in exactly this shape:\n"
            '{"plan": [{"agent": "<one of the ids above>", "instruction": "<what they should do>"}]}'
        )

        try:
            raw = self.llm.complete(
                system_prompt=self.ceo_cfg["system_prompt"],
                user_content=prompt,
                model=config.effective_model("ceo"),  # honour the UI model picker
                effort=self.ceo_cfg.get("effort", "high"),
                max_tokens=2000,
                budget=config.AGENT_TEAM_BUDGET,
            )
            plan = self._parse_plan(raw)
            if plan:
                return plan
        except BudgetExceededError:
            raise  # let the run handler cut off cleanly
        except Exception as e:
            self.memory.log("ceo", f"Planning fell back to default ({e}).", type="info")

        return self._default_plan()

    def _parse_plan(self, raw):
        data = _extract_json(raw)
        if not data or "plan" not in data:
            return None
        valid_ids = set(self.workers.keys())
        plan = []
        for step in data["plan"]:
            agent_id = step.get("agent")
            instruction = (step.get("instruction") or "").strip()
            if agent_id in valid_ids and instruction:
                plan.append({"agent": agent_id, "instruction": instruction})
        return plan or None

    def _default_plan(self):
        """If planning fails, just run every worker in org-chart order."""
        goal = self.memory.goal()
        defaults = {
            "document_analyst": f"Read every uploaded document and extract the facts, "
                                f"figures, and key points relevant to: {goal}",
            "records_manager": f"Catalog every uploaded document into an organized records "
                               f"register (name, type, category, a one-line description, owner, "
                               f"and date/version), then flag anything missing, duplicated, "
                               f"outdated, mislabeled, sensitive, or non-compliant, with fixes — "
                               f"all in service of: {goal}",
            "data_analyst": f"Analyze the documents and the Document Analyst's findings for "
                            f"trends, totals, outliers, and insights relevant to: {goal}",
            "report_writer": f"Write a clear, well-structured report that answers: {goal}",
            "media_producer": f"Produce the creative & media deliverables the goal calls for — "
                              f"e.g. a slide-by-slide deck outline, document drafts ready to export "
                              f"to PDF/Word, and detailed, ready-to-render image and video briefs "
                              f"(prompts, storyboards, scripts) — building on your teammates' work, "
                              f"in service of: {goal}",
        }
        plan = []
        for w in config.get_workers():
            instruction = defaults.get(w["id"], f"Do your part to help achieve: {goal}")
            plan.append({"agent": w["id"], "instruction": instruction})
        return plan

    # ----------------------------------------------------------------------
    def _synthesize(self):
        snap = self.memory.snapshot()
        sections = []
        for agent_id, text in snap.get("artifacts", {}).items():
            mate = config.get_agent(agent_id)
            title = mate["title"] if mate else agent_id
            sections.append(f"### From {title}:\n{text}")
        body = "\n\n".join(sections) if sections else "(No specialist output was produced.)"

        prompt = (
            f"The user's goal:\n{self.memory.goal()}\n\n"
            f"Here is everything your specialists produced:\n\n{body}\n\n"
            "Assemble the FINAL deliverable for the user. Start with a tight executive "
            "summary (3-6 bullet points of the most important findings and recommendations), "
            "then present the full report in clean Markdown with clear section headings. "
            "If a Report Writer draft exists above, use it as your backbone and fix any gaps "
            "using the analysts' work. Do not mention this assembly process — just deliver "
            "the finished document. "
            # Phase 6: the verification trail must survive synthesis untouched.
            "CRITICAL: preserve EVERY inline source anchor (the 【1】【2】… markers after numbers) "
            "exactly where they are, and if the Report Writer draft ends with a line beginning "
            "'SOURCES_JSON:', copy that entire line verbatim as the very last line of your output — "
            "do not translate, reformat, renumber, or drop it."
        )
        final = self.llm.complete(
            system_prompt=self.ceo_cfg["system_prompt"],
            user_content=prompt,
            model=config.effective_model("ceo"),  # honour the UI model picker
            effort=self.ceo_cfg.get("effort", "high"),
            # Must fit the whole assembled report PLUS the writer's SOURCES_JSON
            # line copied verbatim — 8000 clipped the array on real runs.
            max_tokens=16000,
            budget=config.AGENT_TEAM_BUDGET,
        )
        if getattr(self.llm, "last_stop_reason", None) == "max_tokens":
            # The SOURCES_JSON line rides at the very END, so it is the first
            # casualty of a cut — the salvage in _apply_verification_trail kicks in.
            self.memory.log("ceo", "最终汇总输出达到长度上限，结尾可能被截断。",
                            type="warning")
        return final

    # ----------------------------------------------------------------------
    # Verification trail (Phase 6) — split off the machine SOURCES_JSON block,
    # run a cheap auditor over the report's numbers, and record both on the run.
    # ----------------------------------------------------------------------
    def _apply_verification_trail(self, final):
        """Parse the Report Writer's SOURCES_JSON, store the 数据来源与核验 rows on
        the run, run a best-effort cheap-model audit of the report's numbers, and
        PREPEND a 人工确认 warning if any number can't be backed by the system's
        known figures. Returns the report body the USER should see (machine block
        stripped, warning prepended if needed). Never raises — verification must
        never block delivery.

        When the strict parse fails on a report that clearly carries anchors,
        _salvage_sources() recovers the rows the writer ACTUALLY wrote (from the
        truncated tail and/or the writer's own draft artifact) instead of
        dropping the whole trust table."""
        try:
            body, sources, status = verification.split_report_and_sources(final)
        except Exception:
            # A parser hiccup must not lose the report — ship it as-is, no table.
            self.memory.set_sources([], "malformed")
            return final

        recovered = False
        if status != "ok":
            body, sources, status, recovered = self._salvage_sources(final, body, status)

        self.memory.set_sources(sources or [], status, recovered=recovered)
        if status == "ok":
            self.memory.log("ceo",
                            f"已解析数据溯源表（{len(sources)} 条来源）。", type="success")
        else:
            self.memory.log("ceo",
                            "未能解析数据溯源表（SOURCES_JSON 缺失或格式有误），仍照常交付报告。",
                            type="info")

        warning = ""
        try:
            unverified = verification.audit_report(
                body, self._known_numbers(), self.llm,
                budget=config.AGENT_TEAM_BUDGET)  # model: audit_model() (HAIKU, or domestic)
            warning = verification.build_audit_warning(unverified)
            if unverified:
                self.memory.log(
                    "ceo",
                    f"自动核验发现 {len(unverified)} 个未匹配数字，已在报告顶部加注人工确认提示。",
                    type="warning")
        except Exception:
            warning = ""

        return (warning + body) if warning else body

    def _salvage_sources(self, final, body, status):
        """The SOURCES_JSON block is contracted to be the LAST thing in the
        output, so an output-token cut destroys it first (job onboard-26c4746a00:
        clipped mid-row 47 of 75). Recover the complete rows that were ACTUALLY
        written — from the truncated tail of the final output, or from the Report
        Writer's own draft artifact (which the CEO may also have dropped or
        re-clipped while copying). Never invents a row: the trail is the trust
        feature, so a row the writer didn't author must not appear. No-op for
        deliverables without anchors (e.g. a poem or a slide outline).

        Returns (body, sources, status, recovered) — unchanged input (with
        recovered=False) when nothing can be salvaged."""
        if not verification.report_has_anchors(body):
            return body, None, status, False

        candidates = []
        rows = verification.recover_sources(final)
        if rows:
            candidates.append((rows, "最终汇总的截断块"))
        draft = self.memory.get_artifact("report_writer") or ""
        rows = verification.recover_sources(draft)
        if rows:
            candidates.append((rows, "报告撰写人的草稿"))
        if not candidates:
            return body, None, status, False

        # The draft can only have MORE complete rows than a re-clipped copy —
        # prefer whichever source preserved the most.
        rows, origin = max(candidates, key=lambda c: len(c[0]))
        self.memory.log(
            "ceo",
            f"SOURCES_JSON 块缺失或被截断，已从{origin}恢复 {len(rows)} 条来源"
            f"（正文共 {verification.count_anchors(body)} 个锚点）。",
            type="warning")
        return body, rows, "ok", True

    def _known_numbers(self):
        """The set of numbers the system can actually back up: every metric value
        in the company brain, plus the exact figures from this run's pre-computed
        trend/anomaly engine. Best-effort — returns whatever it can gather."""
        nums = set()
        if self.company_id:
            try:
                nums.update(metrics_store.known_numbers_for_company(self.company_id))
            except Exception:
                pass
        for a in (self._anomalies or []):
            for key in ("latest", "wow_pct"):
                v = a.get(key)
                if v is not None:
                    nums.add(str(v))
        return nums

    # ----------------------------------------------------------------------
    def _log_token_warnings(self):
        """Surface any new budget warnings into the live activity feed."""
        if not self.token_manager:
            return
        for w in self.token_manager.status().get("warnings", []):
            if w not in self._logged_warnings:
                self._logged_warnings.add(w)
                self.memory.log("token_manager", w, type="warning")

    def _handle_cutoff(self, err):
        """A budget ran out mid-run: stop spending and assemble a partial result."""
        self.memory.log(
            "token_manager",
            f"Budget reached ({err.budget_name}). Cutting off to avoid overspending — "
            "assembling a partial deliverable from work done so far.",
            type="warning",
        )
        snap = self.memory.snapshot()
        sections = []
        for agent_id, text in snap.get("artifacts", {}).items():
            mate = config.get_agent(agent_id)
            title = mate["title"] if mate else agent_id
            sections.append(f"## {title}\n\n{text}")
        body = "\n\n".join(sections) if sections else \
            "_No specialist finished before the budget was reached._"
        banner = (f"> **Note:** This run was stopped early to stay within your token budget "
                  f"({err}). Below is the work the team completed before stopping.\n\n")
        # Assembled WITHOUT another API call, so it costs no tokens.
        self.memory.set_final_report(banner + body)
        self.memory.log("ceo", "Partial deliverable assembled (stopped for budget).",
                        type="success")


# ---------------------------------------------------------------------------
def _extract_json(text):
    """Pull the first JSON object out of a model response, tolerating code fences."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start:end + 1]
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None

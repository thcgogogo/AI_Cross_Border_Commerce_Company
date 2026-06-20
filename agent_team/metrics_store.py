"""
metrics_store.py — Read & write the persistent company brain (see db.py / models.py).

This is the glue between the agent pipeline and the long-term memory:

  get_or_create_company / create_run / finish_run
        bookkeeping for each account and each job.

  extract_metrics_to_db(analysis, company_id, run_id, llm)
        ONE cheap LLM call that pulls concrete numeric metrics out of the Data
        Analyst's output and stores them. Best-effort: any failure is logged and
        swallowed — extraction must NEVER fail the agent run.

  load_memory_context(company_id)  /  build_memory_block(company_id)
        assemble what previous runs learned (profile + report preferences +
        glossary + recent metrics) into a compact string the CEO and Report
        Writer can read, capped to ~4000 tokens.

  migrate_accounts_to_companies(customers_json_path)
        on app start, back-fill a companies row for every existing JSON account.

All public functions return plain values (ints / strings / counts), never live
ORM objects, so results are safe to pass across the background job threads.
"""

import hashlib
import json
import math
import os
import re
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func

from . import config, crypto, db
from .models import (
    ACCOUNT_ACTIVE,
    AnonFusionUsage,
    AUDIT_ACTIONS,
    CODE_PURPOSES,
    DELIVERY_FAILED,
    DELIVERY_SENT,
    DELIVERY_STATUSES,
    FAPIAO_REJECTED,
    FAPIAO_REQUESTED,
    FAPIAO_STATUSES,
    LIFECYCLE_KINDS,
    ONBOARDING_COMPLETED,
    ONBOARDING_EVENT_KINDS,
    ONBOARDING_LAST_STEP,
    ONBOARDING_SKIPPED,
    ONBOARDING_STEPS,
    ORDER_CREATED,
    ORDER_FAILED,
    ORDER_PAID,
    ORDER_REFUNDED,
    ORDER_SINGLE_REPORT,
    ORDER_STATUSES,
    ORDER_TOPUP,
    ORDER_TYPES,
    AGENT_RUN_FAILED,
    AGENT_RUN_RUNNING,
    AGENT_RUN_STATUSES,
    QUOTA_RUN_KINDS,
    AR_FLAG_CLASSIFIED,
    AR_FLAG_OPEN,
    AR_FLAG_RESOLVED,
    DUNNING_STATE_APPROVED,
    DUNNING_STATE_DRAFT,
    DUNNING_STATE_EDITED,
    DUNNING_STATE_SENT,
    DUNNING_STATE_DISMISSED,
    DUNNING_TIER_HANDOFF,
    RUN_FAILED,
    RUN_KIND_AR,
    RUN_KIND_DEVICE,
    RUN_KIND_IMPORT,
    RUN_KIND_RECON,
    RUN_KIND_REPORT,
    RUN_KINDS,
    RUN_RUNNING,
    RUN_STATUSES,
    RUN_SUCCEEDED,
    SETTLE_EXC_CLASSIFIED,
    SETTLE_EXC_OPEN,
    SETTLE_EXC_RESOLVED,
    SOURCE_LATEST_UPLOAD,
    SOURCE_MODES,
    SUB_ACTIVE,
    SUB_CANCELED,
    SUB_STATUSES,
    VOTE_VALUES,
    Account,
    AgentCatalog,
    AgentInstall,
    AgentRun,
    AuditLog,
    ChangelogSeen,
    Company,
    CompanyProfile,
    Delivery,
    FapiaoRequest,
    Feedback,
    GlossaryTerm,
    LifecycleEvent,
    Metric,
    OnboardingEvent,
    Order,
    PhoneCode,
    PlatformBreaker,
    PlatformSpend,
    ReconReport,
    ReportPreference,
    Run,
    ScheduledJob,
    SchedulerHeartbeat,
    SettlementException,
    SettlementReport,
    ARReport,
    ARFlag,
    CrossBorderReport,
    CrossBorderFlag,
    CB_FLAG_OPEN,
    CB_FLAG_CLASSIFIED,
    CB_FLAG_RESOLVED,
    DunningMessage,
    FusionAttachment,
    FusionConversation,
    FusionMessage,
    FUSION_ATTACH_DELETED,
    FUSION_ATTACH_DOCUMENT,
    FUSION_ATTACH_READY,
    FUSION_CONVO_ACTIVE,
    FUSION_CONVO_DELETED,
    FUSION_MSG_OK,
    FUSION_ROLE_USER,
    FUSION_ROLE_ASSISTANT,
    Subscription,
    SupportRequest,
)
from .token_manager import BudgetExceededError


# ---------------------------------------------------------------------------
# Phase 11 — the agent catalog (deployable-agent marketplace for the desktop daemon).
# Pure DB; carries only the RUNNER-VISIBLE manifest (no model id / provider / ¥ — the
# route layer builds the whitelist). publish_agent versions by content_hash so a
# no-op republish never inflates the version.
# ---------------------------------------------------------------------------
def latest_agent_catalog(agent_id, published_only=True):
    """The newest catalog row for `agent_id` as a plain dict, or None."""
    with db.session_scope() as s:
        q = s.query(AgentCatalog).filter(AgentCatalog.agent_id == agent_id)
        if published_only:
            q = q.filter(AgentCatalog.published.is_(True))
        row = q.order_by(AgentCatalog.version.desc()).first()
        if not row:
            return None
        return {"agent_id": row.agent_id, "version": row.version,
                "content_hash": row.content_hash, "manifest_json": row.manifest_json}


def publish_agent(agent_id, name_cn, manifest, content_hash):
    """Snapshot a runner-visible `manifest` dict into agent_catalog. Idempotent by
    content_hash: if the latest row already has this hash, return (its_version, False)
    without inserting; otherwise insert version = max+1 (stamping manifest_version +
    content_hash inline) and return (version, True)."""
    with db.session_scope() as s:
        latest = (s.query(AgentCatalog).filter(AgentCatalog.agent_id == agent_id)
                  .order_by(AgentCatalog.version.desc()).first())
        if latest and latest.content_hash == content_hash:
            return latest.version, False
        version = (latest.version if latest else 0) + 1
        stamped = dict(manifest)
        stamped["manifest_version"] = version
        stamped["content_hash"] = content_hash
        s.add(AgentCatalog(
            agent_id=agent_id, version=version, name_cn=name_cn,
            manifest_json=json.dumps(stamped, ensure_ascii=False),
            content_hash=content_hash, published=True))
        return version, True


def set_agent_install(account_id, agent_id, active=True, pinned_version=None):
    """Upsert one account's install of `agent_id` (insert, else update active/pin).
    active=False is the soft uninstall — the row stays for history; the catalog query
    filters on active."""
    with db.session_scope() as s:
        row = (s.query(AgentInstall)
               .filter(AgentInstall.account_id == account_id,
                       AgentInstall.agent_id == agent_id).one_or_none())
        if row is None:
            s.add(AgentInstall(account_id=account_id, agent_id=agent_id,
                               active=bool(active), pinned_version=pinned_version))
        else:
            row.active = bool(active)
            row.pinned_version = pinned_version


def list_agent_installs(account_id):
    """This account's installs as plain dicts (newest first)."""
    with db.session_scope() as s:
        rows = (s.query(AgentInstall).filter(AgentInstall.account_id == account_id)
                .order_by(AgentInstall.created_at.desc()).all())
        return [{"agent_id": r.agent_id, "active": bool(r.active),
                 "pinned_version": r.pinned_version} for r in rows]


def create_device_run(company_id, goal_text, device_id):
    """Open a runs row for a desktop (device) run: kind='device_run', origin='device',
    device_id stamped. Returns the run_id, or None. device_run is excluded from the
    quota meter, the ¥29 credit consume, and the default 历史报告 filter — exactly like
    'import' — because count_runs_*/list_runs/consume_report_credit all gate on
    kind == 'report'."""
    if not company_id:
        return None
    with db.session_scope() as s:
        run = Run(company_id=company_id, goal_text=goal_text or "", status=RUN_RUNNING,
                  kind=RUN_KIND_DEVICE, origin="device",
                  device_id=str(device_id)[:32] if device_id else None)
        s.add(run)
        s.flush()
        return run.id


def create_agent_run(run_id, account_id, device_id, agent_id,
                     agent_catalog_id=None, manifest_hash=None):
    """INSERT an agent_run row (status='running') tied to an open runs.id. Returns the
    agent_run id, or None."""
    if not run_id:
        return None
    with db.session_scope() as s:
        ar = AgentRun(
            run_id=run_id, account_id=account_id,
            device_id=str(device_id)[:32] if device_id else None,
            agent_id=str(agent_id)[:64] if agent_id else None,
            agent_catalog_id=agent_catalog_id,
            manifest_hash=str(manifest_hash)[:71] if manifest_hash else None,
            status=AGENT_RUN_RUNNING)
        s.add(ar)
        s.flush()
        return ar.id


def _agent_run_to_dict(ar):
    if ar is None:
        return None
    return {"id": ar.id, "run_id": ar.run_id, "account_id": ar.account_id,
            "device_id": ar.device_id, "agent_id": ar.agent_id, "status": ar.status,
            "cost_yuan": float(ar.cost_yuan or 0), "report_summary": ar.report_summary or "",
            "report_sha256": ar.report_sha256, "report_bytes": ar.report_bytes,
            "deliverable_kind": ar.deliverable_kind,
            "started_at": _iso(ar.started_at), "finished_at": _iso(ar.finished_at)}


def get_agent_run(agent_run_id):
    with db.session_scope() as s:
        return _agent_run_to_dict(s.get(AgentRun, agent_run_id))


def finish_agent_run(agent_run_id, status, cost_yuan=None, report_summary=None,
                     report_sha256=None, report_bytes=None, deliverable_kind=None):
    """Close out the agent_run SUMMARY. Coerces an unknown status to 'failed'; truncates
    text defensively so a runaway client can't bloat the row. Returns True if updated."""
    if not agent_run_id:
        return False
    if status not in AGENT_RUN_STATUSES:
        status = AGENT_RUN_FAILED
    with db.session_scope() as s:
        ar = s.get(AgentRun, agent_run_id)
        if ar is None:
            return False
        ar.status = status
        ar.finished_at = datetime.utcnow()
        if cost_yuan is not None:
            ar.cost_yuan = _coerce_float(cost_yuan) or 0
        if report_summary is not None:
            ar.report_summary = str(report_summary)[:8000]   # text only, never bytes
        if report_sha256 is not None:
            ar.report_sha256 = str(report_sha256)[:64]
        if report_bytes is not None:
            try:
                ar.report_bytes = int(report_bytes)
            except (TypeError, ValueError):
                pass
        if deliverable_kind is not None:
            ar.deliverable_kind = str(deliverable_kind)[:32]
        return True


def finish_agent_run_for_run(run_id, status, **summary):
    """Find the agent_run tied to `run_id` and close it (convenience for the finish
    endpoint, which knows the run_id but not the agent_run id). Returns True if found."""
    with db.session_scope() as s:
        ar = (s.query(AgentRun).filter(AgentRun.run_id == run_id)
              .order_by(AgentRun.id.desc()).first())
        ar_id = ar.id if ar else None
    return finish_agent_run(ar_id, status, **summary) if ar_id else False


def catalog_for_account(account_id):
    """The resolved runner-visible catalog for a device of this account: for each
    ACTIVE install, the pinned version (if set) else the latest PUBLISHED version.
    Installs with no published catalog row are skipped. NEVER resolves a model — it
    only reads stored manifest_json (whitelisted at publish time)."""
    out = []
    with db.session_scope() as s:
        installs = (s.query(AgentInstall)
                    .filter(AgentInstall.account_id == account_id,
                            AgentInstall.active.is_(True)).all())
        for inst in installs:
            q = s.query(AgentCatalog).filter(
                AgentCatalog.agent_id == inst.agent_id,
                AgentCatalog.published.is_(True))
            if inst.pinned_version is not None:
                q = q.filter(AgentCatalog.version == inst.pinned_version)
            row = q.order_by(AgentCatalog.version.desc()).first()
            if not row:
                continue
            try:
                manifest = json.loads(row.manifest_json)
            except Exception:
                continue
            out.append({"agent_id": row.agent_id, "version": row.version,
                        "content_hash": row.content_hash, "manifest": manifest})
    return out


# The Phase 5 on-disk layout, anchored at the project root (same root db.py uses
# for data/company_brain.db). Kept here so the scheduler + orchestrator wiring
# agree on where uploads and rendered PDFs live without importing app.py.
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_PKG_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")     # data/uploads/<company_id>/<batch>/
REPORTS_DIR = os.path.join(DATA_DIR, "reports")     # data/reports/<company_id>/<run_id>.pdf

# Default brand colour (mirrors pdf_report.DEFAULT_BRAND_COLOR — duplicated as a
# bare literal so this core module need not import reportlab at load time).
DEFAULT_BRAND_COLOR = "#1D9E75"

# VERBATIM — do not edit. The exact extraction contract from the spec.
METRIC_EXTRACTION_SYSTEM_PROMPT = (
    'You are a data extraction engine. From the analysis below, extract every '
    'concrete numeric business metric into a JSON array. Each element: '
    '{"metric_name": snake_case English, "metric_name_cn": Chinese name, '
    '"value": number, "unit": string, "period_label": string, "dimension": '
    'string or null, "source_file": string or null, "source_detail": string or '
    'null}. Respond with ONLY the JSON array. No markdown, no commentary. If a '
    'value is a percentage, store the number (12.5 for 12.5%) with unit "%". '
    'Never invent numbers not present in the analysis.'
)

# VERBATIM — the header the memory block is injected under (see orchestrator).
MEMORY_HEADER = "=== 公司历史记忆 (COMPANY MEMORY — use this to compare against current data) ==="

# The header the KPI-mapping block is injected under (see build_kpi_mapping_block
# / orchestrator). The INSTRUCTION line below is VERBATIM from the Phase 3 spec —
# do not edit it: the Data Analyst must receive these exact words.
KPI_MAPPING_HEADER = "=== KPI 指标映射 (KPI MAPPING) ==="
KPI_MAPPING_INSTRUCTION = (
    "Map spreadsheet columns to KPIs using these aliases BEFORE analyzing. "
    "If a required KPI has no matching column, state that explicitly instead of guessing."
)

# How much memory we inject. The spec caps the block at ~4000 tokens; we keep
# profile + preferences + glossary in full and drop the OLDEST metric periods
# first when we exceed it.
MEMORY_MAX_TOKENS = 4000
RECENT_PERIODS = 8

# Output ceiling for the extraction call. A dense multi-week analysis yields
# dozens of metric rows (a real run produced 9.7k chars and was cut mid-row at
# 4000), so give the reply generous headroom; the truncation repair below still
# salvages complete rows if even this is exceeded.
METRIC_EXTRACTION_MAX_TOKENS = 8000

# The line a grep can find when the brain learned nothing. Phases 1-5 shipped
# with a quiet "skipping (0 metrics)" info line and nobody noticed the metrics
# table stayed empty across every real run — keep this LOUD.
METRIC_EXTRACTION_FAILURE_WARNING = (
    "⚠️ 指标提取失败 — METRIC EXTRACTION YIELDED 0 METRICS after retry; "
    "the company brain learned NOTHING from this run. "
    "与历史对比 will be missing until extraction succeeds."
)


# ---------------------------------------------------------------------------
# Companies & runs
# ---------------------------------------------------------------------------
def get_or_create_company(account_email, company_name=None, industry=None):
    """Return the companies.id for an account email, creating the row if needed.

    Keyed on the (lowercased) login email — the same identity the JSON account
    store uses. Returns None for an empty email (anonymous runs aren't persisted).
    """
    email_norm = (account_email or "").strip().lower()
    if not email_norm:
        return None
    name = (company_name or "").strip() or None
    industry = (industry or "").strip() or None
    with db.session_scope() as s:
        company = s.query(Company).filter(Company.account_email == email_norm).one_or_none()
        if company is None:
            company = Company(account_email=email_norm, company_name=name, industry=industry)
            s.add(company)
            s.flush()  # populate the auto-increment id before we read it
        else:
            # Enrich missing fields without overwriting anything already set.
            if name and not company.company_name:
                company.company_name = name
            if industry and not company.industry:
                company.industry = industry
        return company.id


def create_run(company_id, goal_text, kind=RUN_KIND_REPORT):
    """Open a runs row (status=running) and return its id, or None if no company.

    `kind` separates quota-metered report runs from the free 历史数据导入
    back-fill (kind="import") — import runs used to eat the 免费体验 plan's
    2-lifetime quota and show up as dead rows in 历史报告 (Phase 6 B1)."""
    if not company_id:
        return None
    if kind not in RUN_KINDS:
        kind = RUN_KIND_REPORT
    with db.session_scope() as s:
        run = Run(company_id=company_id, goal_text=goal_text or "", status="running",
                  kind=kind)
        s.add(run)
        s.flush()
        return run.id


def finish_run(run_id, status, total_cost_yuan=None, report_path=None):
    """Close out a run: set its final status, finish time, ¥ cost and report path."""
    if not run_id:
        return
    if status not in RUN_STATUSES:
        status = RUN_FAILED
    with db.session_scope() as s:
        run = s.get(Run, run_id)
        if run is None:
            return
        run.status = status
        run.finished_at = datetime.utcnow()
        if total_cost_yuan is not None:
            try:
                run.total_cost_yuan = float(total_cost_yuan)
            except (TypeError, ValueError):
                pass
        if report_path is not None:
            run.report_path = report_path
    # B4 — one chokepoint for every run path (interactive / scheduled / wizard):
    # a counting run that landed beyond the base plan quota consumes one 单次
    # 报告 credit. Best-effort: credit bookkeeping must never fail a run.
    if status != RUN_FAILED:
        try:
            consume_report_credit_if_over_quota(run_id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Metric extraction (one extra LLM call after the Data Analyst)
# ---------------------------------------------------------------------------
def extract_metrics_to_db(analysis_output, company_id, run_id, llm=None, log=None):
    """Extract numeric metrics from an analysis and store them. Returns the count
    of rows inserted.

    BEST-EFFORT BY DESIGN: missing inputs, an LLM error, malformed JSON, or a DB
    error are all logged (via the optional `log` callback) and swallowed — this
    must NEVER raise into / fail the agent run. The single exception is a token
    BudgetExceededError, which we re-raise so the orchestrator can cut the run
    off cleanly (it is the global spend guard, not an extraction failure).
    """
    def _note(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    if not analysis_output or not company_id or not run_id:
        return 0

    # Reuse the caller's router so the call is billed to the account exactly like
    # every other model call. Only build a fresh one as a last resort.
    if llm is None:
        try:
            from .providers import ModelRouter
            llm = ModelRouter()
        except Exception as e:  # pragma: no cover - depends on env keys
            _note(f"metric extraction skipped (no LLM available: {e}).")
            return 0

    rows = None
    for attempt in (1, 2):  # one retry on total failure
        try:
            raw = llm.complete(
                system_prompt=METRIC_EXTRACTION_SYSTEM_PROMPT,
                user_content=f"ANALYSIS:\n{analysis_output}",
                # HAIKU by default; the domestic model under TEAM_MODEL_PRESET=domestic
                # (this is not an agent, so the founder UI can't reassign it — hence the
                # dedicated EXTRACTION_MODEL knob keeps the company brain off Anthropic).
                model=config.extraction_model(),
                effort="low",            # ignored for Haiku, harmless if not
                max_tokens=METRIC_EXTRACTION_MAX_TOKENS,
                budget=config.AGENT_TEAM_BUDGET,
            )
        except BudgetExceededError:
            raise  # let the run handler cut off cleanly
        except Exception as e:
            _note(f"metric extraction LLM call failed on attempt {attempt} ({e}).")
            continue
        if getattr(llm, "last_stop_reason", None) == "max_tokens":
            _note("metric extraction reply hit the output-token limit — "
                  "salvaging complete rows from the truncated array.")
        rows = _parse_metrics_json(raw)
        if rows:
            break
        _note(f"metric extraction attempt {attempt} produced no parseable metric rows.")
    if not rows:
        _note(METRIC_EXTRACTION_FAILURE_WARNING)
        return 0

    inserted = 0
    try:
        with db.session_scope() as s:
            for item in rows:
                metric = _metric_from_item(item, company_id, run_id)
                if metric is not None:
                    s.add(metric)
                    inserted += 1
    except Exception as e:  # never let a DB hiccup fail the run
        _note(f"metric extraction DB write failed — skipping ({e}).")
        return 0

    _note(f"metric extraction stored {inserted} metric(s) to the company brain.")
    return inserted


def _parse_metrics_json(raw):
    """Pull a JSON array out of an LLM response, tolerating ```json fences
    (closed OR unclosed — an output-token cut leaves the fence open), prose
    around the array, and an array whose tail was truncated mid-row. Returns a
    list, or None. NEVER raises.

    Mirrors the Phase-3 SOURCES_JSON salvage (verification.recover_sources):
    strict parse first, then close the array after the last COMPLETE row —
    a half-written row is dropped, never guessed at."""
    if not raw or not isinstance(raw, str):
        return None
    candidates = []
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    # Outermost array of the fence-stripped text. Stripping fence markers (not
    # just matching closed pairs) also covers the unclosed-fence shape.
    bare = re.sub(r"```[a-zA-Z]*", "", raw)
    start, end = bare.find("["), bare.rfind("]")
    if start != -1 and end > start:
        candidates.append(bare[start:end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, list):
            return data
    return _repair_truncated_metrics_array(bare)


def _repair_truncated_metrics_array(text):
    """Parse a JSON array whose closing bracket (and likely its final element)
    was cut off mid-stream: close the array right after a complete object,
    trying from the LAST '}' backward, and return the first variant that
    parses to a non-empty list. Returns a list, or None. (Deliberate twin of
    verification._repair_truncated_array — the agent pipeline is frozen, so we
    mirror rather than import its private helper.)"""
    if not text:
        return None
    start = text.find("[")
    if start == -1:
        return None
    frag = text[start:]
    for end in reversed([m.end() for m in re.finditer(r"\}", frag)]):
        try:
            val = json.loads(frag[:end] + "]")
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(val, list):
            return val or None
    return None


def _metric_from_item(item, company_id, run_id):
    """Build a Metric from one extracted dict; None if there's no usable number."""
    if not isinstance(item, dict):
        return None
    value = _coerce_float(item.get("value"))
    if value is None:
        return None
    return Metric(
        company_id=company_id,
        run_id=run_id,
        metric_name=_short(item.get("metric_name"), 128),
        metric_name_cn=_short(item.get("metric_name_cn"), 128),
        value=value,
        unit=_short(item.get("unit"), 32),
        period_label=_short(item.get("period_label"), 64),
        dimension=_short(item.get("dimension"), 128),
        source_file=_short(item.get("source_file"), 255),
        source_detail=_short(item.get("source_detail"), 255),
    )


def _coerce_float(v):
    if isinstance(v, bool):  # bool is an int subclass — reject it explicitly
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        # float() happily parses "NaN"/"Infinity"/"1e400" — non-finite values
        # serialize as bare NaN/Infinity tokens, which is invalid JSON, so one
        # such row would blank the whole 企业大脑 summary. Drop them at write time.
        cleaned = v.strip().replace(",", "").rstrip("%").strip()
        try:
            out = float(cleaned)
        except ValueError:
            return None
        return out if math.isfinite(out) else None
    return None


def _short(v, n):
    if v is None:
        return None
    s = str(v).strip()
    return s[:n] if s else None


# ---------------------------------------------------------------------------
# Brain summary reads (redesign Phase 6 / B6) — the 企业大脑 hero stats and the
# KPI-tile index. Read-only compositions; trends.py does the per-series math.
# ---------------------------------------------------------------------------
def brain_stats(company_id):
    """The 企业大脑 hero counters: {metrics_count, kpi_count, weeks_covered,
    glossary_count, preferences_count}. metrics_count is total stored metric
    ROWS (how much the brain has recorded); kpi_count is DISTINCT metric names
    (how many different indicators it tracks); weeks_covered is DISTINCT
    period labels; preferences_count counts ACTIVE standing instructions (the
    ones reports actually apply). Clean zeros for an empty/missing brain."""
    zeros = {"metrics_count": 0, "kpi_count": 0, "weeks_covered": 0,
             "glossary_count": 0, "preferences_count": 0}
    if not company_id:
        return zeros
    with db.session_scope() as s:
        row = (s.query(func.count(Metric.id),
                       func.count(func.distinct(Metric.metric_name)),
                       func.count(func.distinct(Metric.period_label)))
               .filter(Metric.company_id == company_id).one())
        glossary = (s.query(func.count(GlossaryTerm.id))
                    .filter(GlossaryTerm.company_id == company_id).scalar())
        prefs = (s.query(func.count(ReportPreference.id))
                 .filter(ReportPreference.company_id == company_id,
                         ReportPreference.active.is_(True)).scalar())
        return {
            "metrics_count": int(row[0] or 0),
            "kpi_count": int(row[1] or 0),
            "weeks_covered": int(row[2] or 0),
            "glossary_count": int(glossary or 0),
            "preferences_count": int(prefs or 0),
        }


def metric_series_index(company_id):
    """Every (metric_name, dimension) series the brain holds, in ONE GROUP BY
    over ix_metrics_company_id: [{"metric_name", "dimension", "periods",
    "last_id"}], where periods is the DISTINCT period_label coverage and
    last_id the newest row id (recency tiebreak for KPI-tile ranking)."""
    if not company_id:
        return []
    with db.session_scope() as s:
        rows = (s.query(Metric.metric_name, Metric.dimension,
                        func.count(func.distinct(Metric.period_label)),
                        func.max(Metric.id))
                .filter(Metric.company_id == company_id)
                .group_by(Metric.metric_name, Metric.dimension)
                .all())
        return [{"metric_name": name, "dimension": dim,
                 "periods": int(periods or 0), "last_id": int(last_id)}
                for name, dim, periods, last_id in rows
                if name]  # a row with no metric_name can't be charted


def metric_briefs(metric_ids):
    """{id: {"metric_name_cn", "unit"}} for the given metric row ids — the
    KPI tiles' display name + unit come from each series' NEWEST row."""
    ids = [int(i) for i in (metric_ids or []) if i]
    if not ids:
        return {}
    with db.session_scope() as s:
        rows = s.query(Metric).filter(Metric.id.in_(ids)).all()
        return {r.id: {"metric_name_cn": r.metric_name_cn, "unit": r.unit}
                for r in rows}


# ---------------------------------------------------------------------------
# Memory context (injected into the CEO + Report Writer before the run)
# ---------------------------------------------------------------------------
def build_memory_block(company_id):
    """The full block to inject: the verbatim header + the memory body. Returns
    "" when there's nothing to inject (a brand-new company), so the caller can
    skip injection cleanly."""
    body = load_memory_context(company_id)
    if not body:
        return ""
    return f"{MEMORY_HEADER}\n{body}"


def load_memory_context(company_id, max_periods=RECENT_PERIODS, max_tokens=MEMORY_MAX_TOKENS):
    """Assemble what previous runs learned into a compact string:
      - the company profile JSON,
      - all active report preferences,
      - all glossary terms,
      - the most recent `max_periods` periods of metrics, as a compact table.

    Capped to ~`max_tokens`: profile + preferences + glossary are always kept in
    full; the OLDEST metric periods are dropped first. Returns "" if empty.
    """
    if not company_id:
        return ""
    with db.session_scope() as s:
        company = s.get(Company, company_id)
        if company is None:
            return ""

        head_sections = []

        profile = s.query(CompanyProfile).filter_by(company_id=company_id).one_or_none()
        if profile and (profile.profile_json or "").strip():
            head_sections.append("## 公司档案 / Company profile\n" + profile.profile_json.strip())

        prefs = (s.query(ReportPreference)
                 .filter_by(company_id=company_id, active=True)
                 .order_by(ReportPreference.created_at.asc(), ReportPreference.id.asc())
                 .all())
        pref_lines = [f"- {p.instruction.strip()}" for p in prefs if (p.instruction or "").strip()]
        if pref_lines:
            head_sections.append("## 报告偏好 / Report preferences\n" + "\n".join(pref_lines))

        terms = (s.query(GlossaryTerm)
                 .filter_by(company_id=company_id)
                 .order_by(GlossaryTerm.term.asc())
                 .all())
        term_lines = [f"- {t.term}: {t.definition or ''}".rstrip() for t in terms]
        if term_lines:
            head_sections.append("## 术语表 / Glossary\n" + "\n".join(term_lines))

        head = "\n\n".join(head_sections)

        period_blocks = _recent_metric_blocks(s, company_id, max_periods)

        if not head and not period_blocks:
            return ""

        return _assemble_capped(head, period_blocks, max_tokens)


def _recent_metric_blocks(session, company_id, max_periods):
    """Return formatted metric tables, one per period, NEWEST period first.

    "Newest" is defined by the largest metric id seen in each period_label, which
    is deterministic and matches insertion order across runs.
    """
    metrics = (session.query(Metric)
               .filter(Metric.company_id == company_id)
               .order_by(Metric.id.asc())
               .all())
    if not metrics:
        return []

    last_id, by_period = {}, {}
    for m in metrics:
        label = m.period_label or "(未标注 / unlabeled)"
        by_period.setdefault(label, []).append(m)
        last_id[label] = max(last_id.get(label, m.id), m.id)

    ranked = sorted(by_period.keys(), key=lambda lbl: last_id[lbl], reverse=True)
    kept = ranked[:max_periods]

    blocks = []
    for label in kept:
        rows = ["| 指标 metric | 数值 value | 单位 unit | 维度 dimension | 来源 source |",
                "| --- | --- | --- | --- | --- |"]
        for m in by_period[label]:
            name = m.metric_name_cn or m.metric_name or ""
            if m.metric_name_cn and m.metric_name:
                name = f"{m.metric_name_cn} ({m.metric_name})"
            rows.append(
                f"| {name} | {_fmt_num(m.value)} | {m.unit or ''} "
                f"| {m.dimension or ''} | {m.source_file or ''} |"
            )
        blocks.append(f"【{label}】\n" + "\n".join(rows))
    return blocks


def _assemble_capped(head, period_blocks, max_tokens):
    intro = "最近指标 / Recent metrics (newest period first):"
    blocks = list(period_blocks)

    def assemble(bs):
        parts = []
        if head:
            parts.append(head)
        if bs:
            parts.append(intro + "\n\n" + "\n\n".join(bs))
        return "\n\n".join(parts)

    text = assemble(blocks)
    # Drop the oldest period (end of the newest-first list) until we fit.
    while blocks and _estimate_tokens(text) > max_tokens:
        blocks.pop()
        text = assemble(blocks)
    return text


def _estimate_tokens(s):
    """Rough token count for mixed Chinese/English text: ~1 token per CJK char,
    ~1 token per 4 other chars. Good enough to keep us under the cap."""
    if not s:
        return 0
    cjk = sum(1 for ch in s if "一" <= ch <= "鿿")
    other = len(s) - cjk
    return cjk + (other // 4) + 1


def _fmt_num(v):
    if v is None:
        return ""
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


# ---------------------------------------------------------------------------
# Company Brain CRUD (Phase 3) — the editable backing for the "企业大脑" page.
# Every function is keyed on company_id (derived from the logged-in account's
# email upstream) and returns plain values, never live ORM objects.
# ---------------------------------------------------------------------------
def get_profile(company_id):
    """Return the company profile as a plain dict (the parsed profile_json), or {}.

    The profile holds: company_name, industry, business_desc, fiscal_week_start
    ("Mon"/"Sun"), and "kpi_mappings" (a list of {kpi_name_cn, aliases, unit})."""
    if not company_id:
        return {}
    with db.session_scope() as s:
        row = s.query(CompanyProfile).filter_by(company_id=company_id).one_or_none()
        if not row or not (row.profile_json or "").strip():
            return {}
        try:
            data = json.loads(row.profile_json)
        except (json.JSONDecodeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}


def save_profile(company_id, profile):
    """Upsert the company profile JSON blob. `profile` is a plain dict; it is
    stored verbatim (so the page can round-trip whatever fields it sends).
    Returns the saved dict, or {} if there's no company."""
    if not company_id:
        return {}
    if not isinstance(profile, dict):
        profile = {}
    payload = json.dumps(profile, ensure_ascii=False)
    with db.session_scope() as s:
        row = s.query(CompanyProfile).filter_by(company_id=company_id).one_or_none()
        if row is None:
            s.add(CompanyProfile(company_id=company_id, profile_json=payload))
        else:
            row.profile_json = payload
    return profile


def get_kpi_mappings(company_id):
    """The list of KPI column-mappings stored in the profile (or [])."""
    mappings = get_profile(company_id).get("kpi_mappings")
    return mappings if isinstance(mappings, list) else []


def build_kpi_mapping_block(company_id):
    """The KPI-mapping block injected into the Data Analyst's prompt: the VERBATIM
    mapping instruction followed by one alias line per configured KPI. Returns ""
    when the company has no KPI mappings, so the caller can skip injection."""
    mappings = get_kpi_mappings(company_id)
    lines = []
    for m in mappings:
        if not isinstance(m, dict):
            continue
        name = (m.get("kpi_name_cn") or m.get("kpi_name") or "").strip()
        aliases = (m.get("aliases") or "").strip()
        unit = (m.get("unit") or "").strip()
        if not name and not aliases:
            continue
        unit_part = f" [{unit}]" if unit else ""
        alias_part = f" ← aliases: {aliases}" if aliases else ""
        lines.append(f"- {name}{unit_part}{alias_part}")
    if not lines:
        return ""
    return KPI_MAPPING_HEADER + "\n" + KPI_MAPPING_INSTRUCTION + "\n" + "\n".join(lines)


# ---- Glossary (term -> definition) ----------------------------------------
def list_glossary(company_id):
    """All glossary rows for a company as [{id, term, definition}], term-sorted."""
    if not company_id:
        return []
    with db.session_scope() as s:
        rows = (s.query(GlossaryTerm)
                .filter_by(company_id=company_id)
                .order_by(GlossaryTerm.term.asc())
                .all())
        return [{"id": t.id, "term": t.term, "definition": t.definition or ""} for t in rows]


def add_glossary_term(company_id, term, definition=""):
    """Add ONE glossary entry, or update the definition if the term already exists
    (respecting the unique (company_id, term) constraint). Returns the row id, or
    None for an empty term / missing company."""
    if not company_id:
        return None
    term = (term or "").strip()
    if not term:
        return None
    definition = (definition or "").strip()
    with db.session_scope() as s:
        row = (s.query(GlossaryTerm)
               .filter_by(company_id=company_id, term=term)
               .one_or_none())
        if row is None:
            row = GlossaryTerm(company_id=company_id, term=term, definition=definition)
            s.add(row)
            s.flush()
        else:
            row.definition = definition
        return row.id


def update_glossary_term(company_id, term_id, term=None, definition=None):
    """Edit a glossary row's term and/or definition. Returns True if a row changed."""
    if not (company_id and term_id):
        return False
    with db.session_scope() as s:
        row = s.query(GlossaryTerm).filter_by(id=term_id, company_id=company_id).one_or_none()
        if row is None:
            return False
        if term is not None and term.strip():
            row.term = term.strip()
        if definition is not None:
            row.definition = definition.strip()
        return True


def delete_glossary_term(company_id, term_id):
    """Delete one glossary row. Returns True if a row was removed."""
    if not (company_id and term_id):
        return False
    with db.session_scope() as s:
        row = s.query(GlossaryTerm).filter_by(id=term_id, company_id=company_id).one_or_none()
        if row is None:
            return False
        s.delete(row)
        return True


def add_glossary_terms(company_id, term_to_def):
    """Bulk add/update glossary rows (used by the UNKNOWN_TERMS confirm flow).
    `term_to_def` is a {term: definition} mapping. Returns the count written;
    empty terms are skipped. Existing terms have their definition updated."""
    if not company_id or not isinstance(term_to_def, dict):
        return 0
    written = 0
    with db.session_scope() as s:
        for term, definition in term_to_def.items():
            t = (term or "").strip()
            if not t:
                continue
            d = (definition or "").strip()
            row = s.query(GlossaryTerm).filter_by(company_id=company_id, term=t).one_or_none()
            if row is None:
                s.add(GlossaryTerm(company_id=company_id, term=t, definition=d))
            else:
                row.definition = d
            written += 1
    return written


# ---- Report preferences (standing instructions, on/off) -------------------
def list_report_preferences(company_id):
    """All report-preference rows as [{id, instruction, active}], oldest first."""
    if not company_id:
        return []
    with db.session_scope() as s:
        rows = (s.query(ReportPreference)
                .filter_by(company_id=company_id)
                .order_by(ReportPreference.created_at.asc(), ReportPreference.id.asc())
                .all())
        return [{"id": p.id, "instruction": p.instruction or "", "active": bool(p.active)}
                for p in rows]


def add_report_preference(company_id, instruction):
    """Add a standing report instruction (active by default). Returns its id."""
    if not company_id:
        return None
    instruction = (instruction or "").strip()
    if not instruction:
        return None
    with db.session_scope() as s:
        row = ReportPreference(company_id=company_id, instruction=instruction, active=True)
        s.add(row)
        s.flush()
        return row.id


def set_report_preference_active(company_id, pref_id, active):
    """Toggle one preference on/off. Returns True if a row changed."""
    if not (company_id and pref_id):
        return False
    with db.session_scope() as s:
        row = s.query(ReportPreference).filter_by(id=pref_id, company_id=company_id).one_or_none()
        if row is None:
            return False
        row.active = bool(active)
        return True


def delete_report_preference(company_id, pref_id):
    """Delete one preference. Returns True if a row was removed."""
    if not (company_id and pref_id):
        return False
    with db.session_scope() as s:
        row = s.query(ReportPreference).filter_by(id=pref_id, company_id=company_id).one_or_none()
        if row is None:
            return False
        s.delete(row)
        return True


# ---- The learning loop: parse UNKNOWN_TERMS out of an analysis ------------
def parse_unknown_terms(text):
    """Pull the JSON list the Data Analyst appends when it meets a term it cannot
    interpret: `UNKNOWN_TERMS: ["KA渠道", "动销率"]`. Returns a de-duplicated list
    of non-empty strings (order preserved), or [] if absent/malformed. NEVER raises."""
    if not text or not isinstance(text, str):
        return []
    m = re.search(r"UNKNOWN_TERMS\s*:\s*(\[[^\]]*\])", text)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out, seen = [], set()
    for item in data:
        if not isinstance(item, str):
            continue
        t = item.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# One-time migration: JSON accounts -> companies rows
# ---------------------------------------------------------------------------
def migrate_accounts_to_companies(customers_json_path):
    """Create a companies row for every existing JSON account. Idempotent — skips
    accounts that already have a company. Returns the number of NEW rows created.

    Reads the JSON store directly because customers.py exposes no iterate-all API.
    """
    try:
        with open(customers_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return 0

    accounts = (data or {}).get("customers") or []
    created = 0
    with db.session_scope() as s:
        for rec in accounts:
            email = (rec.get("email") or "").strip().lower()
            if not email:
                continue
            if s.query(Company.id).filter(Company.account_email == email).first():
                continue  # already migrated
            name = (rec.get("company") or rec.get("name") or "").strip() or None
            s.add(Company(account_email=email, company_name=name))
            created += 1
    return created


# ---------------------------------------------------------------------------
# Phase 7 — scheduler liveness heartbeat (read by /healthz)
# ---------------------------------------------------------------------------
# The dedicated scheduler process stamps this every ~minute. /healthz compares the
# latest beat against config.HEARTBEAT_STALE_SECS, so a killed scheduler container
# becomes observable within ~2 minutes. One logical row is reused (keyed by name),
# so it never grows.
_HEARTBEAT_NAME = "scheduler"


def write_scheduler_heartbeat():
    """Stamp the scheduler's liveness marker to 'now' (upsert the single row).
    Best-effort: returns True on success, False if the write failed — so the
    scheduler loop never crashes on a transient DB hiccup."""
    try:
        with db.session_scope() as s:
            row = (s.query(SchedulerHeartbeat)
                   .filter(SchedulerHeartbeat.name == _HEARTBEAT_NAME).one_or_none())
            if row is None:
                s.add(SchedulerHeartbeat(name=_HEARTBEAT_NAME, beat_at=datetime.utcnow()))
            else:
                row.beat_at = datetime.utcnow()
        return True
    except Exception:
        return False


def last_scheduler_heartbeat():
    """The most recent scheduler heartbeat as an ISO-8601 string, or None if the
    scheduler has never beaten (e.g. its container isn't running yet)."""
    try:
        with db.session_scope() as s:
            row = (s.query(SchedulerHeartbeat)
                   .filter(SchedulerHeartbeat.name == _HEARTBEAT_NAME).one_or_none())
            if row is None or row.beat_at is None:
                return None
            return row.beat_at.isoformat()
    except Exception:
        return None


# ===========================================================================
# Phase 5 — scheduling, delivery, branding, report storage
# ===========================================================================

# ---- On-disk paths (uploads kept per account; PDFs per account/run) -------
def company_uploads_dir(company_id):
    return os.path.join(UPLOADS_DIR, str(company_id))


def new_upload_batch_dir(company_id):
    """A fresh, timestamp-named directory for ONE upload batch. The timestamp
    name sorts chronologically, so `latest_upload_dir` is a simple max()."""
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
    path = os.path.join(company_uploads_dir(company_id), stamp)
    os.makedirs(path, exist_ok=True)
    return path


def latest_upload_dir(company_id):
    """The newest persisted upload batch directory for an account, or None."""
    base = company_uploads_dir(company_id)
    if not os.path.isdir(base):
        return None
    subs = [os.path.join(base, n) for n in os.listdir(base)
            if os.path.isdir(os.path.join(base, n))]
    if not subs:
        return None
    subs.sort(key=lambda p: os.path.basename(p), reverse=True)
    return subs[0]


def list_files(directory):
    """Absolute paths of the regular files directly inside `directory`, sorted."""
    if not directory or not os.path.isdir(directory):
        return []
    out = [os.path.join(directory, n) for n in os.listdir(directory)
           if os.path.isfile(os.path.join(directory, n))]
    out.sort()
    return out


def company_reports_dir(company_id):
    return os.path.join(REPORTS_DIR, str(company_id))


def report_pdf_path(company_id, run_id):
    """The conventional location of a run's branded PDF (file may not exist yet)."""
    return os.path.join(company_reports_dir(company_id), "%s.pdf" % run_id)


# ---- Branding (logo path / brand colour / WeChat webhook) -----------------
# Stored inside the existing company_profile JSON blob so no schema change is
# needed. Credentials/webhook URLs live here but are NEVER written to logs.
def merge_profile(company_id, updates):
    """Shallow-merge `updates` into the stored profile JSON and return the result."""
    if not company_id:
        return {}
    if not isinstance(updates, dict):
        updates = {}
    with db.session_scope() as s:
        row = s.query(CompanyProfile).filter_by(company_id=company_id).one_or_none()
        data = {}
        if row and (row.profile_json or "").strip():
            try:
                parsed = json.loads(row.profile_json)
                if isinstance(parsed, dict):
                    data = parsed
            except (json.JSONDecodeError, ValueError):
                data = {}
        data.update(updates)
        payload = json.dumps(data, ensure_ascii=False)
        if row is None:
            s.add(CompanyProfile(company_id=company_id, profile_json=payload))
        else:
            row.profile_json = payload
        return data


def get_branding(company_id):
    """The branding fields used by the PDF + WeChat delivery, with defaults."""
    prof = get_profile(company_id)
    return {
        "logo_path": (prof.get("logo_path") or "").strip(),
        "brand_color": (prof.get("brand_color") or DEFAULT_BRAND_COLOR).strip(),
        "wechat_webhook": (prof.get("wechat_webhook") or "").strip(),
    }


# ---- Runs (lookup + history for the 历史报告 page) -------------------------
def _run_to_dict(r):
    return {
        "id": r.id,
        "company_id": r.company_id,
        "goal_text": r.goal_text or "",
        "status": r.status,
        "kind": r.kind or RUN_KIND_REPORT,
        "total_cost_yuan": float(r.total_cost_yuan or 0.0),
        "report_path": r.report_path or "",
        # Phase 11 — provenance: 'cloud' (web/scheduled) or 'device' (synced from a
        # paired desktop daemon). The 历史 UI badges device runs + offers no PDF.
        "origin": r.origin or "cloud",
        "device_id": r.device_id or "",
        "started_at": _iso(r.started_at),
        "finished_at": _iso(r.finished_at),
    }


def get_run(run_id):
    """One run as a plain dict (incl. company_id for ownership checks), or None."""
    if not run_id:
        return None
    with db.session_scope() as s:
        r = s.get(Run, run_id)
        return _run_to_dict(r) if r is not None else None


def list_runs(company_id, limit=50, kind=RUN_KIND_REPORT):
    """An account's runs, newest first, as plain dicts (for the history page).

    Defaults to kind="report" — 历史数据导入 back-fill rows are bookkeeping,
    not reports, and used to show up in 历史报告 as dead has_content=false
    rows (Phase 6 B1). Pass kind=None to list everything."""
    if not company_id:
        return []
    with db.session_scope() as s:
        q = s.query(Run).filter_by(company_id=company_id)
        if kind is not None:
            # `kind` may be one value or an iterable (the 历史 page passes
            # (report, device_run) to show local runs alongside cloud ones).
            if isinstance(kind, (list, tuple, set, frozenset)):
                q = q.filter(Run.kind.in_(list(kind)))
            else:
                q = q.filter(Run.kind == kind)
        rows = q.order_by(Run.id.desc()).limit(int(limit)).all()
        return [_run_to_dict(r) for r in rows]


# ---- Scheduled jobs CRUD ---------------------------------------------------
def _loads_list(text):
    if not text:
        return []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _job_to_dict(j):
    return {
        "id": j.id,
        "company_id": j.company_id,
        "name_cn": j.name_cn,
        "cron_expr": j.cron_expr,
        "industry_override": j.industry_override or "",
        "goal_template": j.goal_template or "",
        "engine_kind": j.engine_kind or "",
        "source_mode": j.source_mode,
        "fixed_file_paths": _loads_list(j.fixed_file_paths),
        "delivery_channels": _loads_list(j.delivery_channels),
        "budget_cny": j.budget_cny,
        "active": bool(j.active),
        "last_run_at": _iso(j.last_run_at),
        "last_status": j.last_status or "",
        "created_at": _iso(j.created_at),
    }


def create_scheduled_job(company_id, name_cn, cron_expr, goal_template="",
                         source_mode=SOURCE_LATEST_UPLOAD, industry_override=None,
                         fixed_file_paths=None, delivery_channels=None,
                         budget_cny=None, active=True, engine_kind=None):
    """Insert a 定时任务 row and return its id (None if no company / no name)."""
    if not company_id:
        return None
    name_cn = (name_cn or "").strip()
    cron_expr = (cron_expr or "").strip()
    if not name_cn or not cron_expr:
        return None
    if source_mode not in SOURCE_MODES:
        source_mode = SOURCE_LATEST_UPLOAD
    with db.session_scope() as s:
        job = ScheduledJob(
            company_id=company_id,
            name_cn=name_cn,
            cron_expr=cron_expr,
            industry_override=(industry_override or "").strip() or None,
            goal_template=goal_template or "",
            engine_kind=(engine_kind or "").strip() or None,
            source_mode=source_mode,
            fixed_file_paths=json.dumps(fixed_file_paths or [], ensure_ascii=False),
            delivery_channels=json.dumps(delivery_channels or [], ensure_ascii=False),
            budget_cny=_coerce_float(budget_cny),
            active=bool(active),
        )
        s.add(job)
        s.flush()
        return job.id


def list_scheduled_jobs(company_id):
    """All 定时任务 for an account, oldest first, as plain dicts."""
    if not company_id:
        return []
    with db.session_scope() as s:
        rows = (s.query(ScheduledJob)
                .filter_by(company_id=company_id)
                .order_by(ScheduledJob.id.asc())
                .all())
        return [_job_to_dict(j) for j in rows]


def get_scheduled_job(job_id):
    """One job as a plain dict, enriched with the owning account's email + company
    name, or None. The scheduler re-reads a job through here at fire time, and the
    executor needs account_email to resolve the customer for billing — so this MUST
    carry the same enrichment as list_active_scheduled_jobs (an inner join, so a job
    whose company row vanished resolves to None and is simply skipped)."""
    if not job_id:
        return None
    with db.session_scope() as s:
        row = (s.query(ScheduledJob, Company.account_email, Company.company_name)
               .join(Company, Company.id == ScheduledJob.company_id)
               .filter(ScheduledJob.id == job_id)
               .one_or_none())
        if row is None:
            return None
        j, email, cname = row
        d = _job_to_dict(j)
        d["account_email"] = email
        d["company_name"] = cname or ""
        return d


def list_active_scheduled_jobs():
    """Every ACTIVE job across all accounts, enriched with the owning account's
    email + name so the scheduler can resolve the customer for billing. The
    scheduler loads these at boot and after any change."""
    with db.session_scope() as s:
        rows = (s.query(ScheduledJob, Company.account_email, Company.company_name)
                .join(Company, Company.id == ScheduledJob.company_id)
                .filter(ScheduledJob.active.is_(True))
                .order_by(ScheduledJob.id.asc())
                .all())
        out = []
        for j, email, cname in rows:
            d = _job_to_dict(j)
            d["account_email"] = email
            d["company_name"] = cname or ""
            out.append(d)
        return out


def update_scheduled_job(company_id, job_id, **fields):
    """Update an account's job in place (ownership enforced via company_id).
    Returns True if the row was found and updated."""
    if not (company_id and job_id):
        return False
    allowed = {"name_cn", "cron_expr", "industry_override", "goal_template",
               "source_mode", "fixed_file_paths", "delivery_channels",
               "budget_cny", "active", "engine_kind"}
    with db.session_scope() as s:
        j = s.query(ScheduledJob).filter_by(id=job_id, company_id=company_id).one_or_none()
        if j is None:
            return False
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("fixed_file_paths", "delivery_channels"):
                setattr(j, k, json.dumps(v or [], ensure_ascii=False))
            elif k == "source_mode":
                j.source_mode = v if v in SOURCE_MODES else j.source_mode
            elif k == "industry_override":
                j.industry_override = (v or "").strip() or None
            elif k == "budget_cny":
                j.budget_cny = _coerce_float(v)
            elif k == "active":
                j.active = bool(v)
            else:
                setattr(j, k, v)
        return True


def delete_scheduled_job(company_id, job_id):
    """Delete an account's job (and, by cascade, its delivery rows). Returns True
    if a row was removed."""
    if not (company_id and job_id):
        return False
    with db.session_scope() as s:
        j = s.query(ScheduledJob).filter_by(id=job_id, company_id=company_id).one_or_none()
        if j is None:
            return False
        s.delete(j)
        return True


def mark_job_run(job_id, status):
    """Stamp a job's last_run_at=now and last_status after a fire (or a skip)."""
    if not job_id:
        return
    with db.session_scope() as s:
        j = s.get(ScheduledJob, job_id)
        if j is not None:
            j.last_run_at = datetime.utcnow()
            j.last_status = (status or "")[:64]


# ---- Deliveries (one row per channel attempt; never stores creds/URLs) -----
def _delivery_to_dict(d):
    return {
        "id": d.id,
        "job_id": d.job_id,
        "run_id": d.run_id,
        "channel": d.channel,
        "status": d.status,
        "error": d.error or "",
        "created_at": _iso(d.created_at),
    }


def record_delivery(channel, status, job_id=None, run_id=None, error=None):
    """Log a single delivery attempt and return its id. `error` is a short,
    human-readable reason — callers MUST NOT pass webhook URLs or SMTP creds."""
    if status not in DELIVERY_STATUSES:
        status = DELIVERY_FAILED
    with db.session_scope() as s:
        d = Delivery(
            job_id=job_id,
            run_id=run_id,
            channel=str(channel or "")[:32] or "system",
            status=status,
            error=(error or None),
        )
        s.add(d)
        s.flush()
        return d.id


def list_deliveries(job_id=None, run_id=None, company_id=None, limit=200):
    """Delivery rows, newest first, filtered by job / run / owning company."""
    with db.session_scope() as s:
        q = s.query(Delivery)
        if job_id is not None:
            q = q.filter(Delivery.job_id == job_id)
        if run_id is not None:
            q = q.filter(Delivery.run_id == run_id)
        if company_id is not None:
            job_ids = [r[0] for r in s.query(ScheduledJob.id)
                       .filter(ScheduledJob.company_id == company_id).all()]
            run_ids = [r[0] for r in s.query(Run.id)
                       .filter(Run.company_id == company_id).all()]
            from sqlalchemy import or_
            q = q.filter(or_(Delivery.job_id.in_(job_ids or [-1]),
                             Delivery.run_id.in_(run_ids or [-1])))
        rows = q.order_by(Delivery.id.desc()).limit(int(limit)).all()
        return [_delivery_to_dict(d) for d in rows]


# ---- Report-section feedback (Phase 6 feedback loop) ----------------------
def _feedback_to_dict(f, account_email=None):
    return {
        "id": f.id,
        "company_id": f.company_id,
        "account_email": account_email,
        "run_id": f.run_id,
        "section_title": f.section_title or "",
        "vote": f.vote,
        "comment": f.comment or "",
        "created_at": _iso(f.created_at),
    }


def record_feedback(company_id, run_id, section_title, vote, comment=None):
    """Record one 👍/👎 (+ optional comment) on a report section. Returns the new
    row id, or None when the company id / vote is invalid."""
    if not company_id or vote not in VOTE_VALUES:
        return None
    with db.session_scope() as s:
        row = Feedback(
            company_id=company_id,
            run_id=run_id if run_id else None,
            section_title=_short(section_title, 255) or "",
            vote=vote,
            comment=(comment or "").strip() or None,
        )
        s.add(row)
        s.flush()
        return row.id


def get_feedback(feedback_id):
    """One feedback row as a plain dict (incl. company_id for ownership checks),
    or None."""
    if not feedback_id:
        return None
    with db.session_scope() as s:
        f = s.get(Feedback, feedback_id)
        return _feedback_to_dict(f) if f is not None else None


def list_feedback(company_id=None, vote=None, limit=1000):
    """Feedback rows, newest first, optionally filtered by owning company and/or
    vote. Each row carries the company's account_email so the founder dashboard
    can show who said what across every company."""
    vote = vote if vote in VOTE_VALUES else None
    with db.session_scope() as s:
        q = s.query(Feedback, Company.account_email).outerjoin(
            Company, Feedback.company_id == Company.id)
        if company_id is not None:
            q = q.filter(Feedback.company_id == company_id)
        if vote is not None:
            q = q.filter(Feedback.vote == vote)
        rows = q.order_by(Feedback.id.desc()).limit(int(limit)).all()
        return [_feedback_to_dict(f, email) for (f, email) in rows]


def known_numbers_for_company(company_id):
    """Every numeric metric value known for this company, as a set of normalized
    strings — the auditor's "system-verified numbers" set. Includes a few common
    renderings (raw, integer, comma-grouped) so the cheap auditor matches figures
    however the report formats them."""
    out = set()
    if not company_id:
        return out
    with db.session_scope() as s:
        rows = (s.query(Metric.value)
                .filter(Metric.company_id == company_id, Metric.value.isnot(None))
                .all())
    for (v,) in rows:
        out.update(_number_variants(v))
    return out


def _number_variants(value):
    """A handful of string renderings of one numeric value, so a textual match
    against report prose is forgiving of formatting (1234567 / 1,234,567 / 1234567.0)."""
    variants = set()
    try:
        f = float(value)
    except (TypeError, ValueError):
        s = str(value).strip()
        return {s} if s else set()
    variants.add(repr(f))
    variants.add(("%f" % f).rstrip("0").rstrip("."))
    if f == int(f):
        i = int(f)
        variants.add(str(i))
        variants.add("{:,}".format(i))
    else:
        variants.add(str(f))
    return {v for v in variants if v}


def _iso(dt):
    return dt.isoformat(sep=" ", timespec="seconds") if dt else None


# ===========================================================================
# Phase 8 — phone verification codes, audit trail, platform spend + breaker
# ===========================================================================
# These are the low-level DB primitives. The POLICY around platform spend (caps,
# tripping the breaker, firing alerts) lives in platform_guard.py; the SMS send +
# code generation live in sms.py. Everything here stores only HASHES of codes and
# never persists a secret value.

_BREAKER_NAME = "platform"


def _hash_code(code):
    """SHA-256 hex of a verification code. We persist ONLY this — never the code."""
    return hashlib.sha256(str(code or "").encode("utf-8")).hexdigest()


def hour_bucket(when=None):
    """The "YYYY-MM-DDTHH" UTC bucket key for `when` (now if None)."""
    return (when or datetime.utcnow()).strftime("%Y-%m-%dT%H")


# ---- Phone verification codes ---------------------------------------------
def count_recent_phone_codes(phone, within_secs=3600):
    """How many codes were issued to `phone` within the last `within_secs` — the
    per-phone send-rate gate (≤ SMS_MAX_SENDS_PER_HOUR per hour)."""
    if not phone:
        return 0
    cutoff = datetime.utcnow() - timedelta(seconds=int(within_secs))
    with db.session_scope() as s:
        return int(s.query(PhoneCode.id)
                   .filter(PhoneCode.phone == phone,
                           PhoneCode.created_at >= cutoff).count())


def issue_phone_code(phone, purpose, code_plain, ttl_secs=None, max_per_hour=None):
    """Rate-check then persist a new code (hash only) for `phone`/`purpose`.

    Returns {"ok": bool, "reason": str|None, "code_id": int|None}. Does NOT send
    the SMS — the caller does that via sms.get_provider() with the plaintext, which
    is never stored. `ok=False` with reason "发送过于频繁" when the hourly cap is hit."""
    if purpose not in CODE_PURPOSES:
        return {"ok": False, "reason": "无效的验证类型", "code_id": None}
    ttl = int(ttl_secs if ttl_secs is not None else config.SMS_CODE_TTL_SECS)
    cap = int(max_per_hour if max_per_hour is not None else config.SMS_MAX_SENDS_PER_HOUR)
    if count_recent_phone_codes(phone, 3600) >= cap:
        return {"ok": False, "reason": "发送过于频繁，请稍后再试", "code_id": None}
    now = datetime.utcnow()
    with db.session_scope() as s:
        row = PhoneCode(
            phone=phone,
            code_hash=_hash_code(code_plain),
            purpose=purpose,
            attempts=0,
            consumed=False,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl),
        )
        s.add(row)
        s.flush()
        return {"ok": True, "reason": None, "code_id": row.id}


def verify_phone_code(phone, purpose, code_plain, max_attempts=None):
    """Check `code_plain` against the newest still-valid code for `phone`/`purpose`.

    The whole fetch-compare-increment happens in ONE transaction so concurrent
    guesses can't race past the attempt cap. Returns {"ok": bool, "reason": str}.
    On a correct code the row is consumed (single-use). On the (max_attempts)-th
    wrong guess the row is burned, so a further attempt sees no valid code."""
    cap = int(max_attempts if max_attempts is not None else config.SMS_MAX_VERIFY_ATTEMPTS)
    now = datetime.utcnow()
    with db.session_scope() as s:
        row = (s.query(PhoneCode)
               .filter(PhoneCode.phone == phone,
                       PhoneCode.purpose == purpose,
                       PhoneCode.consumed.is_(False),
                       PhoneCode.expires_at > now)
               .order_by(PhoneCode.id.desc())
               .first())
        if row is None:
            return {"ok": False, "reason": "验证码无效或已过期"}
        row.attempts = (row.attempts or 0) + 1
        if _hash_code(code_plain) == row.code_hash:
            row.consumed = True
            return {"ok": True, "reason": None}
        if row.attempts >= cap:
            row.consumed = True          # burn the code after too many wrong tries
            return {"ok": False, "reason": "验证码错误次数过多，请重新获取"}
        return {"ok": False, "reason": "验证码错误"}


# ---- Security audit trail --------------------------------------------------
def write_audit(account_id, action, ip=None, detail=None):
    """Append one audit row (best-effort; never raises). `detail` must be a short,
    scrubbed note — NEVER a password, API key, or webhook URL. Returns the id or
    None. An invalid action is dropped (returns None) rather than silently mislabeled."""
    if action not in AUDIT_ACTIONS:
        return None
    try:
        with db.session_scope() as s:
            row = AuditLog(
                account_id=(str(account_id)[:32] if account_id else None),
                action=action,
                ip=(str(ip)[:64] if ip else None),
                detail=(str(detail)[:255] if detail else None),
            )
            s.add(row)
            s.flush()
            return row.id
    except Exception:
        return None


def list_audit(account_id=None, action=None, limit=200):
    """Audit rows, newest first, optionally filtered by account and/or action."""
    with db.session_scope() as s:
        q = s.query(AuditLog)
        if account_id is not None:
            q = q.filter(AuditLog.account_id == str(account_id))
        if action is not None:
            q = q.filter(AuditLog.action == action)
        rows = q.order_by(AuditLog.id.desc()).limit(int(limit)).all()
        return [{
            "id": r.id, "account_id": r.account_id, "action": r.action,
            "ip": r.ip, "detail": r.detail, "created_at": _iso(r.created_at),
        } for r in rows]


# ---- Platform spend (the circuit-breaker's cost meter) --------------------
def record_platform_spend(provider, yuan, account_id=None, when=None):
    """Append one upstream-spend row (best-effort; never raises). Called from the
    universal billing chokepoint for every billed upstream call. Returns id or None."""
    try:
        amount = float(yuan or 0)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    when = when or datetime.utcnow()
    try:
        with db.session_scope() as s:
            row = PlatformSpend(
                hour_bucket=hour_bucket(when),
                provider=str(provider or "unknown")[:32],
                account_id=(str(account_id)[:32] if account_id else None),
                yuan=amount,
                created_at=when,
            )
            s.add(row)
            s.flush()
            return row.id
    except Exception:
        return None


def sum_platform_spend(since, account_id=None):
    """SUM of platform_spend.yuan for rows created at/after `since` (a datetime),
    optionally restricted to one account. Returns a float (0.0 when none)."""
    from sqlalchemy import func
    with db.session_scope() as s:
        q = s.query(func.coalesce(func.sum(PlatformSpend.yuan), 0))
        q = q.filter(PlatformSpend.created_at >= since)
        if account_id is not None:
            q = q.filter(PlatformSpend.account_id == str(account_id))
        return float(q.scalar() or 0)


# ---- Circuit-breaker latch (single reused row) ----------------------------
def _breaker_dict(row):
    return {
        "tripped": bool(row.tripped),
        "scope": row.scope,
        "reason": row.reason,
        "tripped_at": _iso(row.tripped_at),
        "cleared_at": _iso(row.cleared_at),
        "updated_at": _iso(row.updated_at),
    }


def _get_or_create_breaker(session):
    row = (session.query(PlatformBreaker)
           .filter(PlatformBreaker.name == _BREAKER_NAME).one_or_none())
    if row is None:
        row = PlatformBreaker(name=_BREAKER_NAME, tripped=False,
                              updated_at=datetime.utcnow())
        session.add(row)
        session.flush()
    return row


def get_breaker():
    """The breaker latch as a plain dict (created in the OFF state on first read)."""
    with db.session_scope() as s:
        return _breaker_dict(_get_or_create_breaker(s))


def trip_breaker(scope, reason):
    """Latch the breaker tripped (idempotent — keeps the first trip's timestamp if
    already tripped). Returns the breaker dict."""
    now = datetime.utcnow()
    with db.session_scope() as s:
        row = _get_or_create_breaker(s)
        if not row.tripped:
            row.tripped = True
            row.tripped_at = now
        row.scope = (str(scope)[:16] if scope else row.scope)
        row.reason = (str(reason)[:255] if reason else row.reason)
        row.updated_at = now
        return _breaker_dict(row)


def reset_breaker():
    """Clear the breaker and stamp `cleared_at`=now. Rolling spend sums then count
    only rows created AFTER this instant, so service genuinely resumes and the
    breaker re-trips only on NEW over-cap spend. Returns the breaker dict."""
    now = datetime.utcnow()
    with db.session_scope() as s:
        row = _get_or_create_breaker(s)
        row.tripped = False
        row.scope = None
        row.reason = None
        row.cleared_at = now
        row.updated_at = now
        return _breaker_dict(row)


def breaker_cleared_at():
    """The breaker's last-reset instant as a raw datetime (or None). platform_guard
    uses it to lower-bound the rolling spend window, so a reset zeroes the counter."""
    with db.session_scope() as s:
        return _get_or_create_breaker(s).cleared_at


# ===========================================================================
# Phase 9 — orders, subscriptions, fapiao, reconciliation
# ===========================================================================
# The DB primitives for the money path. POLICY lives elsewhere: provider crypto
# in payments.py, the renewal tick in payments.daily_subscription_tick, the bill
# comparison in reconciliation.py, the routes in app.py. The one invariant THIS
# layer owns is the idempotent created→paid transition: mark_order_paid is a
# single transaction that verifies the amount, flips the status, and credits the
# wallet / activates the subscription — so a replayed notify can never double-credit.

_CENT2 = Decimal("0.01")


def _money2(x):
    """A fen-quantized Decimal, or None on junk (callers decide how to fail)."""
    try:
        return Decimal(str(x)).quantize(_CENT2)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _parse_dt(s):
    """A datetime from our _iso() format ("YYYY-MM-DD HH:MM:SS"), or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


# ---- Orders -----------------------------------------------------------------
def _order_to_dict(o):
    return {
        "id": o.id,
        "order_no": o.order_no,
        "account_id": o.account_id,
        "type": o.type,
        "plan_code": o.plan_code,
        "amount_yuan": float(o.amount_yuan or 0),
        "provider": o.provider,
        "provider_txn_id": o.provider_txn_id,
        "status": o.status,
        "subject": o.subject,
        "refunded_yuan": float(o.refunded_yuan or 0),
        "created_at": _iso(o.created_at),
        "paid_at": _iso(o.paid_at),
    }


def new_order_no():
    """A fresh merchant order number (out_trade_no): sortable timestamp + random
    suffix, well under every provider's 64-char limit."""
    return "P%s%s" % (datetime.utcnow().strftime("%Y%m%d%H%M%S"),
                      secrets.token_hex(5))


def create_order(account_id, otype, amount_yuan, provider, plan_code=None,
                 subject=None):
    """Open an order row (status=created). Returns the order dict.
    Raises ValueError on a bad type or a non-positive amount."""
    if otype not in ORDER_TYPES:
        raise ValueError("Unknown order type: %r" % (otype,))
    amt = _money2(amount_yuan)
    if amt is None or amt <= 0:
        raise ValueError("Order amount must be a positive ¥ number.")
    with db.session_scope() as s:
        row = Order(
            order_no=new_order_no(),
            account_id=str(account_id),
            type=otype,
            plan_code=(plan_code or None),
            amount_yuan=amt,
            provider=str(provider or "mock")[:32],
            status=ORDER_CREATED,
            subject=(str(subject)[:255] if subject else None),
            refunded_yuan=Decimal("0.00"),
        )
        s.add(row)
        s.flush()
        return _order_to_dict(row)


def get_order(order_no):
    """The order dict for a merchant order number, or None."""
    if not order_no:
        return None
    with db.session_scope() as s:
        o = s.query(Order).filter(Order.order_no == str(order_no).strip()).one_or_none()
        return _order_to_dict(o) if o else None


def list_orders(account_id=None, status=None, otype=None, limit=100):
    """Orders newest-first, optionally filtered."""
    with db.session_scope() as s:
        q = s.query(Order)
        if account_id is not None:
            q = q.filter(Order.account_id == str(account_id))
        if status in ORDER_STATUSES:
            q = q.filter(Order.status == status)
        if otype in ORDER_TYPES:
            q = q.filter(Order.type == otype)
        rows = q.order_by(Order.id.desc()).limit(int(limit)).all()
        return [_order_to_dict(o) for o in rows]


def list_paid_orders_between(provider, start, end):
    """Orders for `provider` paid in [start, end) — the reconciliation window."""
    with db.session_scope() as s:
        rows = (s.query(Order)
                .filter(Order.provider == provider,
                        Order.paid_at.isnot(None),
                        Order.paid_at >= start,
                        Order.paid_at < end)
                .order_by(Order.id.asc()).all())
        return [_order_to_dict(o) for o in rows]


def mark_order_failed(order_no):
    """created → failed (the provider refused to open the trade). Any other state
    is left untouched. Returns the order dict or None."""
    with db.session_scope() as s:
        o = (s.query(Order).filter(Order.order_no == str(order_no or "").strip())
             .with_for_update().one_or_none())
        if o is None:
            return None
        if o.status == ORDER_CREATED:
            o.status = ORDER_FAILED
        return _order_to_dict(o)


def mark_order_paid(order_no, paid_amount, provider_txn_id=None):
    """THE idempotent created→paid transition — Phase 9's money chokepoint.

    ONE transaction: lock the order row, no-op successfully if it is already paid
    (a REPLAYED notify must change nothing), verify the provider-VERIFIED paid
    amount equals the stored order amount to the fen (the client is never
    trusted), flip the status, then credit the wallet (top-up) or activate/extend
    the subscription — all or nothing.

    Returns {"ok": bool, "replay": bool, "reason": str|None, "order": dict|None};
    ok=False reasons: amount_invalid | unknown_order | amount_mismatch."""
    paid = _money2(paid_amount)
    if paid is None or paid <= 0:
        return {"ok": False, "replay": False, "reason": "amount_invalid", "order": None}
    now = datetime.utcnow()
    with db.session_scope() as s:
        o = (s.query(Order).filter(Order.order_no == str(order_no or "").strip())
             .with_for_update().one_or_none())
        if o is None:
            return {"ok": False, "replay": False, "reason": "unknown_order", "order": None}
        if o.status in (ORDER_PAID, ORDER_REFUNDED):
            # Replayed notify (delivered N times) → successful no-op, credit untouched.
            return {"ok": True, "replay": True, "reason": None, "order": _order_to_dict(o)}
        if paid != Decimal(o.amount_yuan).quantize(_CENT2):
            return {"ok": False, "replay": False, "reason": "amount_mismatch",
                    "order": _order_to_dict(o)}
        # created → paid. (failed → paid is allowed too: if our create_order
        # bookkeeping failed but the provider-side trade went through and the user
        # actually paid, the verified money must land, not silently vanish.)
        o.status = ORDER_PAID
        o.paid_at = now
        if provider_txn_id:
            o.provider_txn_id = str(provider_txn_id)[:128]
        if o.type == ORDER_TOPUP:
            acc = (s.query(Account).filter(Account.id == o.account_id)
                   .with_for_update().one_or_none())
            if acc is not None and acc.balance_yuan is not None:
                acc.balance_yuan = (Decimal(acc.balance_yuan) + paid).quantize(_CENT2)
        elif o.type == ORDER_SINGLE_REPORT:
            # EXPLICIT branch (redesign B4): falling through to the
            # subscription else would call _activate_subscription_locked with
            # plan_code=None, which defaults to "basic" — a ¥29 order must
            # never grant a ¥199 subscription. It grants ONE report credit.
            acc = (s.query(Account).filter(Account.id == o.account_id)
                   .with_for_update().one_or_none())
            if acc is not None:
                acc.report_credits = int(acc.report_credits or 0) + 1
        else:
            _activate_subscription_locked(s, o.account_id, o.plan_code, now)
        return {"ok": True, "replay": False, "reason": None, "order": _order_to_dict(o)}


def deduct_report_credit(account_id):
    """Remove ONE 单次报告 credit — the refund path (redesign B4), mirroring
    deduct_balance's contract: raises ValueError with a clear Chinese message
    when no unconsumed credit remains, so the caller REFUSES the refund instead
    of going negative. Returns the remaining credit count, or None if the
    account is unknown."""
    with db.session_scope() as s:
        acc = (s.query(Account).filter(Account.id == str(account_id))
               .with_for_update().one_or_none())
        if acc is None:
            return None
        credits = int(acc.report_credits or 0)
        if credits < 1:
            raise ValueError("该单次报告额度已被消费（剩余 0 份），无法退款。")
        acc.report_credits = credits - 1
        return credits - 1


def consume_report_credit_if_over_quota(run_id):
    """B4/C4 — make 单次报告 credits one-shot. Called when a run finishes: if
    the run COUNTS toward the quota meter (kind='report', not failed, not
    already credit-funded) and the quota-metered total now EXCEEDS the plan's
    base quota, this run rode on a purchased credit — consume exactly one AND
    stamp the run credit_funded, which removes it from the quota counters.
    Without the stamp the consumed credit's run would count against the base
    quota forever, so a SECOND ¥29 purchase on the lifetime trial bought
    nothing (used=3 ≥ 2+1).

    Runs that fit inside the base quota never touch credits, so a free user
    with the 2-lifetime trial + 1 credit gets 3 runs total, and a failed run
    never eats the ¥29. Two runs finishing in the same instant: the first one
    marks+consumes and the metered count drops back to the quota, so the
    second sees no overrun — credits can't be over-consumed (the account row
    is locked for the decrement). Returns True when a credit was consumed."""
    if not run_id:
        return False
    with db.session_scope() as s:
        run = s.get(Run, run_id)
        # 对账 (RUN_KIND_RECON) folds into the report quota/credits, so it can
        # consume a 单次 credit too — QUOTA_RUN_KINDS covers both kinds.
        if (run is None or run.kind not in QUOTA_RUN_KINDS
                or run.status == RUN_FAILED or run.credit_funded):
            return False
        company_id = run.company_id
        comp = s.get(Company, company_id)
        email = (comp.account_email or "").strip().lower() if comp else None
    if not email:
        return False
    with db.session_scope() as s:
        acc = s.query(Account).filter(Account.email == email).one_or_none()
        account_id = acc.id if acc else None
    if not account_id:
        return False

    plan_code, _sub, _locked = effective_plan(account_id)
    p = config.plan(plan_code)
    quota = int(p.get("monthly_report_quota") or 0)
    if not quota:
        return False
    used = (count_runs_total(company_id) if p.get("lifetime_quota")
            else count_runs_this_month(company_id))
    if used <= quota:
        return False  # within the base plan quota — credits untouched
    with db.session_scope() as s:
        acc = (s.query(Account).filter(Account.id == account_id)
               .with_for_update().one_or_none())
        if acc is None or int(acc.report_credits or 0) < 1:
            return False
        acc.report_credits = int(acc.report_credits) - 1
        run = s.get(Run, run_id)
        if run is not None:
            run.credit_funded = True
        return True


def mark_order_refunded(order_no, amount_yuan):
    """Record a (full or partial) refund on a PAID order: accumulate
    refunded_yuan; the status flips to refunded only when fully refunded. The
    remaining-headroom check happens under the row lock. Returns
    {"ok", "reason", "order"} — reasons: amount_invalid | unknown_order |
    not_paid | over_refund."""
    amt = _money2(amount_yuan)
    if amt is None or amt <= 0:
        return {"ok": False, "reason": "amount_invalid", "order": None}
    with db.session_scope() as s:
        o = (s.query(Order).filter(Order.order_no == str(order_no or "").strip())
             .with_for_update().one_or_none())
        if o is None:
            return {"ok": False, "reason": "unknown_order", "order": None}
        if o.status not in (ORDER_PAID, ORDER_REFUNDED):
            return {"ok": False, "reason": "not_paid", "order": _order_to_dict(o)}
        total = Decimal(o.amount_yuan).quantize(_CENT2)
        done = Decimal(o.refunded_yuan or 0).quantize(_CENT2)
        if amt > total - done:
            return {"ok": False, "reason": "over_refund", "order": _order_to_dict(o)}
        o.refunded_yuan = done + amt
        if o.refunded_yuan >= total:
            o.status = ORDER_REFUNDED
        return {"ok": True, "reason": None, "order": _order_to_dict(o)}


# ---- Subscriptions ----------------------------------------------------------
def _sub_to_dict(x):
    return {
        "id": x.id,
        "account_id": x.account_id,
        "plan_code": x.plan_code,
        "status": x.status,
        "current_period_start": _iso(x.current_period_start),
        "current_period_end": _iso(x.current_period_end),
        "cancel_at_period_end": bool(x.cancel_at_period_end),
        "renewal_order_no": x.renewal_order_no,
        "created_at": _iso(x.created_at),
        "updated_at": _iso(x.updated_at),
    }


def _activate_subscription_locked(s, account_id, plan_code, now):
    """Inside mark_order_paid's transaction: activate or extend the subscription.

    Same plan & still paid-through → the new period CHAINS onto the current end
    (paying the renewal early never costs days). Lapsed, or a plan change → the
    new period starts now (no proration). Payment always clears past_due, the
    open renewal pointer, and any pending cancel-at-period-end."""
    plan_code = (plan_code or "basic").strip().lower()
    period = timedelta(days=config.SUBSCRIPTION_PERIOD_DAYS)
    sub = (s.query(Subscription).filter(Subscription.account_id == str(account_id))
           .with_for_update().one_or_none())
    if sub is None:
        s.add(Subscription(account_id=str(account_id), plan_code=plan_code,
                           status=SUB_ACTIVE, current_period_start=now,
                           current_period_end=now + period,
                           cancel_at_period_end=False,
                           created_at=now, updated_at=now))
        return
    if (sub.plan_code == plan_code and sub.status != SUB_CANCELED
            and sub.current_period_end and sub.current_period_end > now):
        start = sub.current_period_end
    else:
        start = now
    sub.plan_code = plan_code
    sub.status = SUB_ACTIVE
    sub.current_period_start = start
    sub.current_period_end = start + period
    sub.cancel_at_period_end = False
    sub.renewal_order_no = None
    sub.updated_at = now


def get_subscription(account_id):
    """The account's subscription dict, or None (None ⇒ the free plan)."""
    if not account_id:
        return None
    with db.session_scope() as s:
        sub = (s.query(Subscription)
               .filter(Subscription.account_id == str(account_id)).one_or_none())
        return _sub_to_dict(sub) if sub else None


def effective_plan(account_id, now=None):
    """(plan_code, sub_dict|None, locked) — what the plan-gating middleware reads.

    Computed from TIMESTAMPS, not just the stored status, so the gate is correct
    even before the daily tick has persisted a transition:
      no/canceled subscription              → ("free", sub|None, False)
      paid through period end (+grace)      → (plan,  sub, False)
      past grace, inside the cancel window  → (plan,  sub, True)   # past_due lock
      past the past-due cancel window       → ("free", sub, False)
    """
    now = now or datetime.utcnow()
    sub = get_subscription(account_id)
    if not sub or sub["status"] == SUB_CANCELED:
        return "free", sub, False
    end = _parse_dt(sub["current_period_end"])
    if end is None:
        return "free", sub, False
    grace = timedelta(days=config.subscription_grace_days())
    if now <= end + grace:
        if sub["cancel_at_period_end"] and now > end:
            return "free", sub, False     # user chose not to renew — no lock, just free
        return sub["plan_code"], sub, False
    if sub["cancel_at_period_end"]:
        return "free", sub, False
    cancel_after = timedelta(days=config.subscription_past_due_cancel_days())
    if now <= end + grace + cancel_after:
        return sub["plan_code"], sub, True
    return "free", sub, False


def list_subscriptions_for_tick():
    """Every not-yet-canceled subscription — the daily billing tick's worklist."""
    with db.session_scope() as s:
        rows = (s.query(Subscription)
                .filter(Subscription.status != SUB_CANCELED)
                .order_by(Subscription.id.asc()).all())
        return [_sub_to_dict(x) for x in rows]


def set_subscription_status(account_id, status):
    """Persist a tick-driven transition (past_due / canceled). Returns dict|None."""
    if status not in SUB_STATUSES:
        return None
    with db.session_scope() as s:
        sub = (s.query(Subscription).filter(Subscription.account_id == str(account_id))
               .with_for_update().one_or_none())
        if sub is None:
            return None
        sub.status = status
        sub.updated_at = datetime.utcnow()
        return _sub_to_dict(sub)


def set_renewal_order(account_id, order_no):
    """Point the subscription at its open renewal order (tick dedup)."""
    with db.session_scope() as s:
        sub = (s.query(Subscription).filter(Subscription.account_id == str(account_id))
               .with_for_update().one_or_none())
        if sub is None:
            return None
        sub.renewal_order_no = (str(order_no)[:64] if order_no else None)
        sub.updated_at = datetime.utcnow()
        return _sub_to_dict(sub)


def set_cancel_at_period_end(account_id, flag):
    """The user's 取消续订 / 恢复续订 toggle. Access continues to period end;
    the tick then lets the period lapse instead of issuing a renewal order."""
    with db.session_scope() as s:
        sub = (s.query(Subscription).filter(Subscription.account_id == str(account_id))
               .with_for_update().one_or_none())
        if sub is None:
            return None
        sub.cancel_at_period_end = bool(flag)
        sub.updated_at = datetime.utcnow()
        return _sub_to_dict(sub)


def count_runs_this_month(company_id, now=None):
    """How many quota-metered REPORT runs this company started this calendar
    month — the plan quota meter. Failed runs don't count, kind="import"
    back-fills don't count (Phase 6 B1), and neither do credit_funded runs
    (Phase 7 B4 — a consumed 单次报告 credit paid for those; counting them
    against the base quota would make repeat purchases worthless)."""
    if not company_id:
        return 0
    now = now or datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with db.session_scope() as s:
        return int(s.query(Run.id)
                   .filter(Run.company_id == company_id,
                           Run.kind.in_(QUOTA_RUN_KINDS),
                           Run.credit_funded.is_(False),
                           Run.started_at >= month_start,
                           Run.status != RUN_FAILED).count())


def count_runs_total(company_id):
    """How many quota-metered REPORT runs this company has EVER started — the
    meter for plans whose quota is a lifetime total (the 免费体验 trial,
    redesign B3). Failed runs, kind="import" back-fills, and credit_funded
    runs don't count, same as the monthly meter."""
    if not company_id:
        return 0
    with db.session_scope() as s:
        return int(s.query(Run.id)
                   .filter(Run.company_id == company_id,
                           Run.kind.in_(QUOTA_RUN_KINDS),
                           Run.credit_funded.is_(False),
                           Run.status != RUN_FAILED).count())


def account_contact(account_id):
    """{"email", "name"} for an account (renewal notices), or None."""
    if not account_id:
        return None
    with db.session_scope() as s:
        acc = s.get(Account, str(account_id))
        if acc is None:
            return None
        return {"email": acc.email, "name": acc.name or "客户"}


def company_id_for_email(email):
    """The companies.id for an account email — QUERY ONLY (no create), or None.
    Used where a missing brain row must not be conjured (e.g. renewal notices)."""
    norm = (email or "").strip().lower()
    if not norm:
        return None
    with db.session_scope() as s:
        row = (s.query(Company.id)
               .filter(Company.account_email == norm).one_or_none())
        return row[0] if row else None


# ---- Fapiao (发票) requests --------------------------------------------------
def _fapiao_to_dict(f, order=None, account_email=None):
    d = {
        "id": f.id,
        "account_id": f.account_id,
        "order_id": f.order_id,
        "title": f.title,
        "tax_no": f.tax_no,
        "email": f.email,
        "status": f.status,
        "note": f.note,
        "created_at": _iso(f.created_at),
        "updated_at": _iso(f.updated_at),
    }
    if order is not None:
        d["order_no"] = order.order_no
        d["amount_yuan"] = float(order.amount_yuan or 0)
    if account_email is not None:
        d["account_email"] = account_email
    return d


def create_fapiao_request(account_id, order_id, title, tax_no, email=None):
    """Open a 发票 request against one paid order. ONE live request per order:
    raises ValueError if a non-rejected request already exists (a rejected one
    may be re-submitted with corrected 抬头/税号)."""
    now = datetime.utcnow()
    with db.session_scope() as s:
        existing = (s.query(FapiaoRequest.id)
                    .filter(FapiaoRequest.order_id == int(order_id),
                            FapiaoRequest.status != FAPIAO_REJECTED).first())
        if existing:
            raise ValueError("该订单已申请过发票，请勿重复提交。")
        row = FapiaoRequest(
            account_id=str(account_id),
            order_id=int(order_id),
            title=str(title)[:255],
            tax_no=str(tax_no)[:32],
            email=(str(email)[:320] if email else None),
            status=FAPIAO_REQUESTED,
            created_at=now,
            updated_at=now,
        )
        s.add(row)
        s.flush()
        return _fapiao_to_dict(row)


def get_fapiao_request(req_id):
    with db.session_scope() as s:
        f = s.get(FapiaoRequest, int(req_id))
        if f is None:
            return None
        o = s.get(Order, f.order_id)
        acc = s.get(Account, f.account_id)
        return _fapiao_to_dict(f, order=o, account_email=(acc.email if acc else None))


def list_fapiao_requests(account_id=None, status=None, limit=500):
    """Fapiao requests newest-first, joined with their order (order_no, ¥) and
    account email — the bookkeeper's worklist (/admin/fapiao + CSV export)."""
    with db.session_scope() as s:
        q = (s.query(FapiaoRequest, Order, Account)
             .join(Order, FapiaoRequest.order_id == Order.id)
             .outerjoin(Account, Account.id == FapiaoRequest.account_id))
        if account_id is not None:
            q = q.filter(FapiaoRequest.account_id == str(account_id))
        if status in FAPIAO_STATUSES:
            q = q.filter(FapiaoRequest.status == status)
        rows = q.order_by(FapiaoRequest.id.desc()).limit(int(limit)).all()
        return [_fapiao_to_dict(f, order=o, account_email=(a.email if a else None))
                for f, o, a in rows]


def set_fapiao_status(req_id, status, note=None):
    """The bookkeeper's action: requested → issued / rejected (+ optional note).
    Returns the updated dict, or None for an unknown id / bad status."""
    if status not in FAPIAO_STATUSES:
        return None
    with db.session_scope() as s:
        f = s.get(FapiaoRequest, int(req_id))
        if f is None:
            return None
        f.status = status
        if note is not None:
            f.note = str(note)[:255] or None
        f.updated_at = datetime.utcnow()
        o = s.get(Order, f.order_id)
        acc = s.get(Account, f.account_id)
        return _fapiao_to_dict(f, order=o, account_email=(acc.email if acc else None))


# ---- Reconciliation reports --------------------------------------------------
def save_recon_report(provider, bill_date, bill_rows, matched, mismatches):
    """Upsert the (provider, bill_date) reconciliation result. `mismatches` is a
    list of {"kind", "order_no", "detail"} dicts; stored as JSON."""
    payload = json.dumps(list(mismatches or []), ensure_ascii=False)
    with db.session_scope() as s:
        row = (s.query(ReconReport)
               .filter(ReconReport.provider == provider,
                       ReconReport.bill_date == bill_date)
               .with_for_update().one_or_none())
        if row is None:
            row = ReconReport(provider=provider, bill_date=bill_date)
            s.add(row)
        row.bill_rows = int(bill_rows or 0)
        row.matched = int(matched or 0)
        row.mismatch_count = len(mismatches or [])
        row.mismatches_json = payload
        row.created_at = datetime.utcnow()
        s.flush()
        return _recon_to_dict(row)


def _recon_to_dict(r):
    try:
        mismatches = json.loads(r.mismatches_json) if r.mismatches_json else []
    except ValueError:
        mismatches = []
    return {
        "id": r.id, "provider": r.provider, "bill_date": r.bill_date,
        "bill_rows": int(r.bill_rows or 0), "matched": int(r.matched or 0),
        "mismatch_count": int(r.mismatch_count or 0), "mismatches": mismatches,
        "created_at": _iso(r.created_at),
    }


def list_recon_reports(limit=60):
    """Recent reconciliation results, newest bill-date first."""
    with db.session_scope() as s:
        rows = (s.query(ReconReport)
                .order_by(ReconReport.bill_date.desc(), ReconReport.id.desc())
                .limit(int(limit)).all())
        return [_recon_to_dict(r) for r in rows]


# ===========================================================================
# 对账 / 平台结算核对 — CUSTOMER-scoped settlement reconciliation persistence.
# Distinct from the recon_reports helpers above (our OWN internal billing recon).
# Durable per-account settings (费率 / lag / learned rules) live in the existing
# company_profile.profile_json under the "recon" key (no migration); per-run results
# + the exception worklist live in settlement_reports / settlement_exceptions.
# ===========================================================================
def get_recon_settings(company_id):
    """This account's 对账 settings ({platforms, recurring_rules, learned_rules}) from
    the company brain, or {} (mirrors get_branding)."""
    return (get_profile(company_id) or {}).get("recon", {}) or {}


def learned_recon_rules(company_id):
    """The learned exception-classification rules the engine applies before computing
    the residual, so a prior one-tap classify improves the next run."""
    return list(get_recon_settings(company_id).get("learned_rules") or [])


def platform_overrides(company_id, platform_key):
    """This account's per-platform overrides (费率 / settlement_lag_days / column_map),
    or {} — merged into the shared pack by settlement_templates.apply_overrides."""
    plats = get_recon_settings(company_id).get("platforms") or {}
    return plats.get(platform_key, {}) or {}


def _append_learned_rule(company_id, rule):
    """Non-destructively append one learned rule to profile_json["recon"]."""
    settings = dict(get_recon_settings(company_id))
    rules = list(settings.get("learned_rules") or [])
    rules.append(rule)
    settings["learned_rules"] = rules
    merge_profile(company_id, {"recon": settings})


# ---- 跨境财务 (cross-border finance) per-account settings ----------------------
# The durable seller identity for the 跨境财务 hub: the selected 贸易方式 (trade
# mode), the platforms in play, and the 收款工具 (收款服务商). Stored under
# profile_json["cross_border"] alongside the per-workflow namespaces above
# (["recon"]/["ar"]); JSON column, so no schema change. Drives which mode-gated
# sub-tabs render and seeds the per-engine settings the later phases add.

CROSS_BORDER_MODES = ("9810", "general", "9710", "9610", "1210")
CROSS_BORDER_DEFAULT_MODE = "9810"   # 海外仓 / Amazon FBA — the first customer


def get_cross_border_settings(company_id):
    """This account's 跨境财务 settings ({mode, platforms, payment_tools}) from the
    company brain. Defaults to the 9810 海外仓 profile when unset (mirrors
    get_recon_settings / get_ar_settings)."""
    cb = (get_profile(company_id) or {}).get("cross_border", {}) or {}
    mode = cb.get("mode")
    if mode not in CROSS_BORDER_MODES:
        mode = CROSS_BORDER_DEFAULT_MODE
    return {
        "mode": mode,
        "platforms": list(cb.get("platforms") or []),
        "payment_tools": list(cb.get("payment_tools") or []),
    }


def save_cross_border_settings(company_id, *, mode=None, platforms=None,
                               payment_tools=None):
    """Persist the 跨境财务 seller identity into profile_json["cross_border"]
    (shallow-merge, preserving any per-engine sub-keys a later phase stores).
    Only the provided fields are touched. Returns the saved, normalized settings."""
    cb = dict((get_profile(company_id) or {}).get("cross_border", {}) or {})
    if mode is not None:
        cb["mode"] = mode if mode in CROSS_BORDER_MODES else CROSS_BORDER_DEFAULT_MODE
    if platforms is not None:
        cb["platforms"] = [str(p).strip() for p in platforms if str(p).strip()]
    if payment_tools is not None:
        cb["payment_tools"] = [str(t).strip() for t in payment_tools if str(t).strip()]
    merge_profile(company_id, {"cross_border": cb})
    return get_cross_border_settings(company_id)


def _recon_decimal(x):
    try:
        return Decimal(str(x if x is not None else 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def save_settlement_report(company_id, run_id, result, *, account_id=None,
                           platform=None, period_label=None):
    """Upsert one 对账 run's result (keyed company_id+run_id) and REPLACE its
    exception worklist rows (idempotent on re-run). `result` is the engine output
    dict. Returns the saved report dict."""
    if not company_id or not run_id:
        return None
    result = result or {}
    summary = result.get("summary") or {}
    bridge = result.get("bridge") or {}
    exceptions = result.get("exceptions") or []
    payload = json.dumps(result, ensure_ascii=False)
    with db.session_scope() as s:
        row = (s.query(SettlementReport)
               .filter_by(company_id=company_id, run_id=run_id)
               .with_for_update().one_or_none())
        if row is None:
            row = SettlementReport(company_id=company_id, run_id=run_id)
            s.add(row)
        row.account_id = account_id
        row.platform = platform or summary.get("platform")
        row.period_label = period_label or summary.get("period_label")
        row.summary_json = payload
        row.bridge_ok = bool(bridge.get("ok", True))
        row.unexplained_amount = _recon_decimal(summary.get("未解释差异", 0))
        row.unexplained_count = int(summary.get("未解释笔数", 0) or 0)
        # created_at is stamped once by the column default on insert — do NOT reset it
        # on an upsert (a re-save must preserve the original creation time).
        # Replace the worklist for this run (a re-run supersedes the prior one).
        (s.query(SettlementException)
         .filter_by(company_id=company_id, run_id=run_id).delete())
        for e in exceptions:
            s.add(SettlementException(
                company_id=company_id, run_id=run_id,
                layer=(e.get("layer") or "")[:16], bucket=(e.get("bucket") or "")[:24],
                cause=(e.get("cause") or "")[:48], amount=_recon_decimal(e.get("amount", 0)),
                platform_ref=(e.get("platform_ref") or "")[:128],
                bank_ref=(e.get("bank_ref") or "")[:128],
                ledger_ref=(e.get("ledger_ref") or "")[:128],
                status=e.get("status") or SETTLE_EXC_OPEN,
                user_label=(e.get("user_label") or None),
                detail_json=json.dumps({"detail": e.get("detail", "")}, ensure_ascii=False),
            ))
        s.flush()
        return _settlement_report_to_dict(row)


def _settlement_report_to_dict(r):
    try:
        result = json.loads(r.summary_json) if r.summary_json else {}
    except ValueError:
        result = {}
    return {
        "id": r.id, "run_id": r.run_id, "company_id": r.company_id,
        "platform": r.platform or "", "period_label": r.period_label or "",
        "bridge_ok": bool(r.bridge_ok),
        "unexplained_amount": float(r.unexplained_amount or 0),
        "unexplained_count": int(r.unexplained_count or 0),
        "summary": result.get("summary") or {},
        "buckets": result.get("buckets") or {},
        "attributions": result.get("attributions") or [],
        "bridge": result.get("bridge") or {},
        "source_rows": result.get("source_rows") or [],
        "created_at": _iso(r.created_at),
    }


def get_settlement_result(company_id, run_id):
    """The FULL engine-output blob for a 对账 run (incl. exceptions + source_rows),
    ownership-filtered. Used to build the audit-ready ledger export. None if missing."""
    if not company_id or not run_id:
        return None
    with db.session_scope() as s:
        row = (s.query(SettlementReport)
               .filter_by(company_id=company_id, run_id=run_id).one_or_none())
        if row is None:
            return None
        try:
            return json.loads(row.summary_json) if row.summary_json else {}
        except ValueError:
            return {}


def get_settlement_report(company_id, run_id):
    """One 对账 result as a customer-safe dict (no ¥ COGS — none is stored here), with
    an ownership filter. None if missing or not this company's."""
    if not company_id or not run_id:
        return None
    with db.session_scope() as s:
        row = (s.query(SettlementReport)
               .filter_by(company_id=company_id, run_id=run_id).one_or_none())
        return _settlement_report_to_dict(row) if row is not None else None


def list_settlement_reports(company_id, limit=60):
    """This account's 对账 history, newest first (the 对账 list surface)."""
    if not company_id:
        return []
    with db.session_scope() as s:
        rows = (s.query(SettlementReport)
                .filter_by(company_id=company_id)
                .order_by(SettlementReport.id.desc()).limit(int(limit)).all())
        return [_settlement_report_to_dict(r) for r in rows]


def _settlement_exc_to_dict(e):
    try:
        detail = (json.loads(e.detail_json) if e.detail_json else {}).get("detail", "")
    except ValueError:
        detail = ""
    return {
        "id": e.id, "run_id": e.run_id, "layer": e.layer or "", "bucket": e.bucket or "",
        "cause": e.cause or "", "amount": float(e.amount or 0),
        "platform_ref": e.platform_ref or "", "bank_ref": e.bank_ref or "",
        "ledger_ref": e.ledger_ref or "", "status": e.status or SETTLE_EXC_OPEN,
        "user_label": e.user_label or "", "detail": detail,
        "created_at": _iso(e.created_at), "updated_at": _iso(e.updated_at),
    }


def list_settlement_exceptions(company_id, run_id=None, status=None, limit=300):
    """The exception worklist for an account (optionally one run / one status)."""
    if not company_id:
        return []
    with db.session_scope() as s:
        q = s.query(SettlementException).filter_by(company_id=company_id)
        if run_id:
            q = q.filter_by(run_id=run_id)
        if status:
            q = q.filter_by(status=status)
        rows = q.order_by(SettlementException.id.asc()).limit(int(limit)).all()
        return [_settlement_exc_to_dict(e) for e in rows]


# The one-tap classification labels the worklist offers -> (canonical cause, status).
_RECON_LABELS = {
    "refund": ("退款/退货", SETTLE_EXC_CLASSIFIED),
    "这是退款": ("退款/退货", SETTLE_EXC_CLASSIFIED),
    "checked": ("已核对", SETTLE_EXC_RESOLVED),
    "已核对": ("已核对", SETTLE_EXC_RESOLVED),
    "pending": ("挂账", SETTLE_EXC_CLASSIFIED),
    "挂账": ("挂账", SETTLE_EXC_CLASSIFIED),
}


def classify_settlement_exception(company_id, exception_id, label):
    """One-tap classify an exception: flip its status/cause AND write a learned rule
    so the NEXT run auto-classifies the same kind of line. Ownership-enforced via the
    company_id filter. Returns the updated exception dict, or None if not found."""
    if not company_id or not exception_id:
        return None
    key = (label or "").strip()
    cause, status = _RECON_LABELS.get(key, (None, SETTLE_EXC_CLASSIFIED))
    learn = None
    with db.session_scope() as s:
        e = (s.query(SettlementException)
             .filter_by(id=exception_id, company_id=company_id).one_or_none())
        if e is None:
            return None
        e.status = status
        e.user_label = key[:48]
        if cause:
            e.cause = cause
        e.updated_at = datetime.utcnow()
        result = _settlement_exc_to_dict(e)
        # Build the learned rule from the strongest identifying ref this line has.
        for field in ("ledger_ref", "platform_ref", "bank_ref"):
            val = getattr(e, field, None)
            if val:
                learn = {"match": {field: val}, "cause": cause or e.cause, "label": key}
                break
    if cause and learn:
        _append_learned_rule(company_id, learn)
    return result


# ===========================================================================
# 应收账款管理 + 催收 (AR aging + dunning) — CUSTOMER-scoped persistence.
# A NEW, additive sibling of the 对账 helpers above (the two share no code today).
# Durable per-account AR settings (账期/column overrides + learned grading rules) live
# in company_profile.profile_json under the "ar" key (no migration); per-run results +
# the flag worklist live in ar_reports / ar_flags; 催收 sent-state in dunning_messages.
# ===========================================================================
def get_ar_settings(company_id):
    """This account's AR settings ({formats, learned_rules, ...}) from the company
    brain, or {} (mirrors get_recon_settings)."""
    return (get_profile(company_id) or {}).get("ar", {}) or {}


def learned_ar_rules(company_id):
    """The learned flag-classification rules the engine applies before grading, so a
    prior one-tap classify improves the next run."""
    return list(get_ar_settings(company_id).get("learned_rules") or [])


def ar_format_overrides(company_id, format_key):
    """This account's per-format overrides (账期/列名映射), merged into the shared pack by
    ar_templates.apply_overrides. {} when none."""
    fmts = get_ar_settings(company_id).get("formats") or {}
    return fmts.get(format_key, {}) or {}


def _append_ar_learned_rule(company_id, rule):
    """Non-destructively append one learned rule to profile_json["ar"]."""
    settings = dict(get_ar_settings(company_id))
    rules = list(settings.get("learned_rules") or [])
    rules.append(rule)
    settings["learned_rules"] = rules
    merge_profile(company_id, {"ar": settings})


def read_reconciled_received(company_id, period_label=None, platform=None):
    """The 对账-OUTPUT dependency: read the reconciled received TOTAL (到账, fallback
    实收) for a platform+period from the newest matching SettlementReport, as an
    AGGREGATE cross-check input for the AR engine. Returns {到账, 实收, platform,
    period_label} or None when no 对账 has run (graceful fallback — AR runs anyway).

    This is a per-platform-period aggregate, NOT per-invoice — AR uses it only to flag a
    discrepancy vs its own applied-payments total, never to source payments."""
    if not company_id:
        return None
    with db.session_scope() as s:
        q = s.query(SettlementReport).filter_by(company_id=company_id)
        if period_label:
            q = q.filter_by(period_label=period_label)
        if platform:
            q = q.filter_by(platform=platform)
        row = q.order_by(SettlementReport.id.desc()).first()
        if row is None:
            return None
        try:
            summary = (json.loads(row.summary_json) if row.summary_json else {}).get(
                "summary") or {}
        except ValueError:
            summary = {}
        arrived = summary.get("到账")
        settled = summary.get("实收")
        if arrived is None and settled is None:
            return None
        return {"到账": arrived, "实收": settled, "platform": row.platform or "",
                "period_label": row.period_label or ""}


def save_ar_report(company_id, run_id, result, *, account_id=None, format_key=None,
                   period_label=None):
    """Upsert one AR run's result (keyed company_id+run_id) and REPLACE its flag
    worklist rows (idempotent on re-run). `result` is the ar_aging_engine output dict.
    Returns the saved report dict. Mirrors save_settlement_report."""
    if not company_id or not run_id:
        return None
    result = result or {}
    summary = result.get("summary") or {}
    bridge = result.get("bridge") or {}
    flags = result.get("exceptions") or []
    payload = json.dumps(result, ensure_ascii=False)
    dso = summary.get("DSO")
    with db.session_scope() as s:
        row = (s.query(ARReport)
               .filter_by(company_id=company_id, run_id=run_id)
               .with_for_update().one_or_none())
        if row is None:
            row = ARReport(company_id=company_id, run_id=run_id)
            s.add(row)
        row.account_id = account_id
        row.format_key = format_key or summary.get("platform")
        row.period_label = period_label or summary.get("period_label")
        row.summary_json = payload
        row.bridge_ok = bool(bridge.get("ok", True))
        row.total_outstanding = _recon_decimal(summary.get("应收账款总额", 0))
        row.overdue_total = _recon_decimal(summary.get("逾期总额", 0))
        row.dso = (_recon_decimal(dso) if dso is not None else None)
        row.suspect_count = int(summary.get("疑似条目数", 0) or 0)
        # created_at stamped once on insert — never reset on an upsert.
        (s.query(ARFlag).filter_by(company_id=company_id, run_id=run_id).delete())
        for e in flags:
            reasons = e.get("suspect_reasons") or []
            s.add(ARFlag(
                company_id=company_id, run_id=run_id,
                layer=(e.get("layer") or "")[:16], bucket=(e.get("bucket") or "")[:24],
                grade=(e.get("grade") or "")[:8], cause=(e.get("cause") or "")[:48],
                suspect_reasons=("，".join(reasons))[:255],
                amount=_recon_decimal(e.get("amount", 0)),
                customer_ref=(e.get("customer_ref") or "")[:128],
                invoice_ref=(e.get("invoice_ref") or "")[:128],
                status=e.get("status") or AR_FLAG_OPEN,
                user_label=(e.get("user_label") or None),
                detail_json=json.dumps({"detail": e.get("detail", "")},
                                       ensure_ascii=False),
            ))
        s.flush()
        return _ar_report_to_dict(row)


def _ar_report_to_dict(r):
    try:
        result = json.loads(r.summary_json) if r.summary_json else {}
    except ValueError:
        result = {}
    return {
        "id": r.id, "run_id": r.run_id, "company_id": r.company_id,
        "format_key": r.format_key or "", "period_label": r.period_label or "",
        "bridge_ok": bool(r.bridge_ok),
        "total_outstanding": float(r.total_outstanding or 0),
        "overdue_total": float(r.overdue_total or 0),
        "dso": (float(r.dso) if r.dso is not None else None),
        "suspect_count": int(r.suspect_count or 0),
        "summary": result.get("summary") or {},
        "buckets": result.get("buckets") or {},
        "customers": result.get("customers") or [],
        "attributions": result.get("attributions") or [],
        "bridge": result.get("bridge") or {},
        "source_rows": result.get("source_rows") or [],
        "created_at": _iso(r.created_at),
    }


def get_ar_result(company_id, run_id):
    """The FULL engine-output blob for an AR run (incl. invoices/applications/
    source_rows), ownership-filtered. Used to build the audit-ready ledger export."""
    if not company_id or not run_id:
        return None
    with db.session_scope() as s:
        row = (s.query(ARReport)
               .filter_by(company_id=company_id, run_id=run_id).one_or_none())
        if row is None:
            return None
        try:
            return json.loads(row.summary_json) if row.summary_json else {}
        except ValueError:
            return {}


def get_ar_report(company_id, run_id):
    """One AR result as a customer-safe dict (no ¥ COGS — none stored), ownership
    filtered. None if missing or not this company's."""
    if not company_id or not run_id:
        return None
    with db.session_scope() as s:
        row = (s.query(ARReport)
               .filter_by(company_id=company_id, run_id=run_id).one_or_none())
        return _ar_report_to_dict(row) if row is not None else None


def list_ar_reports(company_id, limit=60):
    """This account's AR history, newest first."""
    if not company_id:
        return []
    with db.session_scope() as s:
        rows = (s.query(ARReport).filter_by(company_id=company_id)
                .order_by(ARReport.id.desc()).limit(int(limit)).all())
        return [_ar_report_to_dict(r) for r in rows]


def _ar_flag_to_dict(e):
    try:
        detail = (json.loads(e.detail_json) if e.detail_json else {}).get("detail", "")
    except ValueError:
        detail = ""
    return {
        "id": e.id, "run_id": e.run_id, "layer": e.layer or "", "bucket": e.bucket or "",
        "grade": e.grade or "", "cause": e.cause or "",
        "suspect_reasons": [r for r in (e.suspect_reasons or "").split("，") if r],
        "amount": float(e.amount or 0),
        "customer_ref": e.customer_ref or "", "invoice_ref": e.invoice_ref or "",
        "status": e.status or AR_FLAG_OPEN, "user_label": e.user_label or "",
        "detail": detail,
        "created_at": _iso(e.created_at), "updated_at": _iso(e.updated_at),
    }


def list_ar_flags(company_id, run_id=None, status=None, grade=None, limit=500):
    """The AR flag worklist for an account (optionally one run / status / grade)."""
    if not company_id:
        return []
    with db.session_scope() as s:
        q = s.query(ARFlag).filter_by(company_id=company_id)
        if run_id:
            q = q.filter_by(run_id=run_id)
        if status:
            q = q.filter_by(status=status)
        if grade:
            q = q.filter_by(grade=grade)
        rows = q.order_by(ARFlag.id.asc()).limit(int(limit)).all()
        return [_ar_flag_to_dict(e) for e in rows]


# The one-tap classification labels the AR worklist offers -> (canonical cause, status).
_AR_LABELS = {
    "confirmed": ("已确认应收", AR_FLAG_RESOLVED),
    "已确认": ("已确认应收", AR_FLAG_RESOLVED),
    "paid": ("已收款", AR_FLAG_RESOLVED),
    "已收款": ("已收款", AR_FLAG_RESOLVED),
    "dispute": ("争议", AR_FLAG_CLASSIFIED),
    "争议": ("争议", AR_FLAG_CLASSIFIED),
    "pending": ("挂账", AR_FLAG_CLASSIFIED),
    "挂账": ("挂账", AR_FLAG_CLASSIFIED),
}


def classify_ar_flag(company_id, flag_id, label):
    """One-tap classify an AR flag: flip status/cause AND write a learned rule so the
    NEXT run auto-classifies the same line. Ownership-enforced. Returns the updated flag
    dict, or None if not found. Mirrors classify_settlement_exception."""
    if not company_id or not flag_id:
        return None
    key = (label or "").strip()
    cause, status = _AR_LABELS.get(key, (None, AR_FLAG_CLASSIFIED))
    learn = None
    with db.session_scope() as s:
        e = (s.query(ARFlag).filter_by(id=flag_id, company_id=company_id).one_or_none())
        if e is None:
            return None
        e.status = status
        e.user_label = key[:48]
        if cause:
            e.cause = cause
        e.updated_at = datetime.utcnow()
        result = _ar_flag_to_dict(e)
        # Build the learned rule from the strongest identifying ref this flag has.
        for field in ("invoice_ref", "customer_ref"):
            val = getattr(e, field, None)
            if val:
                learn = {"match": {field: val}, "cause": cause or e.cause, "label": key}
                break
    if cause and learn:
        _append_ar_learned_rule(company_id, learn)
    return result


# ---- 跨境财务 engines (三流合一 / 退税 / 发票审核 / 无票免税) persistence -----------
# ONE shared table pair; `engine_kind` scopes each engine's reports + worklist.
def save_crossborder_report(company_id, run_id, engine_kind, result, *,
                            account_id=None, period_label=None):
    """Upsert one 跨境财务 engine run's result (keyed company+run) and REPLACE its
    worklist flags from result["exceptions"] (idempotent on re-run). Returns the saved
    report dict. Mirrors save_ar_report; these engines compute every number (no LLM)."""
    if not company_id or not run_id:
        return None
    result = result or {}
    summary = result.get("summary") or {}
    bridge = result.get("bridge") or {}
    flags = result.get("exceptions") or []
    payload = json.dumps(result, ensure_ascii=False)
    kind = (engine_kind or "")[:32]
    with db.session_scope() as s:
        row = (s.query(CrossBorderReport)
               .filter_by(company_id=company_id, run_id=run_id)
               .with_for_update().one_or_none())
        if row is None:
            row = CrossBorderReport(company_id=company_id, run_id=run_id)
            s.add(row)
        row.account_id = account_id
        row.engine_kind = kind
        row.period_label = period_label or summary.get("period_label")
        row.summary_json = payload
        row.bridge_ok = bool(bridge.get("ok", True))
        row.exception_count = len(flags)
        (s.query(CrossBorderFlag)
         .filter_by(company_id=company_id, run_id=run_id).delete())
        for e in flags:
            s.add(CrossBorderFlag(
                company_id=company_id, run_id=run_id, engine_kind=kind,
                ref=(str(e.get("ref") or ""))[:128],
                cause=(e.get("cause") or "")[:64],
                severity=(e.get("severity") or "")[:8],
                amount=_recon_decimal(e.get("amount", 0)),
                detail=(e.get("detail") or ""),
                status=e.get("status") or CB_FLAG_OPEN,
                user_label=(e.get("user_label") or None),
            ))
        s.flush()
        return _crossborder_report_to_dict(row)


def _crossborder_report_to_dict(r):
    try:
        result = json.loads(r.summary_json) if r.summary_json else {}
    except ValueError:
        result = {}
    return {
        "id": r.id, "run_id": r.run_id, "company_id": r.company_id,
        "engine_kind": r.engine_kind or "", "period_label": r.period_label or "",
        "bridge_ok": bool(r.bridge_ok), "exception_count": int(r.exception_count or 0),
        "summary": result.get("summary") or {},
        "bridge": result.get("bridge") or {},
        "source_rows": result.get("source_rows") or [],
        "created_at": _iso(r.created_at),
    }


def get_crossborder_report(company_id, run_id):
    """One 跨境财务 result as a customer-safe dict (no ¥ COGS — none stored)."""
    if not company_id or not run_id:
        return None
    with db.session_scope() as s:
        row = (s.query(CrossBorderReport)
               .filter_by(company_id=company_id, run_id=run_id).one_or_none())
        return _crossborder_report_to_dict(row) if row is not None else None


def list_crossborder_reports(company_id, engine_kind=None, limit=60):
    """This account's 跨境财务 history for one engine kind, newest first."""
    if not company_id:
        return []
    with db.session_scope() as s:
        q = s.query(CrossBorderReport).filter_by(company_id=company_id)
        if engine_kind:
            q = q.filter_by(engine_kind=engine_kind)
        rows = q.order_by(CrossBorderReport.id.desc()).limit(int(limit)).all()
        return [_crossborder_report_to_dict(r) for r in rows]


def latest_settlement_orders(company_id, period_label=None, platform=None):
    """The newest 对账 run's compact per-order list ([{ref, amount, net, date}]) for a
    period — the 三流合一 sub-tab imports this as its 订单 leg so the seller needn't
    re-upload. Returns [] when no 对账 ran for the period (graceful)."""
    if not company_id:
        return []
    with db.session_scope() as s:
        q = s.query(SettlementReport).filter_by(company_id=company_id)
        if period_label:
            q = q.filter_by(period_label=period_label)
        if platform:
            q = q.filter_by(platform=platform)
        row = q.order_by(SettlementReport.id.desc()).first()
        if row is None:
            return []
        try:
            return (json.loads(row.summary_json) if row.summary_json else {}).get(
                "orders") or []
        except ValueError:
            return []


def latest_crossborder_summary(company_id, engine_kind, period_label=None):
    """The newest CrossBorderReport summary for one engine kind (+ optional period) — an
    AGGREGATE cross-read input for a DOWNSTREAM sub-tab (e.g. 退税 reading 发票审核's
    有效进项税额). Returns the summary dict, or None when that engine hasn't run for the
    period (graceful — the downstream engine runs anyway). Mirrors read_reconciled_received."""
    if not company_id or not engine_kind:
        return None
    with db.session_scope() as s:
        q = s.query(CrossBorderReport).filter_by(company_id=company_id,
                                                 engine_kind=engine_kind)
        if period_label:
            q = q.filter_by(period_label=period_label)
        row = q.order_by(CrossBorderReport.id.desc()).first()
        if row is None:
            return None
        try:
            return (json.loads(row.summary_json) if row.summary_json else {}).get(
                "summary") or None
        except ValueError:
            return None


def _crossborder_flag_to_dict(e):
    return {
        "id": e.id, "run_id": e.run_id, "engine_kind": e.engine_kind or "",
        "ref": e.ref or "", "cause": e.cause or "", "severity": e.severity or "",
        "amount": float(e.amount or 0), "detail": e.detail or "",
        "status": e.status or CB_FLAG_OPEN, "user_label": e.user_label or "",
        "created_at": _iso(e.created_at), "updated_at": _iso(e.updated_at),
    }


def list_crossborder_flags(company_id, engine_kind=None, run_id=None, status=None,
                           limit=500):
    """One 跨境财务 engine's 不一致/异常 worklist (optionally one run / status)."""
    if not company_id:
        return []
    with db.session_scope() as s:
        q = s.query(CrossBorderFlag).filter_by(company_id=company_id)
        if engine_kind:
            q = q.filter_by(engine_kind=engine_kind)
        if run_id:
            q = q.filter_by(run_id=run_id)
        if status:
            q = q.filter_by(status=status)
        rows = q.order_by(CrossBorderFlag.id.asc()).limit(int(limit)).all()
        return [_crossborder_flag_to_dict(e) for e in rows]


# One-tap labels the 跨境财务 worklist offers -> (canonical label, status).
_CB_LABELS = {
    "resolved": ("已核实", CB_FLAG_RESOLVED), "已核实": ("已核实", CB_FLAG_RESOLVED),
    "ok": ("无需处理", CB_FLAG_RESOLVED), "无需处理": ("无需处理", CB_FLAG_RESOLVED),
    "pending": ("待处理", CB_FLAG_CLASSIFIED), "待处理": ("待处理", CB_FLAG_CLASSIFIED),
    "followup": ("转人工核查", CB_FLAG_CLASSIFIED), "转人工": ("转人工核查", CB_FLAG_CLASSIFIED),
}


def classify_crossborder_flag(company_id, flag_id, label):
    """One-tap classify a 跨境财务 worklist flag. Ownership-enforced; returns the updated
    flag dict or None. No learned-rule loop (these engines are stateless re-checks)."""
    if not company_id or not flag_id:
        return None
    key = (label or "").strip()
    new_label, status = _CB_LABELS.get(key, (key or None, CB_FLAG_CLASSIFIED))
    with db.session_scope() as s:
        e = (s.query(CrossBorderFlag)
             .filter_by(id=flag_id, company_id=company_id).one_or_none())
        if e is None:
            return None
        e.status = status
        e.user_label = (new_label or "")[:48] or None
        e.updated_at = datetime.utcnow()
        s.flush()
        return _crossborder_flag_to_dict(e)


# ---- 催收 (dunning) sent-state — the no-double-dun machinery -----------------
def _dunning_to_dict(m):
    try:
        facts = json.loads(m.facts_json) if m.facts_json else []
    except ValueError:
        facts = []
    return {
        "id": m.id, "run_id": m.run_id, "customer_key": m.customer_key,
        "invoice_key": m.invoice_key or "", "tier": m.tier, "channel": m.channel or "",
        "grade": m.grade or "", "state": m.state,
        "days_overdue": int(m.days_overdue_at_draft or 0),
        "amount": float(m.amount_at_draft or 0), "facts": facts,
        "draft_body": m.draft_body or "", "edited_body": m.edited_body or "",
        "body": (m.edited_body or m.draft_body or ""),
        "audit_warning": m.audit_warning or "",
        "drafted_at": _iso(m.drafted_at), "approved_at": _iso(m.approved_at),
        "sent_at": _iso(m.sent_at),
    }


def last_dunning(company_id, customer_key):
    """The most-recent SENT 催收 message for a customer (any tier) — the cadence anchor
    a re-run reads to decide NEW / ESCALATED / LOCKED. None if never dunned."""
    if not company_id or not customer_key:
        return None
    with db.session_scope() as s:
        m = (s.query(DunningMessage)
             .filter_by(company_id=company_id, customer_key=customer_key,
                        state=DUNNING_STATE_SENT)
             .order_by(DunningMessage.sent_at.desc(), DunningMessage.id.desc()).first())
        return _dunning_to_dict(m) if m is not None else None


def record_dunning_draft(company_id, customer_key, tier, *, run_id=None, account_id=None,
                         invoice_key=None, grade=None, facts=None, draft_body="",
                         audit_warning="", days_overdue=0, amount=0):
    """Upsert the OPEN draft for (company, customer_key, invoice_key, tier). A re-run
    that re-enters a NEW/ESCALATED case REPLACES a stale unsent DRAFT rather than piling
    up duplicates; an EDITED/APPROVED draft (human-touched) is left intact. Returns the
    draft dict."""
    if not company_id or not customer_key or not tier:
        return None
    with db.session_scope() as s:
        m = (s.query(DunningMessage)
             .filter_by(company_id=company_id, customer_key=customer_key,
                        invoice_key=invoice_key, tier=tier)
             .filter(DunningMessage.state.in_(
                 (DUNNING_STATE_DRAFT, DUNNING_STATE_EDITED, DUNNING_STATE_APPROVED)))
             .with_for_update().order_by(DunningMessage.id.desc()).first())
        if m is not None and m.state != DUNNING_STATE_DRAFT:
            return _dunning_to_dict(m)        # human already acting on it — don't clobber
        if m is None:
            m = DunningMessage(company_id=company_id, customer_key=customer_key,
                               invoice_key=invoice_key, tier=tier)
            s.add(m)
        m.run_id = run_id
        m.account_id = account_id
        m.grade = (grade or "")[:8] or None
        m.facts_json = json.dumps(facts or [], ensure_ascii=False)
        m.draft_body = draft_body or ""
        m.audit_warning = audit_warning or ""
        m.days_overdue_at_draft = int(days_overdue or 0)
        m.amount_at_draft = _recon_decimal(amount)
        m.state = DUNNING_STATE_DRAFT
        m.updated_at = datetime.utcnow()
        s.flush()
        return _dunning_to_dict(m)


def get_dunning_message(company_id, message_id):
    if not company_id or not message_id:
        return None
    with db.session_scope() as s:
        m = (s.query(DunningMessage)
             .filter_by(id=message_id, company_id=company_id).one_or_none())
        return _dunning_to_dict(m) if m is not None else None


def list_dunning_messages(company_id, run_id=None, state=None, limit=500):
    if not company_id:
        return []
    with db.session_scope() as s:
        q = s.query(DunningMessage).filter_by(company_id=company_id)
        if run_id:
            q = q.filter_by(run_id=run_id)
        if state:
            q = q.filter_by(state=state)
        rows = q.order_by(DunningMessage.id.asc()).limit(int(limit)).all()
        return [_dunning_to_dict(m) for m in rows]


def save_dunning_edit(company_id, message_id, body):
    """Human edited a draft's wording. -> state EDITED, edited_body set. Never sends."""
    if not company_id or not message_id:
        return None
    with db.session_scope() as s:
        m = (s.query(DunningMessage)
             .filter_by(id=message_id, company_id=company_id)
             .with_for_update().one_or_none())
        if m is None or m.state == DUNNING_STATE_SENT:
            return None
        m.edited_body = body or ""
        m.state = DUNNING_STATE_EDITED
        m.updated_at = datetime.utcnow()
        return _dunning_to_dict(m)


def set_dunning_state(company_id, message_id, state, *, channel=None, delivery_id=None):
    """Transition a draft (approve / dismiss / sent). The ONLY path to SENT — called by
    the human approve-then-send route, NEVER the orchestrator/scheduler. Stamps
    approved_at on APPROVED and sent_at (the cadence anchor) on SENT."""
    if not company_id or not message_id or state not in (
            DUNNING_STATE_APPROVED, DUNNING_STATE_DISMISSED, DUNNING_STATE_SENT):
        return None
    with db.session_scope() as s:
        m = (s.query(DunningMessage)
             .filter_by(id=message_id, company_id=company_id)
             .with_for_update().one_or_none())
        if m is None:
            return None
        now = datetime.utcnow()
        if state == DUNNING_STATE_APPROVED:
            m.approved_at = now
        if channel:
            m.channel = channel[:16]
        if state == DUNNING_STATE_SENT:
            if m.approved_at is None:
                m.approved_at = now
            m.sent_at = now
            if delivery_id is not None:
                m.delivery_id = int(delivery_id)
        m.state = state
        m.updated_at = now
        return _dunning_to_dict(m)


# ===========================================================================
# Phase 10 — onboarding funnel, support log, changelog badge, lifecycle marks
# ===========================================================================
# DB primitives only. The wizard ROUTES live in app.py, the funnel/percentage
# SQL in admin/queries.py, and the lifecycle email POLICY in lifecycle.py.

# ---- Onboarding wizard events ----------------------------------------------
def record_onboarding_event(account_id, step, event):
    """Append one wizard funnel row (account did `event` at `step`). Returns the
    new id, or None on junk input (unknown step/event, no account)."""
    if event not in ONBOARDING_EVENT_KINDS or not account_id:
        return None
    try:
        step = int(step)
    except (TypeError, ValueError):
        return None
    if step not in ONBOARDING_STEPS:
        return None
    with db.session_scope() as s:
        row = OnboardingEvent(account_id=str(account_id)[:32], step=step, event=event)
        s.add(row)
        s.flush()
        return row.id


def onboarding_state(account_id):
    """The wizard's RESUMABLE state for one account, derived from the event log.

    Returns {"started": bool, "steps": {"1".."6": None|"completed"|"skipped"},
    "done": bool, "next_step": int}. The latest action per step wins (so a step
    skipped then later completed reads completed); the wizard counts as done once
    the last step (6) has any action — every step is individually skippable."""
    steps = {n: None for n in ONBOARDING_STEPS if n != 0}
    started = False
    if account_id:
        with db.session_scope() as s:
            rows = (s.query(OnboardingEvent.step, OnboardingEvent.event)
                    .filter(OnboardingEvent.account_id == str(account_id))
                    .order_by(OnboardingEvent.id.asc()).all())
        for step, event in rows:
            started = True
            if step in steps and event in (ONBOARDING_COMPLETED, ONBOARDING_SKIPPED):
                steps[step] = event
    done = steps[ONBOARDING_LAST_STEP] is not None
    next_step = next((n for n in sorted(steps) if steps[n] is None),
                     ONBOARDING_LAST_STEP)
    return {"started": started,
            "steps": {str(k): v for k, v in steps.items()},
            "done": done,
            "next_step": next_step}


def list_onboarding_events(account_id=None, limit=2000):
    """Raw funnel rows, oldest first (tests + debugging)."""
    with db.session_scope() as s:
        q = s.query(OnboardingEvent)
        if account_id is not None:
            q = q.filter(OnboardingEvent.account_id == str(account_id))
        rows = q.order_by(OnboardingEvent.id.asc()).limit(int(limit)).all()
        return [{"id": r.id, "account_id": r.account_id, "step": r.step,
                 "event": r.event, "created_at": _iso(r.created_at)} for r in rows]


# ---- Support requests (the 联系支持 log) ------------------------------------
def record_support_request(account_id, email, message, page=None):
    """Log one support message. Returns the id, or None for an empty message.
    `message` is raw user text — render-side escaping is mandatory."""
    message = (message or "").strip()
    if not message:
        return None
    with db.session_scope() as s:
        row = SupportRequest(
            account_id=(str(account_id)[:32] if account_id else None),
            email=(str(email)[:320] if email else None),
            message=message[:5000],
            page=(str(page)[:64] if page else None),
        )
        s.add(row)
        s.flush()
        return row.id


def list_support_requests(limit=200):
    """Support messages newest-first, each with the account email when known."""
    with db.session_scope() as s:
        q = (s.query(SupportRequest, Account.email)
             .outerjoin(Account, Account.id == SupportRequest.account_id))
        rows = q.order_by(SupportRequest.id.desc()).limit(int(limit)).all()
        return [{
            "id": r.id, "account_id": r.account_id,
            "account_email": acc_email, "email": r.email,
            "message": r.message, "page": r.page,
            "created_at": _iso(r.created_at),
        } for r, acc_email in rows]


# ---- Changelog badge (per-account last-seen entry) --------------------------
def get_changelog_seen(account_id):
    """The newest CHANGELOG entry id this account has viewed, or None."""
    if not account_id:
        return None
    with db.session_scope() as s:
        row = (s.query(ChangelogSeen)
               .filter(ChangelogSeen.account_id == str(account_id)).one_or_none())
        return row.last_seen if row else None


def set_changelog_seen(account_id, entry_id):
    """Upsert the account's last-seen CHANGELOG entry (clears the 新功能 badge)."""
    if not account_id:
        return
    with db.session_scope() as s:
        row = (s.query(ChangelogSeen)
               .filter(ChangelogSeen.account_id == str(account_id)).one_or_none())
        if row is None:
            row = ChangelogSeen(account_id=str(account_id)[:32])
            s.add(row)
        row.last_seen = (str(entry_id)[:64] if entry_id else None)
        row.updated_at = datetime.utcnow()


# ---- Lifecycle email dedup marks --------------------------------------------
def record_lifecycle_event_once(account_id, kind, detail=None):
    """Record a lifecycle mark UNLESS one already exists for (account, kind,
    detail). Returns True when newly recorded (caller should send the email),
    False when it already existed (a re-run / replay — do nothing)."""
    if kind not in LIFECYCLE_KINDS or not account_id:
        return False
    detail = (str(detail)[:255] if detail else None)
    with db.session_scope() as s:
        q = s.query(LifecycleEvent.id).filter(
            LifecycleEvent.account_id == str(account_id),
            LifecycleEvent.kind == kind)
        q = q.filter(LifecycleEvent.detail == detail) if detail is not None \
            else q.filter(LifecycleEvent.detail.is_(None))
        if q.first():
            return False
        s.add(LifecycleEvent(account_id=str(account_id)[:32], kind=kind,
                             detail=detail))
        return True


def list_lifecycle_events(account_id=None, kind=None, limit=500):
    """Lifecycle marks newest-first (tests + debugging)."""
    with db.session_scope() as s:
        q = s.query(LifecycleEvent)
        if account_id is not None:
            q = q.filter(LifecycleEvent.account_id == str(account_id))
        if kind in LIFECYCLE_KINDS:
            q = q.filter(LifecycleEvent.kind == kind)
        rows = q.order_by(LifecycleEvent.id.desc()).limit(int(limit)).all()
        return [{"id": r.id, "account_id": r.account_id, "kind": r.kind,
                 "detail": r.detail, "created_at": _iso(r.created_at)} for r in rows]


def list_accounts_for_nudge(min_age_days=3, max_age_days=14, now=None):
    """The day-3 nudge's candidate list: ACTIVE accounts with an email whose age
    is in [min_age_days, max_age_days]. The age ceiling keeps a fresh deploy of
    this feature from nudging every long-standing account at once; the per-account
    dedup is record_lifecycle_event_once."""
    now = now or datetime.utcnow()
    newest = now - timedelta(days=int(min_age_days))
    oldest = now - timedelta(days=int(max_age_days))
    with db.session_scope() as s:
        rows = (s.query(Account)
                .filter(Account.email.isnot(None),
                        Account.status == ACCOUNT_ACTIVE,
                        Account.created_at <= newest,
                        Account.created_at >= oldest)
                .order_by(Account.created_at.asc()).all())
        return [{"id": a.id, "email": a.email, "name": a.name or "客户",
                 "created_at": _iso(a.created_at)} for a in rows]


def company_has_successful_run(company_id):
    """True once the company has at least one SUCCEEDED run (= a finished first
    report). The day-3 nudge targets accounts where this is still False."""
    if not company_id:
        return False
    with db.session_scope() as s:
        return s.query(Run.id).filter(Run.company_id == company_id,
                                      Run.status == RUN_SUCCEEDED).first() is not None


# ---------------------------------------------------------------------------
# Fusion chat — sign-in-required, multi-turn conversations (account-owned, content
# encrypted at rest). The customer routes own the ownership check (compare against
# the logged-in account); these helpers just read/write rows and (de)crypt content.
# ---------------------------------------------------------------------------
def _fusion_convo_dict(c):
    return {"id": c.id, "account_id": c.account_id, "title": c.title,
            "status": c.status, "model_tier": c.model_tier,
            "message_count": int(c.message_count or 0),
            "created_at": _iso(c.created_at), "updated_at": _iso(c.updated_at),
            "last_message_at": _iso(c.last_message_at)}


def _fusion_msg_dict(m):
    """Decrypt one stored turn into a plain dict (per-model `answers` parsed). Content that
    can't be decrypted (a key rotated out of FERNET_KEYS) is dropped to '' rather than
    surfaced as raw ciphertext — so it never shows in the UI nor re-enters model context."""
    content = (crypto.decrypt_string_strict(m.content) or "") if m.content else ""
    answers = None
    if m.answers_json:
        try:
            dec = crypto.decrypt_string_strict(m.answers_json)
            answers = json.loads(dec) if dec else None
        except (ValueError, TypeError):
            answers = None
    fused = ({"by": m.fused_by, "by_label": m.fused_by_label, "note": m.fused_note}
             if m.role == FUSION_ROLE_ASSISTANT else None)
    return {"id": m.id, "message_index": m.message_index, "role": m.role,
            "content": content or "", "answers": answers, "fused": fused,
            "status": m.status, "created_at": _iso(m.created_at)}


def create_fusion_conversation(account_id, title=None, model_tier=None):
    """Open a Fusion conversation for an account; return its id (or None)."""
    if not account_id:
        return None
    with db.session_scope() as s:
        c = FusionConversation(account_id=str(account_id), title=(title or None),
                               model_tier=(model_tier or None),
                               status=FUSION_CONVO_ACTIVE)
        s.add(c)
        s.flush()
        return c.id


def get_fusion_conversation(conversation_id):
    """Return a conversation dict (NO messages) or None. Caller checks ownership
    by comparing the returned `account_id` to the logged-in account."""
    if not conversation_id:
        return None
    with db.session_scope() as s:
        c = s.get(FusionConversation, conversation_id)
        return _fusion_convo_dict(c) if c is not None else None


def list_fusion_conversations(account_id, limit=100):
    """Active conversations for an account, most-recent activity first (list view)."""
    if not account_id:
        return []
    from sqlalchemy import func
    order = func.coalesce(FusionConversation.last_message_at,
                          FusionConversation.created_at).desc()
    with db.session_scope() as s:
        rows = (s.query(FusionConversation)
                .filter(FusionConversation.account_id == str(account_id),
                        FusionConversation.status == FUSION_CONVO_ACTIVE)
                .order_by(order).limit(int(limit)).all())
        return [_fusion_convo_dict(c) for c in rows]


def get_fusion_messages(conversation_id):
    """The conversation's turns in order, DECRYPTED (per-model answers parsed). Used
    by both the history view and the multi-turn context builder."""
    if not conversation_id:
        return []
    with db.session_scope() as s:
        rows = (s.query(FusionMessage)
                .filter(FusionMessage.conversation_id == conversation_id)
                .order_by(FusionMessage.message_index.asc(),
                          FusionMessage.id.asc()).all())
        return [_fusion_msg_dict(m) for m in rows]


def append_fusion_message(conversation_id, role, content, answers=None,
                          fused_meta=None, models_used=None, input_tokens=None,
                          output_tokens=None, cost_yuan=None, status=None):
    """Append a turn (encrypting `content` + `answers_json`), bump the conversation's
    counters/timestamp, auto-title from the first user turn, and return the message id.
    `answers` is the per-model list; `fused_meta` is {by, by_label, note}."""
    if not conversation_id:
        return None
    status = status or FUSION_MSG_OK
    enc_content = crypto.encrypt_string(content) if content else content
    enc_answers = (crypto.encrypt_string(json.dumps(answers, ensure_ascii=False))
                   if answers is not None else None)
    fm = fused_meta or {}
    cost_dec = None
    if cost_yuan is not None:
        try:
            cost_dec = round(float(cost_yuan), 6)
        except (TypeError, ValueError):
            cost_dec = None
    from sqlalchemy import func
    with db.session_scope() as s:
        # Lock the conversation row so two concurrent appends to the SAME conversation
        # can't both read the counter and collide on message_index (real on Postgres; a
        # harmless no-op on SQLite, which has no concurrency here). Also reject a
        # conversation soft-deleted mid-flight, so a late append can't land in a hidden
        # thread (bills COGS into something the user can no longer see).
        c = (s.query(FusionConversation)
             .filter_by(id=conversation_id).with_for_update().one_or_none())
        if c is None or c.status != FUSION_CONVO_ACTIVE:
            return None
        # Derive the index from the actual max row under the lock (authoritative — survives
        # any drift in the denormalized counter), then keep the counter in sync.
        max_idx = (s.query(func.max(FusionMessage.message_index))
                   .filter(FusionMessage.conversation_id == conversation_id).scalar())
        idx = int(max_idx) + 1 if max_idx is not None else 0
        m = FusionMessage(
            conversation_id=conversation_id, message_index=idx, role=role,
            content=enc_content, answers_json=enc_answers,
            fused_by=(fm.get("by") or None),
            fused_by_label=(fm.get("by_label") or None),
            fused_note=(fm.get("note") or None),
            models_used=((",".join(models_used))[:255] if models_used else None),
            input_tokens=(int(input_tokens) if input_tokens is not None else None),
            output_tokens=(int(output_tokens) if output_tokens is not None else None),
            cost_yuan=cost_dec, status=status)
        s.add(m)
        c.message_count = idx + 1
        now = datetime.utcnow()
        c.updated_at = now
        c.last_message_at = now
        if not c.title and role == FUSION_ROLE_USER and content:
            c.title = content.strip().replace("\n", " ")[:60]
        s.flush()
        return m.id


def rename_fusion_conversation(account_id, conversation_id, title):
    """Rename a conversation the account owns. Returns True if updated."""
    if not (account_id and conversation_id):
        return False
    new_title = (title or "").strip()[:255]
    if not new_title:
        return False
    with db.session_scope() as s:
        c = (s.query(FusionConversation)
             .filter_by(id=conversation_id, account_id=str(account_id),
                        status=FUSION_CONVO_ACTIVE).one_or_none())
        if c is None:
            return False
        c.title = new_title
        c.updated_at = datetime.utcnow()
        return True


def soft_delete_fusion_conversation(account_id, conversation_id):
    """Soft-delete (status='deleted') a conversation the account owns — encrypted
    history is retained but hidden from the list and refused on fetch. Returns True."""
    if not (account_id and conversation_id):
        return False
    with db.session_scope() as s:
        c = (s.query(FusionConversation)
             .filter_by(id=conversation_id, account_id=str(account_id)).one_or_none())
        if c is None:
            return False
        c.status = FUSION_CONVO_DELETED
        c.updated_at = datetime.utcnow()
        return True


# ---- anonymous Fusion teaser counter (server-side, keyed on anon_id) -------
def reserve_anon_fusion_turn(anon_id, limit):
    """Atomically reserve one free anonymous teaser turn for `anon_id`. The conversation
    counter is the editable session cookie no more — it lives here, row-locked so two
    concurrent requests can't both pass the cap. Returns (allowed: bool, remaining: int);
    refund with refund_anon_fusion_turn() if the run produced no answer / was blocked. With
    no anon_id we don't block (the per-IP rate limit + spend breaker are the backstops)."""
    limit = int(limit)
    if not anon_id:
        return True, limit
    from sqlalchemy.exc import IntegrityError
    now = datetime.utcnow()
    with db.session_scope() as s:
        row = (s.query(AnonFusionUsage)
               .filter_by(anon_id=anon_id).with_for_update().one_or_none())
        if row is None:
            try:
                with s.begin_nested():            # SAVEPOINT — survive a concurrent insert
                    s.add(AnonFusionUsage(anon_id=anon_id, used=0,
                                          created_at=now, updated_at=now))
                    s.flush()
                row = (s.query(AnonFusionUsage)
                       .filter_by(anon_id=anon_id).with_for_update().one())
            except IntegrityError:
                row = (s.query(AnonFusionUsage)
                       .filter_by(anon_id=anon_id).with_for_update().one())
        used = int(row.used or 0)
        if used >= limit:
            return False, 0
        row.used = used + 1
        row.updated_at = now
        return True, max(0, limit - row.used)


def refund_anon_fusion_turn(anon_id):
    """Give a reserved teaser turn back when the run produced no answer or was blocked."""
    if not anon_id:
        return
    with db.session_scope() as s:
        row = (s.query(AnonFusionUsage)
               .filter_by(anon_id=anon_id).with_for_update().one_or_none())
        if row is not None and int(row.used or 0) > 0:
            row.used = int(row.used) - 1
            row.updated_at = datetime.utcnow()


def anon_fusion_remaining(anon_id, limit):
    """Free anonymous teaser turns left for `anon_id` (the quota endpoint + chat replies)."""
    limit = int(limit)
    if not anon_id:
        return limit
    with db.session_scope() as s:
        row = s.query(AnonFusionUsage).filter_by(anon_id=anon_id).one_or_none()
        used = int(row.used or 0) if row is not None else 0
        return max(0, limit - used)


def prune_anon_fusion_usage(older_than_days=90):
    """Delete anon teaser counters untouched for `older_than_days` (housekeeping — a
    long-absent visitor then gets a fresh teaser). Returns the row count removed."""
    cutoff = datetime.utcnow() - timedelta(days=int(older_than_days))
    with db.session_scope() as s:
        return int(s.query(AnonFusionUsage)
                   .filter(AnonFusionUsage.updated_at < cutoff).delete() or 0)


# ---- Fusion file attachments (R1) -----------------------------------------
def _fusion_attachment_public(a):
    """Customer-safe attachment metadata — never the stored_path or parsed_text."""
    return {"id": a.id, "filename": a.filename, "media_type": a.media_type,
            "kind": a.kind, "file_size": int(a.file_size or 0),
            "char_count": int(a.char_count or 0), "created_at": _iso(a.created_at)}


def _attach_id_list(ids):
    return [int(i) for i in (ids or [])
            if isinstance(i, int) or (isinstance(i, str) and i.isdigit())]


def create_fusion_attachment(conversation_id, account_id, filename, *, media_type=None,
                             kind=FUSION_ATTACH_DOCUMENT, file_size=0, content_hash=None,
                             char_count=0, stored_path=None, parsed_text=None):
    """Persist one attachment row (parsed_text ENCRYPTED at rest). Returns its public dict."""
    if not (conversation_id and account_id):
        return None
    with db.session_scope() as s:
        a = FusionAttachment(
            conversation_id=conversation_id, account_id=str(account_id),
            filename=(filename or "file")[:255], media_type=(media_type or None),
            kind=kind, file_size=int(file_size or 0), content_hash=(content_hash or None),
            char_count=int(char_count or 0), stored_path=(stored_path or None),
            parsed_text=(crypto.encrypt_string(parsed_text) if parsed_text else None),
            status=FUSION_ATTACH_READY)
        s.add(a)
        s.flush()
        return _fusion_attachment_public(a)


def count_pending_fusion_attachments(conversation_id, account_id):
    """How many READY, not-yet-sent (message_index NULL) attachments a conversation holds —
    bounds how many files a user can stage before sending."""
    if not (conversation_id and account_id):
        return 0
    with db.session_scope() as s:
        return int(s.query(FusionAttachment)
                   .filter(FusionAttachment.conversation_id == conversation_id,
                           FusionAttachment.account_id == str(account_id),
                           FusionAttachment.status == FUSION_ATTACH_READY,
                           FusionAttachment.message_index.is_(None)).count())


def get_fusion_attachments(conversation_id, account_id, ids, *, want_text=False):
    """Fetch READY attachments by id, enforcing conversation + account ownership. Returns
    dicts (public metadata, plus decrypted `text` when want_text), ordered to follow `ids`.
    Silently drops any id not owned / not in this conversation / not ready."""
    id_list = _attach_id_list(ids)
    if not (conversation_id and account_id and id_list):
        return []
    with db.session_scope() as s:
        rows = (s.query(FusionAttachment)
                .filter(FusionAttachment.id.in_(id_list),
                        FusionAttachment.conversation_id == conversation_id,
                        FusionAttachment.account_id == str(account_id),
                        FusionAttachment.status == FUSION_ATTACH_READY).all())
        by_id = {a.id: a for a in rows}
        out = []
        for i in id_list:
            a = by_id.get(i)
            if a is None:
                continue
            d = _fusion_attachment_public(a)
            if want_text:
                d["text"] = (crypto.decrypt_string_strict(a.parsed_text) or "") if a.parsed_text else ""
            out.append(d)
        return out


def link_fusion_attachments(conversation_id, account_id, ids, message_index):
    """Stamp message_index on the given owned attachments — called when the user turn that
    carried them is persisted. Returns the count linked."""
    id_list = _attach_id_list(ids)
    if not (conversation_id and account_id and id_list):
        return 0
    with db.session_scope() as s:
        return int((s.query(FusionAttachment)
                    .filter(FusionAttachment.id.in_(id_list),
                            FusionAttachment.conversation_id == conversation_id,
                            FusionAttachment.account_id == str(account_id))
                    .update({FusionAttachment.message_index: int(message_index)},
                            synchronize_session=False)) or 0)


def fusion_attachments_by_message(conversation_id):
    """All of a conversation's linked attachments grouped by message_index — one query for
    rendering the history (each user bubble shows its file chips)."""
    if not conversation_id:
        return {}
    out = {}
    with db.session_scope() as s:
        rows = (s.query(FusionAttachment)
                .filter(FusionAttachment.conversation_id == conversation_id,
                        FusionAttachment.status == FUSION_ATTACH_READY,
                        FusionAttachment.message_index.isnot(None))
                .order_by(FusionAttachment.id.asc()).all())
        for a in rows:
            out.setdefault(int(a.message_index), []).append(_fusion_attachment_public(a))
    return out


def soft_delete_fusion_attachments(conversation_id):
    """Mark a conversation's attachments deleted (called when the conversation is deleted)."""
    if not conversation_id:
        return 0
    with db.session_scope() as s:
        return int((s.query(FusionAttachment)
                    .filter(FusionAttachment.conversation_id == conversation_id,
                            FusionAttachment.status == FUSION_ATTACH_READY)
                    .update({FusionAttachment.status: FUSION_ATTACH_DELETED},
                            synchronize_session=False)) or 0)


def prune_fusion_attachments(older_than_days=30):
    """Hard-delete soft-deleted attachment rows older than the window, returning their
    stored file paths so the caller (fusion_attachments) can remove the encrypted blobs."""
    cutoff = datetime.utcnow() - timedelta(days=int(older_than_days))
    with db.session_scope() as s:
        rows = (s.query(FusionAttachment)
                .filter(FusionAttachment.status == FUSION_ATTACH_DELETED,
                        FusionAttachment.created_at < cutoff).all())
        paths = [a.stored_path for a in rows if a.stored_path]
        for a in rows:
            s.delete(a)
        return paths


def purge_account_conversations(account_id):
    """Hard-delete ALL of an account's Fusion conversations + messages — for a future
    account-deletion path (there is no auto-cascade today). Returns the convo count."""
    if not account_id:
        return 0
    with db.session_scope() as s:
        ids = [row[0] for row in s.query(FusionConversation.id)
               .filter(FusionConversation.account_id == str(account_id)).all()]
        if not ids:
            return 0
        s.query(FusionMessage).filter(FusionMessage.conversation_id.in_(ids)).delete(
            synchronize_session=False)
        s.query(FusionConversation).filter(FusionConversation.id.in_(ids)).delete(
            synchronize_session=False)
        return len(ids)

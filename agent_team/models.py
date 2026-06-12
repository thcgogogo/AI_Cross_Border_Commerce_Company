"""
models.py — The persistent "company brain" schema.

The company's long-term memory:

  companies           one row per account (keyed on the login email)
  runs                one row per agent-team job, with status + ¥ cost
  metrics             every concrete numeric business metric an analysis found
  company_profile     a JSON blob: industry, fiscal convention, KPI defs, …
  glossary            company-specific term → definition
  report_preferences  the user's standing instructions for how reports look
  scheduled_jobs      a saved "定时任务": cron + goal + delivery channels (Phase 5)
  deliveries          one row per attempt to email / WeChat a finished report
  feedback            one 👍/👎 (+ optional comment) on a report section (Phase 6)

Plus the operational / account tables:

  accounts            the unified-API login + ¥ wallet (Phase 7)
  scheduler_heartbeat the scheduler liveness marker (Phase 7)

And the Phase 8 security & abuse-hardening tables:

  phone_codes         pending SMS verification codes (hash only) for signup/reset
  audit_log           append-only security trail (login, topup, download, …)
  platform_spend      rolling ¥ we spend on upstream provider keys (the cost meter)
  platform_breaker    the spend circuit-breaker latch (manual admin reset)

And the Phase 9 payments / subscriptions / fapiao tables:

  orders              one payment order (top-up or subscription period)
  subscriptions       one row per account: plan + current paid-through period
  fapiao_requests     发票 requests against paid orders (manual issuance at first)
  recon_reports       nightly provider-bill vs orders reconciliation results

And the Phase 10 onboarding / support / lifecycle tables:

  onboarding_events   the wizard funnel: one row per (account, step, action)
  support_requests    every 联系支持 message a user leaves (the support log)
  changelog_seen      which CHANGELOG entry each account last viewed (新功能 badge)
  lifecycle_events    dedup marks for lifecycle emails (welcome / day-3 nudge / receipts)

Written in the classic-declarative style so the same code runs on SQLAlchemy
1.4 and 2.x. Each model subclasses the shared Base from db.py.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .db import Base

# The allowed values for runs.status (kept here so the rest of the app imports
# them from one place instead of re-typing the strings).
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"
RUN_STOPPED_BUDGET = "stopped_budget"
RUN_STATUSES = (RUN_RUNNING, RUN_SUCCEEDED, RUN_FAILED, RUN_STOPPED_BUDGET)

# The allowed values for runs.kind (Phase 6 B1): what produced the row.
RUN_KIND_REPORT = "report"   # interactive / scheduled pipeline run — quota-metered
RUN_KIND_IMPORT = "import"   # 历史数据导入 back-fill — free, hidden from 历史报告
RUN_KINDS = (RUN_KIND_REPORT, RUN_KIND_IMPORT)

# Where a scheduled run gets its input files from.
SOURCE_LATEST_UPLOAD = "latest_upload"   # the most recent files the account uploaded
SOURCE_FIXED_FILES = "fixed_files"       # an explicit, pinned list of file paths
SOURCE_MODES = (SOURCE_LATEST_UPLOAD, SOURCE_FIXED_FILES)

# The outcome of a single delivery attempt (one row per channel per run).
DELIVERY_SENT = "sent"
DELIVERY_FAILED = "failed"
DELIVERY_SKIPPED = "skipped"             # a gated fire — nothing was generated
DELIVERY_STATUSES = (DELIVERY_SENT, DELIVERY_FAILED, DELIVERY_SKIPPED)

# A reader's reaction to one report section (Phase 6 feedback loop).
VOTE_UP = "up"
VOTE_DOWN = "down"
VOTE_VALUES = (VOTE_UP, VOTE_DOWN)

# An account's lifecycle state (Phase 7 — accounts moved out of the JSON store).
ACCOUNT_ACTIVE = "active"
ACCOUNT_SUSPENDED = "suspended"
ACCOUNT_STATUSES = (ACCOUNT_ACTIVE, ACCOUNT_SUSPENDED)

# Phase 8 — what a pending SMS code is for (gates which flow may consume it).
CODE_SIGNUP = "signup"
CODE_RESET = "reset"
CODE_PURPOSES = (CODE_SIGNUP, CODE_RESET)

# Phase 8 — audit_log.action values (the security-relevant events we record).
AUDIT_LOGIN = "login"
AUDIT_PASSWORD_CHANGE = "password_change"
AUDIT_TOPUP = "topup"
AUDIT_DOWNLOAD = "download"
AUDIT_ADMIN = "admin_action"
AUDIT_ACTIONS = (AUDIT_LOGIN, AUDIT_PASSWORD_CHANGE, AUDIT_TOPUP,
                 AUDIT_DOWNLOAD, AUDIT_ADMIN)

# Phase 9 — what a payment order buys. Phase 7 (redesign B4) adds the 单次
# ¥29 report: one paid order grants ONE report credit (accounts.report_credits)
# that raises the plan's effective quota — the commitment-free bridge to
# subscription from the brief.
ORDER_TOPUP = "topup"
ORDER_SUBSCRIPTION = "subscription"
ORDER_SINGLE_REPORT = "single_report"
ORDER_TYPES = (ORDER_TOPUP, ORDER_SUBSCRIPTION, ORDER_SINGLE_REPORT)

# Phase 9 — an order's lifecycle. created→paid happens ONLY inside the verified-
# notify transaction (metrics_store.mark_order_paid); created→failed when the
# provider refused to open the trade; paid→refunded when fully refunded.
ORDER_CREATED = "created"
ORDER_PAID = "paid"
ORDER_FAILED = "failed"
ORDER_REFUNDED = "refunded"
ORDER_STATUSES = (ORDER_CREATED, ORDER_PAID, ORDER_FAILED, ORDER_REFUNDED)

# Phase 9 — a subscription's lifecycle (grace is computed from timestamps, not a
# stored state: active means "paid through current_period_end").
SUB_ACTIVE = "active"
SUB_PAST_DUE = "past_due"
SUB_CANCELED = "canceled"
SUB_STATUSES = (SUB_ACTIVE, SUB_PAST_DUE, SUB_CANCELED)

# Phase 9 — fapiao (发票) request states. Issuance is manual (代记账) at first:
# the bookkeeper works off /admin/fapiao + its CSV export and marks each row.
FAPIAO_REQUESTED = "requested"
FAPIAO_ISSUED = "issued"
FAPIAO_REJECTED = "rejected"
FAPIAO_STATUSES = (FAPIAO_REQUESTED, FAPIAO_ISSUED, FAPIAO_REJECTED)

# Phase 10 — what happened at one onboarding-wizard step. The funnel query counts
# DISTINCT accounts per (step, completed); "started" (recorded once at step 0 when
# the wizard opens) is the funnel's denominator.
ONBOARDING_STARTED = "started"
ONBOARDING_COMPLETED = "completed"
ONBOARDING_SKIPPED = "skipped"
ONBOARDING_EVENT_KINDS = (ONBOARDING_STARTED, ONBOARDING_COMPLETED, ONBOARDING_SKIPPED)
# The wizard's steps, so every module agrees on the numbering. 0 is the wizard
# itself opening; 1–6 are the six spec steps (公司信息 → 数据 → KPI →
# 历史导入 → 首份报告 → 定时; plan §3.10, Phase 8).
ONBOARDING_STEPS = (0, 1, 2, 3, 4, 5, 6)
ONBOARDING_LAST_STEP = 6

# Phase 10 — lifecycle email kinds (the dedup marks in lifecycle_events).
LIFECYCLE_WELCOME = "welcome"
LIFECYCLE_NUDGE_DAY3 = "nudge_day3"
LIFECYCLE_RECEIPT = "receipt"
LIFECYCLE_KINDS = (LIFECYCLE_WELCOME, LIFECYCLE_NUDGE_DAY3, LIFECYCLE_RECEIPT)


class Company(Base):
    """One per account. `account_email` mirrors the login email in the JSON
    account store (customers.json) — that store stays the source of truth for
    auth/billing in this phase, so this is a natural key, not a SQL FK."""
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    account_email = Column(String(320), unique=True, nullable=False, index=True)
    company_name = Column(String(255))
    industry = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    runs = relationship("Run", back_populates="company",
                        cascade="all, delete-orphan")
    metrics = relationship("Metric", back_populates="company",
                          cascade="all, delete-orphan")
    profile = relationship("CompanyProfile", back_populates="company",
                          uselist=False, cascade="all, delete-orphan")
    glossary = relationship("GlossaryTerm", back_populates="company",
                           cascade="all, delete-orphan")
    report_preferences = relationship("ReportPreference", back_populates="company",
                                     cascade="all, delete-orphan")
    scheduled_jobs = relationship("ScheduledJob", back_populates="company",
                                 cascade="all, delete-orphan")
    feedback = relationship("Feedback", back_populates="company",
                           cascade="all, delete-orphan")


class Run(Base):
    """One agent-team job."""
    __tablename__ = "runs"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    goal_text = Column(Text)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    status = Column(Enum(*RUN_STATUSES, name="run_status"),
                    default=RUN_RUNNING, nullable=False)
    # What produced this row: "report" (interactive/scheduled pipeline run) or
    # "import" (历史数据导入 back-fill). Import runs must never eat the plan's
    # report quota nor appear in 历史报告 (redesign Phase 6 B1).
    kind = Column(String(32), nullable=False, default=RUN_KIND_REPORT,
                  server_default=RUN_KIND_REPORT)
    # Phase 7 (B4): True when this run was paid for by a 单次报告 credit. Such
    # runs are EXCLUDED from the plan-quota counters (they still appear in
    # 历史报告) — otherwise a consumed credit's run would count against the
    # base quota forever and a SECOND ¥29 purchase on the lifetime trial would
    # buy nothing (used=3 ≥ 2+1).
    credit_funded = Column(Boolean, nullable=False, default=False,
                           server_default="0")
    total_cost_yuan = Column(Float, default=0.0)
    report_path = Column(String(255), nullable=True)

    company = relationship("Company", back_populates="runs")
    metrics = relationship("Metric", back_populates="run",
                          cascade="all, delete-orphan")


class Metric(Base):
    """A single concrete numeric business metric extracted from an analysis."""
    __tablename__ = "metrics"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    period_label = Column(String(64))          # e.g. "2026-W23" or "2026-05"
    period_start = Column(Date, nullable=True)
    period_end = Column(Date, nullable=True)
    metric_name = Column(String(128))          # snake_case English, e.g. total_sales
    metric_name_cn = Column(String(128))       # Chinese name, e.g. 总销售额
    value = Column(Float)
    unit = Column(String(32))                  # e.g. "CNY", "orders", "%"
    dimension = Column(String(128), nullable=True)   # e.g. "region=华东"
    source_file = Column(String(255))
    source_sheet = Column(String(255), nullable=True)
    source_detail = Column(String(255), nullable=True)  # e.g. "rows 2-145"

    company = relationship("Company", back_populates="metrics")
    run = relationship("Run", back_populates="metrics")


class CompanyProfile(Base):
    """A JSON blob of durable facts about the company: industry, fiscal-week
    convention, org notes, KPI definitions, column mappings (filled in Phase 3)."""
    __tablename__ = "company_profile"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"),
                        unique=True, nullable=False, index=True)
    profile_json = Column(Text)

    company = relationship("Company", back_populates="profile")


class GlossaryTerm(Base):
    """A company-specific term and its definition."""
    __tablename__ = "glossary"
    __table_args__ = (UniqueConstraint("company_id", "term", name="uq_glossary_company_term"),)

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    term = Column(String(255), nullable=False)
    definition = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    company = relationship("Company", back_populates="glossary")


class ReportPreference(Base):
    """A standing instruction for how the user wants reports written/formatted."""
    __tablename__ = "report_preferences"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    instruction = Column(Text)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    company = relationship("Company", back_populates="report_preferences")


class ScheduledJob(Base):
    """A saved "定时任务": a cron expression plus everything needed to run the
    normal pipeline headlessly and deliver the result. The scheduler reads the
    active rows at boot and on change; each fire goes through the SAME billing
    path as an interactive run (it is metered against the account's ¥ balance)."""
    __tablename__ = "scheduled_jobs"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    name_cn = Column(String(255), nullable=False)
    cron_expr = Column(String(128), nullable=False)        # 5-field cron, e.g. "0 8 * * mon"
                                                           # (named weekday — APScheduler numbers
                                                           # day_of_week 0=Monday, so numeric "1"
                                                           # silently means TUESDAY here)
    industry_override = Column(String(64), nullable=True)  # None/"" => use the profile
    goal_template = Column(Text)                           # e.g. "生成上周经营周报"
    source_mode = Column(Enum(*SOURCE_MODES, name="source_mode"),
                         default=SOURCE_LATEST_UPLOAD, nullable=False)
    fixed_file_paths = Column(Text, nullable=True)         # JSON list, only for fixed_files
    delivery_channels = Column(Text)                       # JSON list, e.g. ["email","wechat"]
    budget_cny = Column(Float, nullable=True)              # per-job 预算上限 (None => account default)
    active = Column(Boolean, default=True, nullable=False)
    last_run_at = Column(DateTime, nullable=True)
    last_status = Column(String(64), nullable=True)        # mirrors a run status, or "skipped_no_balance"
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    company = relationship("Company", back_populates="scheduled_jobs")
    deliveries = relationship("Delivery", back_populates="job",
                             cascade="all, delete-orphan")


class Delivery(Base):
    """One attempt to push a finished report out a single channel (email / WeChat),
    or a "余额不足" notice when a scheduled fire produced nothing. Credentials and
    webhook URLs are NEVER stored here — only the channel name and the outcome."""
    __tablename__ = "deliveries"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("scheduled_jobs.id"), nullable=True, index=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=True, index=True)
    channel = Column(String(32), nullable=False)           # "email" | "wechat" | "system"
    status = Column(Enum(*DELIVERY_STATUSES, name="delivery_status"),
                    default=DELIVERY_SENT, nullable=False)
    error = Column(Text, nullable=True)                    # human-readable reason on failure/skip
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    job = relationship("ScheduledJob", back_populates="deliveries")


class Feedback(Base):
    """One reader reaction to a single report section (Phase 6 feedback loop):
    a 👍/👎 plus an optional free-text comment. This is the seed of the
    proprietary quality dataset — every vote is kept across companies so the
    founder dashboard (/admin/feedback) can mine what works and what doesn't."""
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=True, index=True)
    section_title = Column(String(255))                    # which report section
    vote = Column(Enum(*VOTE_VALUES, name="feedback_vote"), nullable=False)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    company = relationship("Company", back_populates="feedback")


class Account(Base):
    """One customer account — the unified-API storefront login + ¥ wallet.

    Phase 7 moves accounts OUT of the old memory_store/customers.json file and into
    this table so balances are safe under concurrency (a real SELECT … FOR UPDATE on
    Postgres) and survive across containers. The shape is a superset of the spec's
    minimal `accounts` columns: it also carries the gateway key fingerprint, the
    chosen models, and the usage log that the dashboard already relies on, so moving
    the store to SQL is a ZERO behaviour change for the rest of the app.

    SECURITY: the gateway key is stored ONLY as a SHA-256 hash (+ a short display
    prefix and last-4); the password ONLY as a salted Werkzeug hash. Neither the raw
    key nor the password is ever persisted, returned, or logged."""
    __tablename__ = "accounts"

    # The id keeps its historical "cus_<hex>" form so existing session cookies and
    # the companies.account_email natural key keep resolving unchanged.
    id = Column(String(32), primary_key=True)
    email = Column(String(320), unique=True, nullable=True, index=True)
    phone = Column(String(32), nullable=True)              # spec column (not yet surfaced in UI)
    name = Column(String(255))
    company = Column(String(255), nullable=True)
    password_hash = Column(String(255), nullable=True)     # salted hash only — never plaintext
    key_hash = Column(String(64), nullable=True, index=True)   # SHA-256 hex of the gateway key
    key_prefix = Column(String(32), nullable=True)         # e.g. "tng-sk-Ab3D"
    key_last4 = Column(String(8), nullable=True)
    models_json = Column(Text, nullable=True)              # JSON list of purchased model ids
    # Money as fixed-precision NUMERIC (never float): None means an unmetered
    # ("unlimited") account; a number is the remaining ¥ balance.
    balance_yuan = Column(Numeric(12, 2), nullable=True)
    spent_yuan = Column(Numeric(12, 2), nullable=False, default=0)
    requests = Column(Integer, nullable=False, default=0)
    usage_json = Column(Text, nullable=True)               # JSON list, capped most-recent N
    status = Column(Enum(*ACCOUNT_STATUSES, name="account_status"),
                    default=ACCOUNT_ACTIVE, nullable=False)
    # Phase 8 abuse controls. rpm_limit caps this key's gateway requests/min
    # (config.DEFAULT_KEY_RPM = 60 by default). daily_cap_yuan is a per-account
    # ceiling on UPSTREAM ¥/day (None => fall back to config.account_daily_cap_yuan).
    rpm_limit = Column(Integer, nullable=False, default=60)
    daily_cap_yuan = Column(Numeric(12, 2), nullable=True)
    # Phase 8 session hardening: a server-side token embedded in the signed session
    # cookie. "Log out everywhere" rotates it, which instantly invalidates every
    # cookie still carrying the old value. NULL on legacy rows => the check is
    # skipped, so existing logins are not forcibly dropped by the upgrade.
    session_token = Column(String(32), nullable=True)
    # Phase 7 (redesign B4): unconsumed 单次报告 ¥29 purchases. Each paid
    # single_report order adds one; the quota gate counts them on top of the
    # plan quota; the run that lands beyond the base quota consumes one.
    report_credits = Column(Integer, nullable=False, default=0, server_default="0")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SchedulerHeartbeat(Base):
    """A liveness marker the dedicated scheduler process writes every ~minute.

    /healthz reads the latest beat: if it is older than config.HEARTBEAT_STALE_SECS
    the scheduler is reported stale (so killing its container is observable within
    ~2 min). A single logical row is reused (keyed by `name`), so this never grows."""
    __tablename__ = "scheduler_heartbeat"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), unique=True, nullable=False, default="scheduler")
    beat_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PhoneCode(Base):
    """A pending SMS verification code (Phase 8). We persist ONLY a SHA-256 hash of
    the 6-digit code — never the code itself. One row per send, so the rows double
    as the per-phone send-rate log (≤ SMS_MAX_SENDS_PER_HOUR per rolling hour).
    `attempts` counts wrong guesses; once it reaches SMS_MAX_VERIFY_ATTEMPTS the
    code is burned (consumed=True), so a further try fails even with the right
    digits. A code is valid only while unconsumed and before `expires_at`."""
    __tablename__ = "phone_codes"

    id = Column(Integer, primary_key=True)
    phone = Column(String(32), nullable=False, index=True)
    code_hash = Column(String(64), nullable=False)             # SHA-256 hex of the code
    purpose = Column(Enum(*CODE_PURPOSES, name="code_purpose"), nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    consumed = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)


class AuditLog(Base):
    """An append-only security audit trail (Phase 8): who did what, from where, and
    when. A row is written on login, password change, top-up, download, and admin
    actions. It holds NO secrets — `detail` is a short, scrubbed note (an amount, a
    filename, a target id), never a password, API key, or webhook URL."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True)
    account_id = Column(String(32), nullable=True, index=True)   # loose ref to accounts.id
    action = Column(Enum(*AUDIT_ACTIONS, name="audit_action"), nullable=False, index=True)
    ip = Column(String(64), nullable=True)
    detail = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class PlatformSpend(Base):
    """Rolling record of what WE spend on upstream provider keys (Phase 8) — the
    cost meter behind the circuit breaker. One row per billed upstream call.
    `hour_bucket` ("YYYY-MM-DDTHH") makes hourly/daily sums cheap; `account_id`
    attributes spend for the per-account daily cap; `created_at` lets the breaker
    sum only spend recorded AFTER its last manual reset."""
    __tablename__ = "platform_spend"

    id = Column(Integer, primary_key=True)
    hour_bucket = Column(String(13), nullable=False, index=True)   # "2026-06-10T14"
    provider = Column(String(32), nullable=False, index=True)
    account_id = Column(String(32), nullable=True, index=True)
    yuan = Column(Numeric(12, 4), nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class PlatformBreaker(Base):
    """The spend circuit-breaker's latch (Phase 8) — a single logical row reused
    forever (keyed by `name`). When `tripped`, new runs are refused with a Chinese
    "系统繁忙" message until an admin resets it at /admin/circuit-breaker. `cleared_at`
    records the last manual reset: rolling spend sums count only platform_spend rows
    created AFTER it, so a reset truly restores service and the breaker re-trips only
    on NEW over-cap spend."""
    __tablename__ = "platform_breaker"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), unique=True, nullable=False, default="platform")
    tripped = Column(Boolean, nullable=False, default=False)
    scope = Column(String(16), nullable=True)        # "hourly" | "daily" | "account"
    reason = Column(String(255), nullable=True)      # short Chinese note, no secrets
    tripped_at = Column(DateTime, nullable=True)
    cleared_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Order(Base):
    """One payment order (Phase 9): a 充值 top-up or one subscription period.

    `order_no` is OUR merchant order number — the out_trade_no every provider
    echoes back in its signed notify and its bill, so it is the join key for the
    paid transition, refunds, fapiao, and reconciliation. Money is NUMERIC to the
    fen; the paid amount is only ever taken from the VERIFIED notify payload and
    must equal `amount_yuan` exactly. `refunded_yuan` accumulates partial refunds;
    status flips to `refunded` only when fully refunded."""
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    order_no = Column(String(64), unique=True, nullable=False, index=True)
    account_id = Column(String(32), nullable=False, index=True)   # loose ref accounts.id
    type = Column(Enum(*ORDER_TYPES, name="order_type"), nullable=False)
    plan_code = Column(String(16), nullable=True)    # subscription orders only
    amount_yuan = Column(Numeric(12, 2), nullable=False)
    provider = Column(String(32), nullable=False)    # "alipay" | "wechat" | "mock"
    provider_txn_id = Column(String(128), nullable=True)
    status = Column(Enum(*ORDER_STATUSES, name="order_status"),
                    default=ORDER_CREATED, nullable=False, index=True)
    subject = Column(String(255), nullable=True)     # the cashier line item title
    refunded_yuan = Column(Numeric(12, 2), nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    paid_at = Column(DateTime, nullable=True, index=True)

    fapiao_requests = relationship("FapiaoRequest", back_populates="order",
                                   cascade="all, delete-orphan")


class Subscription(Base):
    """An account's (single) subscription (Phase 9). One row per account, updated
    in place — payment history lives in `orders`. Renewal is USER-CONFIRMED (the
    China norm): the daily tick creates a renewal order at T-3d and notifies the
    user with a pay link; nothing is ever auto-charged. `renewal_order_no` points
    at the open renewal order so repeated ticks don't spam duplicates; paying any
    subscription order clears it and extends the period."""
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True)
    account_id = Column(String(32), unique=True, nullable=False, index=True)
    plan_code = Column(String(16), nullable=False)   # "basic" | "pro"
    status = Column(Enum(*SUB_STATUSES, name="sub_status"),
                    default=SUB_ACTIVE, nullable=False)
    current_period_start = Column(DateTime, nullable=False)
    current_period_end = Column(DateTime, nullable=False)
    cancel_at_period_end = Column(Boolean, nullable=False, default=False)
    renewal_order_no = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class FapiaoRequest(Base):
    """A customer's 发票 (fapiao) request against one PAID order (Phase 9).

    Captures the invoice title (公司抬头), tax number (税号) and delivery email.
    Issuance is manual at first: the bookkeeper (代记账) works from /admin/fapiao
    and its CSV export, then marks the row issued (which emails the customer) or
    rejected with a note. An e-fapiao API can later slot in behind the same rows
    (see payments.issue_electronic_fapiao)."""
    __tablename__ = "fapiao_requests"

    id = Column(Integer, primary_key=True)
    account_id = Column(String(32), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False, index=True)
    title = Column(String(255), nullable=False)      # 公司抬头
    tax_no = Column(String(32), nullable=False)      # 税号 (统一社会信用代码)
    email = Column(String(320), nullable=True)       # where the fapiao is sent
    status = Column(Enum(*FAPIAO_STATUSES, name="fapiao_status"),
                    default=FAPIAO_REQUESTED, nullable=False, index=True)
    note = Column(String(255), nullable=True)        # bookkeeper note / reject reason
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    order = relationship("Order", back_populates="fapiao_requests")


class ReconReport(Base):
    """One nightly reconciliation result (Phase 9): a provider's bill CSV for one
    (China-local) day compared against our orders. A single row per provider+day,
    upserted on re-runs. `mismatches_json` holds the mismatch list — order_no +
    kind (missing_order / amount_mismatch / status_mismatch / missing_in_bill) +
    a short Chinese detail — rendered on /admin/reconciliation."""
    __tablename__ = "recon_reports"
    __table_args__ = (UniqueConstraint("provider", "bill_date",
                                       name="uq_recon_provider_date"),)

    id = Column(Integer, primary_key=True)
    provider = Column(String(32), nullable=False)
    bill_date = Column(String(10), nullable=False)   # "YYYY-MM-DD" (China local)
    bill_rows = Column(Integer, nullable=False, default=0)
    matched = Column(Integer, nullable=False, default=0)
    mismatch_count = Column(Integer, nullable=False, default=0)
    mismatches_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class OnboardingEvent(Base):
    """One onboarding-wizard funnel event (Phase 10): account X did `event` at
    `step`. Append-only — the wizard's resumable state and the admin funnel are
    both derived from these rows (latest action per step wins for the state;
    DISTINCT accounts per step for the funnel)."""
    __tablename__ = "onboarding_events"

    id = Column(Integer, primary_key=True)
    account_id = Column(String(32), nullable=False, index=True)   # loose ref accounts.id
    step = Column(Integer, nullable=False, index=True)            # 0 (wizard) … 6
    event = Column(Enum(*ONBOARDING_EVENT_KINDS, name="onboarding_event_kind"),
                   nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class SupportRequest(Base):
    """One 联系支持 message (Phase 10). Logged for every submission of the
    floating support form — alongside (never instead of) the 企业微信 QR + mailto
    the user sees. `message` is user text; it is always HTML-escaped on render."""
    __tablename__ = "support_requests"

    id = Column(Integer, primary_key=True)
    account_id = Column(String(32), nullable=True, index=True)    # null = not logged in
    email = Column(String(320), nullable=True)                    # reply-to the user left
    message = Column(Text, nullable=False)
    page = Column(String(64), nullable=True)                      # where the form was opened
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class ChangelogSeen(Base):
    """Which CHANGELOG.md entry an account last viewed (Phase 10). One row per
    account, upserted when /changelog is viewed; the 新功能 menu badge shows while
    the newest entry id differs from `last_seen`."""
    __tablename__ = "changelog_seen"

    id = Column(Integer, primary_key=True)
    account_id = Column(String(32), unique=True, nullable=False, index=True)
    last_seen = Column(String(64), nullable=True)    # the newest entry id at view time
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class LifecycleEvent(Base):
    """A dedup mark for one lifecycle email (Phase 10): welcome at signup, the
    day-3 "还没生成第一份报告？" nudge, a renewal receipt. The senders check for an
    existing (account, kind[, detail]) row before sending, so a re-run of the
    daily tick or a replayed notify can never spam the customer."""
    __tablename__ = "lifecycle_events"

    id = Column(Integer, primary_key=True)
    account_id = Column(String(32), nullable=False, index=True)
    kind = Column(Enum(*LIFECYCLE_KINDS, name="lifecycle_kind"),
                  nullable=False, index=True)
    detail = Column(String(255), nullable=True)      # e.g. the receipt's order_no
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

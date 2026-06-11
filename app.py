"""
app.py — The website + the "start a job" engine.

Run this file (python app.py) and open the printed URL in your browser.

What it does:
  - Serves the simple web UI in the frontend/ folder.
  - POST /api/run        : accepts your goal + uploaded files, then kicks off
                           the agent team in a BACKGROUND thread and immediately
                           returns a job_id (so the page never freezes).
  - GET  /api/status/ID  : the page polls this a few times a second to show the
                           live activity feed and, when finished, the report.
  - GET  /api/agents     : the org chart, so the UI draws your team automatically.
"""

import csv
import io
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import escape as _h
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import (Flask, Response, g, jsonify, redirect, request, send_file,
                   send_from_directory, session)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from sqlalchemy import text
from werkzeug.utils import safe_join

from admin import queries as admin_queries
from agent_team import (
    alerts,
    config,
    crypto,
    db,
    delivery,
    helpdocs,
    industry_packs,
    lifecycle,
    metrics_store,
    onboarding,
    payments,
    platform_guard,
    providers,
    reconciliation,
    security,
    sms,
    trends,
)
from agent_team.customers import CustomerStore
from agent_team.file_tools import parse_file
from agent_team.memory import SharedMemory
from agent_team.models import (
    AUDIT_ADMIN,
    AUDIT_DOWNLOAD,
    AUDIT_LOGIN,
    AUDIT_PASSWORD_CHANGE,
    AUDIT_TOPUP,
    CODE_RESET,
    CODE_SIGNUP,
    DELIVERY_FAILED,
    DELIVERY_SENT,
    FAPIAO_ISSUED,
    FAPIAO_REJECTED,
    FAPIAO_REQUESTED,
    ONBOARDING_COMPLETED,
    ONBOARDING_LAST_STEP,
    ONBOARDING_SKIPPED,
    ONBOARDING_STARTED,
    ORDER_CREATED,
    ORDER_PAID,
    ORDER_SUBSCRIPTION,
    ORDER_TOPUP,
    RUN_FAILED,
    RUN_KIND_IMPORT,
    RUN_STOPPED_BUDGET,
    RUN_SUCCEEDED,
    SOURCE_FIXED_FILES,
    SOURCE_LATEST_UPLOAD,
    VOTE_DOWN,
    VOTE_VALUES,
)
from agent_team.orchestrator import Orchestrator
from agent_team.providers import ModelRouter
from agent_team.scheduler import ReportScheduler
from agent_team.token_manager import BudgetExceededError, TokenManager

# override=True makes the .env file authoritative, so a stale ANTHROPIC_API_KEY
# left over in your shell (a very common cause of "401 invalid key") can't shadow it.
load_dotenv(override=True)

# This app talks to Anthropic's Claude API. If your shell points the Anthropic SDK
# somewhere else — e.g. a DeepSeek/proxy redirect via ANTHROPIC_BASE_URL (common in
# "use model X with Claude Code" guides, usually added to ~/.zshrc) — then a perfectly
# valid Anthropic key gets delivered to that other server and bounces back as a 401.
# The override=True above can't catch this, because ANTHROPIC_BASE_URL isn't in .env.
# So we pin THIS process back to the official Anthropic API. Your shell and any
# Claude Code / DeepSeek setup are left completely untouched.
_base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
if _base and "api.anthropic.com" not in _base:
    print(f"  [i]  Ignoring ANTHROPIC_BASE_URL={_base!r} from your shell — this app "
          f"always uses the official Anthropic API with your .env key.")
    os.environ.pop("ANTHROPIC_BASE_URL", None)
    # Drop the matching proxy bearer token so only your .env key (x-api-key) is used.
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
MEMORY_DIR = os.path.join(BASE_DIR, "memory_store")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")  # real files the team generates
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(MEMORY_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(__name__, static_folder="frontend", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB upload cap


# Website logins are stored in a signed session cookie, which needs a secret key.
# Prefer an explicit SECRET_KEY / FLASK_SECRET_KEY from the environment; otherwise
# (dev only) generate one once and persist it to a git-ignored file so existing
# logins survive a restart without ever hard-coding a secret into the source.
#
# PRODUCTION (APP_ENV=production) REFUSES to boot without an env secret: a generated
# or on-disk dev secret must never silently protect real user sessions.
def _flask_secret():
    env = config.secret_key()
    if env:
        return env
    if config.is_production():
        raise RuntimeError(
            "SECRET_KEY is required in production. Set SECRET_KEY (a long random "
            "string, e.g. `python -c \"import secrets;print(secrets.token_hex(32))\"`) "
            "in the environment / .env before starting. Refusing to boot with a "
            "generated dev secret.")
    path = os.path.join(MEMORY_DIR, ".flask_secret")
    try:
        with open(path) as f:
            saved = f.read().strip()
        if saved:
            return saved
    except OSError:
        pass
    secret = secrets.token_hex(32)
    try:
        with open(path, "w") as f:
            f.write(secret)
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


app.secret_key = _flask_secret()

# Phase 8 — session cookie hardening. HttpOnly (JS can't read the cookie) + SameSite
# Lax (not sent on cross-site POSTs → CSRF defense) are always on. Secure (cookie
# only over HTTPS) is on ONLY in production: forcing it in local http dev would make
# the login cookie silently never come back, so dev/tests keep working unchanged.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.is_production(),
)


# ---------------------------------------------------------------------------
# Phase 8 — rate limiting (flask-limiter). Storage is in-process memory by default
# (fine for one process / dev / tests); point RATELIMIT_STORAGE_URI at redis:// for
# multi-worker gunicorn. Limits are applied per-route below; there is no global
# default. The limit STRINGS are passed as callables so they're read fresh per
# request — a test (or an env edit) can retune them without re-importing the app.
# ---------------------------------------------------------------------------
def _run_rate_key():
    """Rate-limit run-starts PER ACCOUNT (falling back to client IP when somehow
    unauthenticated), so one customer's burst can't starve everyone on a shared NAT."""
    return session.get("customer_id") or get_remote_address()


limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri=config.ratelimit_storage_uri(),
    enabled=config.ratelimit_enabled(),
    headers_enabled=True,
)

# VERBATIM Chinese refusal reused for the 429 (matches the platform-breaker copy).
_RATE_LIMITED_MESSAGE = "请求过于频繁，请稍后再试"


@app.errorhandler(429)
def _too_many_requests(e):
    """Every rate-limit rejection (auth + run-start) answers with a Chinese message
    and HTTP 429, in the same OpenAI-ish JSON envelope the rest of the API uses."""
    return jsonify({"error": _RATE_LIMITED_MESSAGE}), 429


# ---------------------------------------------------------------------------
# Phase 8 — per-key gateway requests-per-minute cap. flask-limiter (above) covers
# auth + run-starts; the gateway needs a PER-KEY rate that honours each account's
# accounts.rpm_limit (default config.DEFAULT_KEY_RPM). We keep a tiny in-process
# sliding-window counter — a deque of recent hit timestamps per key — which is
# enough for a single process / dev. A multi-worker deploy would move this to the
# shared limiter storage (redis), but this keeps the gateway path self-contained.
# ---------------------------------------------------------------------------
_RPM_HITS = {}                       # key -> deque[float] of recent request times
_RPM_LOCK = threading.Lock()


def _rpm_check(key, limit):
    """Record a request for `key` and return True while it stays within `limit`
    requests in the trailing 60 seconds, else False (over cap). A limit of 0/None
    disables the cap. Thread-safe — the gateway is hit from many request threads."""
    if not limit or limit <= 0:
        return True
    now = time.monotonic()
    cutoff = now - 60.0
    with _RPM_LOCK:
        hits = _RPM_HITS.get(key)
        if hits is None:
            hits = _RPM_HITS[key] = deque()
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


# ---------------------------------------------------------------------------
# Observability (Phase 7): error tracking (Sentry) + structured request logging
# ---------------------------------------------------------------------------
# Sentry is OPT-IN — it only initialises when SENTRY_DSN is set, so local dev and
# the whole test suite stay fully offline, and a missing sentry-sdk never breaks
# boot. send_default_pii stays False so request bodies, cookies, and headers
# (which can carry API keys / passwords) are never shipped off-box.
def _init_sentry():
    dsn = config.sentry_dsn()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
    except ImportError:
        print("  [i]  SENTRY_DSN is set but sentry-sdk is not installed — error "
              "tracking is OFF (pip install 'sentry-sdk[flask]').")
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=config.app_env(),
        integrations=[FlaskIntegration()],
        send_default_pii=False,
    )
    return True


_SENTRY_ON = _init_sentry()

# One structured JSON line per request → stdout (captured by gunicorn / Docker).
# We log only request METADATA — request id, account id, route, status, latency.
# NEVER the body, query string, headers, passwords, API keys, or file contents.
_ACCESS_LOG = logging.getLogger("company_agent.access")
if not _ACCESS_LOG.handlers:
    _access_handler = logging.StreamHandler()
    _access_handler.setFormatter(logging.Formatter("%(message)s"))  # message IS the JSON
    _ACCESS_LOG.addHandler(_access_handler)
    _ACCESS_LOG.setLevel(logging.INFO)
    _ACCESS_LOG.propagate = False


@app.before_request
def _request_start():
    g.request_id = uuid.uuid4().hex[:12]
    g.request_started = time.perf_counter()


@app.after_request
def _request_log(response):
    # /healthz is polled every few seconds by nginx / the load balancer — skip it
    # so the access log stays signal, not heartbeat noise. Every other request is
    # logged as one JSON line; the request id is also echoed back for correlation.
    if request.path != "/healthz":
        started = getattr(g, "request_started", None)
        latency_ms = round((time.perf_counter() - started) * 1000, 1) if started else None
        _ACCESS_LOG.info(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": "info",
            "request_id": getattr(g, "request_id", None),
            "account_id": session.get("customer_id"),
            "method": request.method,
            "route": request.path,
            "status": response.status_code,
            "latency_ms": latency_ms,
        }, ensure_ascii=False))
    response.headers["X-Request-Id"] = getattr(g, "request_id", "") or ""
    return response


# Keep running jobs in memory so /api/status is instant. We also persist every
# job to memory_store/ so nothing is lost if the server restarts mid-run.
JOBS = {}

# Historical-import jobs (Company Brain → 导入历史数据): job_id -> progress dict.
# These are short-lived back-fills (no full report pipeline), so they live only in
# memory and are polled via /api/company-brain/import/<job_id>.
IMPORT_JOBS = {}
IMPORT_MAX_FILES = 12           # how many past spreadsheets you can import at once
IMPORT_EST_OUTPUT_TOKENS = 700  # rough JSON-array size per file, for the ¥ estimate
IMPORT_EST_SECONDS_PER_FILE = 15  # rough wall-clock per file — the customer-facing
                                  # estimate (customers never see the ¥ figures)

# The team's "CFO" — one shared, persistent ¥ spend tracker for the whole app.
TOKENS = TokenManager(
    store_path=os.path.join(MEMORY_DIR, "tokens.json"),
    total_budget=config.TOTAL_COST_BUDGET_CNY,
    warn_at=config.WARN_AT_REMAINING,
    task_budgets=config.COST_BUDGETS_CNY,
)

# The unified-API "storefront" ledger — issues customer gateway keys, tracks each
# account's chosen models + balance + usage. Persists to memory_store/customers.json.
CUSTOMERS = CustomerStore(store_path=os.path.join(MEMORY_DIR, "customers.json"))

# The persistent "company brain" — a SQLite DB (data/company_brain.db) that
# remembers what every PAST run learned (metrics, profile, glossary, report
# preferences), keyed on the account email. Initialise it, then back-fill a
# company row for each existing account (idempotent) so memory works for them too.
db.init_db()
_migrated_companies = metrics_store.migrate_accounts_to_companies(
    os.path.join(MEMORY_DIR, "customers.json"))
if _migrated_companies:
    print(f"  [i]  Company brain: created {_migrated_companies} company record(s) "
          f"from existing accounts.")

# Phase 7: accounts now live in the DB (the `accounts` table). If a legacy
# memory_store/customers.json survives from before the migration, absorb it ONCE
# (idempotent — accounts that already exist by id/email are skipped). Best-effort:
# a missing or unreadable file must never stop the site from booting.
try:
    _imported_accounts = CUSTOMERS.import_legacy_json()
except Exception as _imp_err:  # noqa: BLE001 — boot must survive any import failure
    _imported_accounts = 0
    print(f"  [i]  Legacy account import skipped "
          f"({type(_imp_err).__name__}: {_imp_err}).")
if _imported_accounts:
    print(f"  [i]  Accounts: imported {_imported_accounts} legacy account(s) "
          f"from customers.json into the database.")


# Vertical template packs (Phase 4) — fail LOUDLY at app start if any pack on disk
# is malformed, so a broken pack can never silently degrade a run. Pack CONTENT is
# otherwise re-read fresh on every run, so it stays hot-editable without a restart.
def _validate_industry_packs_or_exit():
    """Validate every industry pack on disk at boot. Converts a PackError into a
    clean SystemExit naming the offending file + key (no traceback), so the app
    refuses to start on a broken pack."""
    try:
        industry_packs.validate_all_packs()
    except industry_packs.PackError as e:
        raise SystemExit(f"  [!!]  Industry pack error — the app cannot start: {e}")


_validate_industry_packs_or_exit()


# Phase 8 — make sure no real secret has leaked into a browser-served file. The
# frontend/ directory is served verbatim to every visitor, so if a SECRET_KEY,
# upstream API key, SMTP password, FERNET key, DB URL, etc. ever ends up pasted
# into index.html / app.js / a bundled asset, that's a disclosure. We compare the
# ACTUAL values from the environment against the bytes of each served file.
_SECRET_NAME_HINTS = ("KEY", "SECRET", "PASS", "TOKEN", "WEBHOOK", "DSN", "CREDENTIAL")


def _known_secret_values():
    """{env-var-name: value} for env vars whose NAME looks secret-bearing. The short
    ones are dropped by security.scan_files_for_secrets, so benign vars never match."""
    return {name: val for name, val in os.environ.items()
            if any(hint in name.upper() for hint in _SECRET_NAME_HINTS)}


def _scan_static_for_secrets_or_exit():
    """Boot guard: refuse to serve if a known secret VALUE appears in any static
    file. In production a hit is fatal (SystemExit); in dev we warn loudly. Only the
    file path + the env-var NAME are reported — never the secret value itself."""
    secrets_map = _known_secret_values()
    if not secrets_map:
        return
    static_dir = app.static_folder
    paths = [os.path.join(root, fn)
             for root, _dirs, files in os.walk(static_dir) for fn in files]
    hits = security.scan_files_for_secrets(paths, secrets_map)
    if not hits:
        return
    where = ", ".join(sorted({"%s←$%s" % (os.path.relpath(h["path"]), h["label"])
                              for h in hits}))
    if config.is_production():
        raise SystemExit("  [!!]  Secret leak — a known secret value appears in a "
                         "served static file; refusing to start: " + where)
    print("  [!!]  WARNING: a known secret value appears in a served static file: "
          + where)


_scan_static_for_secrets_or_exit()

# Per-agent model picks (from the UI dropdowns) are persisted here so they
# survive a server restart. Loaded once at startup; saved on every change.
MODEL_OVERRIDES_PATH = os.path.join(MEMORY_DIR, "model_overrides.json")


def _load_model_overrides():
    try:
        with open(MODEL_OVERRIDES_PATH) as f:
            saved = json.load(f) or {}
    except (FileNotFoundError, ValueError, OSError):
        return
    for agent_id, model in saved.items():
        try:
            config.set_model_override(agent_id, model)
        except ValueError:
            pass  # skip entries for agents/models that no longer exist


def _save_model_overrides():
    try:
        with open(MODEL_OVERRIDES_PATH, "w") as f:
            json.dump(config.MODEL_OVERRIDES, f, indent=2)
    except OSError:
        pass


def _agents_payload():
    """The org chart for the UI. `model` is the EFFECTIVE model (UI pick wins).
    `name_cn` is the customer-facing Chinese role name (redesign B1) — the
    restyled live feed shows it instead of the English title."""
    return [
        {"id": a["id"], "name": a["name"], "title": a["title"],
         "name_cn": a.get("name_cn", ""),
         "reports_to": a["reports_to"], "model": config.effective_model(a["id"])}
        for a in config.AGENTS
    ]


_load_model_overrides()


# ---------------------------------------------------------------------------
# Health & monitoring (Phase 7)
# ---------------------------------------------------------------------------
@app.route("/healthz")
def healthz():
    """Liveness/readiness probe for nginx, the load balancer, and `docker compose`.

      - "db": "ok"/"error" — a trivial SELECT 1 round-trip.
      - "scheduler": ISO timestamp of the dedicated scheduler process's last
        heartbeat, or null if it has never beaten.
      - "scheduler_stale": true once that beat is older than HEARTBEAT_STALE_SECS,
        so a killed scheduler container becomes observable within ~2 minutes.

    Returns 200 while the database is reachable (the website can serve) and 503
    when it is not. A stale scheduler is reported as "degraded" but does NOT fail
    the probe — a scheduler outage must not take the website down with it."""
    db_ok = True
    try:
        with db.session_scope() as s:
            s.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    last_beat = metrics_store.last_scheduler_heartbeat()
    stale = None
    if last_beat:
        try:
            beat = datetime.fromisoformat(last_beat)
            if beat.tzinfo is None:
                beat = beat.replace(tzinfo=timezone.utc)  # heartbeat is stored in UTC
            age = (datetime.now(timezone.utc) - beat).total_seconds()
            stale = age > config.HEARTBEAT_STALE_SECS
        except ValueError:
            stale = None

    payload = {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "scheduler": last_beat,
        "scheduler_stale": stale,
    }
    return jsonify(payload), (200 if db_ok else 503)


# ---------------------------------------------------------------------------
# Pages & static files
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    return send_from_directory("frontend", "index.html")


@app.route("/login")
def login_page():
    """SPA entry that pops the login modal — the target of the /company-brain guard."""
    return send_from_directory("frontend", "index.html")


@app.route("/company-brain")
def company_brain_page():
    """The "企业大脑" page. Logged-out visitors are redirected to /login (302); the
    SPA then opens the Company Brain workspace once a session exists (the new
    router normalizes this legacy path to /brain)."""
    if not _current_customer():
        return redirect("/login")
    return send_from_directory("frontend", "index.html")


# UI redesign Phase 2 — the SPA path router (plan §5). Every customer page is a
# real path served by the same shell; logged-out visitors bounce to /login first
# (the same pattern as /company-brain above). /reports/<run_id> additionally
# gets per-report ownership checks with B7 (Phase 4) — here a session is enough
# to load the shell, and the report data API enforces ownership already.
FOUNDER_SPA_PAGES = ("team", "costs", "models", "fusion", "api")


@app.route("/new")
@app.route("/reports")
@app.route("/reports/<int:run_id>")
@app.route("/schedule")
@app.route("/brain")
@app.route("/account")
@app.route("/founder/<founder_page>")
def spa_page(run_id=None, founder_page=None):
    """Serve the SPA shell for the redesigned customer pages (and the founder
    machinery views — their is_admin gating is in the UI; the sensitive /admin*
    pages keep their own server-side checks)."""
    if founder_page is not None and founder_page not in FOUNDER_SPA_PAGES:
        return jsonify({"error": "not found"}), 404
    if not _current_customer():
        return redirect("/login")
    return send_from_directory("frontend", "index.html")


@app.route("/api/agents")
def agents():
    """The org chart, used by the UI to draw the team."""
    return jsonify(_agents_payload())


@app.route("/api/models")
def models_list():
    """The catalog a user may assign to an agent / bundle into a key (drives the
    dropdowns, the Models page, the Fusion council, and the dashboard editor).

    Each row is enriched with a per-model `available` flag (is the provider's key
    configured in .env right now?) so the dashboard editor can show a live/needs-key
    dot for every model — not just the ones already on a key."""
    load_dotenv(override=True)  # so `available` reflects keys you may have just added
    out = []
    for row in config.MODEL_CHOICES:
        m = dict(row)
        m["available"] = providers.model_available(row["id"])
        kind = config.model_kind(row["id"])
        m["kind"] = kind
        m["kind_label"] = config.kind_label(kind)
        # Money is shown in ¥, so attach ¥ pricing alongside the US$ catalog values.
        if kind == "text":
            m["input_per_mtok_cny"] = round(config.usd_to_cny(row.get("input_per_mtok", 0)), 4)
            m["output_per_mtok_cny"] = round(config.usd_to_cny(row.get("output_per_mtok", 0)), 4)
        else:
            m["price_cny"] = round(config.usd_to_cny(row.get("price", 0)), 6)
        out.append(m)
    return jsonify(out)


@app.route("/api/meta")
def meta():
    """Currency + model-function metadata so the UI can format ¥ and build the
    function tabs / modality lines without hard-coding any of it."""
    return jsonify({
        # Redesign B1 — product identity + the zero-prompt report templates, so
        # every customer-facing surface brands and composes goals from config.
        "product_name": config.product_name(),
        "product_tagline": config.product_tagline(),
        "product_byline": config.product_byline(),
        "report_goal_templates": config.REPORT_GOAL_TEMPLATES,
        # Landing/pricing copy (purchasable from Phase 7 / B4 on).
        "single_report_price_yuan": config.SINGLE_REPORT_PRICE_YUAN,
        "currency": config.CURRENCY,
        "currency_symbol": config.CURRENCY_SYMBOL,
        "usd_to_cny": config.USD_TO_CNY,
        "start_balance_cny": config.START_BALANCE_CNY,
        "kinds": config.KINDS,
        "kind_order": config.KIND_ORDER,
        # So the sign-up/reset UI knows whether to ask for a phone + SMS code.
        "phone_verification": config.phone_verification_required(),
        # Phase 10 — the 联系支持 popup's contact targets (both may be "").
        "support_email": config.support_email(),
        "support_qr_url": config.support_wechat_qr_url(),
    })


@app.route("/api/providers")
def providers_list():
    """The model providers (id + display label), so the UI can group models by
    company on the Models and the unified-API store pages. Config-driven."""
    return jsonify([{"id": pid, "label": p.get("label", pid)}
                    for pid, p in config.PROVIDERS.items()])


@app.route("/api/industries")
def industries():
    """The industry template packs on disk (key + Chinese display name), for the
    per-run override dropdown on the task form. Read fresh so a newly-added pack
    shows up without a restart."""
    return jsonify(industry_packs.available_industries())


@app.route("/api/agents/model", methods=["POST"])
def set_agent_model():
    """Assign one agent to a specific model. Persists and returns the updated team."""
    data = request.get_json(silent=True) or {}
    agent_id = (data.get("agent_id") or "").strip()
    model = (data.get("model") or "").strip()
    try:
        config.set_model_override(agent_id, model)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _save_model_overrides()
    return jsonify(_agents_payload())


# ---------------------------------------------------------------------------
# Start a run
# ---------------------------------------------------------------------------
def _plan_gate(customer):
    """Phase 9 plan-gating middleware: reads the account's effective plan and
    answers (allowed, message) for starting a new report run.

    Two rules, both derived from the subscription timestamps (correct even before
    the daily tick persists a transition):
      - past_due → everything locked EXCEPT data export (downloads stay open —
        they are separate routes this gate never touches);
      - the plan's report quota caps run starts: per calendar month normally, or
        as a LIFETIME total when the plan sets lifetime_quota (the 免费体验
        trial's 2 reports, redesign B3); failed runs don't count.
    Quota metering is best-effort: if the brain DB hiccups we allow the run —
    a metering outage must not take the product down."""
    plan_code, _sub, locked = metrics_store.effective_plan(customer["id"])
    if locked:
        return False, ("订阅已逾期，功能已锁定（仅可导出已有数据）。"
                       "请续费后继续使用。")
    p = config.plan(plan_code)
    quota = int(p.get("monthly_report_quota") or 0)
    if quota:
        try:
            company_id = metrics_store.get_or_create_company(
                customer.get("email"),
                company_name=customer.get("company") or customer.get("name"))
            if p.get("lifetime_quota"):
                used = metrics_store.count_runs_total(company_id)
            else:
                used = metrics_store.count_runs_this_month(company_id)
        except Exception:
            return True, None
        if used >= quota:
            if p.get("lifetime_quota"):
                return False, ("免费体验额度已用完（%d/%d 份报告）。"
                               "升级套餐后可继续生成报告。" % (used, quota))
            return False, ("本月报告额度已用完（%d/%d · %s）。"
                           "升级套餐可提高额度。" % (used, quota, p["label"]))
    return True, None


@app.route("/api/run", methods=["POST"])
@limiter.limit(config.run_rate_limit, key_func=_run_rate_key)
def run():
    # Re-read .env on every run so editing your key + clicking "Start the team"
    # picks it up immediately — no server restart needed. override=True means the
    # .env value always wins over any stale ANTHROPIC_API_KEY left in your shell.
    load_dotenv(override=True)

    # The agent team is billed to the logged-in account (like Fusion + the gateway),
    # so a run requires a signed-in customer with credit left.
    customer = _current_customer()
    if not customer:
        return jsonify({"error": "Please log in first — agent-team runs are billed to "
                                 "your account balance."}), 401
    if not CUSTOMERS.has_balance(customer):
        return jsonify({"error": "Your account balance is spent. Top up to run the team."}), 402

    # Phase 8 — platform spend circuit breaker (+ this account's daily ¥ cap). When
    # the platform is over its rolling upstream-spend cap, refuse new runs with the
    # standard "系统繁忙" message until an admin resets the breaker.
    allowed, reason = platform_guard.check(customer["id"], customer.get("daily_cap_yuan"))
    if not allowed:
        return jsonify({"error": reason}), 503

    # Phase 9 — plan gate: a past-due subscription locks new runs (data export
    # stays open), and the plan's monthly report quota caps run starts.
    plan_ok, plan_msg = _plan_gate(customer)
    if not plan_ok:
        return jsonify({"error": plan_msg}), 402

    goal = (request.form.get("goal") or "").strip()
    if not goal:
        return jsonify({"error": "Please describe what you want the team to do."}), 400

    # Per-run industry override from the task form. "" / "auto" means "use the
    # company profile's industry"; it's resolved to a concrete pack just below.
    industry_override = request.form.get("industry")

    # Phase 8 — validate uploads BEFORE creating the job (so a rejected file leaves
    # no half-built job behind): cap the file count, cap each file's size, and refuse
    # anything whose leading bytes look like an executable (filenames lie, magic bytes
    # don't). Size comes from seek/tell on the stream, which is rewound so the later
    # save still reads the whole file.
    upload_files = [f for f in request.files.getlist("files") if f and f.filename]
    if len(upload_files) > config.MAX_UPLOAD_FILES_PER_RUN:
        return jsonify({"error": f"一次最多上传 {config.MAX_UPLOAD_FILES_PER_RUN} 个文件"
                                 f"（你选择了 {len(upload_files)} 个）。"}), 400
    max_mb = config.MAX_UPLOAD_BYTES_PER_FILE // (1024 * 1024)
    for f in upload_files:
        head = f.stream.read(16)
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        if security.looks_executable(head):
            return jsonify({"error": f"文件 “{f.filename}” 看起来是可执行程序，已拒绝上传。"}), 400
        if size > config.MAX_UPLOAD_BYTES_PER_FILE:
            return jsonify({"error": f"文件 “{f.filename}” 超过单个文件 {max_mb} MB 的上限。"}), 400

    job_id = uuid.uuid4().hex[:12]
    memory = SharedMemory(job_id, store_dir=MEMORY_DIR, goal=goal,
                          output_dir=os.path.join(OUTPUT_DIR, job_id))
    JOBS[job_id] = memory

    # Save & parse every uploaded file into shared memory (already validated above).
    job_upload_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_upload_dir, exist_ok=True)
    saved = 0
    saved_paths = []
    for f in upload_files:
        # CJK-safe sanitizer — secure_filename strips every non-ASCII char, so
        # 六月销售明细.csv landed (and displayed in the feed) as "csv".
        safe_name = _inbox_safe_filename(f.filename)
        path = os.path.join(job_upload_dir, safe_name)
        f.save(path)
        saved_paths.append(path)
        doc = parse_file(path)
        # Phase 8 — encrypt the raw upload at rest AFTER parsing it in memory (no-op
        # when FERNET is unset). A leaked disk/backup then never exposes the original.
        crypto.encrypt_file_in_place(path)
        memory.add_document(doc)
        memory.log("system", f"Received file: {doc['name']} ({doc['chars']:,} chars).",
                   type="info")
        saved += 1

    memory.log("system", f"Job created. Goal logged. {saved} file(s) attached.", type="info")

    # Tie this job to the account's persistent "company brain": get-or-create the
    # company row (keyed on email) and open a run row. Best-effort — if the brain
    # is unavailable we still run the team, just without long-term memory.
    company_id = run_id = None
    try:
        company_id = metrics_store.get_or_create_company(
            customer.get("email"),
            company_name=customer.get("company") or customer.get("name"),
        )
        run_id = metrics_store.create_run(company_id, goal)
    except Exception as e:
        memory.log("system", f"Company memory unavailable ({e}); running without it.",
                   type="info")

    # Persist this batch of uploads under the company so a later scheduled
    # "latest_upload" job can reuse the most recent files the account sent.
    if company_id and saved_paths:
        try:
            batch_dir = metrics_store.new_upload_batch_dir(company_id)
            for sp in saved_paths:
                dest = os.path.join(batch_dir, os.path.basename(sp))
                shutil.copy2(sp, dest)  # sp is already encrypted; copy keeps it so
                crypto.encrypt_file_in_place(dest)  # idempotent — guarantees at-rest
        except Exception as e:
            memory.log("system", f"Upload not persisted for scheduling ({e}).", type="info")

    # Decide which industry pack this run uses: the form override wins, else the
    # company profile's industry (Phase 3), else generic. Resolved here (against
    # this request's company_id) so the choice is fixed before the thread starts.
    industry_key = _resolve_industry(industry_override, company_id)

    # Run the team in the background so the web request returns immediately. The
    # customer id (not the request session, which won't exist in the thread) is
    # captured here so every call the team makes is billed to this account. The
    # company_id / run_id let the team read & write the persistent company brain.
    thread = threading.Thread(
        target=_run_team,
        args=(memory, customer["id"], company_id, run_id, industry_key), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


def _make_biller(customer_id):
    """A cost_listener that charges each model call to the account, and cuts the
    run off (BudgetExceededError) once the balance is spent — so a run can never
    push an account below zero. Returns None if there's no account to bill."""
    if not customer_id:
        return None

    def bill(model, input_tokens, output_tokens):
        billed = CUSTOMERS.record_usage(customer_id, model, input_tokens, output_tokens)
        bal = billed.get("balance")
        if bal is not None and bal <= 0:
            raise BudgetExceededError(
                "account", f"Account balance is spent (¥{bal:.2f}).")

    return bill


def _resolve_industry(override, company_id):
    """Pick this run's industry value. A concrete per-run override from the task
    form wins ("" / "auto" mean "no override"); otherwise fall back to the company
    profile's industry (Phase 3); otherwise "" (→ the generic pack). The profile
    read is best-effort — it must never block a run from starting."""
    override = (override or "").strip()
    if override and override.lower() != "auto":
        return override
    if company_id:
        try:
            return (metrics_store.get_profile(company_id) or {}).get("industry") or ""
        except Exception:
            return ""
    return ""


def _run_team(memory, customer_id=None, company_id=None, run_id=None, industry_key=None):
    try:
        Orchestrator(memory, token_manager=TOKENS,
                     cost_listener=_make_biller(customer_id),
                     company_id=company_id, run_id=run_id,
                     industry_key=industry_key).run()
    except Exception as e:
        # Make sure any failure (even while building the team) is visible to the UI.
        if memory.snapshot().get("status") != "error":
            memory.set_error(f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Scheduled runs ("定时任务") — the product shows up on its own cadence.
# A fire reuses the EXACT interactive billing/run path, so it is metered against
# the account's ¥ balance like any run; if the balance is spent it never runs.
# ---------------------------------------------------------------------------
def _budget_capped_biller(customer_id, budget_cny):
    """Bill each model call to the account (the normal path) AND, if the job set a
    per-task 预算上限, cut the run off with BudgetExceededError once THIS fire's own
    spend reaches that cap — without mutating the shared TokenManager state."""
    base = _make_biller(customer_id)
    try:
        cap = float(budget_cny) if budget_cny is not None else 0.0
    except (TypeError, ValueError):
        cap = 0.0
    if cap <= 0:
        return base  # no per-job cap → just the account-balance biller

    spent = {"cny": 0.0}

    def bill(model, input_tokens, output_tokens):
        if base:
            base(model, input_tokens, output_tokens)  # may raise (account empty)
        try:
            spent["cny"] += config.cost_cny(model, input_tokens, output_tokens)
        except Exception:
            pass
        if spent["cny"] >= cap:
            raise BudgetExceededError("job", "本次定时任务预算上限 ¥%.2f 已用尽。" % cap)

    return bill


def _scheduled_job_source_files(job, company_id):
    """The input file paths for one fire. 'fixed_files' → the pinned list (relative
    paths resolved against BASE_DIR); 'latest_upload' → every file in the account's
    most recent persisted upload batch (data/uploads/<company_id>/<batch>/)."""
    mode = job.get("source_mode") or SOURCE_LATEST_UPLOAD
    if mode == SOURCE_FIXED_FILES:
        out = []
        for p in (job.get("fixed_file_paths") or []):
            p = (p or "").strip()
            if not p:
                continue
            full = p if os.path.isabs(p) else os.path.join(BASE_DIR, p)
            if os.path.isfile(full):
                out.append(full)
        return out
    latest = metrics_store.latest_upload_dir(company_id)
    return metrics_store.list_files(latest) if latest else []


def _execute_scheduled_job(job, llm=None):
    """Run one 定时任务 through the SAME billing/run path as an interactive run,
    then deliver the finished report. Called by ReportScheduler on each cron fire
    in production, or directly by a test with an injected `llm` (so no real API
    call is made). `job` is a dict from metrics_store.

    Flow: resolve the account → BALANCE GATE (skip + "余额不足" notice if spent) →
    gather source files → open a run + build shared memory → run the team headlessly
    (the branded PDF is rendered inside the orchestrator's _finish_run) → deliver
    over the job's channels → stamp last_status. Returns a small dict for tests."""
    job_id = job.get("id")
    company_id = job.get("company_id")
    email = (job.get("account_email") or "").strip()
    company_name = (job.get("company_name") or "").strip() or "本公司"
    channels = job.get("delivery_channels") or []
    goal = (job.get("goal_template") or "").strip() or "生成上周经营周报"

    result = {"job_id": job_id, "ran": False, "status": None, "run_id": None}

    customer = CUSTOMERS.find_by_email(email) if email else None
    if not customer:
        metrics_store.mark_job_run(job_id, "skipped_no_account")
        result["status"] = "skipped_no_account"
        return result
    recipient_email = customer.get("email")

    # D3 PLAN-FEATURE GATE (redesign B5) — scheduled auto-delivery is 企业版
    # only. A still-active job whose account dropped the feature (pro→basic
    # downgrade, lapsed subscription) is skipped and RECORDED, never silent.
    # Resolved directly from the subscription — _plan_gate answers quota, not
    # which plan the account is on.
    plan_code, _sub, _locked = metrics_store.effective_plan(customer["id"])
    if not config.plan_has(plan_code, "scheduled_delivery"):
        metrics_store.mark_job_run(job_id, "skipped_plan_feature")
        result["status"] = "skipped_plan_feature"
        return result

    # BALANCE GATE — a scheduled fire must NEVER push an account below zero. If the
    # balance is already spent, do NOT open a run: deliver the "余额不足" notice
    # (best-effort) and record one skipped delivery row per channel for the audit.
    if not CUSTOMERS.has_balance(customer):
        delivery.notify_insufficient_balance(
            company_id=company_id, job_id=job_id, channels=channels,
            recipient_email=recipient_email)
        metrics_store.mark_job_run(job_id, "skipped_no_balance")
        result["status"] = "skipped_no_balance"
        return result

    # Phase 9 — the plan gate applies to scheduled fires too: a past-due lock or
    # an exhausted monthly report quota skips the fire (recorded, never silent).
    plan_ok, _plan_msg = _plan_gate(customer)
    if not plan_ok:
        metrics_store.mark_job_run(job_id, "skipped_plan_limit")
        result["status"] = "skipped_plan_limit"
        return result

    # Gather this fire's input files (latest upload batch, or the pinned list).
    files = _scheduled_job_source_files(job, company_id)

    # Build shared memory headlessly — a synthetic job id keeps outputs/<id>/ tidy
    # and never collides with an interactive browser job.
    headless_job_id = "sched-%s-%s" % (job_id, uuid.uuid4().hex[:8])
    memory = SharedMemory(headless_job_id, store_dir=MEMORY_DIR, goal=goal,
                          output_dir=os.path.join(OUTPUT_DIR, headless_job_id))
    for path in files:
        try:
            # Persisted upload batches are encrypted at rest (Phase 8) — decrypt to a
            # private temp file just for the parse (a no-op passthrough when FERNET is
            # unset), then restore the real filename the temp copy can't carry.
            with crypto.materialize_plaintext(path) as plain:
                doc = parse_file(plain)
            doc["name"] = os.path.basename(path)
            memory.add_document(doc)
        except Exception:
            pass  # one unreadable file must not abort the whole scheduled run

    # Open a run row so this fire bills + persists identically to an interactive run.
    try:
        run_id = metrics_store.create_run(company_id, goal)
    except Exception:
        run_id = None
    result["run_id"] = run_id

    industry_key = _resolve_industry(job.get("industry_override"), company_id)
    biller = _budget_capped_biller(customer["id"], job.get("budget_cny"))

    try:
        Orchestrator(memory, llm=llm, token_manager=TOKENS, cost_listener=biller,
                     company_id=company_id, run_id=run_id,
                     industry_key=industry_key).run()
    except BudgetExceededError:
        pass  # orchestrator already finished the run row as stopped_budget
    except Exception as e:
        if memory.snapshot().get("status") != "error":
            memory.set_error("%s: %s" % (type(e).__name__, e))
    result["ran"] = True

    # The orchestrator recorded the authoritative status on the run row; read it back.
    status = RUN_SUCCEEDED
    if run_id:
        try:
            status = (metrics_store.get_run(run_id) or {}).get("status") or RUN_SUCCEEDED
        except Exception:
            status = RUN_FAILED

    # Deliver the finished report (best-effort; each channel logs its own row).
    try:
        report_md = memory.snapshot().get("final_report") or ""
        pdf_path = (metrics_store.report_pdf_path(company_id, run_id)
                    if (company_id and run_id) else None)
        if pdf_path and not os.path.isfile(pdf_path):
            pdf_path = None
        delivery.deliver_report(
            company_id=company_id, run_id=run_id, channels=channels,
            pdf_path=pdf_path, report_markdown=report_md,
            company_name=company_name, recipient_email=recipient_email,
            job_id=job_id)
    except Exception:
        pass

    metrics_store.mark_job_run(job_id, status)
    result["status"] = status
    return result


# The 定时任务 scheduler (APScheduler). Created at import, but only STARTED at boot
# (in __main__ via _start_scheduler) so importing app.py for tests never spins up a
# background thread. Routes that change jobs call SCHEDULER.reload() to re-sync.
SCHEDULER = ReportScheduler(_execute_scheduled_job)
# Phase 9/10 system jobs (cron in Asia/Shanghai; scheduler.py registers the same
# set for the dedicated worker): renewal notices in the morning, reconciliation
# after the providers cut the previous day's bill, the day-3 onboarding nudge
# mid-morning.
SCHEDULER.add_system_job("billing-daily",
                         payments.daily_subscription_tick, "30 9 * * *")
SCHEDULER.add_system_job("reconciliation-nightly",
                         reconciliation.nightly_reconciliation_tick, "30 3 * * *")
SCHEDULER.add_system_job("lifecycle-daily",
                         lifecycle.daily_lifecycle_tick, "0 10 * * *")


def _start_scheduler():
    """Start the background scheduler and load the active jobs (idempotent)."""
    try:
        SCHEDULER.start()
        n = len(SCHEDULER.job_ids())
        print(f"  [OK]  定时任务 scheduler started ({n} active job(s) loaded).")
    except Exception as e:
        print(f"  [!!]  Scheduler failed to start ({type(e).__name__}: {e}); "
              f"定时任务 will not fire until the server is restarted.")


# ---------------------------------------------------------------------------
# Poll a run
# ---------------------------------------------------------------------------
def _load_job_memory(job_id):
    """The job's SharedMemory (live or persisted), or None when it doesn't exist
    OR its on-disk snapshot is unreadable (corrupt JSON / IO error). Routes treat
    both the same way — a friendly 404 — instead of a 500 (redesign Phase 5 A5)."""
    live = JOBS.get(job_id)
    if live is not None:
        return live
    try:
        return SharedMemory.load(job_id, store_dir=MEMORY_DIR)
    except (ValueError, OSError):
        return None


# Redesign Phase 5 (A1) — the SERVER-side twin of run_feed.js's customer filter.
# Customers get the theater, never the machinery (plan §2 rules 1 + 8): the
# token_manager's lines, the orchestrator's context-block debug confirmations
# (which echo prompt content), and the raw budget/length warnings never leave
# the server for a non-admin session. What gets LOGGED is unchanged — tests and
# founder debugging keep the full feed; the filter lives at the route.
_MACHINERY_INFO_PREFIXES = (
    "Received COMPANY MEMORY",
    "Received KPI MAPPING",
    "Received INDUSTRY PACK",
)
_BUDGET_REACHED_PREFIX = "Token budget reached"
_FRIENDLY_BUDGET_LINE = "已达本次生成上限，本次报告到此为止。"


def _customer_safe_activity(activity):
    """The activity feed with machinery entries removed (and the token-budget
    warning replaced by a friendly Chinese line a customer can understand)."""
    out = []
    for a in activity or []:
        if not isinstance(a, dict):
            continue
        if a.get("agent") == "token_manager":
            continue
        msg = str(a.get("message") or "")
        if a.get("type") == "info" and msg.startswith(_MACHINERY_INFO_PREFIXES):
            continue
        if msg.startswith("Output hit its length ceiling"):
            continue
        if msg.startswith(_BUDGET_REACHED_PREFIX):
            friendly = dict(a)
            friendly["agent"] = "system"
            friendly["message"] = _FRIENDLY_BUDGET_LINE
            out.append(friendly)
            continue
        out.append(a)
    return out


def _job_owner_check(job_id):
    """Redesign Phase 4 ownership gate for the job capability URLs. When the
    job's snapshot is tied to a company brain (company_id set), the requester
    must be a logged-in session whose company OWNS the job — the snapshot holds
    the customer's uploaded raw text, so an unguessable URL alone is no longer
    enough once an owner exists. Jobs without a company_id (pre-login flows,
    plain demo jobs, files with no snapshot) keep the original open behavior.

    Returns (error_response, status_code) to refuse the request, or (None, None)
    to let it through."""
    memory = _load_job_memory(job_id)
    owner_company = memory.snapshot().get("company_id") if memory is not None else None
    if not owner_company:
        return None, None
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if company_id != owner_company:
        return jsonify({"error": "无权访问该任务。"}), 403
    return None, None


@app.route("/api/status/<job_id>")
def status(job_id):
    customer = _current_customer()
    memory = _load_job_memory(job_id)
    if memory is None:
        # Phase 5 (A4) — login comes BEFORE existence disclosure, mirroring the
        # /api/files gate: an anonymous probe gets the same 401 whether the job
        # id exists or not, so unguessable ids can't be confirmed from outside.
        if not customer:
            return jsonify({"error": "请先登录。"}), 401
        return jsonify({"error": "Unknown job id."}), 404
    snap = memory.snapshot()
    # Phase 4 — a job owned by a company is only pollable by that company.
    if snap.get("company_id"):
        refusal, code = _job_owner_check(job_id)
        if refusal is not None:
            return refusal, code
    # Phase 5 (A1) — non-admin sessions get the customer-filtered feed; what is
    # LOGGED is unchanged (founder/is_admin keeps the raw machinery lines).
    if not _is_admin(customer):
        snap["activity"] = _customer_safe_activity(snap.get("activity"))
    return jsonify(snap)


@app.route("/api/files/<job_id>/<path:filename>")
def download_file(job_id, filename):
    """Serve a real file the team generated (deck, PDF, etc.) as a download.

    Safe by construction: job_id is restricted to hex/word chars and safe_join
    refuses any path-traversal in the filename. Files are encrypted at rest
    (Phase 8), so we decrypt into memory and stream that (a no-op passthrough when
    FERNET is unset); sent as an attachment so a downloaded .svg can't execute in
    our origin."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id or ""):
        return jsonify({"error": "Bad job id."}), 400
    # Phase 4 — files of a company-owned job require that company's session.
    refusal, code = _job_owner_check(job_id)
    if refusal is not None:
        return refusal, code
    path = safe_join(os.path.join(OUTPUT_DIR, job_id), filename)
    if not path or not os.path.isfile(path):
        return jsonify({"error": "File not found."}), 404
    _cust = _current_customer()
    metrics_store.write_audit((_cust or {}).get("id"), AUDIT_DOWNLOAD, _client_ip(),
                              detail="file %s/%s" % (job_id, os.path.basename(path)))
    return send_file(io.BytesIO(crypto.read_plaintext(path)),
                     as_attachment=True, download_name=os.path.basename(path))


# ---------------------------------------------------------------------------
# Token Manager (the "CFO") — the side menu reads and configures these.
# ---------------------------------------------------------------------------
@app.route("/api/tokens")
def tokens_status():
    return jsonify(TOKENS.status())


@app.route("/api/tokens/total", methods=["POST"])
def tokens_set_total():
    data = request.get_json(silent=True) or {}
    try:
        TOKENS.set_total_budget(data.get("limit"))
    except (TypeError, ValueError):
        return jsonify({"error": "Limit must be a whole number (or 0 for unlimited)."}), 400
    return jsonify(TOKENS.status())


@app.route("/api/tokens/budget", methods=["POST"])
def tokens_set_budget():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Please give the budget a name."}), 400
    try:
        TOKENS.set_budget(name, data.get("limit"))
    except (TypeError, ValueError):
        return jsonify({"error": "Limit must be a whole number (or 0 for unlimited)."}), 400
    return jsonify(TOKENS.status())


@app.route("/api/tokens/budget/remove", methods=["POST"])
def tokens_remove_budget():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Missing budget name."}), 400
    TOKENS.remove_budget(name)
    return jsonify(TOKENS.status())


@app.route("/api/tokens/reset", methods=["POST"])
def tokens_reset():
    TOKENS.reset_usage()
    return jsonify(TOKENS.status())


# ---------------------------------------------------------------------------
# UNIFIED API — the "get your own API key" storefront + OpenAI-compatible gateway
# ---------------------------------------------------------------------------
# A customer picks models in the store and gets ONE key. They then call any of
# those models through /v1/chat/completions (drop-in OpenAI-compatible), and we
# route to the real provider, charge their balance, and log usage — the three
# layers in customers.py (auth -> billing -> access control).
def _bearer_key():
    """Pull the customer's gateway key from the Authorization header (or X-API-Key).

    We deliberately read the key from a HEADER, never a URL query string, so it
    never lands in server logs or browser history."""
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return (request.headers.get("X-API-Key") or "").strip()


def _gateway_error(message, status, code):
    """An OpenAI-style error body so standard OpenAI client libraries handle it."""
    return jsonify({"error": {"message": message, "type": code, "code": code}}), status


def _account_view(customer):
    """public_view enriched with a per-model `available` flag (live vs. waiting on a key)
    and the founder-mode flag (redesign B2): is_admin marks the account's email as
    being on the $ADMIN_EMAILS allow-list. UI gating only — every /admin* route
    keeps its own server-side authorization check."""
    view = CUSTOMERS.public_view(customer)
    for m in view["models"]:
        m["available"] = providers.model_available(m["id"])
    view["is_admin"] = (customer.get("email") or "").strip().lower() in _admin_emails()
    return view


def _current_customer():
    """The logged-in customer for this browser session, or None.

    Phase 8: besides the account id, the cookie carries a server-side session token
    ("sid"). If the account has a token on file and the cookie's sid doesn't match
    it, the session has been invalidated (password reset / "log out everywhere") —
    we treat the request as logged out. A NULL token (legacy rows) skips the check
    so the upgrade never force-logs-out an existing session."""
    customer = CUSTOMERS.get_by_id(session.get("customer_id"))
    if not customer:
        return None
    server_token = customer.get("session_token")
    if server_token and session.get("sid") != server_token:
        return None
    return customer


def _login_session(customer):
    """Establish a logged-in session for `customer` with session-fixation defense:
    clear any pre-login session contents, then stamp the account id AND its current
    server-side session token. Returns the customer dict (unchanged) for chaining."""
    token = CUSTOMERS.current_session_token(customer["id"])
    session.clear()                       # rotate: discard anything from before login
    session["customer_id"] = customer["id"]
    if token:
        session["sid"] = token
    session.permanent = False
    return customer


def _admin_emails():
    """The founder allow-list from $ADMIN_EMAILS (comma/space/semicolon separated),
    lower-cased. Read fresh each call so .env edits apply without a restart. Empty
    when unset — which means NOBODY is an admin (the dashboard stays locked)."""
    raw = os.environ.get("ADMIN_EMAILS", "") or ""
    return {e.strip().lower() for e in re.split(r"[,;\s]+", raw) if e.strip()}


def _is_admin(customer):
    """True only for a logged-in account whose email is in $ADMIN_EMAILS."""
    if not customer:
        return False
    email = (customer.get("email") or "").strip().lower()
    return bool(email) and email in _admin_emails()


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _valid_email(email):
    return bool(_EMAIL_RE.match((email or "").strip()))


# Lenient phone match (provider-abstracted): an optional + and 6–15 digits, after
# stripping spaces/dashes. China mobiles are 11 digits, but staying permissive keeps
# the SMS provider swappable without re-validating here.
_PHONE_RE = re.compile(r"^\+?\d{6,15}$")


def _normalize_phone(phone):
    """Trim a phone to digits (+ a leading +), or '' if it doesn't look like one."""
    raw = re.sub(r"[\s\-()]", "", (phone or "").strip())
    return raw if _PHONE_RE.match(raw) else ""


def _generate_sms_code():
    """A fresh 6-digit numeric verification code (cryptographically random)."""
    return "%06d" % secrets.randbelow(1_000_000)


def _send_phone_code(phone, purpose):
    """Issue + dispatch one SMS code for `phone`/`purpose`. Returns (ok, error_cn).

    Stores ONLY the hashed code (issue_phone_code), enforces the per-phone hourly
    send cap, then hands the plaintext to the configured SmsProvider. The plaintext
    code is never logged or returned."""
    code = _generate_sms_code()
    issued = metrics_store.issue_phone_code(
        phone, purpose, code,
        ttl_secs=config.SMS_CODE_TTL_SECS,
        max_per_hour=config.SMS_MAX_SENDS_PER_HOUR,
    )
    if not issued.get("ok"):
        return False, issued.get("reason") or "发送过于频繁，请稍后再试"
    ok, _err = sms.get_provider().send(phone, code)
    if not ok:
        return False, "验证码发送失败，请稍后再试"
    return True, None


def _client_ip():
    """Best-effort client IP for the audit log. Honors the first X-Forwarded-For hop
    (the app runs behind nginx), else the socket peer. Trimmed to the column width;
    never raises."""
    fwd = request.headers.get("X-Forwarded-For", "")
    ip = fwd.split(",")[0].strip() if fwd else (request.remote_addr or "")
    return ip[:64]


@app.route("/api/account/models", methods=["POST"])
def account_set_models():
    """DASHBOARD — set which models the logged-in customer's key may call.

    Session-authenticated (you must be signed in): model selection lives behind
    the account, not on the sign-up/login pages. Body: {"models": ["id", ...]}.
    The key is unchanged; it simply unlocks the new selection. Returns the
    updated account view."""
    load_dotenv(override=True)  # so `available` reflects keys you may have just added
    customer = _current_customer()
    if not customer:
        return jsonify({"error": "Please log in to choose your models."}), 401
    data = request.get_json(silent=True) or {}
    models = data.get("models")
    if not isinstance(models, list):
        return jsonify({"error": "Send the models you want as a list."}), 400
    updated = CUSTOMERS.set_models(customer["id"], models)
    if not updated:
        return jsonify({"error": "We couldn't find your account — try logging in again."}), 404
    return jsonify({"customer": _account_view(updated)})


# ---------------------------------------------------------------------------
# PAYMENTS (Phase 9) — real Alipay / WeChat Pay replace the demo top-up.
#
# The money path: POST /api/pay/create opens an order (status=created) + the
# provider-side trade and hands back a pay_url / QR. The PROVIDER later calls
# /pay/notify/<provider>; the handler verifies the signature FIRST, then runs the
# idempotent created→paid transition (metrics_store.mark_order_paid — one
# transaction that also credits the wallet or activates the subscription). A
# replayed notify is a successful no-op; a tampered one changes nothing. Amounts
# are only ever read from the VERIFIED payload, compared against the stored order.
# ---------------------------------------------------------------------------
_pay_log = logging.getLogger("app.payments")


def _pay_notify_response(provider_name, ok, status=200, replay=False, reason=None):
    """Each provider expects its own ack shape (Alipay: a literal "success" body;
    WeChat APIv3: JSON code SUCCESS/FAIL; mock: plain JSON)."""
    if provider_name == "alipay":
        return Response("success" if ok else "failure", status=status,
                        mimetype="text/plain; charset=utf-8")
    if provider_name == "wechat":
        return jsonify({"code": "SUCCESS" if ok else "FAIL",
                        "message": reason or ""}), status
    body = {"ok": ok, "replay": replay}
    if reason:
        body["error"] = reason
    return jsonify(body), status


def _apply_verified_payment(provider_name, order_no, paid_amount, txn_id):
    """Shared by the notify endpoints + the mock cashier: the idempotent paid
    transition, plus ONE audit row on a first (non-replay) transition."""
    result = metrics_store.mark_order_paid(order_no, paid_amount, txn_id)
    if result["ok"] and not result["replay"]:
        o = result["order"]
        detail = ("top-up ¥%.2f via %s order %s"
                  % (o["amount_yuan"], provider_name, o["order_no"])
                  if o["type"] == ORDER_TOPUP else
                  "subscription %s ¥%.2f via %s order %s"
                  % (o["plan_code"], o["amount_yuan"], provider_name, o["order_no"]))
        metrics_store.write_audit(o["account_id"], AUDIT_TOPUP, _client_ip(),
                                  detail=detail)
        # Phase 10 — subscription payment receipt (deduped per order inside
        # lifecycle; best-effort — mail trouble must never fail the notify ack).
        if o["type"] == ORDER_SUBSCRIPTION:
            try:
                lifecycle.send_renewal_receipt(o)
            except Exception:
                pass
    return result


def _handle_pay_notify(provider_name):
    prov = payments.get_provider(provider_name)
    if prov is None:
        # Unconfigured channel — and crucially "mock" the moment a real provider
        # exists, so a known dev secret can never pay a production order.
        return _pay_notify_response(provider_name, False, status=404,
                                    reason="支付渠道未启用")
    verified = prov.verify_notify(request)        # SIGNATURE FIRST, always
    if verified is None:
        return _pay_notify_response(provider_name, False, status=400,
                                    reason="签名校验失败")
    order_no, paid_amount, txn_id = verified
    result = _apply_verified_payment(provider_name, order_no, paid_amount, txn_id)
    if not result["ok"]:
        if result["reason"] == "amount_mismatch":
            # Signed payload, wrong amount vs the stored order — ops must look.
            _pay_log.warning("payment notify amount mismatch: provider=%s order=%s",
                             provider_name, order_no)
        status = 404 if result["reason"] == "unknown_order" else 400
        return _pay_notify_response(provider_name, False, status=status,
                                    reason=result["reason"])
    return _pay_notify_response(provider_name, True, replay=result["replay"])


@app.route("/pay/notify/alipay", methods=["POST"])
def pay_notify_alipay():
    """Alipay async notify (RSA2-verified). Replays are successful no-ops."""
    return _handle_pay_notify("alipay")


@app.route("/pay/notify/wechat", methods=["POST"])
def pay_notify_wechat():
    """WeChat Pay APIv3 notify (platform-cert-verified + AES-GCM-decrypted)."""
    return _handle_pay_notify("wechat")


@app.route("/pay/notify/mock", methods=["POST"])
def pay_notify_mock():
    """Mock notify (HMAC-verified) — dev/tests only. 404s when a real provider
    is configured (payments.get_provider("mock") returns None then)."""
    return _handle_pay_notify("mock")


@app.route("/api/pay/create", methods=["POST"])
def pay_create():
    """Open a payment order for the logged-in account.

    Body: {"type": "topup", "amount": <¥>} or
          {"type": "subscription", "plan_code": "basic"|"pro"},
    plus optional {"provider": "alipay"|"wechat"} (default: first configured).
    A subscription's amount comes from the PLANS table — NEVER from the client.
    Returns {order_no, amount, provider, pay_url, qr_text}."""
    customer = _current_customer()
    if not customer:
        return jsonify({"error": "请先登录后再支付。"}), 401
    data = request.get_json(silent=True) or {}
    otype = (data.get("type") or "").strip().lower()
    prov = payments.get_provider(data.get("provider"))
    if prov is None:
        return jsonify({"error": "该支付渠道未配置。"}), 400

    if otype == ORDER_TOPUP:
        try:
            amount = round(float(data.get("amount")), 2)
        except (TypeError, ValueError):
            return jsonify({"error": "请输入有效的充值金额。"}), 400
        if not (config.TOPUP_MIN_YUAN <= amount <= config.TOPUP_MAX_YUAN):
            return jsonify({"error": "充值金额需在 ¥%g – ¥%g 之间。"
                            % (config.TOPUP_MIN_YUAN, config.TOPUP_MAX_YUAN)}), 400
        plan_code = None
        subject = "账户充值 ¥%.2f" % amount
    elif otype == ORDER_SUBSCRIPTION:
        plan_code = (data.get("plan_code") or "").strip().lower()
        if plan_code not in config.PURCHASABLE_PLANS:
            return jsonify({"error": "无效的套餐。"}), 400
        p = config.plan(plan_code)
        amount = p["monthly_price_yuan"]          # server-side price, never client
        subject = "订阅 · %s（%d天）" % (p["label"], config.SUBSCRIPTION_PERIOD_DAYS)
    else:
        return jsonify({"error": "type 必须是 topup 或 subscription。"}), 400

    order = metrics_store.create_order(customer["id"], otype, amount, prov.name,
                                       plan_code=plan_code, subject=subject)
    res = prov.create_order(order["amount_yuan"], subject, order["order_no"])
    if not res.get("ok"):
        metrics_store.mark_order_failed(order["order_no"])
        return jsonify({"error": res.get("error") or "下单失败，请稍后再试。"}), 502
    return jsonify({
        "order_no": order["order_no"],
        "amount": order["amount_yuan"],
        "provider": prov.name,
        "pay_url": res.get("pay_url"),
        "qr_text": res.get("qr_text"),
        "status": order["status"],
    })


@app.route("/api/pay/status/<order_no>")
def pay_status(order_no):
    """Poll an order's status (the cashier page / dashboard waits on this).
    Login + ownership required; someone else's order_no answers 404."""
    customer = _current_customer()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    order = metrics_store.get_order(order_no)
    if not order or order["account_id"] != customer["id"]:
        return jsonify({"error": "订单不存在。"}), 404
    return jsonify({"order_no": order["order_no"], "status": order["status"],
                    "type": order["type"], "amount": order["amount_yuan"],
                    "paid_at": order["paid_at"]})


@app.route("/pay/mock/<order_no>", methods=["GET", "POST"])
def mock_cashier(order_no):
    """The fake cashier (dev/tests only — 404 when a real provider is configured).

    GET shows the order; POST simulates "the customer paid at the provider": it
    builds a properly SIGNED notify server-side and pushes it through the same
    _apply_verified_payment path the real endpoints use."""
    prov = payments.get_provider("mock")
    if prov is None:
        return Response("404", status=404, mimetype="text/plain; charset=utf-8")
    order = metrics_store.get_order(order_no)
    if not order:
        return Response("404 — 订单不存在", status=404,
                        mimetype="text/plain; charset=utf-8")
    msg = None
    if request.method == "POST" and (request.form.get("action") or "") == "pay":
        fields = prov.notify_fields(order["order_no"], order["amount_yuan"])
        verified = (fields["order_no"], float(fields["paid_amount"]), fields["txn_id"])
        result = _apply_verified_payment("mock", *verified)
        msg = ("✅ 支付成功（模拟）。可以关闭本页返回控制台。" if result["ok"]
               else "支付失败：%s" % result["reason"])
        order = metrics_store.get_order(order_no)
    paid = order["status"] == ORDER_PAID
    html = (
        "<!doctype html><html lang=zh><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>模拟收银台</title><style>"
        "body{font-family:system-ui,Arial,sans-serif;max-width:430px;margin:48px auto;"
        "padding:0 16px;color:#1f2937}h1{font-size:18px}"
        ".card{border:1px solid #e5e7eb;border-radius:12px;padding:18px}"
        ".amt{font-size:30px;font-weight:700;margin:8px 0}"
        ".muted{color:#6b7280;font-size:13px}"
        "button{background:#1677ff;color:#fff;border:0;padding:12px 18px;"
        "border-radius:8px;font-size:15px;cursor:pointer;width:100%}"
        ".ok{background:#ecfdf5;border:1px solid #a7f3d0;color:#065f46;"
        "padding:10px 12px;border-radius:8px}"
        "</style></head><body><h1>模拟收银台 · Mock Cashier</h1><div class=card>"
        "<div class=muted>" + _h(order.get("subject") or "订单") + "</div>"
        "<div class=amt>¥" + _h("%.2f" % order["amount_yuan"]) + "</div>"
        "<div class=muted>订单号 " + _h(order["order_no"]) + " · 状态 "
        + _h(order["status"]) + "</div>"
        + (("<p class=ok>" + _h(msg) + "</p>") if msg else "")
        + ("" if paid else
           "<form method=post style='margin-top:14px'>"
           "<input type=hidden name=action value=pay>"
           "<button type=submit>确认支付（模拟）</button></form>")
        + "<p class=muted style='margin-top:12px'>仅开发环境可见：配置真实支付渠道后本页自动失效。</p>"
        "</div></body></html>")
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/api/orders")
def my_orders():
    """The logged-in account's orders, newest first, each carrying its fapiao
    state (so the dashboard can show 申请发票 on paid orders)."""
    customer = _current_customer()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    orders = metrics_store.list_orders(account_id=customer["id"], limit=50)
    fapiao_by_order = {f["order_id"]: f for f
                       in metrics_store.list_fapiao_requests(account_id=customer["id"])}
    for o in orders:
        f = fapiao_by_order.get(o["id"])
        o["fapiao_status"] = f["status"] if f else None
    return jsonify({"orders": orders})


@app.route("/api/plans")
def plans_list():
    """The plan catalog + the logged-in account's current plan / quota usage."""
    plans = [config.PLANS[c] for c in ("free", "basic", "pro")]
    out = {"plans": plans, "current": None}
    customer = _current_customer()
    if customer:
        plan_code, sub, locked = metrics_store.effective_plan(customer["id"])
        p = config.plan(plan_code)
        used = 0
        try:
            company_id = metrics_store.get_or_create_company(
                customer.get("email"),
                company_name=customer.get("company") or customer.get("name"))
            # Mirror the run gate (redesign B3): lifetime-quota plans (免费体验)
            # meter ALL non-failed runs, not just this calendar month's.
            if p.get("lifetime_quota"):
                used = metrics_store.count_runs_total(company_id)
            else:
                used = metrics_store.count_runs_this_month(company_id)
        except Exception:
            pass
        out["current"] = {
            "plan_code": plan_code, "locked": locked, "subscription": sub,
            "reports_used_this_month": used,
            "monthly_report_quota": p["monthly_report_quota"],
            "lifetime_quota": bool(p.get("lifetime_quota")),
        }
    return jsonify(out)


@app.route("/api/subscription/cancel", methods=["POST"])
def subscription_cancel():
    """取消续订 — access continues to period end; the tick then lets it lapse
    (no renewal order) instead of locking the account."""
    customer = _current_customer()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    sub = metrics_store.set_cancel_at_period_end(customer["id"], True)
    if not sub:
        return jsonify({"error": "当前没有订阅。"}), 404
    return jsonify({"subscription": sub})


@app.route("/api/subscription/resume", methods=["POST"])
def subscription_resume():
    """恢复续订 — undo cancel-at-period-end before the period lapses."""
    customer = _current_customer()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    sub = metrics_store.set_cancel_at_period_end(customer["id"], False)
    if not sub:
        return jsonify({"error": "当前没有订阅。"}), 404
    return jsonify({"subscription": sub})


# 统一社会信用代码 is 18 chars, legacy 税号 15/20 — accept 6–25 alphanumerics.
_TAX_NO_RE = re.compile(r"^[0-9A-Za-z]{6,25}$")


@app.route("/api/fapiao/request", methods=["POST"])
def fapiao_request():
    """申请发票 against one PAID order. Body: {"order_no", "title" 公司抬头,
    "tax_no" 税号, "email"?}. One live request per order; a rejected request may
    be re-submitted with corrected details."""
    customer = _current_customer()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    data = request.get_json(silent=True) or {}
    order = metrics_store.get_order((data.get("order_no") or "").strip())
    if not order or order["account_id"] != customer["id"]:
        return jsonify({"error": "订单不存在。"}), 404
    if order["status"] != ORDER_PAID:
        return jsonify({"error": "只有已支付的订单才能申请发票。"}), 400
    title = (data.get("title") or "").strip()
    tax_no = (data.get("tax_no") or "").strip()
    email = (data.get("email") or "").strip() or customer.get("email")
    if not title:
        return jsonify({"error": "请填写发票抬头（公司名称）。"}), 400
    if not _TAX_NO_RE.match(tax_no):
        return jsonify({"error": "请填写有效的税号（统一社会信用代码）。"}), 400
    if email and not _valid_email(email):
        return jsonify({"error": "请填写有效的接收邮箱。"}), 400
    try:
        req = metrics_store.create_fapiao_request(
            customer["id"], order["id"], title, tax_no, email)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"request": req})


@app.route("/api/account", methods=["GET", "POST"])
def account_view():
    """DASHBOARD — given a key (header or POST body), return that account's view.

    Adds an `available` flag per model so the dashboard shows which models are
    live now vs. waiting on a provider key in .env."""
    load_dotenv(override=True)  # so `available` reflects keys you may have just added
    raw_key = _bearer_key()
    if not raw_key and request.method == "POST":
        raw_key = ((request.get_json(silent=True) or {}).get("key") or "").strip()
    customer = CUSTOMERS.verify_key(raw_key)
    if not customer:
        return jsonify({"error": "We didn't recognize that API key. Check it and try again."}), 404
    return jsonify(_account_view(customer))


# ---------------------------------------------------------------------------
# ACCOUNTS — website login (email + password). Separate from the gateway key:
# this signs a human into the dashboard; the tng-sk-… key authenticates machine
# calls to /v1/chat/completions. Passwords are only ever stored as a salted hash.
#
# Phase 8 (China launch): sign-up and password reset are gated by an SMS code —
# /api/send-code issues a 6-digit code (stored only as a hash, 5-min expiry, max 3
# sends/phone/hour), and signup/reset verify it (max 5 attempts, then the code is
# burned). The auth endpoints are rate-limited 5/min/IP.
# ---------------------------------------------------------------------------
@app.route("/api/send-code", methods=["POST"])
@limiter.limit(config.auth_rate_limit)
def send_code():
    """Send an SMS verification code for sign-up or password reset.

    Body: {"phone": "...", "purpose": "signup"|"reset"}. For a reset you may send
    {"email": "..."} instead and we route the code to the phone on that account.
    Always answers 200 {"ok": true} on success; we never reveal whether a phone or
    email is registered. The per-phone hourly cap returns 429 with a Chinese
    message."""
    data = request.get_json(silent=True) or {}
    purpose = (data.get("purpose") or CODE_SIGNUP).strip().lower()
    if purpose not in (CODE_SIGNUP, CODE_RESET):
        return jsonify({"error": "Unknown code purpose."}), 400

    phone = _normalize_phone(data.get("phone"))
    # Reset by verified email: look up the account's phone to text the code to.
    if not phone and purpose == CODE_RESET:
        acc = CUSTOMERS.find_by_email((data.get("email") or "").strip())
        phone = _normalize_phone((acc or {}).get("phone"))
        if not phone:
            # Don't disclose whether the email/phone exists — pretend success.
            return jsonify({"ok": True})
    if not phone:
        return jsonify({"error": "请输入有效的手机号"}), 400

    ok, err = _send_phone_code(phone, purpose)
    if not ok:
        # The hourly per-phone cap is a throttle → 429 (explicit body, so the
        # global 429 handler's generic copy is not used here).
        return jsonify({"error": err}), 429
    return jsonify({"ok": True})


@app.route("/api/signup", methods=["POST"])
@limiter.limit(config.auth_rate_limit)
def signup():
    """Create a full account (name + email + password + SMS-verified phone, company
    optional), log them in, and return the key ONCE.

    Model selection is deliberately NOT part of sign-up — a new account starts
    with no models and the customer picks them afterwards on their dashboard
    (POST /api/account/models). The key is minted now and unlocks whatever models
    they later choose."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    company = (data.get("company") or "").strip()
    phone = _normalize_phone(data.get("phone"))
    code = (data.get("code") or "").strip()
    if not name:
        return jsonify({"error": "Please enter your name."}), 400
    if not _valid_email(email):
        return jsonify({"error": "Please enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    # Phone + SMS code are required by default (the China norm). PHONE_VERIFICATION=0
    # turns this off for environments that genuinely don't need it.
    if config.phone_verification_required():
        if not phone:
            return jsonify({"error": "请输入有效的手机号"}), 400
        verdict = metrics_store.verify_phone_code(
            phone, CODE_SIGNUP, code, config.SMS_MAX_VERIFY_ATTEMPTS)
        if not verdict.get("ok"):
            return jsonify({"error": verdict.get("reason") or "验证码错误"}), 400

    try:
        # Accounts start with no models; the dashboard's picker adds them.
        out = CUSTOMERS.create_customer([], name=name, email=email,
                                        password=password, company=company,
                                        phone=phone or None)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    customer = CUSTOMERS.get_by_id(out["customer"]["id"])
    _login_session(customer)                  # log them straight in (rotated session)
    # Phase 10 — the welcome email (3-step quickstart). Deduped + best-effort
    # inside lifecycle; the extra guard keeps signup alive through ANY hiccup.
    try:
        lifecycle.send_welcome_email(customer["id"], customer.get("email"),
                                     customer.get("name"))
    except Exception:
        pass
    return jsonify({"api_key": out["api_key"], "customer": _account_view(customer)})


@app.route("/api/login", methods=["POST"])
@limiter.limit(config.auth_rate_limit)
def login():
    """Sign a customer into the dashboard with their email + password."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    customer = CUSTOMERS.verify_login(email, password)
    if not customer:
        return jsonify({"error": "Email or password is incorrect."}), 401
    _login_session(customer)
    metrics_store.write_audit(customer["id"], AUDIT_LOGIN, _client_ip())
    return jsonify({"customer": _account_view(customer)})


@app.route("/api/logout", methods=["POST"])
def logout():
    """Sign the current customer out of THIS browser (clears the session cookie)."""
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/logout-all", methods=["POST"])
def logout_all():
    """"Log out everywhere" — rotate the account's server-side session token so every
    other signed cookie (other browsers/devices) is invalidated on its next request,
    then clear THIS session too. Must be logged in."""
    customer = _current_customer()
    if not customer:
        return jsonify({"error": "Please log in first."}), 401
    CUSTOMERS.rotate_session_token(customer["id"])
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/reset-password", methods=["POST"])
@limiter.limit(config.auth_rate_limit)
def reset_password():
    """Reset a password using an SMS code (the same code flow as sign-up).

    Body: {"phone"|"email": "...", "code": "...", "new_password": "..."}. The code
    must have been requested with purpose="reset". On success the password is
    re-hashed (bcrypt) and the account's session token is rotated, so every existing
    session is logged out. Returns a generic message either way — we never reveal
    whether an account exists for the given phone/email."""
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    new_password = data.get("new_password") or data.get("password") or ""
    if len(new_password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400

    # Resolve which account + which phone the code was sent to.
    phone = _normalize_phone(data.get("phone"))
    if phone:
        account = CUSTOMERS.find_by_phone(phone)
    else:
        account = CUSTOMERS.find_by_email((data.get("email") or "").strip())
        phone = _normalize_phone((account or {}).get("phone"))
    if not phone:
        return jsonify({"error": "请输入有效的手机号"}), 400

    verdict = metrics_store.verify_phone_code(
        phone, CODE_RESET, code, config.SMS_MAX_VERIFY_ATTEMPTS)
    if not verdict.get("ok"):
        return jsonify({"error": verdict.get("reason") or "验证码错误"}), 400

    # Code was valid. If no account is attached to it, stop here (don't disclose).
    if not account:
        return jsonify({"error": "无法重置该账户，请确认信息后重试"}), 404

    CUSTOMERS.set_password(account["id"], new_password)
    CUSTOMERS.rotate_session_token(account["id"])   # invalidate all existing sessions
    metrics_store.write_audit(account["id"], AUDIT_PASSWORD_CHANGE, _client_ip(),
                              detail="password reset via SMS code")
    return jsonify({"ok": True})


@app.route("/api/me")
def me():
    """The logged-in account for this browser session, or {"customer": null}."""
    load_dotenv(override=True)  # so `available` reflects keys you may have just added
    customer = _current_customer()
    if not customer:
        return jsonify({"customer": None})
    return jsonify({"customer": _account_view(customer)})


# ---------------------------------------------------------------------------
# COMPANY BRAIN (企业大脑) — the persistent profile / KPI map / glossary / prefs
# ---------------------------------------------------------------------------
# Every section is keyed on the logged-in account's company row (SQLite), which is
# get-or-created from the account email. The JSON account store stays the source of
# truth for auth + billing; the brain just hangs long-term memory off the email.
def _brain_company():
    """The logged-in customer and their persistent company-brain id, or (None, None)
    when not logged in. Best-effort get-or-create of the company row (keyed on email)."""
    customer = _current_customer()
    if not customer:
        return None, None
    try:
        company_id = metrics_store.get_or_create_company(
            customer.get("email"),
            company_name=customer.get("company") or customer.get("name"),
        )
    except Exception:
        company_id = None
    return customer, company_id


def _clean_kpi_mappings(raw):
    """Sanitize the KPI-mapping rows the page sends into a tidy list of
    {kpi_name_cn, aliases, unit} dicts, dropping rows with no name and no aliases."""
    if not isinstance(raw, list):
        return []
    out = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        name = (m.get("kpi_name_cn") or m.get("kpi_name") or "").strip()
        aliases = (m.get("aliases") or "").strip()
        unit = (m.get("unit") or "").strip()
        if not name and not aliases:
            continue
        out.append({"kpi_name_cn": name, "aliases": aliases, "unit": unit})
    return out


# How many KPI trend tiles the 企业大脑 page shows, and how many sparkline
# points each carries (redesign B6 — "last 12 points").
BRAIN_KPI_TILE_CAP = 8
BRAIN_KPI_SERIES_POINTS = 12


@app.route("/api/company-brain/summary")
def brain_summary():
    """企业大脑 hero stats + KPI trend tiles (redesign B6, plan §3.7) — the
    page that makes Moat 1 visible. Read-only composition over metrics_store
    (counts + ONE series GROUP BY) and trends.py (deterministic deltas /
    streaks / series); zero LLM calls, so it's always fast and free.

    KPI tile selection is data-driven: company-total series (dimension IS
    NULL) ranked by distinct-period coverage then recency, capped at 8; if no
    NULL-dimension totals exist (some analyses only break out dimensions),
    per-dimension series fill in the same way. An empty brain answers clean
    zeros + [] — the page renders its 等待第一份报告 state from that."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        # get_or_create_company THREW (the brain DB is down) — a 200 of zeros
        # here is indistinguishable from a brand-new account, so the page would
        # show 等待第一份报告 to a company with months of data. 503 like the
        # sibling brain routes (Phase 7 A3).
        return jsonify({"error": "企业大脑暂时无法加载，请稍后重试。"}), 503

    out = metrics_store.brain_stats(company_id)
    out["kpis"] = []

    index = metrics_store.metric_series_index(company_id)
    totals = [e for e in index if e["dimension"] is None]
    pool = sorted(totals or index, key=lambda e: (-e["periods"], -e["last_id"]))
    chosen = pool[:BRAIN_KPI_TILE_CAP]

    briefs = metrics_store.metric_briefs([e["last_id"] for e in chosen])
    for e in chosen:
        brief = briefs.get(e["last_id"], {})
        deltas = trends.compute_deltas(company_id, e["metric_name"], e["dimension"])
        streak = trends.detect_streaks(company_id, e["metric_name"], e["dimension"])
        series = trends.metric_series(company_id, e["metric_name"], e["dimension"],
                                      last_n=BRAIN_KPI_SERIES_POINTS)
        name = (brief.get("metric_name_cn") or "").strip() or e["metric_name"]
        if e["dimension"]:  # per-dimension fallback tiles carry the dimension
            name = f"{name}（{e['dimension'].split('=', 1)[-1]}）"
        out["kpis"].append({
            "name": name,
            "latest": deltas["latest"],
            "unit": (brief.get("unit") or "").strip(),
            "delta_pct": deltas["wow_pct"],   # 环比 vs previous period
            "streak": streak,                 # {"direction","length"} | null
            "series": series,                 # [{label, value}] — sparkline
        })
    return jsonify(out)


@app.route("/api/company-brain/profile", methods=["GET", "POST"])
def brain_profile():
    """a. 公司档案 + b. 指标定义 (KPI mapping) — both live in profile_json."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "公司档案暂不可用。"}), 503
    if request.method == "GET":
        return jsonify({"profile": metrics_store.get_profile(company_id)})
    data = request.get_json(silent=True) or {}
    profile = metrics_store.get_profile(company_id)  # preserve any unknown keys
    if "company_name" in data:
        profile["company_name"] = (data.get("company_name") or "").strip()
    if "industry" in data:
        profile["industry"] = (data.get("industry") or "").strip()
    if "business_desc" in data:
        profile["business_desc"] = (data.get("business_desc") or "").strip()
    if "fiscal_week_start" in data:
        fw = (data.get("fiscal_week_start") or "Mon").strip()
        profile["fiscal_week_start"] = fw if fw in ("Mon", "Sun") else "Mon"
    if "kpi_mappings" in data:
        profile["kpi_mappings"] = _clean_kpi_mappings(data.get("kpi_mappings"))
    saved = metrics_store.save_profile(company_id, profile)
    return jsonify({"profile": saved, "ok": True})


@app.route("/api/company-brain/glossary", methods=["GET", "POST"])
def brain_glossary():
    """c. 术语表 — list (GET) or add/update one term (POST)."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "术语表暂不可用。"}), 503
    if request.method == "GET":
        return jsonify({"glossary": metrics_store.list_glossary(company_id)})
    data = request.get_json(silent=True) or {}
    term = (data.get("term") or "").strip()
    if not term:
        return jsonify({"error": "请填写术语。"}), 400
    metrics_store.add_glossary_term(company_id, term, data.get("definition") or "")
    return jsonify({"glossary": metrics_store.list_glossary(company_id), "ok": True})


@app.route("/api/company-brain/glossary/update", methods=["POST"])
def brain_glossary_update():
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    data = request.get_json(silent=True) or {}
    metrics_store.update_glossary_term(
        company_id, data.get("id"),
        term=data.get("term"), definition=data.get("definition"))
    return jsonify({"glossary": metrics_store.list_glossary(company_id), "ok": True})


@app.route("/api/company-brain/glossary/delete", methods=["POST"])
def brain_glossary_delete():
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    data = request.get_json(silent=True) or {}
    metrics_store.delete_glossary_term(company_id, data.get("id"))
    return jsonify({"glossary": metrics_store.list_glossary(company_id), "ok": True})


@app.route("/api/company-brain/preferences", methods=["GET", "POST"])
def brain_preferences():
    """d. 报告偏好 — list (GET) or add one standing instruction (POST)."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "报告偏好暂不可用。"}), 503
    if request.method == "GET":
        return jsonify({"preferences": metrics_store.list_report_preferences(company_id)})
    data = request.get_json(silent=True) or {}
    instruction = (data.get("instruction") or "").strip()
    if not instruction:
        return jsonify({"error": "请填写偏好说明。"}), 400
    metrics_store.add_report_preference(company_id, instruction)
    return jsonify({"preferences": metrics_store.list_report_preferences(company_id), "ok": True})


@app.route("/api/company-brain/preferences/toggle", methods=["POST"])
def brain_preferences_toggle():
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    data = request.get_json(silent=True) or {}
    metrics_store.set_report_preference_active(
        company_id, data.get("id"), bool(data.get("active")))
    return jsonify({"preferences": metrics_store.list_report_preferences(company_id), "ok": True})


@app.route("/api/company-brain/preferences/delete", methods=["POST"])
def brain_preferences_delete():
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    data = request.get_json(silent=True) or {}
    metrics_store.delete_report_preference(company_id, data.get("id"))
    return jsonify({"preferences": metrics_store.list_report_preferences(company_id), "ok": True})


@app.route("/api/glossary/confirm", methods=["POST"])
def glossary_confirm():
    """The UNKNOWN_TERMS confirm form on the result page. Body: {answers: {term: def}}.
    Writes each answered term to the company glossary so EVERY future run knows it
    (load_memory_context already injects the glossary). Derives the company from the
    session — never trusts a job_id — so one account can't write another's brain."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "术语表暂不可用。"}), 503
    data = request.get_json(silent=True) or {}
    answers = data.get("answers")
    if not isinstance(answers, dict):
        return jsonify({"error": "格式错误。"}), 400
    written = metrics_store.add_glossary_terms(company_id, answers)
    return jsonify({"written": written, "ok": True})


# ---------------------------------------------------------------------------
# BRANDING (品牌) — logo + brand colour + 企业微信 webhook, all in profile_json.
# The webhook URL is config the user pastes here; it is stored in the brain but
# NEVER written to logs or the deliveries table (see delivery.py).
# ---------------------------------------------------------------------------
_LOGO_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
_MAX_LOGO_BYTES = 1024 * 1024  # 1 MB, per the spec


def _branding_view(company_id):
    b = metrics_store.get_branding(company_id)
    return {
        "brand_color": b.get("brand_color") or metrics_store.DEFAULT_BRAND_COLOR,
        "wechat_webhook": b.get("wechat_webhook") or "",
        "has_logo": bool(b.get("logo_path")),
    }


def _save_company_logo(company_id, ext, data):
    """Persist an uploaded logo under data/branding/<company_id>/ and return the
    PROJECT_ROOT-relative path stored in the profile (resolved at PDF-render time).
    One canonical filename per company, so a re-upload replaces the previous logo."""
    dir_ = os.path.join(metrics_store.DATA_DIR, "branding", str(company_id))
    os.makedirs(dir_, exist_ok=True)
    for e in _LOGO_EXTS:  # drop any prior logo with a different extension
        if e != ext:
            old = os.path.join(dir_, "logo%s" % e)
            if os.path.isfile(old):
                try:
                    os.remove(old)
                except OSError:
                    pass
    abs_path = os.path.join(dir_, "logo%s" % ext)
    with open(abs_path, "wb") as fh:
        fh.write(data)
    return os.path.relpath(abs_path, metrics_store.PROJECT_ROOT)


def _valid_wechat_webhook(url):
    """True only for a genuine 企业微信群机器人 endpoint (E3). The check itself
    lives in delivery.valid_wechat_webhook so the senders re-validate the SAME
    rule at send time (Phase 7 A5) — this is the save-time entrance."""
    return delivery.valid_wechat_webhook(url)


@app.route("/api/company-brain/branding", methods=["GET", "POST"])
def brain_branding():
    """GET the current branding, or POST (multipart) a logo file + brand_color +
    wechat_webhook. Logo: PNG/JPG, ≤ 1 MB. brand_color: #RRGGBB."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "公司大脑暂不可用。"}), 503
    if request.method == "GET":
        return jsonify({"branding": _branding_view(company_id)})

    updates = {}
    color = (request.form.get("brand_color") or "").strip()
    if color:
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
            return jsonify({"error": "品牌色需为 #RRGGBB 十六进制格式。"}), 400
        updates["brand_color"] = color
    if "wechat_webhook" in request.form:
        webhook = (request.form.get("wechat_webhook") or "").strip()
        # Only the real 企业微信 robot endpoint is accepted ("" clears it). The
        # delivery-test button POSTs to whatever is stored, so an arbitrary URL
        # here was a server-side request forgery surface (Phase 6 E3).
        if webhook and not _valid_wechat_webhook(webhook):
            return jsonify({"error": "仅支持企业微信群机器人地址"
                                     "（https://qyapi.weixin.qq.com/…）。"}), 400
        updates["wechat_webhook"] = webhook

    f = request.files.get("logo")
    if f and f.filename:
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in _LOGO_EXTS:
            return jsonify({"error": "Logo 仅支持 PNG 或 JPG 格式。"}), 400
        data = f.read()
        if len(data) > _MAX_LOGO_BYTES:
            return jsonify({"error": "Logo 文件不能超过 1MB。"}), 400
        updates["logo_path"] = _save_company_logo(company_id, ext, data)

    if updates:
        metrics_store.merge_profile(company_id, updates)
    return jsonify({"branding": _branding_view(company_id), "ok": True})


@app.route("/api/company-brain/logo")
def brain_logo():
    """Serve the current account's logo (for the brain-page preview). Ownership is
    the session's company — never a path/id param — so no account can read another's."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    rel = (metrics_store.get_branding(company_id).get("logo_path") or "").strip()
    if not rel:
        return jsonify({"error": "未设置 Logo。"}), 404
    abs_path = rel if os.path.isabs(rel) else os.path.join(metrics_store.PROJECT_ROOT, rel)
    if not os.path.isfile(abs_path):
        return jsonify({"error": "Logo 文件不存在。"}), 404
    return send_from_directory(os.path.dirname(abs_path), os.path.basename(abs_path))


# ---------------------------------------------------------------------------
# SCHEDULED JOBS (定时任务) — CRUD; every mutation re-syncs the live scheduler.
# ---------------------------------------------------------------------------
_ALLOWED_CHANNELS = ("email", "wechat", "service_account")


def _valid_cron(expr):
    """True if `expr` is a parseable 5-field cron string."""
    expr = (expr or "").strip()
    if not expr:
        return False
    try:
        from apscheduler.triggers.cron import CronTrigger
        CronTrigger.from_crontab(expr)
        return True
    except Exception:
        return False


def _clean_channels(raw):
    """A de-duplicated list of known delivery channels from whatever the UI sent."""
    if not isinstance(raw, list):
        return []
    out = []
    for c in raw:
        c = (c or "").strip().lower()
        if c in _ALLOWED_CHANNELS and c not in out:
            out.append(c)
    return out


# The scheduler runs Asia/Shanghai (agent_team.scheduler.DEFAULT_TIMEZONE) — the
# wall clock the customer typed their 每周一 08:00 in.
_SCHEDULE_TZ = "Asia/Shanghai"


def _job_next_run_at(job):
    """The job's next fire time as an ISO string (Asia/Shanghai), or None for an
    inactive job / malformed cron. Computed STATELESSLY from the cron expression —
    never from live APScheduler state: in production the web process runs with
    RUN_SCHEDULER=0 (a dedicated scheduler container owns the cron loop), so the
    web process has no live Job to ask (redesign B5)."""
    if not job.get("active"):
        return None
    try:
        from apscheduler.triggers.cron import CronTrigger
        trigger = CronTrigger.from_crontab((job.get("cron_expr") or "").strip(),
                                           timezone=_SCHEDULE_TZ)
        nxt = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
        return nxt.isoformat() if nxt else None
    except Exception:
        return None


def _jobs_payload(company_id, customer=None):
    """The account's jobs, each enriched with next_run_at (every jobs response
    carries it so the UI can re-render from any CRUD reply). budget_cny is the
    per-run internal cost cap — machinery, so it ships to founder sessions
    only (the customer UI already hid it; the API leaked it, Phase 6 E2)."""
    jobs = metrics_store.list_scheduled_jobs(company_id)
    admin = _is_admin(customer)
    for j in jobs:
        j["next_run_at"] = _job_next_run_at(j)
        if not admin:
            j.pop("budget_cny", None)
    return jobs


def _scheduling_plan_gate(customer):
    """D3 (redesign B5) — scheduled auto-delivery is 企业版 (pro) only. Returns
    (allowed, refusal_response): the refusal is a structured, friendly 402 the
    schedule page and the wizard both render as the upgrade explainer."""
    plan_code, _sub, _locked = metrics_store.effective_plan(customer["id"])
    if config.plan_has(plan_code, "scheduled_delivery"):
        return True, None
    pro = config.plan("pro")
    return False, (jsonify({
        "error": ("定时自动送达是%s专属功能。升级后，每周一早上 8 点经营报告自动"
                  "生成并送达企业微信和邮箱 — 你什么都不用做。" % pro["label"]),
        "code": "upgrade_required",
        "feature": "scheduled_delivery",
        "required_plan": pro["code"],
        "required_plan_label": pro["label"],
    }), 402)


@app.route("/api/scheduled-jobs", methods=["GET", "POST"])
def scheduled_jobs():
    """List (GET) or create (POST) the logged-in account's 定时任务. Creation is
    plan-gated per D3 — only plans with the scheduled_delivery feature."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "公司大脑暂不可用。"}), 503
    if request.method == "GET":
        return jsonify({"jobs": _jobs_payload(company_id, customer)})

    allowed, refusal = _scheduling_plan_gate(customer)
    if not allowed:
        return refusal

    data = request.get_json(silent=True) or {}
    name_cn = (data.get("name_cn") or "").strip()
    cron_expr = (data.get("cron_expr") or "").strip()
    if not name_cn:
        return jsonify({"error": "请填写任务名称。"}), 400
    if not _valid_cron(cron_expr):
        return jsonify({"error": "频率无效（cron 表达式无法解析）。"}), 400
    source_mode = data.get("source_mode") or SOURCE_LATEST_UPLOAD
    # budget_cny is the internal per-run cost cap — machinery. It has been
    # read-redacted for customers since Phase 6 (E2); the WRITE is founder-only
    # too, or any logged-in account could set its own cap (Phase 7 A5).
    job_id = metrics_store.create_scheduled_job(
        company_id, name_cn, cron_expr,
        goal_template=(data.get("goal_template") or "").strip(),
        source_mode=source_mode,
        industry_override=data.get("industry_override"),
        fixed_file_paths=data.get("fixed_file_paths") or [],
        delivery_channels=_clean_channels(data.get("delivery_channels")),
        budget_cny=(data.get("budget_cny") if _is_admin(customer) else None),
        active=bool(data.get("active", True)),
    )
    SCHEDULER.reload()
    return jsonify({"jobs": _jobs_payload(company_id, customer),
                    "job_id": job_id, "ok": True})


@app.route("/api/scheduled-jobs/update", methods=["POST"])
def scheduled_jobs_update():
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "公司大脑暂不可用。"}), 503
    data = request.get_json(silent=True) or {}
    fields = {k: data[k] for k in (
        "name_cn", "cron_expr", "industry_override", "goal_template",
        "source_mode", "fixed_file_paths", "delivery_channels",
        "budget_cny", "active") if k in data}
    # budget_cny stays founder-only on write, same as the create route (A5).
    if not _is_admin(customer):
        fields.pop("budget_cny", None)
    # D3 — RE-ACTIVATING a job is gated like creating one (deactivating, editing
    # fields, or deleting stays open so a downgraded account can manage its rows).
    if fields.get("active"):
        allowed, refusal = _scheduling_plan_gate(customer)
        if not allowed:
            return refusal
    if "cron_expr" in fields and not _valid_cron((fields.get("cron_expr") or "").strip()):
        return jsonify({"error": "频率无效（cron 表达式无法解析）。"}), 400
    if "delivery_channels" in fields:
        fields["delivery_channels"] = _clean_channels(fields["delivery_channels"])
    ok = metrics_store.update_scheduled_job(company_id, data.get("id"), **fields)
    SCHEDULER.reload()
    if not ok:
        return jsonify({"error": "任务不存在或无权修改。"}), 404
    return jsonify({"jobs": _jobs_payload(company_id, customer), "ok": True})


@app.route("/api/scheduled-jobs/delete", methods=["POST"])
def scheduled_jobs_delete():
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "公司大脑暂不可用。"}), 503
    data = request.get_json(silent=True) or {}
    ok = metrics_store.delete_scheduled_job(company_id, data.get("id"))
    SCHEDULER.reload()
    if not ok:
        return jsonify({"error": "任务不存在或无权删除。"}), 404
    return jsonify({"jobs": _jobs_payload(company_id, customer), "ok": True})


# ---------------------------------------------------------------------------
# DELIVERY TEST (redesign B5) — the 送达渠道 card's 发送测试消息 button. Errors
# are credential-free by construction (delivery.py's senders guarantee it) and
# the webhook URL is NEVER echoed back in the response.
# ---------------------------------------------------------------------------
@app.route("/api/delivery/test", methods=["POST"])
@limiter.limit(config.run_rate_limit, key_func=_run_rate_key)
def delivery_test():
    """Send one test message over a configured channel. Body: {"channel":
    "email"|"wechat"}. Returns {ok: true} or {ok: false, error: <friendly>}."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    data = request.get_json(silent=True) or {}
    channel = (data.get("channel") or "").strip().lower()
    product = config.product_name()
    if channel == "email":
        ok, err = delivery.send_email(
            customer.get("email"), "%s · 测试消息" % product,
            "您好：\n\n这是一条来自 %s 的测试消息。收到它，说明邮箱送达渠道配置"
            "正常，之后的定时经营报告会发送到这个邮箱。\n\n— %s" % (product, product))
    elif channel == "wechat":
        webhook = (metrics_store.get_branding(company_id).get("wechat_webhook")
                   if company_id else "")
        ok, err = delivery.send_wechat_webhook(
            webhook,
            "### %s · 测试消息\n\n收到这条消息，说明企业微信群机器人配置正常。\n"
            "之后的定时经营报告会自动推送到这个群。" % product)
    else:
        return jsonify({"error": "channel 必须是 email 或 wechat。"}), 400
    # One audit row per attempt — channel + outcome only, never the webhook/creds.
    try:
        metrics_store.record_delivery(channel,
                                      DELIVERY_SENT if ok else DELIVERY_FAILED,
                                      error=(None if ok else err))
    except Exception:
        pass
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": err})


# ---------------------------------------------------------------------------
# DATA INBOX (数据收件箱, redesign B5) — view and edit the staged upload batch
# the next "latest_upload" scheduled fire will read. No run is started here.
# ---------------------------------------------------------------------------
def _upload_inbox_payload(company_id):
    """{batch, batch_time, files: [{name, size}]} for the newest upload batch.
    size is the on-disk (ciphertext when FERNET is set) size — the name and the
    count are the load-bearing information for the UI."""
    latest = metrics_store.latest_upload_dir(company_id) if company_id else None
    files = metrics_store.list_files(latest) if latest else []
    batch = os.path.basename(latest) if latest else None
    batch_time = None
    if batch:
        try:  # batch dirs are named UTC "%Y%m%d-%H%M%S-%f" (new_upload_batch_dir)
            batch_time = (datetime.strptime(batch, "%Y%m%d-%H%M%S-%f")
                          .replace(tzinfo=timezone.utc).isoformat())
        except ValueError:
            batch_time = None
    return {
        "batch": batch,
        "batch_time": batch_time,
        "files": [{"name": os.path.basename(p), "size": os.path.getsize(p)}
                  for p in files],
    }


@app.route("/api/uploads/latest")
def uploads_latest():
    """The files the next scheduled "latest_upload" fire will use."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    return jsonify(_upload_inbox_payload(company_id))


# 数据收件箱 file names are customer-facing, load-bearing info — werkzeug's
# secure_filename strips every non-ASCII char, so 六月销售明细.csv would land
# (and display) as "csv", and any two Chinese-named files would collide. This
# keeps CJK while preserving the same safety: basename only (no traversal),
# no separators/控制字符, never empty or dot-only.
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def _inbox_safe_filename(filename, fallback="file"):
    base = os.path.basename((filename or "").replace("\\", "/")).strip()
    base = _UNSAFE_FILENAME_CHARS.sub("_", base).strip(" .")
    if not base:
        return fallback
    return base[:120]


# The inbox batch swap is read-prev → create-new → copy-forward → save; two
# concurrent /api/uploads for the SAME company both read the same "prev", each
# builds its own new batch, and whichever finishes second silently drops the
# other's file (reproduced race, Phase 6 E1). One lock per company serializes
# the swap; different companies never contend.
_UPLOAD_LOCKS = {}
_UPLOAD_LOCKS_GUARD = threading.Lock()


def _company_upload_lock(company_id):
    with _UPLOAD_LOCKS_GUARD:
        lock = _UPLOAD_LOCKS.get(company_id)
        if lock is None:
            lock = _UPLOAD_LOCKS[company_id] = threading.Lock()
        return lock


@app.route("/api/uploads", methods=["POST"])
@limiter.limit(config.upload_rate_limit, key_func=_run_rate_key)
def uploads_modify():
    """Add files to / remove files from the data inbox WITHOUT starting a run.

    Multipart: repeated "files" parts to add, plus an optional "remove" form
    field (a JSON array of file names, or repeated plain values). A pure-removal
    call may instead POST JSON {"remove": ["name.csv", ...]}.

    CRITICAL batch semantics: latest_upload_dir returns only the single newest
    batch, so the current batch's files are COPIED FORWARD into the new batch
    (minus removals / same-name replacements) before the new files are saved —
    otherwise adding one file would silently drop everything previously staged.
    Upload guards are identical to /api/run and run BEFORE anything is written."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "公司档案暂不可用。"}), 503

    upload_files = [f for f in request.files.getlist("files") if f and f.filename]
    remove = set()
    if request.form.getlist("remove"):
        for raw in request.form.getlist("remove"):
            raw = (raw or "").strip()
            if raw.startswith("["):
                try:
                    remove.update(str(x) for x in json.loads(raw))
                except ValueError:
                    pass
            elif raw:
                remove.add(raw)
    else:
        body = request.get_json(silent=True) or {}
        if isinstance(body.get("remove"), list):
            remove.update(str(x) for x in body["remove"])
    if not upload_files and not remove:
        return jsonify({"error": "请添加要上传的文件，或指定要移除的文件。"}), 400

    # Phase 8 upload guards, verbatim from /api/run — count, size, magic bytes.
    if len(upload_files) > config.MAX_UPLOAD_FILES_PER_RUN:
        return jsonify({"error": f"一次最多上传 {config.MAX_UPLOAD_FILES_PER_RUN} 个文件"
                                 f"（你选择了 {len(upload_files)} 个）。"}), 400
    max_mb = config.MAX_UPLOAD_BYTES_PER_FILE // (1024 * 1024)
    for f in upload_files:
        head = f.stream.read(16)
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        if security.looks_executable(head):
            return jsonify({"error": f"文件 “{f.filename}” 看起来是可执行程序，已拒绝上传。"}), 400
        if size > config.MAX_UPLOAD_BYTES_PER_FILE:
            return jsonify({"error": f"文件 “{f.filename}” 超过单个文件 {max_mb} MB 的上限。"}), 400

    new_names = {_inbox_safe_filename(f.filename) for f in upload_files}
    # The whole read-prev → new-batch → write sequence runs under the company's
    # upload lock: concurrent calls would otherwise both copy forward from the
    # same prev batch and silently lose one call's files (E1).
    with _company_upload_lock(company_id):
        # Resolve the CURRENT batch before creating the new one — new_upload_batch_dir
        # creates its (newer) directory immediately, which would become the "latest".
        prev = metrics_store.latest_upload_dir(company_id)
        carry = [p for p in metrics_store.list_files(prev)
                 if os.path.basename(p) not in remove
                 and os.path.basename(p) not in new_names] if prev else []
        if len(carry) + len(upload_files) > config.MAX_UPLOAD_FILES_PER_RUN:
            return jsonify({"error": f"数据收件箱最多保留 {config.MAX_UPLOAD_FILES_PER_RUN} 个文件，"
                                     f"请先移除一些旧文件。"}), 400

        batch_dir = metrics_store.new_upload_batch_dir(company_id)
        for sp in carry:
            dest = os.path.join(batch_dir, os.path.basename(sp))
            shutil.copy2(sp, dest)  # sp is already encrypted; copy keeps it so
            crypto.encrypt_file_in_place(dest)  # idempotent — guarantees at-rest
        for f in upload_files:
            path = os.path.join(batch_dir, _inbox_safe_filename(f.filename))
            f.save(path)
            crypto.encrypt_file_in_place(path)

    return jsonify(_upload_inbox_payload(company_id))


# ---------------------------------------------------------------------------
# REPORTS (历史报告) — list an account's runs + a login+ownership-checked PDF
# download. The download route is the security boundary: a logged-in account can
# ONLY fetch its own company's reports (cross-account → 403).
# ---------------------------------------------------------------------------
@app.route("/api/reports")
def list_reports():
    """The 历史报告 rows — CUSTOMER-SAFE fields only (redesign Phase 5 A3):
    total_cost_yuan is internal COGS and report_path is a raw job id, so neither
    leaves the server; has_content says whether /reports/<id> can render."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"reports": []})
    out = []
    for r in metrics_store.list_runs(company_id, limit=100):
        rid = r.get("id")
        has_pdf = os.path.isfile(metrics_store.report_pdf_path(company_id, rid))
        job_id = (r.pop("report_path", "") or "").strip()
        r.pop("total_cost_yuan", None)
        r["has_content"] = bool(
            job_id and re.fullmatch(r"[A-Za-z0-9_-]+", job_id)
            and (job_id in JOBS
                 or os.path.isfile(os.path.join(MEMORY_DIR, "%s.json" % job_id))))
        r["has_pdf"] = has_pdf
        r["download_url"] = ("/api/reports/%s/download" % rid) if has_pdf else None
        out.append(r)
    return jsonify({"reports": out})


@app.route("/api/reports/<int:run_id>")
def report_detail(run_id):
    """The web report view's data source (redesign §3.4 / B7): one finished run
    as a CUSTOMER-SAFE document. Login required and the run must belong to the
    requesting account's company (the same auth ladder as the PDF download).

    runs.report_path stores the run's JOB ID (orchestrator._finish_run), which
    keys the memory_store snapshot. Only report-facing fields are returned —
    NEVER documents (the uploaded raw text), artifacts, or activity (machinery)."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    run = metrics_store.get_run(run_id)
    if not run:
        return jsonify({"error": "报告不存在。"}), 404
    if not company_id or run.get("company_id") != company_id:
        return jsonify({"error": "无权访问该报告。"}), 403

    has_pdf = os.path.isfile(metrics_store.report_pdf_path(company_id, run_id))
    out = {
        "id": run_id,
        "goal_text": run.get("goal_text") or "",
        "status": run.get("status"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "has_pdf": has_pdf,
        "download_url": ("/api/reports/%s/download" % run_id) if has_pdf else None,
    }

    # History-import and crashed runs never stamped a job id — no web content,
    # though the PDF (when present, included above) may still be downloadable.
    # A corrupt-but-present snapshot degrades the same way (A5), never a 500.
    job_id = (run.get("report_path") or "").strip()
    memory = None
    if job_id and re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
        memory = _load_job_memory(job_id)
    if memory is None:
        out["error"] = "报告内容不可用（该报告可能来自历史导入，或当时未正常完成）。"
        return jsonify(out), 404

    snap = memory.snapshot()
    out.update({
        "final_report": snap.get("final_report") or "",
        "sources": snap.get("sources") or [],
        "sources_status": snap.get("sources_status"),
        "unknown_terms": snap.get("unknown_terms") or [],
    })
    return jsonify(out)


@app.route("/api/reports/<int:run_id>/download")
def download_report_pdf(run_id):
    """Serve a run's branded PDF — login required, and the run MUST belong to the
    requesting account's company. A cross-account request gets 403, never the file."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    run = metrics_store.get_run(run_id)
    if not run:
        return jsonify({"error": "报告不存在。"}), 404
    if not company_id or run.get("company_id") != company_id:
        return jsonify({"error": "无权访问该报告。"}), 403
    pdf_path = metrics_store.report_pdf_path(company_id, run_id)
    if not os.path.isfile(pdf_path):
        return jsonify({"error": "报告 PDF 尚未生成。"}), 404
    metrics_store.write_audit(customer["id"], AUDIT_DOWNLOAD, _client_ip(),
                              detail="report pdf run %s" % run_id)
    # The PDF is encrypted at rest (Phase 8); decrypt into memory and stream that
    # (a no-op passthrough when FERNET is unset).
    return send_file(io.BytesIO(crypto.read_plaintext(pdf_path)),
                     as_attachment=True, download_name="经营周报-%s.pdf" % run_id,
                     mimetype="application/pdf")


# ---- Report feedback (Phase 6 feedback loop) ------------------------------
@app.route("/api/feedback", methods=["POST"])
def submit_feedback():
    """Record one 👍/👎 (+ optional comment) on a single report section. Login is
    required, and a run id (when supplied) MUST belong to the requesting account's
    company — a cross-account run id is rejected, never written."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录后再反馈。"}), 401
    if not company_id:
        return jsonify({"error": "反馈功能暂不可用。"}), 503
    data = request.get_json(silent=True) or {}
    vote = (data.get("vote") or "").strip()
    if vote not in VOTE_VALUES:
        return jsonify({"error": "无效的反馈类型。"}), 400
    section_title = (data.get("section_title") or "").strip()
    comment = (data.get("comment") or "").strip()
    run_id = data.get("run_id")
    try:
        run_id = int(run_id) if run_id not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        run_id = None
    if run_id is not None:
        run = metrics_store.get_run(run_id)
        if not run or run.get("company_id") != company_id:
            return jsonify({"error": "无权对该报告反馈。"}), 403
    fid = metrics_store.record_feedback(company_id, run_id, section_title, vote, comment)
    if not fid:
        return jsonify({"error": "反馈未能保存。"}), 400
    # A 👎 whose comment starts with "以后…" is a candidate for a STANDING preference;
    # the UI surfaces a one-click "存为长期偏好" button for it.
    preference_candidate = (vote == VOTE_DOWN and comment.startswith("以后"))
    return jsonify({"ok": True, "id": fid, "preference_candidate": preference_candidate})


@app.route("/api/feedback/save-preference", methods=["POST"])
def feedback_save_preference():
    """Promote a 👎 comment (e.g. "以后把退款放第一节") into a standing report
    preference, so the NEXT run's memory context picks it up automatically. Login
    required; the feedback row must belong to the requesting account's company."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "暂不可用。"}), 503
    data = request.get_json(silent=True) or {}
    fb = metrics_store.get_feedback(data.get("id"))
    if not fb or fb.get("company_id") != company_id:
        return jsonify({"error": "未找到该反馈。"}), 404
    instruction = (fb.get("comment") or "").strip()
    if not instruction:
        return jsonify({"error": "该反馈没有可保存的说明。"}), 400
    pref_id = metrics_store.add_report_preference(company_id, instruction)
    return jsonify({"ok": True, "preference_id": pref_id,
                    "preferences": metrics_store.list_report_preferences(company_id)})


# ---- Founder feedback dashboard (Phase 6) — gated by $ADMIN_EMAILS --------
def _feedback_filter_vote():
    v = (request.args.get("vote") or "").strip().lower()
    return v if v in VOTE_VALUES else None


@app.route("/admin/feedback")
def admin_feedback():
    """Founder-only: every feedback row across ALL companies, filterable by vote,
    with a CSV export link. Locked to accounts whose email is in $ADMIN_EMAILS;
    everyone else (including logged-in non-admins) gets a 403."""
    if not _is_admin(_current_customer()):
        return Response("403 — 仅限管理员访问 (/admin/feedback)。",
                        status=403, mimetype="text/plain; charset=utf-8")
    vote = _feedback_filter_vote()
    rows = metrics_store.list_feedback(vote=vote)
    return Response(_render_admin_feedback_html(rows, vote),
                    mimetype="text/html; charset=utf-8")


@app.route("/admin/feedback/export.csv")
def admin_feedback_csv():
    """CSV of all feedback (honouring the ?vote filter). Same $ADMIN_EMAILS gate."""
    if not _is_admin(_current_customer()):
        return Response("403", status=403, mimetype="text/plain; charset=utf-8")
    vote = _feedback_filter_vote()
    rows = metrics_store.list_feedback(vote=vote)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "created_at", "account_email", "company_id", "run_id",
                "section_title", "vote", "comment"])
    for r in rows:
        w.writerow([r["id"], r["created_at"] or "", r["account_email"] or "",
                    r["company_id"] or "", r["run_id"] or "", r["section_title"],
                    r["vote"], r["comment"]])
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=feedback.csv"})


# ---- Platform spend circuit breaker (Phase 8) — gated by $ADMIN_EMAILS -----
@app.route("/admin/circuit-breaker", methods=["GET", "POST"])
def admin_circuit_breaker():
    """Founder-only: view the platform-spend breaker and manually resume service.

    GET shows the latch state + rolling hour/day spend vs caps. A POST with
    action=reset clears the latch — the ONLY way to resume after an auto-trip — and
    records an admin_action audit row. $ADMIN_EMAILS gate; everyone else gets 403."""
    customer = _current_customer()
    if not _is_admin(customer):
        return Response("403 — 仅限管理员访问 (/admin/circuit-breaker)。",
                        status=403, mimetype="text/plain; charset=utf-8")
    msg = None
    if request.method == "POST":
        if (request.form.get("action") or "").strip() == "reset":
            platform_guard.reset()
            metrics_store.write_audit(customer["id"], AUDIT_ADMIN, _client_ip(),
                                      detail="circuit breaker reset")
            msg = "已恢复服务：断路器已重置。"
    return Response(_render_breaker_html(platform_guard.status(), msg),
                    mimetype="text/html; charset=utf-8")


def _render_breaker_html(st, msg=None):
    """Render the founder-only circuit-breaker page. Every dynamic value is escaped.
    Built by concatenation (not one big %-format) so the literal % in the CSS is safe."""
    b = st.get("breaker") or {}
    tripped = bool(b.get("tripped"))
    state = ('<span class="bad">🔴 已熔断 TRIPPED</span>' if tripped
             else '<span class="ok">🟢 正常 OK</span>')

    def cap(v):  # 0 / None means "no cap"
        return "无上限" if not v else "¥%s" % _h(str(v))

    rows = [
        ("状态 State", state),
        ("熔断范围 Scope", _h(b.get("scope") or "—")),
        ("熔断原因 Reason", _h(b.get("reason") or "—")),
        ("触发时间 Tripped at", _h(b.get("tripped_at") or "—")),
        ("上次重置 Cleared at", _h(b.get("cleared_at") or "—")),
        ("本小时花费 Hour spend",
         "¥%s / %s" % (_h(str(st.get("hour_spend_yuan"))), cap(st.get("hourly_cap_yuan")))),
        ("今日花费 Day spend",
         "¥%s / %s" % (_h(str(st.get("day_spend_yuan"))), cap(st.get("daily_cap_yuan")))),
        ("单账户日上限 Per-account/day", cap(st.get("account_daily_cap_yuan"))),
    ]
    table = "".join("<tr><th>%s</th><td>%s</td></tr>" % (k, v) for k, v in rows)
    banner = ('<p class="banner">%s</p>' % _h(msg)) if msg else ""
    btn = _h("重置断路器 · 恢复服务" if tripped else "重置断路器（当前正常）")
    head = ("<!doctype html><html lang=zh><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width, initial-scale=1'>"
            "<title>断路器 · Circuit Breaker</title><style>"
            "body{font-family:system-ui,Arial,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;color:#1f2937}"
            "h1{font-size:20px}table{border-collapse:collapse;width:100%;margin:16px 0}"
            "th,td{border:1px solid #e5e7eb;padding:8px 10px;text-align:left}"
            "th{background:#f9fafb;width:45%;font-weight:600}"
            ".ok{color:#047857;font-weight:700}.bad{color:#b91c1c;font-weight:700}"
            ".banner{background:#ecfdf5;border:1px solid #a7f3d0;color:#065f46;padding:10px 12px;border-radius:8px}"
            "button{background:#b91c1c;color:#fff;border:0;padding:10px 16px;border-radius:8px;font-size:15px;cursor:pointer}"
            "</style></head><body><h1>平台支出断路器 · Platform Spend Circuit Breaker</h1>")
    foot = ("<form method=post><input type=hidden name=action value=reset>"
            "<button type=submit>" + btn + "</button></form>"
            "<p style='color:#6b7280;font-size:13px'>仅限管理员（$ADMIN_EMAILS）。"
            "重置会清除熔断闩并归零滚动计费窗口。</p></body></html>")
    return head + banner + "<table>" + table + "</table>" + foot


# ---- Phase 9 admin: orders & refunds, fapiao worklist, reconciliation ------
# All three share the breaker page's plain server-rendered style and the same
# $ADMIN_EMAILS gate; every dynamic value is escaped.
_ADMIN_CSS = (
    "body{font-family:system-ui,Arial,sans-serif;max-width:980px;margin:36px auto;"
    "padding:0 16px;color:#1f2937}h1{font-size:20px}"
    "table{border-collapse:collapse;width:100%;margin:14px 0;font-size:14px}"
    "th,td{border:1px solid #e5e7eb;padding:7px 9px;text-align:left;vertical-align:top}"
    "th{background:#f9fafb;font-weight:600}"
    ".muted{color:#6b7280;font-size:13px}"
    ".chip{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600}"
    ".c-paid{background:#ecfdf5;color:#047857}.c-created{background:#eff6ff;color:#1d4ed8}"
    ".c-failed{background:#fef2f2;color:#b91c1c}.c-refunded{background:#f5f3ff;color:#6d28d9}"
    ".c-requested{background:#fffbeb;color:#b45309}.c-issued{background:#ecfdf5;color:#047857}"
    ".c-rejected{background:#fef2f2;color:#b91c1c}"
    ".banner{background:#ecfdf5;border:1px solid #a7f3d0;color:#065f46;padding:10px 12px;border-radius:8px}"
    ".banner.err{background:#fef2f2;border-color:#fecaca;color:#991b1b}"
    "input[type=number],input[type=text]{padding:5px 7px;border:1px solid #d1d5db;border-radius:6px;width:110px}"
    "button{background:#1f2937;color:#fff;border:0;padding:6px 12px;border-radius:7px;"
    "font-size:13px;cursor:pointer}button.danger{background:#b91c1c}"
    "a{color:#1d4ed8}pre{background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;"
    "padding:10px;font-size:12px;white-space:pre-wrap;word-break:break-all}"
)


def _admin_page(title, body_html, msg=None, msg_err=False):
    banner = ""
    if msg:
        banner = '<p class="banner%s">%s</p>' % (" err" if msg_err else "", _h(msg))
    return Response(
        "<!doctype html><html lang=zh><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>" + _h(title) + "</title><style>" + _ADMIN_CSS + "</style></head>"
        "<body><h1>" + _h(title) + "</h1>"
        "<p class=muted><a href=/admin>看板</a> · <a href=/admin/orders>订单/退款</a> · "
        "<a href=/admin/fapiao>发票</a> · <a href=/admin/reconciliation>对账</a> · "
        "<a href=/admin/circuit-breaker>断路器</a> · <a href=/admin/feedback>反馈</a></p>"
        + banner + body_html + "</body></html>",
        mimetype="text/html; charset=utf-8")


def _status_chip(s):
    return '<span class="chip c-%s">%s</span>' % (_h(s or ""), _h(s or "—"))


def _admin_refund(customer, order_no, amount_raw):
    """The admin-only refund action (full or partial). Returns (ok, message).

    Order of operations: validate headroom → for a top-up, check the wallet still
    covers the refund (credit already spent ⇒ REFUSE with a clear error, per
    spec) → provider refund → deduct wallet + mark order, audit row last."""
    order = metrics_store.get_order(order_no)
    if not order:
        return False, "订单不存在。"
    try:
        amount = round(float(amount_raw), 2)
    except (TypeError, ValueError):
        return False, "请输入有效的退款金额。"
    remaining = round(order["amount_yuan"] - order["refunded_yuan"], 2)
    if order["status"] not in (ORDER_PAID,) or remaining <= 0:
        return False, "该订单不可退款（状态 %s）。" % order["status"]
    if not (0 < amount <= remaining):
        return False, "退款金额需在 ¥0 – ¥%.2f 之间。" % remaining

    # Top-up refunds claw the credit back out of the wallet — refuse up front if
    # the balance no longer covers it (the credit was already spent).
    if order["type"] == ORDER_TOPUP:
        acct = CUSTOMERS.get_by_id(order["account_id"])
        bal = (acct or {}).get("balance")
        if acct is None:
            return False, "找不到该订单的账户。"
        if bal is not None and bal < amount:
            return False, ("无法退款：该笔充值已被消费（当前余额 ¥%.2f < 退款 ¥%.2f）。"
                           % (bal, amount))

    prov = payments.get_provider(order["provider"])
    if prov is None:
        return False, "支付渠道 %s 当前不可用，无法发起退款。" % order["provider"]
    ok, err = prov.refund(order["order_no"], amount,
                          total_yuan=order["amount_yuan"])
    if not ok:
        return False, err or "退款失败。"

    if order["type"] == ORDER_TOPUP:
        try:
            CUSTOMERS.deduct_balance(order["account_id"], amount)
        except ValueError as e:
            # The pre-check passed but the balance was spent in the gap — the
            # provider-side refund already went out, so surface this LOUDLY.
            _pay_log.error("refund deduct failed after provider refund: order=%s",
                           order["order_no"])
            return False, "渠道退款已发起，但扣减余额失败：%s 请人工核对！" % e
    res = metrics_store.mark_order_refunded(order["order_no"], amount)
    if not res["ok"]:
        return False, "退款已发起，但订单标记失败（%s），请人工核对。" % res["reason"]
    metrics_store.write_audit(customer["id"], AUDIT_ADMIN, _client_ip(),
                              detail="refund ¥%.2f order %s" % (amount, order_no))
    return True, "已退款 ¥%.2f（订单 %s）。" % (amount, order_no)


@app.route("/admin/orders", methods=["GET", "POST"])
def admin_orders():
    """Founder-only: recent orders across all accounts + the refund action."""
    customer = _current_customer()
    if not _is_admin(customer):
        return Response("403 — 仅限管理员访问 (/admin/orders)。",
                        status=403, mimetype="text/plain; charset=utf-8")
    msg, err = None, False
    if request.method == "POST" and (request.form.get("action") or "") == "refund":
        ok, msg = _admin_refund(customer, (request.form.get("order_no") or "").strip(),
                                request.form.get("amount"))
        err = not ok
    rows = []
    for o in metrics_store.list_orders(limit=100):
        remaining = round(o["amount_yuan"] - o["refunded_yuan"], 2)
        refund_form = ""
        if o["status"] == ORDER_PAID and remaining > 0:
            refund_form = (
                "<form method=post style='display:flex;gap:6px'>"
                "<input type=hidden name=action value=refund>"
                "<input type=hidden name=order_no value='" + _h(o["order_no"]) + "'>"
                "<input type=number name=amount step=0.01 min=0.01 max='"
                + _h("%.2f" % remaining) + "' value='" + _h("%.2f" % remaining) + "'>"
                "<button class=danger type=submit "
                "onclick=\"return confirm('确认退款？')\">退款</button></form>")
        refunded = ("<div class=muted>已退 ¥%.2f</div>" % o["refunded_yuan"]
                    if o["refunded_yuan"] else "")
        rows.append(
            "<tr><td>" + _h(o["order_no"]) + "<div class=muted>"
            + _h(o["provider"]) + (" · " + _h(o["provider_txn_id"])
                                   if o["provider_txn_id"] else "") + "</div></td>"
            "<td>" + _h(o["type"]) + (("/" + _h(o["plan_code"])) if o["plan_code"] else "")
            + "</td><td>¥" + _h("%.2f" % o["amount_yuan"]) + refunded + "</td>"
            "<td>" + _status_chip(o["status"]) + "</td>"
            "<td class=muted>" + _h(o["account_id"]) + "</td>"
            "<td class=muted>" + _h(o["created_at"] or "")
            + ("<div>付款 " + _h(o["paid_at"]) + "</div>" if o["paid_at"] else "")
            + "</td><td>" + refund_form + "</td></tr>")
    table = ("<table><thead><tr><th>订单</th><th>类型</th><th>金额</th><th>状态</th>"
             "<th>账户</th><th>时间</th><th>退款</th></tr></thead><tbody>"
             + ("".join(rows) or "<tr><td colspan=7 class=muted>暂无订单</td></tr>")
             + "</tbody></table>")
    resp = _admin_page("订单与退款 · Orders & Refunds", table, msg, err)
    if err:
        resp.status_code = 400  # a refused refund is an explicit, visible failure
    return resp


@app.route("/admin/fapiao", methods=["GET", "POST"])
def admin_fapiao():
    """Founder-only: the bookkeeper's 发票 worklist. POST action=issue marks a
    request issued AND emails the customer; action=reject records the reason.
    The CSV export below is what gets handed to the 代记账 bookkeeper."""
    customer = _current_customer()
    if not _is_admin(customer):
        return Response("403 — 仅限管理员访问 (/admin/fapiao)。",
                        status=403, mimetype="text/plain; charset=utf-8")
    msg, err = None, False
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        req_id = request.form.get("id")
        note = (request.form.get("note") or "").strip() or None
        if action in ("issue", "reject") and req_id:
            new_status = FAPIAO_ISSUED if action == "issue" else FAPIAO_REJECTED
            updated = metrics_store.set_fapiao_status(req_id, new_status, note=note)
            if not updated:
                msg, err = "找不到该发票申请。", True
            else:
                metrics_store.write_audit(
                    customer["id"], AUDIT_ADMIN, _client_ip(),
                    detail="fapiao #%s %s" % (req_id, new_status))
                if new_status == FAPIAO_ISSUED and updated.get("email"):
                    # Best-effort customer notice; a mail failure must not undo the mark.
                    delivery.send_email(
                        updated["email"], "发票已开具",
                        "您好：\n\n您申请的发票已开具完成。\n\n"
                        "抬头：%s\n税号：%s\n订单：%s（¥%.2f）\n\n"
                        "如有问题请直接回复本邮件。\n— %s"
                        % (updated["title"], updated["tax_no"],
                           updated.get("order_no") or "", updated.get("amount_yuan") or 0,
                           config.product_name()))
                msg = ("已标记开具并通知客户。" if new_status == FAPIAO_ISSUED
                       else "已驳回该申请。")
    status_filter = (request.args.get("status") or "").strip() or None
    rows = []
    for f in metrics_store.list_fapiao_requests(status=status_filter):
        actions = ""
        if f["status"] == FAPIAO_REQUESTED:
            actions = (
                "<form method=post style='display:flex;gap:6px;flex-wrap:wrap'>"
                "<input type=hidden name=id value='" + _h(str(f["id"])) + "'>"
                "<input type=text name=note placeholder='备注/驳回原因'>"
                "<button name=action value=issue type=submit>标记已开具</button>"
                "<button name=action value=reject class=danger type=submit>驳回</button>"
                "</form>")
        rows.append(
            "<tr><td>" + _h(str(f["id"])) + "</td>"
            "<td>" + _h(f["title"]) + "<div class=muted>税号 " + _h(f["tax_no"])
            + "</div></td>"
            "<td>" + _h(f.get("order_no") or "") + "<div class=muted>¥"
            + _h("%.2f" % (f.get("amount_yuan") or 0)) + "</div></td>"
            "<td>" + _h(f.get("account_email") or "") + "<div class=muted>"
            + _h(f.get("email") or "") + "</div></td>"
            "<td>" + _status_chip(f["status"])
            + (("<div class=muted>" + _h(f["note"]) + "</div>") if f["note"] else "")
            + "</td><td class=muted>" + _h(f["created_at"] or "") + "</td>"
            "<td>" + actions + "</td></tr>")
    nav = ("<p class=muted>筛选：<a href=/admin/fapiao>全部</a> · "
           "<a href='/admin/fapiao?status=requested'>待开</a> · "
           "<a href='/admin/fapiao?status=issued'>已开</a> · "
           "<a href='/admin/fapiao?status=rejected'>已驳回</a>"
           " &nbsp;|&nbsp; <a href=/admin/fapiao/export.csv>⬇ 导出 CSV（给记账）</a></p>")
    table = ("<table><thead><tr><th>#</th><th>抬头/税号</th><th>订单</th>"
             "<th>账户/接收邮箱</th><th>状态</th><th>申请时间</th><th>操作</th></tr>"
             "</thead><tbody>"
             + ("".join(rows) or "<tr><td colspan=7 class=muted>暂无发票申请</td></tr>")
             + "</tbody></table>")
    return _admin_page("发票申请 · Fapiao Requests", nav + table, msg, err)


@app.route("/admin/fapiao/export.csv")
def admin_fapiao_csv():
    """CSV of fapiao requests (honouring ?status) — the bookkeeper's handoff."""
    if not _is_admin(_current_customer()):
        return Response("403", status=403, mimetype="text/plain; charset=utf-8")
    status_filter = (request.args.get("status") or "").strip() or None
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "created_at", "status", "account_email", "order_no",
                "amount_yuan", "title", "tax_no", "email", "note"])
    for f in metrics_store.list_fapiao_requests(status=status_filter):
        w.writerow([f["id"], f["created_at"] or "", f["status"],
                    f.get("account_email") or "", f.get("order_no") or "",
                    "%.2f" % (f.get("amount_yuan") or 0), f["title"], f["tax_no"],
                    f.get("email") or "", f.get("note") or ""])
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             "attachment; filename=fapiao_requests.csv"})


@app.route("/admin/reconciliation", methods=["GET", "POST"])
def admin_reconciliation():
    """Founder-only: nightly bill-vs-orders results. POST action=scan re-scans
    BILLS_DIR for the last 7 days' bill files right now (idempotent upserts)."""
    customer = _current_customer()
    if not _is_admin(customer):
        return Response("403 — 仅限管理员访问 (/admin/reconciliation)。",
                        status=403, mimetype="text/plain; charset=utf-8")
    msg, err = None, False
    if request.method == "POST" and (request.form.get("action") or "") == "scan":
        outcomes = reconciliation.run_reconciliation(days=7)
        done = [o for o in outcomes if o["status"] == "ok"]
        msg = "扫描完成：处理 %d 份账单（目录 %s）。" % (len(done),
                                                       reconciliation.bills_dir())
        metrics_store.write_audit(customer["id"], AUDIT_ADMIN, _client_ip(),
                                  detail="reconciliation manual scan")
    rows = []
    for r in metrics_store.list_recon_reports():
        detail = ""
        if r["mismatches"]:
            lines = "\n".join("[%s] %s：%s" % (m.get("kind"), m.get("order_no"),
                                               m.get("detail"))
                              for m in r["mismatches"])
            detail = "<pre>" + _h(lines) + "</pre>"
        state = ('<span class="chip c-paid">一致</span>' if not r["mismatch_count"]
                 else '<span class="chip c-failed">%d 笔差异</span>' % r["mismatch_count"])
        rows.append(
            "<tr><td>" + _h(r["bill_date"]) + "</td><td>" + _h(r["provider"]) + "</td>"
            "<td>" + _h(str(r["bill_rows"])) + "</td><td>" + _h(str(r["matched"]))
            + "</td><td>" + state + detail + "</td>"
            "<td class=muted>" + _h(r["created_at"] or "") + "</td></tr>")
    intro = ("<p class=muted>每晚 03:30（北京时间）自动比对 <code>"
             + _h(reconciliation.bills_dir())
             + "/&lt;provider&gt;-&lt;YYYY-MM-DD&gt;.csv</code> 与系统订单；"
             "发现差异即发管理员告警。</p>"
             "<form method=post><input type=hidden name=action value=scan>"
             "<button type=submit>立即扫描账单目录（近7天）</button></form>")
    table = ("<table><thead><tr><th>账单日</th><th>渠道</th><th>账单笔数</th>"
             "<th>匹配</th><th>结果</th><th>对账时间</th></tr></thead><tbody>"
             + ("".join(rows) or "<tr><td colspan=6 class=muted>暂无对账记录</td></tr>")
             + "</tbody></table>")
    return _admin_page("支付对账 · Reconciliation", intro + table, msg, err)


# ---------------------------------------------------------------------------
# ONBOARDING WIZARD (Phase 10) — first login → first finished report in 5 steps.
#
# Every step has its own endpoint; completing/skipping a step writes ONE
# onboarding_events row (the funnel + the wizard's resumable state both derive
# from that log). Step 4 reuses the EXACT interactive run pipeline (billed,
# breaker- and plan-gated, live feed via /api/status); step 5 creates the
# standing Monday-08:00 weekly job.
# ---------------------------------------------------------------------------
ONBOARDING_FIRST_GOAL = ("生成第一份经营周报：总结各渠道核心指标、环比变化与异常，"
                         "并给出下周建议。")
ONBOARDING_WEEKLY_JOB_NAME = "每周经营周报"
# Monday 08:00 (the scheduler runs Asia/Shanghai). The weekday is NAMED, not
# numeric, on purpose: APScheduler's from_crontab numbers day_of_week 0=Monday
# (NOT the crontab standard 0=Sunday), so the old "0 8 * * 1" silently fired
# TUESDAY mornings — found by the stateless next_run_at (redesign Phase 5/B1).
# "mon" means Monday under both conventions.
ONBOARDING_WEEKLY_CRON = "0 8 * * mon"


def _onboarding_ctx():
    """(customer, company_id) for wizard routes, or (None, None) when logged out."""
    return _brain_company()


def _onboarding_state_payload(customer, company_id):
    """The wizard's resumable state + step-1 prefills + step-2/3 data context."""
    state = metrics_store.onboarding_state(customer["id"])
    profile = metrics_store.get_profile(company_id) if company_id else {}
    state["company_name"] = (profile.get("company_name")
                             or customer.get("company") or "").strip()
    state["industry"] = (profile.get("industry") or "").strip()
    state["kpi_mappings"] = (profile.get("kpi_mappings")
                             if isinstance(profile.get("kpi_mappings"), list) else [])
    latest = metrics_store.latest_upload_dir(company_id) if company_id else None
    state["data_files"] = [os.path.basename(p)
                           for p in metrics_store.list_files(latest)] if latest else []
    state["demo_file_name"] = onboarding.DEMO_FILE_NAME
    return state


@app.route("/api/onboarding/state")
def onboarding_state_route():
    """Where the wizard should resume (or whether to auto-open it at all)."""
    customer, company_id = _onboarding_ctx()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    return jsonify(_onboarding_state_payload(customer, company_id))


@app.route("/api/onboarding/start", methods=["POST"])
def onboarding_start():
    """The wizard opened — record the funnel's denominator event (once)."""
    customer, company_id = _onboarding_ctx()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    state = metrics_store.onboarding_state(customer["id"])
    if not state["started"]:
        metrics_store.record_onboarding_event(customer["id"], 0, ONBOARDING_STARTED)
    return jsonify(_onboarding_state_payload(customer, company_id))


@app.route("/api/onboarding/skip", methods=["POST"])
def onboarding_skip():
    """Skip one step (every step is skippable; skipping step 5 ends the wizard)."""
    customer, company_id = _onboarding_ctx()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    data = request.get_json(silent=True) or {}
    try:
        step = int(data.get("step"))
    except (TypeError, ValueError):
        return jsonify({"error": "无效的步骤。"}), 400
    if not 1 <= step <= ONBOARDING_LAST_STEP:
        return jsonify({"error": "无效的步骤。"}), 400
    metrics_store.record_onboarding_event(customer["id"], step, ONBOARDING_SKIPPED)
    return jsonify(_onboarding_state_payload(customer, company_id))


@app.route("/api/onboarding/company", methods=["POST"])
def onboarding_company():
    """Step 1 公司信息 — name + industry (the industry selects the template pack
    used by every later run, via the company profile)."""
    customer, company_id = _onboarding_ctx()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "公司档案暂不可用。"}), 503
    data = request.get_json(silent=True) or {}
    company_name = (data.get("company_name") or "").strip()
    industry = (data.get("industry") or "").strip()
    if not company_name:
        return jsonify({"error": "请填写公司名称。"}), 400
    metrics_store.merge_profile(company_id, {"company_name": company_name,
                                             "industry": industry})
    metrics_store.record_onboarding_event(customer["id"], 1, ONBOARDING_COMPLETED)
    return jsonify(_onboarding_state_payload(customer, company_id))


def _onboarding_data_response(customer, company_id, file_name, headers):
    out = _onboarding_state_payload(customer, company_id)
    out["file_name"] = file_name
    out["headers"] = headers
    out["kpi_guesses"] = onboarding.guess_kpi_mappings(headers)
    return jsonify(out)


@app.route("/api/onboarding/upload", methods=["POST"])
def onboarding_upload():
    """Step 2 上传数据 — one spreadsheet into a persisted upload batch (the same
    place /api/run + scheduled jobs read), with the Phase 8 upload guards."""
    customer, company_id = _onboarding_ctx()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "公司档案暂不可用。"}), 503
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "请选择一个数据文件（Excel/CSV）。"}), 400
    head = f.stream.read(16)
    f.stream.seek(0, os.SEEK_END)
    size = f.stream.tell()
    f.stream.seek(0)
    if security.looks_executable(head):
        return jsonify({"error": "文件 “%s” 看起来是可执行程序，已拒绝上传。" % f.filename}), 400
    if size > config.MAX_UPLOAD_BYTES_PER_FILE:
        max_mb = config.MAX_UPLOAD_BYTES_PER_FILE // (1024 * 1024)
        return jsonify({"error": "文件超过单个文件 %d MB 的上限。" % max_mb}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    # CJK-safe (the name echoes through the wizard + KPI step + 数据收件箱).
    safe_name = _inbox_safe_filename(f.filename, fallback="数据文件" + (ext or ".csv"))
    batch_dir = metrics_store.new_upload_batch_dir(company_id)
    path = os.path.join(batch_dir, safe_name)
    f.save(path)
    headers = onboarding.read_table_headers(path)
    crypto.encrypt_file_in_place(path)           # at-rest, like every upload
    metrics_store.record_onboarding_event(customer["id"], 2, ONBOARDING_COMPLETED)
    return _onboarding_data_response(customer, company_id, safe_name, headers)


@app.route("/api/onboarding/demo-data", methods=["POST"])
def onboarding_demo_data():
    """Step 2 用示例数据体验 — stage the bundled demo CSV as if it were uploaded."""
    customer, company_id = _onboarding_ctx()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "公司档案暂不可用。"}), 503
    try:
        staged = onboarding.stage_demo_data(company_id)
    except FileNotFoundError:
        return jsonify({"error": "示例数据暂不可用。"}), 503
    metrics_store.record_onboarding_event(customer["id"], 2, ONBOARDING_COMPLETED)
    return _onboarding_data_response(customer, company_id,
                                     staged["file_name"], staged["headers"])


@app.route("/api/onboarding/kpi-confirm", methods=["POST"])
def onboarding_kpi_confirm():
    """Step 3 KPI 列名确认 — save the (possibly edited) guesses as the standing
    KPI mappings in the company brain; every later run maps columns through them."""
    customer, company_id = _onboarding_ctx()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "公司档案暂不可用。"}), 503
    data = request.get_json(silent=True) or {}
    mappings = _clean_kpi_mappings(data.get("kpi_mappings"))
    metrics_store.merge_profile(company_id, {"kpi_mappings": mappings})
    metrics_store.record_onboarding_event(customer["id"], 3, ONBOARDING_COMPLETED)
    state = _onboarding_state_payload(customer, company_id)
    state["saved_mappings"] = len(mappings)
    return jsonify(state)


def _run_wizard_team(memory, customer_id, company_id, run_id, industry_key):
    """The step-4 background runner: the normal team run, then — only on a
    SUCCEEDED run — the step-4 funnel event (the wow moment actually happened)."""
    _run_team(memory, customer_id, company_id, run_id, industry_key)
    try:
        run = metrics_store.get_run(run_id) if run_id else None
        if run and run.get("status") == RUN_SUCCEEDED:
            metrics_store.record_onboarding_event(customer_id, 4,
                                                  ONBOARDING_COMPLETED)
    except Exception:
        pass


@app.route("/api/onboarding/generate", methods=["POST"])
@limiter.limit(config.run_rate_limit, key_func=_run_rate_key)
def onboarding_generate():
    """Step 4 自动生成第一份报告 — run the team over the latest staged data batch
    through the SAME gates + billing as any interactive run. Returns {job_id};
    the wizard polls /api/status/<job_id> for the live agent feed."""
    load_dotenv(override=True)
    customer, company_id = _onboarding_ctx()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "公司档案暂不可用。"}), 503
    if not CUSTOMERS.has_balance(customer):
        return jsonify({"error": "账户余额不足，请先充值。"}), 402
    allowed, reason = platform_guard.check(customer["id"],
                                           customer.get("daily_cap_yuan"))
    if not allowed:
        return jsonify({"error": reason}), 503
    plan_ok, plan_msg = _plan_gate(customer)
    if not plan_ok:
        return jsonify({"error": plan_msg}), 402

    latest = metrics_store.latest_upload_dir(company_id)
    files = metrics_store.list_files(latest) if latest else []
    if not files:
        return jsonify({"error": "还没有数据：请先上传文件或选择「用示例数据体验」。"}), 400

    job_id = "onboard-" + uuid.uuid4().hex[:10]
    memory = SharedMemory(job_id, store_dir=MEMORY_DIR, goal=ONBOARDING_FIRST_GOAL,
                          output_dir=os.path.join(OUTPUT_DIR, job_id))
    JOBS[job_id] = memory
    for path in files:
        try:
            # Staged batches are encrypted at rest — parse a plaintext temp copy.
            with crypto.materialize_plaintext(path) as plain:
                doc = parse_file(plain)
            doc["name"] = os.path.basename(path)
            memory.add_document(doc)
            memory.log("system", "Received file: %s (%s chars)."
                       % (doc["name"], format(doc["chars"], ",")), type="info")
        except Exception:
            pass  # one unreadable file must not abort the first-report run
    memory.log("system", "新手引导：开始生成第一份报告。", type="info")

    try:
        run_id = metrics_store.create_run(company_id, ONBOARDING_FIRST_GOAL)
    except Exception:
        run_id = None
    industry_key = _resolve_industry(None, company_id)

    thread = threading.Thread(
        target=_run_wizard_team,
        args=(memory, customer["id"], company_id, run_id, industry_key), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/onboarding/schedule", methods=["POST"])
def onboarding_schedule():
    """Step 5 设置每周定时 — ONE click creates (or re-points) the standing
    Monday-08:00 weekly job with the chosen delivery channels. Plan-gated per
    D3: a non-企业版 account gets the structured 402 the wizard renders as the
    upgrade explainer (it used to silently create the job)."""
    customer, company_id = _onboarding_ctx()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "公司档案暂不可用。"}), 503
    allowed, refusal = _scheduling_plan_gate(customer)
    if not allowed:
        return refusal
    data = request.get_json(silent=True) or {}
    channels = _clean_channels(data.get("channels")) or ["email"]

    # Idempotent: a second click updates the existing wizard job, never
    # duplicates. Matched by NAME ONLY — matching on cron too meant a legacy
    # row (pre Monday-cron fix, "0 8 * * 1") slipped past the dedupe and one
    # wizard click created a SECOND weekly job (double weekly billing). The
    # update also re-points the cron, so any legacy Tuesday row is healed here
    # as well as by migration 0005.
    existing = [j for j in metrics_store.list_scheduled_jobs(company_id)
                if j["name_cn"] == ONBOARDING_WEEKLY_JOB_NAME]
    if existing:
        job_id = existing[0]["id"]
        metrics_store.update_scheduled_job(company_id, job_id,
                                           cron_expr=ONBOARDING_WEEKLY_CRON,
                                           delivery_channels=channels, active=True)
    else:
        job_id = metrics_store.create_scheduled_job(
            company_id, ONBOARDING_WEEKLY_JOB_NAME, ONBOARDING_WEEKLY_CRON,
            goal_template="生成上周经营周报",
            source_mode=SOURCE_LATEST_UPLOAD,
            delivery_channels=channels,
            active=True,
        )
    SCHEDULER.reload()
    metrics_store.record_onboarding_event(customer["id"], 5, ONBOARDING_COMPLETED)
    state = _onboarding_state_payload(customer, company_id)
    state["job_id"] = job_id
    state["channels"] = channels
    return jsonify(state)


# ---------------------------------------------------------------------------
# HELP CENTER, CHANGELOG & SUPPORT (Phase 10)
# ---------------------------------------------------------------------------
_PUBLIC_CSS = (
    # Explicit light theme: these pages must look the same regardless of the
    # browser's prefers-color-scheme (no UA dark-mode surprises).
    "html{background:#fff;color-scheme:light}"
    "body{font-family:system-ui,'PingFang SC','Microsoft YaHei',Arial,sans-serif;"
    "background:#fff;"
    "max-width:780px;margin:0 auto;padding:28px 18px 90px;color:#1f2937;line-height:1.75}"
    "h1{font-size:24px;margin:8px 0 4px}h2{font-size:18px;margin:26px 0 8px}"
    "h3{font-size:15px;margin:18px 0 6px}"
    "p{margin:8px 0}ul,ol{margin:8px 0;padding-left:22px}li{margin:4px 0}"
    "code{background:#f3f4f6;border-radius:5px;padding:1px 6px;font-size:13px}"
    "a{color:#1d6f5c}hr{border:0;border-top:1px solid #e5e7eb;margin:22px 0}"
    "blockquote{border-left:3px solid #1D9E75;margin:10px 0;padding:2px 12px;color:#4b5563}"
    ".topnav{display:flex;gap:10px;flex-wrap:wrap;align-items:center;"
    "border-bottom:1px solid #e5e7eb;padding-bottom:12px;margin-bottom:10px;font-size:14px}"
    ".topnav a{text-decoration:none;padding:5px 10px;border-radius:8px;border:1px solid #e5e7eb}"
    ".topnav a.active{background:#1D9E75;border-color:#1D9E75;color:#fff}"
    ".topnav .home{margin-left:auto}"
    ".support-fab{position:fixed;right:22px;bottom:22px;background:#1D9E75;color:#fff;"
    "border:0;border-radius:999px;padding:12px 18px;font-size:14px;cursor:pointer;"
    "box-shadow:0 6px 18px rgba(0,0,0,.18)}"
    ".support-pop{position:fixed;right:22px;bottom:74px;width:300px;background:#fff;"
    "border:1px solid #e5e7eb;border-radius:14px;padding:16px;"
    "box-shadow:0 12px 32px rgba(0,0,0,.16);display:none}"
    ".support-pop.open{display:block}"
    ".support-pop h3{margin:0 0 8px;font-size:15px}"
    ".support-pop textarea{width:100%;box-sizing:border-box;height:74px;border:1px solid #d1d5db;"
    "border-radius:8px;padding:7px;font-size:13px;resize:vertical}"
    ".support-pop input{width:100%;box-sizing:border-box;border:1px solid #d1d5db;"
    "border-radius:8px;padding:7px;font-size:13px;margin-top:6px}"
    ".support-pop button{margin-top:8px;background:#1f2937;color:#fff;border:0;"
    "padding:8px 12px;border-radius:8px;font-size:13px;cursor:pointer}"
    ".support-qr{width:128px;height:128px;border:1px dashed #d1d5db;border-radius:10px;"
    "display:flex;align-items:center;justify-content:center;color:#9ca3af;"
    "font-size:12px;text-align:center;margin:6px 0}"
    ".support-ok{color:#047857;font-size:13px;margin-top:6px}"
)


def _support_widget_html():
    """The floating 联系支持 button + popup (企业微信 QR + mailto + a message form
    that logs to /api/support). Self-contained; embedded on every public page."""
    qr_url = config.support_wechat_qr_url()
    qr = ('<img class="support-qr" src="%s" alt="企业微信群二维码" '
          'style="object-fit:contain">' % _h(qr_url)) if qr_url else \
         '<div class="support-qr">企业微信群二维码<br>(待配置 SUPPORT_WECHAT_QR_URL)</div>'
    email = config.support_email()
    mailto = ('<p style="font-size:13px;margin:6px 0">邮件：<a href="mailto:%s">%s</a></p>'
              % (_h(email), _h(email))) if email else ""
    return (
        '<button class="support-fab" id="supportFab" type="button">💬 联系支持</button>'
        '<div class="support-pop" id="supportPop">'
        '<h3>联系支持</h3>'
        '<p style="font-size:13px;margin:4px 0">扫码加入企业微信支持群：</p>'
        + qr + mailto +
        '<form id="supportForm">'
        '<textarea id="supportMsg" placeholder="或者在这里留言，我们会尽快回复…"></textarea>'
        '<input id="supportEmail" type="email" placeholder="你的邮箱（选填，便于回复）">'
        '<button type="submit">发送留言</button>'
        '<div class="support-ok" id="supportOk" hidden>已收到，我们会尽快联系你。</div>'
        '</form></div>'
        "<script>(function(){"
        "var fab=document.getElementById('supportFab'),pop=document.getElementById('supportPop');"
        "fab.addEventListener('click',function(){pop.classList.toggle('open');});"
        "document.getElementById('supportForm').addEventListener('submit',function(e){"
        "e.preventDefault();"
        "var msg=document.getElementById('supportMsg').value.trim();if(!msg)return;"
        "fetch('/api/support',{method:'POST',headers:{'Content-Type':'application/json'},"
        "body:JSON.stringify({message:msg,email:document.getElementById('supportEmail').value,"
        "page:location.pathname})}).then(function(){"
        "document.getElementById('supportOk').hidden=false;"
        "document.getElementById('supportMsg').value='';});});"
        "})();</script>"
    )


def _public_page(title, body_html, active=None, extra_nav=""):
    """A standalone server-rendered page (help docs, changelog) with the shared
    top nav + the floating 联系支持 widget."""
    nav_links = "".join(
        '<a href="/help/%s"%s>%s</a>'
        % (d["slug"], ' class="active"' if d["slug"] == active else "", _h(d["title"]))
        for d in helpdocs.list_help_docs())
    nav = ('<nav class="topnav">' + nav_links
           + '<a href="/changelog"%s>更新日志</a>'
             % (' class="active"' if active == "changelog" else "")
           + extra_nav
           + '<a class="home" href="/">← 返回控制台</a></nav>')
    return Response(
        "<!doctype html><html lang=zh><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width, initial-scale=1'>"
        "<title>" + _h(title) + " · " + _h(config.product_name()) + "</title>"
        "<style>" + _PUBLIC_CSS + "</style></head><body>"
        + nav + body_html + _support_widget_html() + "</body></html>",
        mimetype="text/html; charset=utf-8")


@app.route("/help")
def help_index():
    """The help center's landing page → the 快速上手 doc."""
    return redirect("/help/quickstart")


@app.route("/help/<slug>")
def help_doc(slug):
    """One markdown help doc, rendered server-side. Unknown slug → 404."""
    md = helpdocs.load_help_doc(slug)
    if md is None:
        return Response("404 — 没有这篇帮助文档。", status=404,
                        mimetype="text/plain; charset=utf-8")
    title = helpdocs.help_doc_title(slug) or "帮助"
    return _public_page(title, helpdocs.render_markdown(md), active=slug)


@app.route("/changelog")
def changelog_page():
    """CHANGELOG.md rendered for users. Viewing it CLEARS the 新功能 badge for
    the logged-in account (that is the acceptance behaviour)."""
    md = helpdocs.load_changelog()
    customer = _current_customer()
    if customer:
        latest = helpdocs.latest_changelog_id(md)
        if latest:
            metrics_store.set_changelog_seen(customer["id"], latest)
    return _public_page("更新日志", helpdocs.render_markdown(md), active="changelog")


@app.route("/api/changelog/state")
def changelog_state():
    """{latest, seen, has_new} — drives the 新功能 badge in the SPA menu."""
    latest = helpdocs.latest_changelog_id()
    customer = _current_customer()
    seen = metrics_store.get_changelog_seen(customer["id"]) if customer else None
    has_new = bool(customer and latest and seen != latest)
    return jsonify({"latest": latest, "seen": seen, "has_new": has_new})


@app.route("/api/support", methods=["POST"])
@limiter.limit(config.auth_rate_limit)
def support_request():
    """Log one 联系支持 message (works logged-in or out) + best-effort admin
    alert. The message is stored verbatim and ALWAYS escaped on render."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "请填写留言内容。"}), 400
    email = (data.get("email") or "").strip()
    if email and not _valid_email(email):
        return jsonify({"error": "请填写有效的邮箱。"}), 400
    customer = _current_customer()
    rid = metrics_store.record_support_request(
        (customer or {}).get("id"), email or (customer or {}).get("email"),
        message, page=(data.get("page") or "").strip() or None)
    if not rid:
        return jsonify({"error": "留言未能保存，请稍后再试。"}), 400
    try:
        who = (customer or {}).get("email") or email or "匿名访客"
        alerts.send_admin_alert(
            "新的支持请求 #%s" % rid,
            "来自：%s\n页面：%s\n\n%s" % (who, data.get("page") or "-", message[:500]))
    except Exception:
        pass  # the DB row is the system of record; the alert is best-effort
    return jsonify({"ok": True, "id": rid})


# ---------------------------------------------------------------------------
# ADMIN OPS DASHBOARD (Phase 10) — the business in one screen.
# ---------------------------------------------------------------------------
def _sparkline_svg(values, width=300, height=46, stroke="#1D9E75"):
    """A dependency-free inline-SVG sparkline. `values` are non-negative numbers;
    an all-zero series renders as a flat baseline."""
    vals = [max(0.0, float(v or 0)) for v in (values or [0])]
    if len(vals) == 1:
        vals = vals * 2
    mx = max(vals) or 1.0
    step = width / float(len(vals) - 1)
    pts = " ".join("%.1f,%.1f" % (i * step, height - 4 - (height - 12) * (v / mx))
                   for i, v in enumerate(vals))
    return ('<svg width="%d" height="%d" viewBox="0 0 %d %d" role="img">'
            '<polyline fill="none" stroke="%s" stroke-width="2" '
            'stroke-linejoin="round" stroke-linecap="round" points="%s"/></svg>'
            % (width, height, width, height, stroke, pts))


def _kpi_card(label, value, sub=""):
    return ('<div class="kpi"><div class="kpi-v">%s</div><div class="kpi-l">%s</div>'
            '%s</div>' % (value, _h(label),
                          ('<div class="kpi-s">%s</div>' % _h(sub)) if sub else ""))


@app.route("/admin")
def admin_dashboard():
    """Founder-only ops dashboard: signups/activation/retention, MRR & churn,
    per-account economics (cost > 60% of revenue flagged), onboarding funnel,
    recent support requests. Plain tables + inline-SVG sparklines — no chart libs.
    Non-admins (logged in or not) get 403."""
    if not _is_admin(_current_customer()):
        return Response("403 — 仅限管理员访问 (/admin)。",
                        status=403, mimetype="text/plain; charset=utf-8")

    spd = admin_queries.signups_per_day(days=30)
    act = admin_queries.activation_rate(days=30)
    mrr = admin_queries.mrr()
    churn = admin_queries.churn_this_month()
    cohorts = admin_queries.retention_cohorts(weeks=8)
    econ = admin_queries.account_economics(days=90)
    funnel = admin_queries.onboarding_funnel()
    support = metrics_store.list_support_requests(limit=10)

    # --- KPI cards -----------------------------------------------------------
    cards = "<div class=kpis>" + "".join([
        _kpi_card("MRR（活跃订阅月费合计）", "¥%s" % _h("%.0f" % mrr["total_yuan"]),
                  " + ".join("%s×%d" % (c, p["count"])
                             for c, p in sorted(mrr["by_plan"].items())) or "暂无订阅"),
        _kpi_card("本月流失率", "%s%%" % _h("%.1f" % (churn["rate"] * 100)),
                  "%d 取消 / 月初 %d 活跃" % (churn["canceled"],
                                              churn["active_at_month_start"])),
        _kpi_card("激活率（7天内出首份报告）", "%s%%" % _h("%.1f" % (act["rate"] * 100)),
                  "%d / %d（近30天注册）" % (act["activated"], act["signups"])),
        _kpi_card("近30天注册", _h(str(sum(r["count"] for r in spd))), "见下方趋势"),
    ]) + "</div>"

    # --- Signups/day sparkline ------------------------------------------------
    first_day, last_day = spd[0]["day"], spd[-1]["day"]
    signup_html = ("<h2>每日注册（近30天）</h2><div class=panel>"
                   + _sparkline_svg([r["count"] for r in spd])
                   + '<div class=muted>%s → %s · 共 %d 人</div></div>'
                   % (_h(first_day), _h(last_day), sum(r["count"] for r in spd)))

    # --- Onboarding funnel -----------------------------------------------------
    frows = []
    for srow in funnel["steps"]:
        frows.append(
            "<tr><td>第 %d 步 · %s</td><td>%d</td><td>"
            '<div class=bar><div class=bar-in style="width:%.1f%%"></div></div>'
            "</td><td>%s%%</td></tr>"
            % (srow["step"], _h(srow["label"]), srow["completed"],
               min(100.0, srow["pct"]), _h("%.1f" % srow["pct"])))
    funnel_html = ("<h2>新手引导漏斗</h2><div class=panel>"
                   "<p class=muted>进入引导的账号：%d（完成率按其计算）</p>"
                   "<table><thead><tr><th>步骤</th><th>完成数</th><th></th>"
                   "<th>完成率</th></tr></thead><tbody>%s</tbody></table></div>"
                   % (funnel["accounts_started"], "".join(frows)))

    # --- Retention cohorts ------------------------------------------------------
    weeks_n = len(cohorts["week_starts"])
    chead = "".join("<th>W%d</th>" % k for k in range(weeks_n))
    crows = []
    for c in cohorts["cohorts"]:
        cells = []
        for v in c["active"]:
            if v is None:
                cells.append('<td class=muted>—</td>')
            else:
                pct = (v / c["signups"]) if c["signups"] else 0.0
                cells.append('<td style="background:rgba(29,158,117,%.2f)">%d</td>'
                             % (min(0.85, pct * 0.85), v))
        crows.append("<tr><td>%s</td><td>%d</td>%s</tr>"
                     % (_h(c["week"]), c["signups"], "".join(cells)))
    cohort_html = ("<h2>周留存（注册周 × 活跃周）</h2><div class=panel>"
                   "<p class=muted>单元格 = 该注册周的账号里，第 Wk 周仍发起过运行的人数。</p>"
                   "<table><thead><tr><th>注册周(一)</th><th>注册数</th>%s</tr></thead>"
                   "<tbody>%s</tbody></table></div>" % (chead, "".join(crows)))

    # --- Per-account economics ---------------------------------------------------
    erows = []
    for r in econ[:50]:
        flag = ('<span class="chip c-failed">成本&gt;60%收入</span>'
                if r["flagged"] else "")
        ratio = ("%.0f%%" % (r["ratio"] * 100)) if r["ratio"] is not None else "—"
        erows.append(
            "<tr><td>%s<div class=muted>%s</div></td><td>¥%s</td><td>¥%s</td>"
            "<td>%s</td><td>%s</td></tr>"
            % (_h(r["email"] or r["account_id"]), _h(r["account_id"]),
               _h("%.2f" % r["revenue_yuan"]), _h("%.2f" % r["cost_yuan"]),
               _h(ratio), flag))
    econ_html = ("<h2>单账户经济模型（近90天）</h2><div class=panel>"
                 "<p class=muted>收入 = 已支付订单净额；成本 = 该账户运行的上游模型 ¥ 消耗"
                 "（runs.total_cost_yuan）。成本占收入 &gt;60%% 标红。</p>"
                 "<table><thead><tr><th>账户</th><th>收入</th><th>模型成本</th>"
                 "<th>成本占比</th><th></th></tr></thead><tbody>%s</tbody></table></div>"
                 % ("".join(erows)
                    or "<tr><td colspan=5 class=muted>近90天暂无收入或消耗</td></tr>"))

    # --- Recent support requests --------------------------------------------------
    srows = []
    for r in support:
        srows.append("<tr><td class=muted>%s</td><td>%s</td><td>%s</td></tr>"
                     % (_h(r["created_at"] or ""),
                        _h(r["account_email"] or r["email"] or "匿名"),
                        _h(r["message"][:120])))
    support_html = ("<h2>最近支持请求</h2><div class=panel>"
                    "<table><thead><tr><th>时间</th><th>来自</th><th>留言</th></tr>"
                    "</thead><tbody>%s</tbody></table></div>"
                    % ("".join(srows)
                       or "<tr><td colspan=3 class=muted>暂无支持请求</td></tr>"))

    extra_css = ("<style>"
                 ".kpis{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0}"
                 ".kpi{flex:1;min-width:180px;border:1px solid #e5e7eb;border-radius:12px;"
                 "padding:12px 14px;background:#fff}"
                 ".kpi-v{font-size:24px;font-weight:700}"
                 ".kpi-l{color:#6b7280;font-size:13px;margin-top:2px}"
                 ".kpi-s{color:#9ca3af;font-size:12px;margin-top:2px}"
                 ".panel{border:1px solid #e5e7eb;border-radius:12px;padding:12px 14px;"
                 "margin:8px 0 18px;background:#fff}"
                 "h2{font-size:16px;margin:18px 0 4px}"
                 ".bar{background:#f3f4f6;border-radius:999px;height:10px;width:100%}"
                 ".bar-in{background:#1D9E75;border-radius:999px;height:10px}"
                 "</style>")
    return _admin_page("运营看板 · Ops Dashboard",
                       extra_css + cards + signup_html + funnel_html
                       + cohort_html + econ_html + support_html)


def _render_admin_feedback_html(rows, vote):
    """Render the (server-side, founder-only) feedback table as a small standalone
    HTML page. Every cell is HTML-escaped — feedback comments are user input."""
    def nav(label, v):
        active = " active" if v == vote else ""
        href = "/admin/feedback" + ("?vote=%s" % v if v else "")
        return '<a class="nav%s" href="%s">%s</a>' % (active, href, label)

    def vote_chip(v):
        return '<span class="up">👍 好评</span>' if v == "up" else '<span class="down">👎 差评</span>'

    body_rows = []
    for r in rows:
        body_rows.append(
            "<tr><td>%s</td><td class=ts>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td class=cm>%s</td></tr>" % (
                r["id"], _h(r["created_at"] or ""), _h(r["account_email"] or "—"),
                _h(str(r["run_id"] or "—")), _h(r["section_title"] or "—"),
                vote_chip(r["vote"]), _h(r["comment"] or ""),
            ))
    table_body = "\n".join(body_rows) or '<tr><td colspan="7" class="muted">暂无反馈</td></tr>'
    csv_href = "/admin/feedback/export.csv" + ("?vote=%s" % vote if vote else "")
    # The CSS carries literal "%" (e.g. width:100%), so keep the static head as a
    # plain literal and only %-format the small dynamic fragments — never the CSS.
    head = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>反馈看板 · Founder Dashboard</title>
<style>
 body{font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a1a1a;margin:0;background:#f6f8f7}
 .wrap{max-width:1100px;margin:0 auto;padding:28px 20px}
 h1{font-size:20px;margin:0 0 4px}
 .sub{color:#666;margin:0 0 18px}
 .bar{display:flex;gap:8px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
 .nav{padding:6px 12px;border-radius:8px;background:#fff;border:1px solid #d8e0dc;color:#1a6;text-decoration:none}
 .nav.active{background:#1D9E75;border-color:#1D9E75;color:#fff}
 .csv{margin-left:auto;padding:6px 12px;border-radius:8px;background:#fff;border:1px solid #d8e0dc;color:#1a6;text-decoration:none}
 table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e9e6;border-radius:10px;overflow:hidden}
 th,td{padding:9px 11px;text-align:left;border-bottom:1px solid #eef2f0;vertical-align:top}
 th{background:#1D9E75;color:#fff;font-weight:600;font-size:13px}
 td.ts{color:#888;white-space:nowrap}
 td.cm{max-width:380px;white-space:pre-wrap;word-break:break-word}
 .up{color:#1D9E75;font-weight:600}.down{color:#c0392b;font-weight:600}
 .muted{color:#999;text-align:center;padding:24px}
 .count{color:#666;font-size:13px}
</style></head><body><div class="wrap">
<h1>反馈看板</h1>
<p class="sub">每个数字可溯源 · 全部公司的报告反馈（Phase 6 质量数据集）</p>
<div class="bar">
"""
    bar = (nav("全部", None) + " " + nav("👍 好评", "up") + " " + nav("👎 差评", "down") +
           '\n <span class="count">共 %d 条</span>\n'
           ' <a class="csv" href="%s">⬇ 导出 CSV</a>\n</div>\n' % (len(rows), _h(csv_href)))
    table_html = (
        "<table><thead><tr><th>#</th><th>时间</th><th>账号</th><th>Run</th>"
        "<th>章节</th><th>评价</th><th>留言</th></tr></thead>\n"
        "<tbody>" + table_body + "</tbody></table>\n</div></body></html>")
    return head + bar + table_html


# ---- Onboarding import (导入历史数据) -------------------------------------
class _ImportUploadRejected(ValueError):
    """A friendly, customer-facing refusal raised by _parse_import_files."""


def _parse_import_files(files):
    """Save & parse up to IMPORT_MAX_FILES uploaded spreadsheets into [(name, text)].
    Skips empties; truncates the list at the cap. Reused by estimate + start.

    Same upload guards as /api/run (the import path used to have NEITHER): each
    file is size-capped and magic-byte-checked BEFORE anything is written, and
    names go through the CJK-safe sanitizer — they echo back in the estimate
    and progress UI, so 六月销售明细.csv must not degrade to "csv". A rejected
    file raises _ImportUploadRejected with the friendly message."""
    checked = []
    max_mb = config.MAX_UPLOAD_BYTES_PER_FILE // (1024 * 1024)
    for f in files[:IMPORT_MAX_FILES]:
        if not f or not f.filename:
            continue
        head = f.stream.read(16)
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        if security.looks_executable(head):
            raise _ImportUploadRejected(f"文件 “{f.filename}” 看起来是可执行程序，已拒绝上传。")
        if size > config.MAX_UPLOAD_BYTES_PER_FILE:
            raise _ImportUploadRejected(f"文件 “{f.filename}” 超过单个文件 {max_mb} MB 的上限。")
        checked.append(f)

    # The on-disk copies exist only so parse_file can read them — the returned
    # (name, text) pairs carry everything estimate/import use afterwards. Always
    # delete the temp dir before returning: estimate calls used to leave up to
    # 12×25MB behind PER CALL (a logged-in disk-DoS, Phase 7 A2).
    import_dir = os.path.join(UPLOAD_DIR, "import_" + uuid.uuid4().hex[:8])
    os.makedirs(import_dir, exist_ok=True)
    out = []
    try:
        for f in checked:
            safe_name = _inbox_safe_filename(f.filename)
            path = os.path.join(import_dir, safe_name)
            f.save(path)
            doc = parse_file(path)
            out.append((doc.get("name") or safe_name, doc.get("text") or ""))
    finally:
        shutil.rmtree(import_dir, ignore_errors=True)
    return out


@app.route("/api/company-brain/import/estimate", methods=["POST"])
@limiter.limit(config.upload_rate_limit, key_func=_run_rate_key)
def brain_import_estimate():
    """Size up an import BEFORE running it. Customers see file count + a rough
    duration — the ¥ figures (internal COGS) are founder-only: raw model cost
    is machinery and never reaches a customer screen (plan §2.1/.5)."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    try:
        parsed = _parse_import_files(request.files.getlist("files"))
    except _ImportUploadRejected as e:
        return jsonify({"error": str(e)}), 400
    if not parsed:
        return jsonify({"error": "请上传至少一个文件。"}), 400
    total, items = 0.0, []
    for name, text in parsed:
        in_tok = max(1, len(text) // 4)
        c = config.cost_cny(config.HAIKU, in_tok, IMPORT_EST_OUTPUT_TOKENS)
        total += c
        items.append({"name": name, "chars": len(text), "cost": round(c, 4)})
    out = {"file_count": len(parsed),
           "estimated_seconds": IMPORT_EST_SECONDS_PER_FILE * len(parsed),
           "estimated_cost": round(total, 4), "files": items}
    if not _is_admin(customer):
        out.pop("estimated_cost")
        for item in out["files"]:
            item.pop("cost")
    return jsonify(out)


@app.route("/api/company-brain/import", methods=["POST"])
@limiter.limit(config.upload_rate_limit, key_func=_run_rate_key)
def brain_import_start():
    """Kick off the back-fill in a background thread; returns a job_id to poll."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    if not company_id:
        return jsonify({"error": "公司大脑暂不可用。"}), 503
    if not CUSTOMERS.has_balance(customer):
        return jsonify({"error": "账户余额不足，请先充值。"}), 402
    try:
        parsed = _parse_import_files(request.files.getlist("files"))
    except _ImportUploadRejected as e:
        return jsonify({"error": str(e)}), 400
    if not parsed:
        return jsonify({"error": "请上传至少一个文件。"}), 400
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "company_id": company_id,    # ownership gate for the progress poll
        "status": "running",          # running | done | stopped_budget
        "total": len(parsed),
        "done": 0,
        "cost": 0.0,
        "files": [{"name": name, "status": "pending", "metrics": 0}
                  for name, _ in parsed],
    }
    IMPORT_JOBS[job_id] = job
    thread = threading.Thread(
        target=_run_import_job,
        args=(job, customer["id"], company_id, parsed), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/company-brain/import/<job_id>")
def brain_import_status(job_id):
    """Progress poll. Login + ownership gated (jobs are per-company), and the
    running ¥ tally is founder-only — customers see counts, never model cost.
    IMPORT_JOBS is in-memory, so after a server restart polls get a plain 404
    (the frontend treats that as "job gone", not an error loop)."""
    customer, company_id = _brain_company()
    if not customer:
        return jsonify({"error": "请先登录。"}), 401
    job = IMPORT_JOBS.get(job_id)
    if job is None or job.get("company_id") != company_id:
        # Same 404 for "never existed" and "not yours" — no existence oracle.
        return jsonify({"error": "未知的导入任务。"}), 404
    out = dict(job)
    out.pop("company_id", None)  # internal bookkeeping, not payload
    if not _is_admin(customer):
        out.pop("cost", None)
    return jsonify(out)


def _run_import_job(job, customer_id, company_id, files, llm=None):
    """Back-fill worker: for each parsed (name, text), run ONLY the cheap
    metric-extraction path (Haiku + extract_metrics_to_db) and store the numbers in
    the company brain — NOT the full report pipeline. Every model call is billed to
    the account through the SAME path as a normal run, and the job stops cleanly the
    moment the balance is spent (BudgetExceededError). `llm` is an injection seam for
    tests; production builds the real wired router."""
    base_bill = _make_biller(customer_id)

    def biller(model, input_tokens, output_tokens):
        # Tally ¥ for the live per-job progress display, then bill for real.
        try:
            job["cost"] += config.cost_cny(model, input_tokens, output_tokens)
        except Exception:
            pass
        if base_bill:
            base_bill(model, input_tokens, output_tokens)  # may raise BudgetExceededError

    router = llm or ModelRouter(token_manager=TOKENS, cost_listener=biller)

    try:
        # kind="import": back-fills never eat the plan's report quota and never
        # appear in 历史报告 (Phase 6 B1).
        run_id = metrics_store.create_run(company_id, "历史数据导入 (historical import)",
                                          kind=RUN_KIND_IMPORT)
    except Exception:
        run_id = None

    final_status = RUN_SUCCEEDED
    for i, (name, text) in enumerate(files):
        fj = job["files"][i] if i < len(job["files"]) else None
        if fj:
            fj["status"] = "working"
        try:
            n = metrics_store.extract_metrics_to_db(text, company_id, run_id, llm=router)
            if fj:
                fj["status"] = "done"
                fj["metrics"] = n
        except BudgetExceededError:
            if fj:
                fj["status"] = "stopped_budget"
            job["status"] = "stopped_budget"
            final_status = RUN_STOPPED_BUDGET
            break
        except Exception as e:
            if fj:
                fj["status"] = "error"
                fj["error"] = str(e)
        job["done"] += 1

    try:
        if run_id:
            metrics_store.finish_run(run_id, final_status,
                                     total_cost_yuan=round(job["cost"], 4))
    except Exception:
        pass

    if job["status"] == "running":
        job["status"] = "done"


@app.route("/v1/models")
def gateway_models():
    """OpenAI-compatible model list — only the models this key may use."""
    customer = CUSTOMERS.verify_key(_bearer_key())
    if not customer:
        return _gateway_error("Invalid API key.", 401, "invalid_api_key")
    now = int(time.time())
    data = [{"id": m, "object": "model", "created": now,
             "owned_by": config.provider_for(m)} for m in customer.get("models", [])]
    return jsonify({"object": "list", "data": data})


@app.route("/v1/chat/completions", methods=["POST"])
def gateway_chat_completions():
    """THE GATEWAY — an OpenAI-compatible endpoint guarded by the 3 layers."""
    load_dotenv(override=True)  # pick up newly-added provider keys without a restart

    # LAYER 1 — AUTH: the request must carry a valid customer key.
    customer = CUSTOMERS.verify_key(_bearer_key())
    if not customer:
        return _gateway_error(
            "Invalid API key. Pass your tng-sk-… key as 'Authorization: Bearer <key>'.",
            401, "invalid_api_key")

    # Phase 8 — per-key requests-per-minute cap (this account's accounts.rpm_limit,
    # default config.DEFAULT_KEY_RPM). Throttles a hot loop on ONE customer key
    # without penalising everyone behind a shared IP.
    rpm = customer.get("rpm_limit") or config.DEFAULT_KEY_RPM
    if not _rpm_check(customer["id"], rpm):
        return _gateway_error(
            f"Rate limit exceeded — at most {rpm} requests per minute on your key.",
            429, "rate_limit_exceeded")

    # Phase 8 — platform spend circuit breaker (+ this account's daily ¥ cap). When
    # the platform is over its rolling upstream-spend cap, refuse new calls with the
    # standard "系统繁忙" message until an admin resets the breaker.
    allowed, reason = platform_guard.check(customer["id"], customer.get("daily_cap_yuan"))
    if not allowed:
        return _gateway_error(reason, 503, "platform_busy")

    data = request.get_json(silent=True) or {}
    model = (data.get("model") or "").strip()
    messages = data.get("messages")
    if not model or not isinstance(messages, list) or not messages:
        return _gateway_error("Request must include 'model' and a non-empty 'messages' array.",
                              400, "invalid_request_error")
    if not config.get_model(model):
        return _gateway_error(f"Unknown model '{model}'.", 404, "model_not_found")
    if not config.is_text_model(model):
        kind = config.kind_label(config.model_kind(model))
        return _gateway_error(
            f"'{model}' is a {kind} model; this chat endpoint serves text models only.",
            400, "unsupported_model")

    # LAYER 3 — ACCESS CONTROL: only the models this customer bought.
    if not CUSTOMERS.can_use(customer, model):
        allowed = ", ".join(customer.get("models") or []) or "(none)"
        return _gateway_error(
            f"Your key doesn't include access to '{model}'. Models on your plan: {allowed}.",
            403, "model_access_denied")

    # LAYER 2 — BILLING (pre-check): refuse if the balance is already spent.
    if not CUSTOMERS.has_balance(customer):
        return _gateway_error("Your account balance is exhausted. Top up to keep going.",
                              402, "insufficient_balance")

    try:
        max_tokens = int(data.get("max_tokens") or 1024)
    except (TypeError, ValueError):
        max_tokens = 1024

    # Route to the real provider (Claude natively; others via the adapter).
    try:
        result = providers.gateway_chat(model, messages, max_tokens=max_tokens)
    except providers.ProviderKeyMissing as e:
        return _gateway_error(str(e), 503, "provider_key_missing")
    except providers.LLMError as e:
        return _gateway_error(str(e), 502, "upstream_error")

    # LAYER 2 — BILLING (charge): record real usage and decrement the balance.
    billed = CUSTOMERS.record_usage(customer["id"], model,
                                    result["input_tokens"], result["output_tokens"])
    return jsonify({
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": result["text"]}}],
        "usage": {"prompt_tokens": result["input_tokens"],
                  "completion_tokens": result["output_tokens"],
                  "total_tokens": result["input_tokens"] + result["output_tokens"]},
        # Handy extras (ignored by standard OpenAI clients): live cost + balance.
        "x_tangent": {"cost": round(billed["cost"], 6), "balance": billed["balance"],
                      "provider": result["provider"]},
    })


# ---------------------------------------------------------------------------
# MODEL FUSION — run one prompt across several models, then fuse to the best answer
# ---------------------------------------------------------------------------
# A "council" of models each answer the same prompt; a fuser model then cross-
# checks them against one another (catching where one is wrong and another right)
# and returns one clean, reconciled answer. This is a showcase feature like the
# Chat bot — it uses the server's own provider keys, not a customer gateway key.
@app.route("/api/fusion", methods=["POST"])
def fusion():
    """Run `prompt` across `models` concurrently and fuse them into one answer.

    Body: {"prompt": "...", "models": ["id", ...], "fuser": "auto" | "<model id>"}.
    Each model runs through the same provider router the gateway uses (Claude
    natively; others via the OpenAI-compatible adapter). A model whose provider
    key is missing comes back as a clear per-model note instead of failing the
    whole request, so a Claude-only setup still works out of the box."""
    load_dotenv(override=True)  # pick up any provider keys added since startup

    # Like the agent team + the gateway, Fusion is billed to the logged-in account.
    customer = _current_customer()
    if not customer:
        return jsonify({"error": "Please log in first — Fusion runs are billed to "
                                 "your account balance."}), 401
    if not CUSTOMERS.has_balance(customer):
        return jsonify({"error": "Your account balance is spent. Top up to use Fusion."}), 402

    # Phase 8 — platform spend circuit breaker (+ this account's daily ¥ cap).
    allowed, reason = platform_guard.check(customer["id"], customer.get("daily_cap_yuan"))
    if not allowed:
        return jsonify({"error": reason}), 503

    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    models = data.get("models")
    fuser = (data.get("fuser") or "auto").strip()
    if not prompt:
        return jsonify({"error": "Type a question or task to run."}), 400
    if not isinstance(models, list) or not models:
        return jsonify({"error": "Pick at least one model to run."}), 400

    # Only text models run through the chat path; drop non-text models + duplicates.
    picked, seen = [], set()
    for m in models:
        if m in seen:
            continue
        if config.get_model(m) and config.is_text_model(m):
            seen.add(m)
            picked.append(m)
    if not picked:
        return jsonify({"error": "Pick at least one text model — only text models can be fused."}), 400
    picked = picked[:5]  # keep the fan-out (and the bill) sane

    messages = [{"role": "user", "content": prompt}]

    def run_one(mid):
        label = (config.get_model(mid) or {}).get("label", mid)
        try:
            r = providers.gateway_chat(mid, messages, max_tokens=1024)
            return {"model": mid, "label": label, "ok": True,
                    "text": r["text"], "provider": r["provider"],
                    "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"]}
        except (providers.ProviderKeyMissing, providers.LLMError) as e:
            return {"model": mid, "label": label, "ok": False, "error": str(e)}

    # Fan out concurrently so total latency ≈ the slowest single model.
    with ThreadPoolExecutor(max_workers=min(len(picked), 5)) as pool:
        answers = list(pool.map(run_one, picked))

    fused = _fuse_answers(prompt, answers, fuser)

    # BILLING — charge every real API call (each candidate + the fuser, when it
    # actually ran) to the logged-in account, in ¥. The single-candidate and
    # fuser-failed paths reuse an answer without a new call, so they aren't billed.
    total_cost = 0.0
    balance = customer.get("balance")
    for a in answers:
        if a.get("ok"):
            billed = CUSTOMERS.record_usage(customer["id"], a["model"],
                                            a.get("input_tokens", 0), a.get("output_tokens", 0))
            total_cost += billed["cost"]
            balance = billed["balance"]
    if fused and fused.get("input_tokens") is not None:
        billed = CUSTOMERS.record_usage(customer["id"], fused["by"],
                                        fused.get("input_tokens", 0), fused.get("output_tokens", 0))
        total_cost += billed["cost"]
        balance = billed["balance"]

    return jsonify({"prompt": prompt, "answers": answers, "fused": fused,
                    "cost": round(total_cost, 6), "balance": balance})


def _fuse_answers(prompt, answers, fuser):
    """Reconcile several model answers into one best result. Returns
    {"text", "by", "by_label", "note"} or None if every model failed."""
    ok = [a for a in answers if a.get("ok")]
    if not ok:
        return None
    if len(ok) == 1:
        a = ok[0]
        return {"text": a["text"], "by": a["model"], "by_label": a["label"],
                "note": "Only one model returned an answer, so it's shown as-is."}

    # Choose the fuser. An explicit, live choice wins; "auto" prefers Opus (its
    # key is verified at startup) and falls back to the first model that answered.
    if fuser and fuser != "auto" and config.get_model(fuser) and providers.model_available(fuser):
        fuser_model = fuser
    elif providers.model_available(config.OPUS):
        fuser_model = config.OPUS
    else:
        fuser_model = ok[0]["model"]
    fuser_label = (config.get_model(fuser_model) or {}).get("label", fuser_model)

    transcript = "\n\n".join(
        f"### Candidate {i + 1} — from {a['label']}\n{a['text']}"
        for i, a in enumerate(ok))
    system = (
        "You are the Fusion judge. Several AI models were each given the SAME prompt. "
        "Return the single BEST answer by cross-checking the candidates against one "
        "another: where they agree, treat it as more reliable; where they disagree, "
        "reason out which is correct and discard the false claims; incorporate any "
        "good point one model caught that the others missed. Output one clean, "
        "self-contained answer for the user. Do NOT mention the candidates, the other "
        "models, or that any fusion happened — just give the best possible response."
    )
    user = (f"The user's prompt was:\n\n{prompt}\n\n"
            f"Here are the candidate answers:\n\n{transcript}\n\n"
            "Now write the single best, fact-checked answer to the user's prompt.")
    fuse_messages = [{"role": "system", "content": system},
                     {"role": "user", "content": user}]
    try:
        r = providers.gateway_chat(fuser_model, fuse_messages, max_tokens=1600)
        # input/output tokens are returned so the caller can bill this fuser call.
        return {"text": r["text"], "by": fuser_model, "by_label": fuser_label,
                "note": f"Fused from {len(ok)} model answers.",
                "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"]}
    except (providers.ProviderKeyMissing, providers.LLMError):
        a = ok[0]  # fusion failed — fall back to the first successful answer
        return {"text": a["text"], "by": a["model"], "by_label": a["label"],
                "note": "The fuser was unavailable, so the first model's answer is shown."}


def _check_api_key():
    """Validate the key at startup so problems show up immediately (not after a run).

    Uses the FREE models.list endpoint, so this costs no tokens. Returns (ok, message)
    and never prints the key itself — only its last 4 characters."""
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        key = key.strip().strip('"').strip("'").strip()
    if not key:
        return False, ("No ANTHROPIC_API_KEY found. Copy .env.example to .env and paste your "
                       "key from https://console.anthropic.com/settings/keys.")
    try:
        anthropic.Anthropic(api_key=key).models.list(limit=1)
        return True, f"API key verified OK (ends in …{key[-4:]})."
    except anthropic.APIStatusError as e:
        if e.status_code == 401:
            return False, ("API key was REJECTED (401 - invalid key). Open .env, paste your current "
                           "key from https://console.anthropic.com/settings/keys, save, and restart.")
        return False, f"Could not verify the API key (HTTP {e.status_code}): {e}"
    except Exception as e:
        return False, f"Couldn't reach Claude to verify the key ({type(e).__name__}). Check your internet."


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print("\n" + "=" * 60)
    print("  AI Agent Team is running!")
    print(f"  Open this in your browser:  http://localhost:{port}")
    print("  Press CONTROL-C here to stop it.")
    print("=" * 60)
    # Quick, free auth check so a bad key is caught NOW, not mid-run.
    ok, msg = _check_api_key()
    print(("  [OK]  " if ok else "  [!!]  ") + msg)
    if not ok:
        print("  The server will still start, but runs will fail until the key is fixed.")
    # Bring the 定时任务 scheduler up so saved weekly/monthly jobs fire on cadence —
    # UNLESS it runs as its own process/container (RUN_SCHEDULER=0 in production,
    # where scheduler.py owns the cron loop and the Postgres advisory lock).
    if config.run_scheduler_in_process():
        _start_scheduler()
    else:
        print("  [i]  In-process scheduler disabled (RUN_SCHEDULER=0) — the "
              "dedicated scheduler process owns 定时任务.")
    print("=" * 60 + "\n")
    # threaded=True so background jobs and status polls run at the same time.
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)

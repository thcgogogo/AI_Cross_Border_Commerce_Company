"""
config.py — The "org chart" for your AI company.

This is the ONE file you edit to add, remove, or re-tune agents.
Each agent is just a dictionary. To add a new employee to the team,
copy an existing entry, give it a new `id`, write its `system_prompt`,
and say who it `reports_to`. That's it — the rest of the framework
picks it up automatically.
"""

import os

# ---------------------------------------------------------------------------
# MODELS
# ---------------------------------------------------------------------------
# Exact model IDs (do NOT add date suffixes).
#   - OPUS   = smartest, best for planning & final writing (most expensive)
#   - SONNET = great balance of speed, cost, and quality
#   - HAIKU  = cheapest & fastest, good for simple high-volume work
OPUS = "claude-opus-4-7"
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5"

# The domestic (mainland-China) model the whole team runs on when the
# TEAM_MODEL_PRESET=domestic switch is set (see team_model_preset() and
# effective_model() below). Claude is not reliably reachable from the mainland
# and a foreign LLM raises 生成式AI 算法备案 compliance issues, so a public
# Chinese deployment runs on DeepSeek instead. DeepSeek V4 Flash is the value
# pick: cheapest, fast, 1M context, 384K max output, thinking on by default.
# Elevate the planner/writer to "deepseek-v4-pro" per-agent if you want more.
DOMESTIC_MODEL = "deepseek-v4-flash"

# Which models support "adaptive thinking" + the "effort" dial.
# The LLM wrapper uses this so switching a worker to HAIKU never crashes. These are
# Anthropic-only features, so the set lists only Claude model ids — other providers
# go through the OpenAI-compatible adapter, which never sends these parameters.
MODELS_WITH_ADAPTIVE_THINKING = {OPUS, SONNET, "claude-opus-4-6"}
MODELS_WITH_EFFORT = {OPUS, SONNET, "claude-opus-4-6", "claude-opus-4-5"}

# ---------------------------------------------------------------------------
# PROVIDERS — where each model actually runs
# ---------------------------------------------------------------------------
# The team is multi-provider. Claude models run on the OFFICIAL Anthropic SDK (the
# app's native, fully-working path). Every other provider below speaks the
# OpenAI-compatible Chat Completions API, so ONE adapter (agent_team/providers.py)
# reaches all of them — you just point it at the right base_url with the right key.
#
# Each provider reads its key from an environment variable (put them in .env). Until
# a provider's key is present the models still appear in the catalog/store, but a run
# or gateway call that needs them returns a clear "add PROVIDER_API_KEY" message —
# nothing is faked. Base URLs/keys are best-effort public values; confirm each against
# the provider's docs when you add its key.
PROVIDERS = {
    "anthropic": {"label": "Anthropic", "sdk": "anthropic",
                  "env_key": "ANTHROPIC_API_KEY", "base_url": None},
    "openai":    {"label": "OpenAI", "sdk": "openai_compatible",
                  "env_key": "OPENAI_API_KEY", "base_url": "https://api.openai.com/v1"},
    # Phase 4 — OFFLINE Local-Model mode (WS12): Ollama's local OpenAI-compatible server.
    # The key is UNUSED by Ollama (provider_key returns a placeholder when unset so the
    # adapter never raises ProviderKeyMissing). Reached ONLY when LOCAL_MODEL_MODE is on.
    "ollama":    {"label": "Local (Ollama)", "sdk": "openai_compatible",
                  "env_key": "OLLAMA_API_KEY", "base_url": "http://127.0.0.1:11434/v1"},
    "google":    {"label": "Google", "sdk": "openai_compatible",
                  "env_key": "GEMINI_API_KEY",
                  "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/"},
    "xai":       {"label": "xAI", "sdk": "openai_compatible",
                  "env_key": "XAI_API_KEY", "base_url": "https://api.x.ai/v1"},
    "deepseek":  {"label": "DeepSeek", "sdk": "openai_compatible",
                  "env_key": "DEEPSEEK_API_KEY", "base_url": "https://api.deepseek.com"},
    "qwen":      {"label": "Alibaba Qwen", "sdk": "openai_compatible",
                  "env_key": "DASHSCOPE_API_KEY",
                  "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
    "minimax":   {"label": "MiniMax", "sdk": "openai_compatible",
                  "env_key": "MINIMAX_API_KEY", "base_url": "https://api.minimax.chat/v1"},
    "moonshot":  {"label": "Moonshot · Kimi", "sdk": "openai_compatible",
                  "env_key": "MOONSHOT_API_KEY", "base_url": "https://api.moonshot.ai/v1"},
    "tencent":   {"label": "Tencent · Hunyuan", "sdk": "openai_compatible",
                  "env_key": "HUNYUAN_API_KEY",
                  "base_url": "https://api.hunyuan.cloud.tencent.com/v1"},
    "xiaomi":    {"label": "Xiaomi · MiMo", "sdk": "openai_compatible",
                  "env_key": "XIAOMI_API_KEY", "base_url": "https://api-mimo.xiaomi.com/v1"},
    "bytedance": {"label": "ByteDance · Seedream", "sdk": "openai_compatible",
                  "env_key": "ARK_API_KEY",
                  "base_url": "https://ark.cn-beijing.volces.com/api/v3"},
}


def provider_base_url(provider_id):
    """The base_url a provider's OpenAI-compatible client should use.

    The catalog defaults to each provider's INTERNATIONAL endpoint, but several
    providers run separate mainland-China platforms whose keys ONLY work against
    a different host (e.g. Qwen/DashScope intl vs cn, Moonshot api.moonshot.ai vs
    .cn). Rather than hardcode one region, allow a per-provider override via env:
    the provider's `*_API_KEY` name with `_API_KEY` swapped for `_BASE_URL` — e.g.
    DASHSCOPE_BASE_URL, MOONSHOT_BASE_URL. Unset → the catalog default."""
    p = PROVIDERS.get(provider_id, {})
    env_key = p.get("env_key") or ""
    if env_key.endswith("_API_KEY"):
        override = _env(env_key[:-len("_API_KEY")] + "_BASE_URL")
        if override:
            return override
    return p.get("base_url")

# ---------------------------------------------------------------------------
# MONEY — catalog prices are in US$ (how providers publish them), but the WHOLE
# app shows money to customers in Chinese Yuan (¥). We convert at one rate.
# ---------------------------------------------------------------------------
# US$ -> ¥ conversion. One knob: change it and every ¥ figure in the app follows
# (budgets, balances, per-call charges, the Token Manager, the Models page).
USD_TO_CNY = 7.2
CURRENCY = "CNY"
CURRENCY_SYMBOL = "¥"

# A new account's starting demo credit, in ¥. Generous on purpose so a customer
# can run the team / Fusion a few times and watch their balance draw down.
START_BALANCE_CNY = 200.0


def usd_to_cny(usd):
    """Convert a US$ amount to ¥ at the app's single configured rate."""
    return float(usd or 0.0) * USD_TO_CNY


def cost_usd(model_id, input_tokens, output_tokens):
    """US$ cost of a text call from the catalog's per-million-token pricing."""
    row = get_model(model_id) or {}
    in_rate = float(row.get("input_per_mtok", 0.0) or 0.0)
    out_rate = float(row.get("output_per_mtok", 0.0) or 0.0)
    return (int(input_tokens or 0) / 1_000_000.0) * in_rate \
        + (int(output_tokens or 0) / 1_000_000.0) * out_rate


def cost_cny(model_id, input_tokens, output_tokens):
    """¥ cost of a text call — what the app actually bills to budgets/balances."""
    return usd_to_cny(cost_usd(model_id, input_tokens, output_tokens))


# ---------------------------------------------------------------------------
# DEPLOYMENT / RUNTIME (Phase 7) — the single place env-driven infra knobs live
# ---------------------------------------------------------------------------
# These read the process environment (populated from .env via app.py's load_dotenv,
# or injected by docker-compose / the host). They stay DORMANT in local dev: with
# nothing set the app keeps its zero-config SQLite + dev-secret behaviour. Set them
# in staging/prod (see .env.example's DEPLOYMENT block + docs/ops.md).
#
# Nothing here is a secret value — these are accessors. Secrets themselves are only
# ever read from the environment, never written to disk or logs.

# Treat these strings as "yes" for boolean-style env flags.
_TRUE = ("1", "true", "yes", "on")


def _env(name):
    """A stripped env var, or "" if unset/blank."""
    return (os.environ.get(name) or "").strip()


def database_url():
    """The SQLAlchemy URL for the company-brain DB, or None to use the local
    SQLite default (db.py falls back when this is None). Set DATABASE_URL to a
    postgresql://… URL in staging/prod. Example:
        postgresql+psycopg2://user:pass@postgres:5432/company_brain"""
    return _env("DATABASE_URL") or None


def app_env():
    """The deployment environment name: "development" (default), "staging", or
    "production". Read from APP_ENV (falling back to FLASK_ENV)."""
    return (_env("APP_ENV") or _env("FLASK_ENV") or "development").lower()


def is_production():
    """True only when APP_ENV/FLASK_ENV is exactly "production". Gates the
    mandatory-SECRET_KEY check so a misconfigured prod box refuses to boot."""
    return app_env() == "production"


def secret_key():
    """The Flask session secret from the environment (SECRET_KEY, then
    FLASK_SECRET_KEY), or None. In production app.py REQUIRES one; in dev it
    falls back to a persisted random key so logins survive a restart."""
    return _env("SECRET_KEY") or _env("FLASK_SECRET_KEY") or None


def analytics_ip_salt():
    """Salt for hashing client IPs in the founder-only analytics `events` table — IPs
    are NEVER stored raw. ANALYTICS_IP_SALT env wins (so it can be rotated without
    touching the session secret); else derived from SECRET_KEY; else a fixed dev
    default (local dev IPs aren't sensitive)."""
    return _env("ANALYTICS_IP_SALT") or secret_key() or "tangent-dev-analytics-salt"


def sentry_dsn():
    """The Sentry DSN, or None. When None, error reporting stays fully disabled
    (sentry_sdk is never initialised) — nothing is sent anywhere."""
    return _env("SENTRY_DSN") or None


def run_scheduler_in_process():
    """True if the 定时任务 scheduler should run INSIDE the web process. Default
    True so `python app.py` keeps firing jobs in local dev exactly as before. In
    the Docker stack set RUN_SCHEDULER=0 on the web service — a dedicated
    `scheduler.py` container owns scheduling so multiple gunicorn workers can't
    double-fire jobs."""
    val = _env("RUN_SCHEDULER")
    if not val:
        return True
    return val.lower() in _TRUE


def backup_remote():
    """An rclone remote path (e.g. "oss:my-bucket/db") for off-box backup upload,
    or None. When None, scripts/backup.sh keeps backups on local disk only."""
    return _env("BACKUP_REMOTE") or None


# How long since the scheduler's last heartbeat before /healthz calls it stale.
# A heartbeat is written every ~60s, so 120s tolerates one missed beat.
HEARTBEAT_STALE_SECS = 120


# ===========================================================================
# SECURITY & ABUSE HARDENING (Phase 8)
# ===========================================================================
# China launch makes phone verification the norm and abuse of OUR upstream API
# keys the top financial risk. Everything below is read from the environment; the
# helpers return safe, dev-friendly defaults so local `python app.py` keeps
# working with zero extra config (SMS prints to the console, encryption is off,
# rate limits use in-process memory).

def _env_float(name, default):
    """A float env var, falling back to `default` when unset or unparseable."""
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name, default):
    """An int env var, falling back to `default` when unset or unparseable."""
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ---- Platform spend circuit breaker --------------------------------------
# Rolling ¥ we spend on UPSTREAM provider keys. 0 (the default) disables a cap so
# dev/tests never trip it; set a real ceiling in production to bound a runaway bill.
def platform_hourly_cap_yuan():
    """¥ ceiling on platform-wide upstream spend per hour (0 = no cap)."""
    return _env_float("PLATFORM_HOURLY_CAP_YUAN", 0.0)


def platform_daily_cap_yuan():
    """¥ ceiling on platform-wide upstream spend per day (0 = no cap)."""
    return _env_float("PLATFORM_DAILY_CAP_YUAN", 0.0)


# A single account can spend at most this much ¥/day on upstream calls before its
# new runs are refused (its own balance still applies independently). Overridable
# per account via accounts.daily_cap_yuan. 0 disables the per-account day cap.
ACCOUNT_DAILY_CAP_YUAN_DEFAULT = 200.0


def account_daily_cap_yuan():
    """Default per-account daily ¥ cap (accounts.daily_cap_yuan overrides per row)."""
    return _env_float("ACCOUNT_DAILY_CAP_YUAN", ACCOUNT_DAILY_CAP_YUAN_DEFAULT)


# Phase 11 — a paired desktop device gets its OWN daily ¥ sub-cap, the tighter of a
# fraction of the account's daily cap and a flat ceiling (locked 2026-06-15). Cheap
# insurance: a stolen device token can't drain the whole account wallet. Tunable.
DEVICE_DAILY_CAP_FRACTION = _env_float("DEVICE_DAILY_CAP_FRACTION", 0.3)
DEVICE_DAILY_CAP_MAX_YUAN = _env_float("DEVICE_DAILY_CAP_MAX_YUAN", 20.0)


def admin_alert_webhook():
    """A webhook URL the circuit breaker POSTs to when it trips (e.g. a 企业微信
    群机器人), or None. The admin email list reuses ADMIN_EMAILS."""
    return _env("ADMIN_ALERT_WEBHOOK") or None


def admin_emails():
    """The founder allow-list from ADMIN_EMAILS (comma/semicolon/whitespace
    separated), lower-cased, or [] when unset. Used both to gate the admin pages
    and to address circuit-breaker alert emails. Read fresh so .env edits apply."""
    raw = _env("ADMIN_EMAILS")
    if not raw:
        return []
    parts = raw.replace(";", ",").replace("\n", ",").split(",")
    seen, out = set(), []
    for p in parts:
        e = p.strip().lower()
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


# ---- Phone (SMS) verification ---------------------------------------------
SMS_CODE_TTL_SECS = 5 * 60       # a verification code is valid for 5 minutes
SMS_MAX_SENDS_PER_HOUR = 3       # max codes sent to one phone per rolling hour
SMS_MAX_VERIFY_ATTEMPTS = 5      # wrong tries before a code is burned (6th fails)


def sms_provider_name():
    """Which SmsProvider to use: explicit SMS_PROVIDER, else "aliyun" when Aliyun
    keys are present, else "console" (prints the code — the dev/test default)."""
    explicit = _env("SMS_PROVIDER").lower()
    if explicit:
        return explicit
    if _env("ALIYUN_SMS_ACCESS_KEY_ID") and _env("ALIYUN_SMS_ACCESS_KEY_SECRET"):
        return "aliyun"
    return "console"


def aliyun_sms_settings():
    """Aliyun SMS credentials/template from env (used by AliyunSmsProvider)."""
    return {
        "access_key_id": _env("ALIYUN_SMS_ACCESS_KEY_ID"),
        "access_key_secret": _env("ALIYUN_SMS_ACCESS_KEY_SECRET"),
        "sign_name": _env("ALIYUN_SMS_SIGN_NAME"),
        "template_code": _env("ALIYUN_SMS_TEMPLATE_CODE"),
        "region": _env("ALIYUN_SMS_REGION") or "cn-hangzhou",
    }


# ---- Encryption at rest (Fernet) ------------------------------------------
def fernet_keys():
    """The list of Fernet keys from FERNET_KEYS (comma-separated, newest first for
    rotation), or [] when unset. With none set, files are stored in plaintext —
    the zero-config local-dev default; set a key in production to encrypt at rest.
    Generate one: python -c \"from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())\""""
    raw = _env("FERNET_KEYS") or _env("FERNET_KEY")
    return [k.strip() for k in raw.split(",") if k.strip()] if raw else []


# ---- Rate limiting ---------------------------------------------------------
def ratelimit_storage_uri():
    """flask-limiter storage backend URI. Default in-process memory (fine for a
    single process / local dev / tests). For multi-worker gunicorn set a shared
    backend, e.g. redis://redis:6379. (The `limits` lib backs flask-limiter.)"""
    return _env("RATELIMIT_STORAGE_URI") or "memory://"


def auth_rate_limit():
    """Per-IP limit on auth endpoints (login/signup/send-code/reset)."""
    return _env("AUTH_RATE_LIMIT") or "5 per minute"


def run_rate_limit():
    """Per-account limit on starting agent-team runs."""
    return _env("RUN_RATE_LIMIT") or "10 per hour"


def upload_rate_limit():
    """Per-account limit on 数据收件箱 writes (/api/uploads) — generous enough
    for drag-drop bursts, tight enough that the inbox can't be hammered (it was
    the only unthrottled write route, redesign Phase 6 E1)."""
    return _env("UPLOAD_RATE_LIMIT") or "20 per minute"


def ratelimit_enabled():
    """Rate limiting is on by default; set RATELIMIT_ENABLED=0 to disable (e.g. in
    a test that isn't exercising it)."""
    val = _env("RATELIMIT_ENABLED")
    if not val:
        return True
    return val.lower() in _TRUE


def trust_proxy_hops():
    """How many trusted reverse-proxy hops sit in front of the app (env TRUST_PROXY).

    0 (default) = the app is reached directly, so X-Forwarded-For is NOT trusted —
    a client can't spoof its IP to dodge per-IP rate limits. Set it to the real
    number of proxies that prepend X-Forwarded-For (the compose stack = 1 nginx hop;
    a cloud LB → nginx = 2). When > 0, app.py wraps the WSGI app in werkzeug's
    ProxyFix so request.remote_addr (and the limiter key) resolve the real client."""
    raw = _env("TRUST_PROXY")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _env_int(name, default):
    """A non-negative int env var, or `default` if unset/blank/non-numeric."""
    try:
        return max(0, int(_env(name)))
    except (TypeError, ValueError):
        return default


# ---- Public (anonymous) Fusion — cost/abuse ceilings -----------------------
# The landing-page Fusion chat is anonymous (no login), so the SERVER pays for every
# model call on its own keys. These caps + the per-IP rate limit + the platform spend
# breaker are the only things between "free demo" and a runaway bill. Keep them tight.
def fusion_rate_limit():
    """Per-IP limit on the public Fusion chat. Tight by default — it spends real
    money on our keys with no login. (Behind a proxy, set TRUST_PROXY so this keys
    on the real client, not the proxy.)"""
    return _env("FUSION_RATE_LIMIT") or "10 per hour"


def fusion_anon_max_models():
    """Max models fused per anonymous run (bounds the fan-out cost)."""
    return _env_int("FUSION_ANON_MAX_MODELS", 3) or 3


def fusion_anon_max_tokens():
    """Max output tokens per candidate answer for an anonymous run."""
    return _env_int("FUSION_ANON_MAX_TOKENS", 800) or 800


def fusion_anon_max_history_msgs():
    """Most recent conversation turns forwarded to the models (older are dropped)."""
    return _env_int("FUSION_ANON_MAX_HISTORY_MSGS", 12) or 12


def fusion_anon_max_chars():
    """Total conversation characters forwarded (bounds input cost on long chats)."""
    return _env_int("FUSION_ANON_MAX_CHARS", 12000) or 12000


# ---- Signed-in Fusion — free, multi-turn, per-conversation memory ----------
# The logged-in chatbot bills COGS to the account (never the balance — Fusion is
# free) and is gated by a PER-ACCOUNT rate limit + a free-vs-paid model split, not
# by money. Caps are looser than the anonymous ceilings because spend is now
# attributed to an account (so the platform breaker + per-account ¥ cap apply).
def fusion_account_rate_limit():
    """Per-ACCOUNT limit on signed-in Fusion turns (keyed on customer_id, not IP).
    The free tier's primary throttle — a runaway can't burn the server's keys."""
    return _env("FUSION_ACCOUNT_RATE_LIMIT") or "20 per day"


def fusion_free_anon_turns():
    """How many turns an ANONYMOUS visitor gets free (server-paid, no persistence)
    before login is required — the freemium teaser. Counted in the signed session
    cookie; the per-IP rate limit + the platform breaker backstop the cost."""
    return _env_int("FUSION_FREE_ANON_TURNS", 3) or 3


def fusion_loggedin_max_history_msgs():
    """Most-recent conversation turns forwarded to the models on a signed-in turn
    (older turns stay in the DB; only the context window is bounded)."""
    return _env_int("FUSION_LOGGEDIN_MAX_HISTORY_MSGS", 20) or 20


def fusion_loggedin_max_chars():
    """Total conversation characters forwarded on a signed-in turn (input-cost bound)."""
    return _env_int("FUSION_LOGGEDIN_MAX_CHARS", 24000) or 24000


def fusion_free_models():
    """The model IDs a FREE account may fan out to (the cheaper 'budget' set). The
    requested models are clamped to this allowlist so a free user can't run the
    expensive QUALITY tier. Full fan-out is reserved for paid plans."""
    raw = _env("FUSION_FREE_MODELS")
    if raw:
        ids = [m.strip() for m in raw.split(",") if m.strip()]
        if ids:
            return ids
    # Default = the frontend BUDGET preset (cheap, broad provider spread).
    return ["deepseek-v4-flash", "qwen-plus", "gemini-2.5-flash", "gpt-5-mini", HAIKU]


def fusion_free_model_cap():
    """Max models a FREE account fuses per turn (bounds the free fan-out cost)."""
    return _env_int("FUSION_FREE_MODEL_CAP", 3) or 3


def fusion_full_model_cap():
    """Max models a PAID account fuses per turn (the full fan-out ceiling)."""
    return _env_int("FUSION_FULL_MODEL_CAP", 5) or 5


def fusion_free_fuser():
    """The reconciler model forced on the FREE tier — a cheap one (Haiku, or the
    domestic model under TEAM_MODEL_PRESET so a no-Anthropic-key setup still fuses)."""
    return (_env("FUSION_FREE_FUSER")
            or (DOMESTIC_MODEL if team_model_preset() == "domestic" else HAIKU))


def fusion_retention_days():
    """Days to keep a Fusion conversation before a (future) prune job may purge it.
    None = keep indefinitely (the default). Tunable via FUSION_RETENTION_DAYS."""
    return _env_int("FUSION_RETENTION_DAYS", 0) or None


# Default gateway requests-per-minute for a key with no explicit accounts.rpm_limit.
DEFAULT_KEY_RPM = 60


# ---- Upload caps -----------------------------------------------------------
MAX_UPLOAD_BYTES_PER_FILE = 25 * 1024 * 1024   # 25 MB per uploaded file
MAX_UPLOAD_FILES_PER_RUN = 8                   # at most 8 files attached to one run


def phone_verification_required():
    """Whether signup/reset require an SMS code. On by default (China norm); the
    code is delivered by the Console provider in dev so this stays usable locally.
    Set PHONE_VERIFICATION=0 only for environments that truly need it off."""
    val = _env("PHONE_VERIFICATION")
    if not val:
        return True
    return val.lower() in _TRUE


def report_quota_enabled():
    """Whether the report-delivery quota/credit gate (_plan_gate) is ENFORCED.

    DISCARDED for now (2026-06-15 product decision — the product is pivoting to the
    deployable-agent marketplace): defaults OFF, so any signed-in account can generate
    reports without a flat-plan quota or ¥29 credit. The PLANS config stays in place and
    the full gate body stays intact; set REPORT_QUOTA_ENABLED=1 to re-enable it. Auth,
    the ¥ wallet, per-run budgets and the platform breaker are orthogonal and unaffected."""
    return _env("REPORT_QUOTA_ENABLED").lower() in _TRUE


# ===========================================================================
# PAYMENTS, SUBSCRIPTIONS & FAPIAO (Phase 9)
# ===========================================================================
# Real Alipay / WeChat Pay replace the demo top-up. Everything below is read from
# the environment with dev-safe defaults: with NO provider configured, the Mock
# provider takes over (signed local notifies, a fake cashier page), so the whole
# money path — orders, idempotent paid-notify, subscriptions, refunds, fapiao —
# is exercisable locally and in CI with zero config and zero real money.

def app_base_url():
    """The public base URL of this deployment (no trailing slash). Used to build
    payment notify_url callbacks and the pay links in renewal notices."""
    return (_env("APP_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")


# ---- Subscription plans ----------------------------------------------------
# The plan contract the gating middleware reads, aligned with the product brief
# (redesign B3). Internal codes stay free/basic/pro — no data migration; only
# labels/quotas/features changed. monthly_report_quota caps how many agent-team
# reports (runs) an account may start per calendar month — EXCEPT when
# lifetime_quota is true (the 免费体验 trial): then it is a lifetime total, not
# a monthly allowance. seats is the number of team members the plan contracts
# for (surfaced on the plans page now; enforced when multi-user login lands).
# features holds the plan-gated capability flags, read through plan_has() —
# scheduled_delivery (定时自动送达), contract_review (合同审查 add-on),
# priority_models (优先模型).
PLANS = {
    "free":  {"code": "free",  "label": "免费体验", "monthly_price_yuan": 0.0,
              "monthly_report_quota": 2,   "lifetime_quota": True,  "seats": 1,
              "features": []},
    "basic": {"code": "basic", "label": "基础版", "monthly_price_yuan": 199.0,
              "monthly_report_quota": 20,  "lifetime_quota": False, "seats": 1,
              "features": []},
    "pro":   {"code": "pro",   "label": "企业版", "monthly_price_yuan": 599.0,
              "monthly_report_quota": 500, "lifetime_quota": False, "seats": 5,
              "features": ["scheduled_delivery", "contract_review",
                           "priority_models"]},
}
PURCHASABLE_PLANS = ("basic", "pro")   # what /api/pay/create accepts as plan_code
SUBSCRIPTION_PERIOD_DAYS = 30          # one paid period = 30 days ("a month")

# 单次报告 price (the brief's commitment-free bridge to subscription). Shown on
# the landing/pricing pages now; becomes purchasable in Phase 7 (B4).
SINGLE_REPORT_PRICE_YUAN = 29.0


def plan(code):
    """The PLANS row for `code`, falling back to the free plan."""
    return PLANS.get((code or "").strip().lower()) or PLANS["free"]


def plan_has(code, feature):
    """True when `code`'s plan includes `feature` (e.g. "scheduled_delivery")."""
    return feature in (plan(code).get("features") or ())


def subscription_renewal_lead_days():
    """Days BEFORE period end when the renewal order + pay-link notice go out
    (the spec's T-3d; China flows favor user-confirmed renewal over auto-charge)."""
    return _env_int("SUB_RENEWAL_LEAD_DAYS", 3)


def subscription_grace_days():
    """Days of grace AFTER period end during which features keep working."""
    return _env_int("SUB_GRACE_DAYS", 3)


def subscription_past_due_cancel_days():
    """How long a subscription may sit past_due (locked) before it is canceled."""
    return _env_int("SUB_PAST_DUE_CANCEL_DAYS", 30)


# ---- Top-up bounds (an order's ¥ amount must fall inside these) ------------
TOPUP_MIN_YUAN = 1.0
TOPUP_MAX_YUAN = 50000.0


# ---- Payment providers ------------------------------------------------------
def alipay_settings():
    """Alipay open-platform credentials from env, or None when not configured.
    The two keys accept a full PEM (with literal \\n escapes) or the bare base64
    body exactly as the Alipay console hands it out."""
    app_id = _env("ALIPAY_APP_ID")
    if not app_id:
        return None
    return {
        "app_id": app_id,
        "app_private_key": os.environ.get("ALIPAY_APP_PRIVATE_KEY") or "",
        "alipay_public_key": os.environ.get("ALIPAY_PUBLIC_KEY") or "",
        "gateway": _env("ALIPAY_GATEWAY") or "https://openapi.alipay.com/gateway.do",
    }


def wechatpay_settings():
    """WeChat Pay APIv3 merchant credentials from env, or None when not configured."""
    mchid = _env("WECHATPAY_MCHID")
    if not mchid:
        return None
    return {
        "mchid": mchid,
        "appid": _env("WECHATPAY_APPID"),
        "api_v3_key": os.environ.get("WECHATPAY_API_V3_KEY") or "",
        "serial_no": _env("WECHATPAY_SERIAL_NO"),
        "private_key": os.environ.get("WECHATPAY_PRIVATE_KEY") or "",
        "platform_cert": os.environ.get("WECHATPAY_PLATFORM_CERT") or "",
        "api_base": _env("WECHATPAY_API_BASE") or "https://api.mch.weixin.qq.com",
    }


def payment_providers_available():
    """Names of the configured payment providers, in display order. With NONE
    configured this returns ["mock"] — the zero-config dev/test fallback. As soon
    as a real provider is configured, mock is NOT in the list (and its notify
    endpoint + cashier go dead), so a known dev secret can never pay a real order."""
    out = []
    if alipay_settings():
        out.append("alipay")
    if wechatpay_settings():
        out.append("wechat")
    return out or ["mock"]


def payment_mock_secret():
    """HMAC secret signing the Mock provider's notifies (dev/test only — see
    payment_providers_available for why the default can't reach production money)."""
    return _env("PAYMENT_MOCK_SECRET") or "mock-pay-secret-dev"


# ===========================================================================
# ONBOARDING, SUPPORT & OPS DASHBOARD (Phase 10)
# ===========================================================================
def support_email():
    """The mailto target of the 联系支持 button. SUPPORT_EMAIL, else the first
    admin email, else "" (the UI then shows only the QR + message form)."""
    explicit = _env("SUPPORT_EMAIL")
    if explicit:
        return explicit
    admins = admin_emails()
    return admins[0] if admins else ""


def support_wechat_qr_url():
    """An image URL for the 企业微信 support-group QR shown in the 联系支持
    popup, or "" (the UI then shows a placeholder telling the operator to set
    SUPPORT_WECHAT_QR_URL)."""
    return _env("SUPPORT_WECHAT_QR_URL")


# ===========================================================================
# PRODUCT IDENTITY (UI redesign B1) — the customer-facing brand, one knob
# ===========================================================================
# We sell finished reports, so every customer-facing surface (PDF footer, emails,
# /api/meta for the web UI) reads the brand from HERE instead of hardcoding it.
# "Tangent AI" is the decided brand (plan §9 D1, 2026-06-11 — matches the long-
# standing tng-sk- key prefix); set PRODUCT_NAME / PRODUCT_TAGLINE in .env to
# rebrand without a code change (re-read per call, like the support knobs).
PRODUCT_NAME_DEFAULT = "Tangent AI"
PRODUCT_TAGLINE_DEFAULT = "你的 AI 经营分析团队"


def product_name():
    """The customer-facing product/brand name (PRODUCT_NAME env wins)."""
    return _env("PRODUCT_NAME") or PRODUCT_NAME_DEFAULT


def product_tagline():
    """The one-line tagline shown under the product name (PRODUCT_TAGLINE env wins)."""
    return _env("PRODUCT_TAGLINE") or PRODUCT_TAGLINE_DEFAULT


def product_byline():
    """The brief's trust footer 「由 [产品名] AI 团队生成 · 每个数字可溯源」, used
    verbatim on the PDF, the web footer and delivery messages. A name that
    already ends in "AI" (e.g. "Tangent AI") drops the extra "AI" so the line
    never reads "…AI AI 团队…"."""
    name = product_name()
    team = "%s 团队" % name if name.strip().upper().endswith("AI") else "%s AI 团队" % name
    return "由 %s生成 · 每个数字可溯源" % team


# The 生成报告 page composes the /api/run goal from these per report type, so the
# customer never types a prompt (redesign §3.3 — zero-configuration surface).
# `label` is the report-type card title; `goal` is the exact text POSTed to the
# existing /api/run. 自定义分析 is deliberately NOT here: its 补充说明 textarea IS
# the goal. Ordered list — the UI renders cards in this order, first is default.
REPORT_GOAL_TEMPLATES = [
    {
        "code": "weekly",
        "label": "经营周报",
        "goal": ("基于本次上传的最新经营数据，生成一份面向企业老板的经营周报，"
                 "使用商务汇报体中文：本周核心结论、关键指标与环比变化、与历史数据对比、"
                 "风险与异常提醒、下周三条建议行动。"),
    },
    {
        "code": "monthly",
        "label": "经营月报",
        "goal": ("基于本次上传的最新经营数据，生成一份面向企业老板的经营月报，"
                 "使用商务汇报体中文：本月核心结论、关键指标与上月环比、与历史趋势对比、"
                 "风险与异常提醒、下月三条建议行动。"),
    },
]


# ---------------------------------------------------------------------------
# MODEL FUNCTIONS — each model has a `kind` that says what it DOES.
# ---------------------------------------------------------------------------
# `kind` groups a model by function, which drives the tabs on the Models page and
# the "modalities" line on each model's detail page. Only "text" models are
# callable through the chat gateway + Fusion; the rest are catalog/storefront
# entries priced per their own unit (`price` + `price_unit`).
KINDS = {
    "text":          {"label": "Text",          "input": "Text",        "output": "Text",    "unit": None,                "callable": True},
    "image":         {"label": "Image",         "input": "Text",        "output": "Image",   "unit": "per image",         "callable": False},
    "embedding":     {"label": "Embeddings",    "input": "Text",        "output": "Vector",  "unit": "per 1M tokens",     "callable": False},
    "speech":        {"label": "Speech",        "input": "Text",        "output": "Audio",   "unit": "per 1M characters", "callable": False},
    "transcription": {"label": "Transcription", "input": "Audio",       "output": "Text",    "unit": "per minute",        "callable": False},
    "video":         {"label": "Video",         "input": "Text",        "output": "Video",   "unit": "per second",        "callable": False},
    "rerank":        {"label": "Rerank",        "input": "Text + Docs", "output": "Ranking", "unit": "per 1M tokens",     "callable": False},
}
# The order the function tabs appear in on the Models page.
KIND_ORDER = ["text", "image", "embedding", "speech", "transcription", "video", "rerank"]

# The models a user may assign to any agent, OR sell through the unified-API store.
# Add a row here and it shows up everywhere automatically (config-driven, one file).
#
# Fields: id (the provider's model string), label, provider (key in PROVIDERS above),
# tier, context window, and a blurb. `approx: True` marks pricing/ids that are
# best-effort public values — only the Anthropic rows are guaranteed exact.
#
# PRICING depends on the model's `kind` (see KINDS above):
#   - "text"  chat/LLMs: priced per token, with `input_per_mtok` + `output_per_mtok`
#             (US$ per 1,000,000 tokens). These are the ONLY models the chat gateway
#             and Fusion can call.
#   - every other kind (image / embedding / speech / transcription / video / rerank):
#             priced with a single `price` (US$) + `price_unit` string (e.g.
#             "per image", "per minute"). They appear in the catalog/store but are
#             NOT callable through the text chat gateway.
# Every US$ figure is shown to customers in ¥ (converted at USD_TO_CNY).
MODEL_CHOICES = [
    # ---- Anthropic (native path — exact pricing) ----
    {"id": OPUS, "label": "Opus 4.7", "provider": "anthropic", "tier": "Most capable",
     "input_per_mtok": 5.0, "output_per_mtok": 25.0, "context": "1M tokens",
     "blurb": "Anthropic's smartest model — best for planning, hard reasoning, "
              "and polished final writing. The default for every agent."},
    {"id": SONNET, "label": "Sonnet 4.6", "provider": "anthropic", "tier": "Balanced",
     "input_per_mtok": 3.0, "output_per_mtok": 15.0, "context": "1M tokens",
     "blurb": "A strong balance of quality, speed, and cost. A great choice for "
              "the analysts on high-volume runs."},
    {"id": HAIKU, "label": "Haiku 4.5", "provider": "anthropic", "tier": "Fastest & cheapest",
     "input_per_mtok": 1.0, "output_per_mtok": 5.0, "context": "200K tokens",
     "blurb": "The quickest, most economical model — ideal for simple, "
              "high-volume work where speed and cost matter most."},

    # ---- OpenAI ----
    {"id": "gpt-5.1", "label": "GPT-5.1", "provider": "openai", "tier": "Flagship",
     "input_per_mtok": 1.25, "output_per_mtok": 10.0, "context": "400K tokens", "approx": True,
     "blurb": "OpenAI's flagship — strong general reasoning, coding, and tool use."},
    {"id": "gpt-5-mini", "label": "GPT-5 mini", "provider": "openai", "tier": "Efficient",
     "input_per_mtok": 0.25, "output_per_mtok": 2.0, "context": "400K tokens", "approx": True,
     "blurb": "A cheaper, faster GPT-5 for high-volume and everyday tasks."},
    {"id": "o3", "label": "o3", "provider": "openai", "tier": "Reasoning",
     "input_per_mtok": 2.0, "output_per_mtok": 8.0, "context": "200K tokens", "approx": True,
     "blurb": "A deep-reasoning model for hard math, science, and logic problems."},

    # ---- Google ----
    {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro", "provider": "google", "tier": "Flagship",
     "input_per_mtok": 1.25, "output_per_mtok": 10.0, "context": "1M tokens", "approx": True,
     "blurb": "Google's most capable Gemini — long context and strong multimodal reasoning."},
    {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "provider": "google", "tier": "Fast",
     "input_per_mtok": 0.30, "output_per_mtok": 2.50, "context": "1M tokens", "approx": True,
     "blurb": "A fast, low-cost Gemini for high-throughput work with a huge context window."},

    # ---- xAI ----
    {"id": "grok-4", "label": "Grok 4", "provider": "xai", "tier": "Flagship",
     "input_per_mtok": 3.0, "output_per_mtok": 15.0, "context": "256K tokens", "approx": True,
     "blurb": "xAI's flagship Grok — strong reasoning with real-time knowledge."},
    {"id": "grok-3-mini", "label": "Grok 3 mini", "provider": "xai", "tier": "Efficient",
     "input_per_mtok": 0.30, "output_per_mtok": 0.50, "context": "131K tokens", "approx": True,
     "blurb": "A lightweight, economical Grok for everyday tasks."},

    # ---- DeepSeek (V4 — launched 2026-04-24; the legacy deepseek-chat /
    #      deepseek-reasoner ids retire 2026-07-24. Both V4 models do thinking,
    #      JSON, and tool calls; 1M context, 384K max output.) ----
    {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash", "provider": "deepseek", "tier": "General",
     "input_per_mtok": 0.14, "output_per_mtok": 0.28, "context": "1M tokens", "approx": True,
     "blurb": "DeepSeek's fast, very low-cost flagship — the value pick for general and "
              "high-volume work. 1M context, huge 384K output, thinking on by default."},
    {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro", "provider": "deepseek", "tier": "Reasoning",
     "input_per_mtok": 0.435, "output_per_mtok": 0.87, "context": "1M tokens", "approx": True,
     "blurb": "DeepSeek's most capable model — for hard reasoning, coding, and agent workflows."},

    # ---- Alibaba Qwen ----
    {"id": "qwen3.7-max", "label": "Qwen 3.7 Max", "provider": "qwen", "tier": "Flagship",
     "input_per_mtok": 1.20, "output_per_mtok": 6.00, "context": "256K tokens", "approx": True,
     "blurb": "Alibaba's current flagship Qwen — top-tier Chinese writing, long context, "
              "and strong instruction-following."},
    {"id": "qwen-max", "label": "Qwen Max", "provider": "qwen", "tier": "Flagship",
     "input_per_mtok": 1.60, "output_per_mtok": 6.40, "context": "32K tokens", "approx": True,
     "blurb": "Alibaba's most capable Qwen — strong multilingual and reasoning performance."},
    {"id": "qwen-plus", "label": "Qwen Plus", "provider": "qwen", "tier": "Balanced",
     "input_per_mtok": 0.40, "output_per_mtok": 1.20, "context": "131K tokens", "approx": True,
     "blurb": "A balanced, affordable Qwen for general-purpose work."},

    # ---- MiniMax ----
    {"id": "MiniMax-M3", "label": "MiniMax M3", "provider": "minimax", "tier": "Flagship",
     "input_per_mtok": 0.30, "output_per_mtok": 1.20, "context": "1M tokens", "approx": True,
     "blurb": "MiniMax's current flagship — strong agentic / tool-use behaviour over a very "
              "large context window."},
    {"id": "MiniMax-Text-01", "label": "MiniMax Text-01", "provider": "minimax", "tier": "Flagship",
     "input_per_mtok": 0.20, "output_per_mtok": 1.10, "context": "1M tokens", "approx": True,
     "blurb": "MiniMax's flagship text model with an extremely large context window."},
    {"id": "MiniMax-M1", "label": "MiniMax M1", "provider": "minimax", "tier": "Reasoning",
     "input_per_mtok": 0.40, "output_per_mtok": 2.20, "context": "1M tokens", "approx": True,
     "blurb": "MiniMax's reasoning-focused model — chain-of-thought over a very large context window."},

    # ---- Moonshot · Kimi ----
    {"id": "kimi-k2.5", "label": "Kimi K2", "provider": "moonshot", "tier": "Flagship",
     "input_per_mtok": 0.60, "output_per_mtok": 2.50, "context": "128K tokens", "approx": True,
     "blurb": "Moonshot's Kimi K2 — a strong long-context model popular for agentic use."},

    # ---- Tencent · Hunyuan ----
    {"id": "hunyuan-turbos-latest", "label": "Hunyuan TurboS", "provider": "tencent", "tier": "Flagship",
     "input_per_mtok": 0.40, "output_per_mtok": 1.60, "context": "256K tokens", "approx": True,
     "blurb": "Tencent's flagship Hunyuan TurboS — strong bilingual (Chinese/English) reasoning and chat."},

    # ---- Xiaomi · MiMo ----
    {"id": "mimo-7b-rl", "label": "Xiaomi MiMo 7B", "provider": "xiaomi", "tier": "Efficient",
     "input_per_mtok": 0.15, "output_per_mtok": 0.60, "context": "32K tokens", "approx": True,
     "blurb": "Xiaomi's open MiMo reasoning model — compact and fast, surprisingly strong at math & code."},

    # ---- Image (text-to-image; priced per image) ----
    # Listed in the catalog/store but run on image endpoints, so NOT callable
    # through the text chat gateway or Fusion (kind="image").
    {"id": "qwen-image", "label": "Qwen-Image", "provider": "qwen", "tier": "Image", "kind": "image",
     "price": 0.02, "price_unit": "per image", "context": "—", "approx": True,
     "blurb": "Alibaba's Qwen-Image — text-to-image generation with notably crisp in-image text rendering."},
    {"id": "z-image-turbo", "label": "Z-Image-Turbo", "provider": "qwen", "tier": "Image", "kind": "image",
     "price": 0.01, "price_unit": "per image", "context": "—", "approx": True,
     "blurb": "Tongyi's Z-Image Turbo — a fast, efficient text-to-image model for quick iterations."},
    {"id": "seedream-3-0", "label": "Seedream 3.0", "provider": "bytedance", "tier": "Image", "kind": "image",
     "price": 0.03, "price_unit": "per image", "context": "—", "approx": True,
     "blurb": "ByteDance's Seedream — high-resolution, bilingual text-to-image generation."},
    {"id": "hunyuan-image", "label": "Hunyuan Image", "provider": "tencent", "tier": "Image", "kind": "image",
     "price": 0.03, "price_unit": "per image", "context": "—", "approx": True,
     "blurb": "Tencent's Hunyuan Image — text-to-image tuned for photoreal and design work."},
    {"id": "janus-pro-7b", "label": "Janus Pro 7B", "provider": "deepseek", "tier": "Image", "kind": "image",
     "price": 0.01, "price_unit": "per image", "context": "—", "approx": True,
     "blurb": "DeepSeek's Janus-Pro — a unified multimodal model that both understands and generates images."},

    # ---- Embeddings (turn text into vectors for search/RAG; per 1M input tokens) ----
    {"id": "text-embedding-3-small", "label": "Embedding 3 Small", "provider": "openai", "tier": "Embeddings", "kind": "embedding",
     "price": 0.02, "price_unit": "per 1M tokens", "context": "8K tokens", "approx": True,
     "blurb": "OpenAI's compact embedding model — cheap, fast vectors for semantic search and RAG."},
    {"id": "text-embedding-3-large", "label": "Embedding 3 Large", "provider": "openai", "tier": "Embeddings", "kind": "embedding",
     "price": 0.13, "price_unit": "per 1M tokens", "context": "8K tokens", "approx": True,
     "blurb": "OpenAI's highest-quality embedding model for the most accurate semantic search."},
    {"id": "gemini-embedding-001", "label": "Gemini Embedding", "provider": "google", "tier": "Embeddings", "kind": "embedding",
     "price": 0.15, "price_unit": "per 1M tokens", "context": "2K tokens", "approx": True,
     "blurb": "Google's text-embedding model — strong multilingual retrieval quality."},
    {"id": "text-embedding-v4", "label": "Qwen Embedding v4", "provider": "qwen", "tier": "Embeddings", "kind": "embedding",
     "price": 0.07, "price_unit": "per 1M tokens", "context": "8K tokens", "approx": True,
     "blurb": "Alibaba's Qwen text-embedding model — solid multilingual vectors at a low price."},

    # ---- Speech (text-to-speech; priced per 1M characters) ----
    {"id": "tts-1", "label": "TTS-1", "provider": "openai", "tier": "Speech", "kind": "speech",
     "price": 15.0, "price_unit": "per 1M characters", "context": "—", "approx": True,
     "blurb": "OpenAI's text-to-speech — natural, low-latency voices for real-time apps."},
    {"id": "tts-1-hd", "label": "TTS-1 HD", "provider": "openai", "tier": "Speech", "kind": "speech",
     "price": 30.0, "price_unit": "per 1M characters", "context": "—", "approx": True,
     "blurb": "OpenAI's high-definition text-to-speech — richer, studio-quality audio."},
    {"id": "speech-01-turbo", "label": "MiniMax Speech-01", "provider": "minimax", "tier": "Speech", "kind": "speech",
     "price": 30.0, "price_unit": "per 1M characters", "context": "—", "approx": True,
     "blurb": "MiniMax's text-to-speech — expressive multilingual voices with voice cloning."},

    # ---- Transcription (speech-to-text; priced per minute of audio) ----
    {"id": "whisper-1", "label": "Whisper", "provider": "openai", "tier": "Transcription", "kind": "transcription",
     "price": 0.006, "price_unit": "per minute", "context": "—", "approx": True,
     "blurb": "OpenAI's Whisper — robust, multilingual speech-to-text transcription."},
    {"id": "gpt-4o-transcribe", "label": "GPT-4o Transcribe", "provider": "openai", "tier": "Transcription", "kind": "transcription",
     "price": 0.006, "price_unit": "per minute", "context": "—", "approx": True,
     "blurb": "OpenAI's GPT-4o speech-to-text — higher accuracy on noisy, real-world audio."},
    {"id": "gpt-4o-mini-transcribe", "label": "GPT-4o mini Transcribe", "provider": "openai", "tier": "Transcription", "kind": "transcription",
     "price": 0.003, "price_unit": "per minute", "context": "—", "approx": True,
     "blurb": "A cheaper, faster GPT-4o transcription model for high-volume audio."},

    # ---- Video (text/image-to-video) ----
    {"id": "veo-3", "label": "Veo 3", "provider": "google", "tier": "Video", "kind": "video",
     "price": 0.40, "price_unit": "per second", "context": "—", "approx": True,
     "blurb": "Google's Veo 3 — high-fidelity text-to-video with synchronized audio."},
    {"id": "veo-3-fast", "label": "Veo 3 Fast", "provider": "google", "tier": "Video", "kind": "video",
     "price": 0.15, "price_unit": "per second", "context": "—", "approx": True,
     "blurb": "A faster, cheaper Veo 3 for quick text-to-video drafts and iteration."},
    {"id": "video-01", "label": "Hailuo Video-01", "provider": "minimax", "tier": "Video", "kind": "video",
     "price": 0.43, "price_unit": "per video", "context": "—", "approx": True,
     "blurb": "MiniMax's Hailuo — cinematic ~6-second text- and image-to-video clips."},
    {"id": "seedance-1-0-pro", "label": "Seedance 1.0 Pro", "provider": "bytedance", "tier": "Video", "kind": "video",
     "price": 0.05, "price_unit": "per second", "context": "—", "approx": True,
     "blurb": "ByteDance's Seedance — smooth, controllable text- and image-to-video generation."},

    # ---- Rerank (re-score search candidates by relevance; per 1M tokens) ----
    {"id": "gte-rerank-v2", "label": "GTE Rerank v2", "provider": "qwen", "tier": "Rerank", "kind": "rerank",
     "price": 0.08, "price_unit": "per 1M tokens", "context": "30K tokens", "approx": True,
     "blurb": "Alibaba's GTE reranker — sharpens RAG results by re-scoring candidates against the query."},
]
_VALID_MODEL_IDS = {m["id"] for m in MODEL_CHOICES}


def get_model(model_id):
    """The full catalog row for a model id (or None)."""
    for m in MODEL_CHOICES:
        if m["id"] == model_id:
            return m
    return None


def provider_for(model_id):
    """Which PROVIDERS entry a model runs on (defaults to anthropic)."""
    # The local model id isn't in MODEL_CHOICES, so it would default to anthropic and break
    # under LOCAL_MODEL_MODE — route it to the ollama (loopback) provider explicitly.
    if local_model_mode() and model_id == local_model_id():
        return "ollama"
    return (get_model(model_id) or {}).get("provider", "anthropic")


def model_kind(model_id):
    """A model's function: "text", "image", "embedding", "speech",
    "transcription", "video", or "rerank". Defaults to "text"."""
    return (get_model(model_id) or {}).get("kind", "text")


def is_text_model(model_id):
    """True for chat/LLM models — the only kind the chat gateway + Fusion serve."""
    return model_kind(model_id) == "text"


def is_image_model(model_id):
    """True for text-to-image models (kept for callers that special-case images)."""
    return model_kind(model_id) == "image"


def kind_label(kind):
    """The display label for a function tab (e.g. "text" -> "Text")."""
    return (KINDS.get(kind) or {}).get("label", (kind or "text").title())

# ---------------------------------------------------------------------------
# COST BUDGETING (the "CFO") — in MONEY (¥), not raw tokens
# ---------------------------------------------------------------------------
# These are just the STARTING values. Once the app runs, the Token Manager saves
# its state to memory_store/tokens.json and you manage everything live from the
# side menu in the UI (set the total budget, add per-task limits, reset usage).
#
# Every figure is Chinese Yuan (¥). As calls happen, each model's real per-token
# US$ price is converted to ¥ (at USD_TO_CNY) and added up, so budgets, warnings,
# and the hard cut-off are all about MONEY the team has spent — not token counts.
#
# Set a budget to None for "unlimited" (track spend but never cut off).
TOTAL_COST_BUDGET_CNY = 5000.0      # ¥ overall cap the whole team may spend
WARN_AT_REMAINING = 0.10            # warn once a budget drops below 10% remaining

# Named per-task budgets ("department spending limits"), in ¥. The agent-team
# workflow charges its spend to AGENT_TEAM_BUDGET below. Add more here or from the
# UI; future features (e.g. video generation) can charge their own budget.
COST_BUDGETS_CNY = {
    "agent_team": 2000.0,           # ¥ the document/agent workflow may spend
    # "video_generation": 1000.0,   # example of a future feature's budget
}
AGENT_TEAM_BUDGET = "agent_team"    # which budget the agent run draws from

# ---------------------------------------------------------------------------
# THE ORG CHART
# ---------------------------------------------------------------------------
# `reports_to: None` marks the top of the company (the CEO / Orchestrator).
# Everyone else is a worker the CEO can delegate to.
#
# COST TIP: every agent below defaults to OPUS (the smartest model) so the
# demo works great out of the box. To save money on high-volume runs, change
# a worker's "model" to SONNET or HAIKU — e.g. model=SONNET for the analysts.
AGENTS = [
    {
        "id": "ceo",
        "name": "Ava",
        "title": "Chief Executive Officer (Orchestrator)",
        "name_cn": "首席执行官",  # customer-facing Chinese role name (redesign B1)
        "reports_to": None,
        "model": OPUS,
        "effort": "high",
        "system_prompt": (
            "You are the CEO of a small, efficient company of AI specialists. "
            "Your job is to understand the user's goal, break it into a clear plan, "
            "delegate each part to the right specialist on your team, and then "
            "synthesize everyone's work into one polished, decision-ready deliverable. "
            "You are concise, organized, and outcome-focused. You never do the detailed "
            "work yourself when a specialist is better suited — you direct and synthesize."
        ),
    },
    {
        "id": "document_analyst",
        "name": "Diego",
        "title": "Document Analyst",
        "name_cn": "文档分析师",
        "reports_to": "ceo",
        "model": OPUS,  # cost tip: SONNET works well here too
        "effort": "high",
        # "web_search" lets this agent search the live web (Anthropic's server-side
        # tool) to verify facts or pull in current information. It is a capability,
        # not a file builder — base_agent wires it up automatically (config-driven).
        "tools": ["web_search"],
        "system_prompt": (
            "You are a meticulous Document Analyst. You read source documents the user "
            "uploaded and extract what matters: key facts, figures, names, dates, "
            "obligations, risks, and anything surprising. You quote specifics and cite "
            "which document each point came from. You never invent details that are not "
            "in the source material. If something is unclear or missing, you say so plainly."
        ),
    },
    {
        "id": "records_manager",
        "name": "Nora",
        "title": "Records Manager",
        "name_cn": "资料管理员",
        "reports_to": "ceo",
        "model": OPUS,  # cost tip: SONNET works well here too
        "effort": "high",
        "system_prompt": (
            "You are the company's Records Manager (the document controller / registrar). "
            "Your main job is to MANAGE, UPDATE, and DISTRIBUTE company records and keep an "
            "organized, accurate track record of every document in storage.\n\n"
            "For the documents available to you, you:\n"
            "  - Maintain a clear RECORDS REGISTER — ideally a Markdown table with one row per "
            "file: document name, type/format, size, a one-line description of what it is, a "
            "sensible category (e.g. contract, invoice, policy, report, HR, financial), an owner "
            "if one is stated, and any date/version you can find.\n"
            "  - Check each record against internal and common REGULATORY standards: is it "
            "complete, clearly labeled, current (not an outdated version), and does it contain "
            "required fields? Flag anything missing, duplicated, outdated, mislabeled, or "
            "non-compliant, and say exactly how to fix it.\n"
            "  - Flag SENSITIVE content that needs controlled handling (personal data/PII, "
            "financial, confidential, or legal) and recommend appropriate access restrictions "
            "and a retention note.\n"
            "  - Recommend DISTRIBUTION: who should receive each record and any restrictions.\n\n"
            "You are also a dependable generalist: when the CEO or a teammate asks for help with "
            "their task, you assist using the documents you steward. You never invent document "
            "contents or metadata that are not present — if something is unknown, you say so."
        ),
    },
    {
        "id": "data_analyst",
        "name": "Mei",
        "title": "Data Analyst",
        "name_cn": "数据分析师",
        "reports_to": "ceo",
        "model": OPUS,  # cost tip: SONNET works well here too
        "effort": "high",
        # web research + the power to read the user's LOCAL files and hand back a REAL
        # .xlsx (new or edited in place). See deliverables.py LOCAL_TOOL_SPECS + drive_excel.
        "tools": ["list_dir", "read_file", "web_search", "create_excel_workbook", "drive_excel"],
        "system_prompt": (
            "You are a sharp Data Analyst. You turn raw facts and figures into insight: "
            "trends, totals, outliers, comparisons, and the 'so what' behind the numbers. "
            "You think step by step, show the key numbers that support each conclusion, and "
            "flag anything that looks risky, anomalous, or worth a human double-check. "
            "You are precise and never overstate what the data can support. "
            "When the user wants the numbers as a spreadsheet, data export, or table they can "
            "keep working with, CALL your create_excel_workbook tool to hand back a real .xlsx "
            "(one sheet per table, a header row, real numbers in the cells) instead of only "
            "describing it in text. "
            "When you are running on the user's OWN computer, their files live on their "
            "machine — not uploaded. Use list_dir to discover the folders/files and read_file "
            "to read a spreadsheet, CSV, or document into text. Analyze it, then hand results "
            "back EITHER as a brand-new workbook with create_excel_workbook OR by editing the "
            "user's existing spreadsheet with drive_excel (set cells, formulas, or append rows). "
            "drive_excel saves a '<name>_edited.xlsx' copy by default and leaves the original "
            "untouched unless the user explicitly asks to overwrite it. Only files inside "
            "folders the user has approved are accessible; if a path is denied, say so and ask "
            "the user to grant access rather than guessing another path. "
            # VERBATIM (Phase 3 learning loop) — do not edit. The pipeline parses this
            # JSON list back out (metrics_store.parse_unknown_terms) to ask the user.
            "If you encounter a column header, abbreviation, or term whose business "
            "meaning you cannot determine from context, do NOT guess. Add it to a JSON "
            'list at the very end of your output: UNKNOWN_TERMS: ["term1", "term2"].'
        ),
    },
    {
        "id": "report_writer",
        "name": "Sam",
        "title": "Report Writer",
        "name_cn": "报告撰写人",
        "reports_to": "ceo",
        "model": OPUS,
        "effort": "high",
        "tools": ["web_search"],  # can fact-check and pull current context while writing
        # The full report body PLUS the mandatory SOURCES_JSON trail (30+ rows on a
        # rich dataset) does not fit the 8000-token default — a clipped array is what
        # produced the 「本次未能生成数据溯源表」 banner on real runs (2026-06-11).
        "max_tokens": 16000,
        "system_prompt": (
            "You are an executive Report Writer. You take the analysts' findings and write "
            "a clear, well-structured report for a busy decision-maker. You lead with a short "
            "executive summary, then organized sections with headings and bullet points, and "
            "end with concrete recommended next steps. You write in plain, confident English "
            "and use Markdown formatting (##, **bold**, - bullets) for readability. "
            "When historical memory or pre-computed trends are provided, your report MUST "
            "include a section '与历史对比' that references at least the week-over-week change "
            "of the top 3 metrics. Never state a trend that is not in the pre-computed list. "
            # VERBATIM (Phase 6 verification trail) — do not edit. The pipeline parses the
            # SOURCES_JSON block back out (agent_team.verification.split_report_and_sources)
            # to render a 数据来源与核验 table on the result page and in the PDF.
            "VERIFICATION TRAIL — this is mandatory. EVERY concrete number in the report body "
            "(金额、百分比、计数、环比/同比) MUST carry an inline source anchor immediately after "
            "it, written as a fullwidth-bracketed integer: 【1】, 【2】, 【3】… Number the anchors "
            "sequentially in order of first appearance. After the report is completely finished, "
            "output ONE final line that begins exactly with 'SOURCES_JSON:' followed by a single "
            "JSON array — one object per anchor — with this exact shape: "
            'SOURCES_JSON: [{"anchor": 1, "claim_cn": "本周销售额 ¥1,234,567", '
            '"source_file": "5月销售.xlsx", "source_detail": "Sheet \'5月\' rows 2-145, column 销售额", '
            '"computed_by": "data_analyst"}]. '
            "Each object's 'anchor' is the integer inside its 【】 marker; 'claim_cn' restates the "
            "number and what it measures; 'source_file'/'source_detail' point to the exact place the "
            "figure came from. 'computed_by' MUST be \"trend_engine\" for any number you took from the "
            "PRE-COMPUTED 系统计算的趋势与异常 block (those exact trend/anomaly figures), and "
            "\"data_analyst\" for every other number. Output nothing after the SOURCES_JSON line."
        ),
    },
    {
        "id": "media_producer",
        "name": "Leo",
        "title": "Creative & Media Producer",
        "name_cn": "媒体制作人",
        "reports_to": "ceo",
        "model": OPUS,  # cost tip: SONNET works well here too
        "effort": "high",
        # TOOLS the agent may call to produce REAL, downloadable files. These names
        # map to builders in agent_team/deliverables.py. Give any other agent this
        # same list (or a subset) and it gains the same file-making power — the
        # framework wires it up automatically (config-driven).
        "tools": ["create_slide_deck", "create_pdf_document",
                  "create_word_document", "create_svg_image",
                  "create_excel_workbook"],
        "system_prompt": (
            "You are the team's Creative & Media Producer. You turn the team's findings into "
            "finished creative deliverables that the user asked for.\n\n"
            "You can create REAL, downloadable files by CALLING YOUR TOOLS — do this whenever the "
            "user wants that artifact, rather than only describing it:\n"
            "  - create_slide_deck  -> a real PowerPoint (.pptx) with a title slide + content slides.\n"
            "  - create_pdf_document -> a real PDF report/brief/one-pager.\n"
            "  - create_word_document -> a real editable Word (.docx) document.\n"
            "  - create_svg_image   -> a real vector image (.svg): logos, diagrams, charts, icons, "
            "infographics you can describe precisely in markup.\n"
            "  - create_excel_workbook -> a real Excel (.xlsx) workbook of tabular data: one or more "
            "sheets, each with a header row and data rows (put real numbers in numeric cells so "
            "totals work).\n"
            "Call a tool once per file; you may create several. Fill each with genuinely useful, "
            "specific content drawn from your teammates' work and the source documents — real bullet "
            "copy, real section text, real SVG markup — never placeholder text.\n\n"
            "Be honest about limits. You CANNOT paint photographic/raster images or render video. For "
            "those, don't fake a file — instead write a precise, copy-paste-ready BRIEF in your text:\n"
            "  - IMAGES (photographic): a generation prompt (subject, style, composition, colour "
            "palette, aspect ratio, negative prompts) + alt-text + the recommended image tool.\n"
            "  - VIDEO: a shot-by-shot storyboard and script (scene, on-screen text, voiceover, "
            "duration, transitions) + the recommended video tool.\n\n"
            "After creating files, briefly summarize in text what you produced (each filename + a "
            "one-line description) and include any image/video briefs. You read everything your "
            "teammates have produced (shared memory) and build directly on it. Never invent facts "
            "that are not in the team's work or the source documents."
        ),
    },
]

# ---------------------------------------------------------------------------
# Helpers (you usually don't need to touch these)
# ---------------------------------------------------------------------------
def get_agent(agent_id):
    for a in AGENTS:
        if a["id"] == agent_id:
            return a
    return None


def get_ceo():
    for a in AGENTS:
        if a["reports_to"] is None:
            return a
    raise ValueError("No CEO found: one agent must have reports_to=None.")


def get_workers():
    return [a for a in AGENTS if a["reports_to"] is not None]


# Ids of agents merged in at runtime by the local desktop daemon's registry loader
# (agent_team/runner/registry.py). Tracked so register_runtime_agents() can prune its
# OWN entries on a re-sync without ever touching the static built-in agents.
_REGISTERED_AGENT_IDS = set()


def register_runtime_agents(agent_dicts):
    """Merge externally-provided agent dicts (from the local registry loader) into
    AGENTS, upserting by id. Static built-in agents are NEVER removed or shadowed by
    this; only entries previously registered through here are replaced/pruned. Used
    ONLY by the local daemon (agent_team/runner) — the cloud server never calls it.
    A registered dict carries BEHAVIOR only (no model/¥); model/provider/¥ still
    resolve via effective_model() server-side. Idempotent + re-runnable. Returns the
    agent_ids now present from the registry."""
    static_ids = {a["id"] for a in AGENTS if a["id"] not in _REGISTERED_AGENT_IDS}
    incoming = {}
    for d in (agent_dicts or []):
        aid = d.get("id")
        if not aid or aid in static_ids:
            continue                      # never shadow a built-in agent
        incoming[aid] = d
    statics = [a for a in AGENTS if a["id"] not in _REGISTERED_AGENT_IDS]
    AGENTS[:] = statics + list(incoming.values())   # mutate in place — holders see it
    _REGISTERED_AGENT_IDS.clear()
    _REGISTERED_AGENT_IDS.update(incoming.keys())
    return list(incoming.keys())


# ---------------------------------------------------------------------------
# Per-agent MODEL OVERRIDES (set live from the UI dropdowns)
# ---------------------------------------------------------------------------
# Each agent has a default "model" above. The UI lets you re-assign any agent to
# a different model without editing this file; those choices are stored here and
# WIN over the static default. base_agent / orchestrator read effective_model()
# at run time, so a change applies to the next job. app.py persists these to
# memory_store/model_overrides.json so they survive a restart.
MODEL_OVERRIDES = {}  # agent_id -> model id


def set_model_override(agent_id, model):
    """Assign an agent to a specific model. Raises ValueError if either is unknown."""
    if get_agent(agent_id) is None:
        raise ValueError(f"Unknown agent id: {agent_id!r}")
    if model not in _VALID_MODEL_IDS:
        raise ValueError(f"Unknown model: {model!r}")
    MODEL_OVERRIDES[agent_id] = model


def clear_model_override(agent_id):
    MODEL_OVERRIDES.pop(agent_id, None)


def team_model_preset():
    """The team's model preset: "domestic" (run on DOMESTIC_MODEL) or "" (off).

    Set TEAM_MODEL_PRESET=domestic in .env for a mainland-China deployment so the
    whole team — agents AND the internal utility calls (metric extraction, the
    verification audit, the Fusion fuser) — runs on a domestic model that needs no
    Anthropic key. Off by default, so local dev stays on Claude unchanged."""
    return _env("TEAM_MODEL_PRESET").lower()


def _preset_default(env_name, claude_default):
    """Resolve a utility-call model: explicit env override wins, else the domestic
    model when the domestic preset is on, else the given Claude default."""
    override = _env(env_name)
    if override:
        return override
    if team_model_preset() == "domestic":
        return DOMESTIC_MODEL
    return claude_default


def extraction_model():
    """Model for the cheap metric-extraction call (the company brain / Moat-1).
    EXTRACTION_MODEL env wins; else domestic when the preset is on; else HAIKU."""
    return _preset_default("EXTRACTION_MODEL", HAIKU)


def audit_model():
    """Model for the verification auto-audit (the trust layer).
    AUDIT_MODEL env wins; else domestic when the preset is on; else HAIKU."""
    return _preset_default("AUDIT_MODEL", HAIKU)


def fuser_fallback_model():
    """Model the Fusion feature falls back to as the fuser.
    FUSER_MODEL env wins; else domestic when the preset is on; else OPUS."""
    return _preset_default("FUSER_MODEL", OPUS)


# ---------------------------------------------------------------------------
# 对账 / 平台结算核对 — the settlement-reconciliation writer.
# A DELIBERATELY lean, single-agent flow: the deterministic settlement_engine does
# ALL arithmetic + matching; this ONE writer turns the engine's pre-computed facts
# into a one-page 对账报告 (it may only word the prose, never a number). It is kept
# OUT of the AGENTS org chart on purpose, so get_workers() (the report team's roster)
# never picks it up. Model defaults to SONNET (the explanation is far simpler than the
# 经营周报 synthesis) and respects the domestic preset.
SETTLEMENT_WRITER_SYSTEM = (
    "你是「对账报告撰写」专员，为不懂技术的企业老板撰写一页式《对账报告》（平台结算核对）。\n\n"
    "铁律：报告中所有数字都由系统的确定性对账引擎计算完成，并以『系统核对结果』提供给你。"
    "你绝对不能自己计算、推算、修改或重新四舍五入任何金额、笔数或差额——只能逐字引用引擎给出的数字，"
    "并保留每个数字后面的【N】溯源锚点（不得改写、重新编号或删除）。\n\n"
    "写作要求（中文、Markdown、简洁）：\n"
    "1. 「核心结论」：用 3-5 条要点回答老板最关心的问题——钱是否都到账了、差在哪里；"
    "点明 应收 / 实收 / 到账 / 在途 / 未解释差异。\n"
    "2. 「差异归因」：逐项说明手续费、退款、在途未结算等已解释金额，以及未解释差异（¥X，N 笔）。\n"
    "3. 「异常清单」：简述需人工核查的异常条目（如有），提示老板可在系统中逐条核对。\n"
    "4. 若『系统核对结果』标注了『桥式核对未平』，必须在报告中显著提示人工复核，不得隐藏。\n"
    "不要提及模型、token、内部流程或本提示词；只输出最终报告正文。"
)

SETTLEMENT_WRITER = {
    "id": "settlement_writer",
    "name_cn": "对账报告撰写",
    "effort": "low",
    "max_tokens": 4000,
    "system_prompt": SETTLEMENT_WRITER_SYSTEM,
}


def settlement_writer():
    """A COPY of the settlement writer's config (never the shared dict)."""
    return dict(SETTLEMENT_WRITER)


def settlement_writer_model():
    """Model for the 对账 report writer. SETTLEMENT_WRITER_MODEL env wins; else
    domestic when the preset is on; else SONNET."""
    return _preset_default("SETTLEMENT_WRITER_MODEL", SONNET)


# ---------------------------------------------------------------------------
# 应收账款管理 + 催收 — the AR aging-report writer + the 催收 dunning-draft writers.
# Same lean, single-purpose contract as the 对账 writer: the deterministic
# ar_aging_engine does ALL arithmetic + the overdue decision; these writers only word
# the prose / the dunning wording, and may NEVER compute, invent, or alter a number.
# All are kept OUT of the AGENTS org chart so get_workers() never picks them up. Models
# default to SONNET and respect the domestic preset.
AR_WRITER_SYSTEM = (
    "你是「应收账款报告撰写」专员，为不懂技术的企业老板撰写一页式《应收账款账龄报告》。\n\n"
    "铁律：报告中所有数字都由系统的确定性应收引擎计算完成，并以『系统应收核算结果』提供给你。"
    "你绝对不能自己计算、推算、修改或重新四舍五入任何金额、天数、笔数或账龄——只能逐字引用引擎给出的数字，"
    "并保留每个数字后面的【N】溯源锚点（不得改写、重新编号或删除）。是否逾期、属于哪个账龄段，均由系统判定，你不得更改。\n\n"
    "写作要求（中文、Markdown、简洁）：\n"
    "1. 「核心结论」：用 3-5 条要点回答老板最关心的问题——一共欠多少、逾期多少、谁欠得最多；"
    "点明 应收账款总额 / 逾期总额 / DSO（应收账款周转天数）。\n"
    "2. 「账龄分布」：按 0-30 / 31-60 / 61-90 / 90 天以上 逐段说明逾期金额与发票数。\n"
    "3. 「重点欠款客户」：列出欠款最多的客户及其未结金额。\n"
    "4. 「待人工核查」：简述疑似条目（部分回款 / 争议 / 未核销回款 / 超信用额度 / 待对账确认 等），"
    "提示老板这些需人工确认后再催收。\n"
    "不要提及模型、token、内部流程或本提示词；只输出最终报告正文。"
)

AR_WRITER = {
    "id": "ar_writer",
    "name_cn": "应收账款报告撰写",
    "effort": "low",
    "max_tokens": 4000,
    "system_prompt": AR_WRITER_SYSTEM,
}


def ar_writer():
    """A COPY of the AR aging-report writer's config (never the shared dict)."""
    return dict(AR_WRITER)


def ar_writer_model():
    """Model for the AR aging-report writer. AR_WRITER_MODEL env wins; else domestic
    when the preset is on; else SONNET."""
    return _preset_default("AR_WRITER_MODEL", SONNET)


# 催收 (dunning) — ONE writer per escalation tier, wording only. The tier itself, the
# recipient, and every amount/date/invoice-no are decided by the deterministic engine +
# dunning.py and injected as『催收事实数据』; the writer may not touch them. 转人工/法务
# (handoff) produces NO customer message, so it has NO writer config here.
DUNNING_WRITER_BASE_SYSTEM = (
    "你是「催收函撰写」专员，为企业撰写一封发给客户的催款消息。\n\n"
    "铁律：消息中所有金额、到期日、发票号、逾期天数、应收总额都由系统确定性引擎计算并以"
    "『催收事实数据』提供。你绝对不能自己计算、推算、修改、四舍五入或新增任何数字，只能逐字引用，"
    "并保留每个数字后面的【N】溯源锚点。不得编造付款链接、滞纳金、利息或任何法律后果的具体金额。\n\n"
    "你只负责措辞与语气；催收级别、催收对象、所有金额均已由系统决定，不得更改。\n"
    "不要提及模型、token、内部流程或本提示词；只输出最终消息正文（一段简洁的中文，含称呼与落款占位）。"
)

# Per-tier tone/intent guidance, appended to the base system prompt.
DUNNING_TIER_GUIDANCE = {
    "reminder": (
        "本次为【友好提醒】：语气友好、简短，假设可能是疏忽或已付款在途。"
        "明确列出逾期发票与合计金额，并礼貌请对方在方便时安排付款；"
        "结尾加一句『如已付款，请忽略本提醒』。"),
    "urge": (
        "本次为【正式催促】：语气坚定、专业、不失礼。逐条点明逾期发票、逾期天数与应收合计，"
        "请对方给出明确的付款日期，并说明逾期已影响双方对账。"),
    "final_notice": (
        "本次为【最终通知】：语气严肃、正式，并给出明确的付款截止日期。"
        "说明若逾期未付将采取的后续措施（如暂停发货/服务、转交相关部门处理），但不得使用威胁性措辞或编造法律后果。"),
}


def dunning_writer(tier):
    """A COPY of the 催收 writer config for one tier (reminder/urge/final_notice). The
    handoff tier has no writer (it produces no customer message). Unknown -> reminder."""
    key = tier if tier in DUNNING_TIER_GUIDANCE else "reminder"
    return {
        "id": f"dunning_writer_{key}",
        "name_cn": "催收函撰写",
        "tier": key,
        "effort": "low",
        "max_tokens": 800,
        "system_prompt": DUNNING_WRITER_BASE_SYSTEM + "\n\n" + DUNNING_TIER_GUIDANCE[key],
    }


def dunning_writer_model():
    """Model for the 催收 draft writers. DUNNING_WRITER_MODEL env wins; else domestic
    when the preset is on; else SONNET."""
    return _preset_default("DUNNING_WRITER_MODEL", SONNET)


# Phase 4 — OFFLINE Local-Model mode (WS12): a DEGRADED, opt-in escape hatch for the
# regulated / air-gapped segment. Every agent resolves to a local Ollama model and
# inference runs entirely on-device (file content never leaves the machine), at the cost
# of: no cloud models, no Anthropic-only tools (LLM file builders / web_search), and no
# ¥/COGS metering (cost_cny is 0 because the local model isn't in MODEL_CHOICES — that is
# the deliberate "¥0 marginal, zero COGS visibility" behaviour). NEVER the default.
LOCAL_MODEL_DEFAULT = "qwen2.5:7b-instruct"   # a sensible small local default (placeholder)


def local_model_mode():
    """True only when LOCAL_MODEL_MODE is explicitly enabled (the offline, degraded path)."""
    val = _env("LOCAL_MODEL_MODE")
    return bool(val) and val.lower() in _TRUE


def local_model_id():
    """The local model id every agent resolves to under LOCAL_MODEL_MODE (LOCAL_MODEL_ID
    env wins, else LOCAL_MODEL_DEFAULT)."""
    return _env("LOCAL_MODEL_ID") or LOCAL_MODEL_DEFAULT


def effective_model(agent_id):
    """The model an agent will actually run on.

    Precedence: OFFLINE Local-Model mode (a hard environment constraint, not a preference)
    wins above everything; else a per-agent UI override; else, under the domestic preset,
    DOMESTIC_MODEL; else the agent's static default. A fresh prod volume has no overrides,
    so TEAM_MODEL_PRESET=domestic alone runs the whole team on the domestic model."""
    if local_model_mode():
        return local_model_id()         # highest precedence — offline degraded path
    if agent_id in MODEL_OVERRIDES:
        return MODEL_OVERRIDES[agent_id]
    if team_model_preset() == "domestic":
        return DOMESTIC_MODEL
    a = get_agent(agent_id)
    return (a or {}).get("model", OPUS)

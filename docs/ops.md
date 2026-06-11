# Operations runbook (Phase 7–10)

How to deploy, back up, restore, and monitor the AI Agent Team in production on a
mainland-China cloud VM (Alibaba ECS / Tencent CVM, or any Docker host).

The whole stack runs with **Docker Compose**: an nginx reverse proxy in front of
the Flask app (gunicorn), a separate scheduler worker, and PostgreSQL 16.

```
        ┌─────────┐     ┌──────────────┐     ┌────────────┐
client →│  nginx  │ →   │  app (gunicorn)│ →  │ postgres:16│
        │  :80    │     │   :8000        │     │  (volume)  │
        └─────────┘     └──────────────┘     └────────────┘
                          ┌──────────────┐         ▲
                          │  scheduler   │ ────────┘
                          │ (cron worker)│
                          └──────────────┘
```

- **app** — serves the website + API; runs `alembic upgrade head` on start
  (`RUN_MIGRATIONS=1`). It is the *only* migrator.
- **scheduler** — a separate process running the 定时任务 cron engine. It writes a
  liveness heartbeat every minute and guards every job with a Postgres advisory
  lock, so a job can never fire twice even during a rolling deploy.
- **postgres** — data lives in the named volume `pgdata` (survives `docker compose
  down`; only `down -v` deletes it).
- Generated reports, uploaded logos, the token ledger, etc. live in the named
  volumes `appdata` / `outputs` / `memory` / `backups`.

---

## 1. First deploy (copy-paste)

```bash
# 0. Install Docker Engine + the compose plugin (once per VM), e.g. on Debian/Ubuntu:
#    curl -fsSL https://get.docker.com | sh

# 1. Get the code onto the server, then enter the project directory.
cd /opt/company-agent          # wherever you cloned/copied it

# 2. Create your .env from the template and fill it in.
cp .env.example .env
nano .env                      # set ANTHROPIC_API_KEY, SECRET_KEY, POSTGRES_PASSWORD,
                               # APP_ENV=production  (see .env.example for the full list)

#    Generate a strong SECRET_KEY:
python3 -c "import secrets; print(secrets.token_hex(32))"

# 3. Build and start everything in the background.
docker compose up -d --build

# 4. Watch it come up; the app logs "migrations applied" then gunicorn boots.
docker compose logs -f app
```

Open `http://<server-ip>/`. To put a domain + HTTPS in front, terminate TLS at
your cloud load balancer, or add a `443` server block + certs to `nginx.conf`.

> **Note on `DATABASE_URL`:** in Compose it is composed automatically from
> `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` and points at the
> `postgres` service — so you only set the password in one place. To use a
> managed/external Postgres instead, set `DATABASE_URL` explicitly in the `app`
> and `scheduler` service `environment:` blocks.

**Migrating from an old JSON-based deploy?** Accounts now live in the database. The
app imports a legacy `memory_store/customers.json` automatically on first boot, so
usually there is nothing to do. To run it explicitly (e.g. seeding a fresh Postgres
during a cutover) — it is idempotent, so re-running imports nothing new:
```bash
docker compose exec app python scripts/migrate_json_accounts.py
# or point at a specific file:
docker compose exec app python scripts/migrate_json_accounts.py /path/to/customers.json
```

---

## 2. Acceptance tests

These are the five checks the Phase 7 work is verified against. Run them after a
deploy or any infra change.

### Test 1 — clean `docker compose up` serves, healthz green, a real run completes
```bash
docker compose up -d --build
# Wait ~30s, then:
curl -fsS http://localhost/healthz | python3 -m json.tool
#   → {"status":"ok","db":"ok","scheduler":"<iso>","scheduler_stale":false}
# Then log in via the UI and start a normal job; confirm it finishes and the
# report renders (this exercises the full agent pipeline against Postgres).
```

### Test 2 — `alembic upgrade head` on an empty DB == the models (no drift)
```bash
# Inside the app container, against the live (already-migrated) DB:
docker compose exec app alembic check
#   → "No new upgrade operations detected."   (i.e. the schema matches the models)
```
You can also prove it from scratch on SQLite locally:
```bash
COMPANY_BRAIN_DB=/tmp/fresh.db alembic upgrade head
COMPANY_BRAIN_DB=/tmp/fresh.db alembic check     # → No new upgrade operations detected.
```

### Test 3 — concurrent spend never drives a balance negative
```bash
pytest tests/test_phase7_concurrency.py -q
```

### Test 4 — kill the scheduler → healthz reports a stale heartbeat within 2 min
```bash
docker compose stop scheduler
sleep 130
curl -fsS http://localhost/healthz | python3 -m json.tool
#   → {"status":"ok", ... "scheduler_stale":true}   (db stays ok; site stays up)
docker compose start scheduler                       # bring it back
```

### Test 5 — backup then restore into a scratch DB → row counts match
See [§4 Restore](#4-restore-tested) below — that procedure *is* this test.

---

## 3. Backups

`scripts/backup.sh` dumps Postgres to `backups/YYYY-MM-DD.sql.gz`, keeps a weekly
snapshot under `backups/weekly/`, optionally uploads offsite via rclone, and prunes
to **14 daily + 8 weekly**.

Run it manually:
```bash
./scripts/backup.sh
ls -lh backups/
```

Schedule it (host crontab, as a docker-capable user; 02:30 daily):
```bash
crontab -e
# add:
30 2 * * *  /opt/company-agent/scripts/backup.sh >> /var/log/company-backup.log 2>&1
```

**Offsite copy (recommended).** Install + configure [rclone](https://rclone.org)
with a remote for your bucket (Alibaba OSS / Tencent COS expose an S3-compatible
endpoint), then set `BACKUP_REMOTE` in `.env`, e.g.:
```ini
BACKUP_REMOTE=oss:my-bucket/company-agent
```
Each run then also `rclone copy`s the dump to that bucket.

---

## 4. Restore (tested)

### 4a. Verify a backup into a SCRATCH database (this is acceptance Test 5)

Restores the latest dump into a throwaway database and compares row counts to the
live one. **Non-destructive** — it never touches the production DB.

```bash
LATEST=$(ls -1 backups/*.sql.gz | sort -r | head -1)
echo "Restoring $LATEST into scratch DB 'restore_check' …"

# 1. Create an empty scratch DB inside the postgres container.
docker compose exec -T postgres createdb -U "$POSTGRES_USER" restore_check

# 2. Load the dump into it.
gunzip -c "$LATEST" | docker compose exec -T postgres psql -U "$POSTGRES_USER" -d restore_check

# 3. Compare row counts, live vs. restored, for the core tables.
for t in accounts companies runs metrics scheduled_jobs deliveries feedback; do
  live=$(docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select count(*) from $t")
  rest=$(docker compose exec -T postgres psql -U "$POSTGRES_USER" -d restore_check  -tAc "select count(*) from $t")
  printf "%-16s live=%-8s restored=%-8s %s\n" "$t" "$live" "$rest" "$([ "$live" = "$rest" ] && echo OK || echo MISMATCH)"
done

# 4. Drop the scratch DB.
docker compose exec -T postgres dropdb -U "$POSTGRES_USER" restore_check
```
Every row should print `OK`.

### 4b. Restore into PRODUCTION (disaster recovery — destructive)

> This **replaces** the live database. Stop the app + scheduler first so nothing
> writes during the restore.

```bash
LATEST=$(ls -1 backups/*.sql.gz | sort -r | head -1)   # or a specific file

# 1. Stop the writers (keep postgres running).
docker compose stop app scheduler

# 2. Drop & recreate the database, then load the dump.
docker compose exec -T postgres dropdb   -U "$POSTGRES_USER" "$POSTGRES_DB"
docker compose exec -T postgres createdb -U "$POSTGRES_USER" "$POSTGRES_DB"
gunzip -c "$LATEST" | docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"

# 3. Bring the app back (it runs `alembic upgrade head`, a no-op if the dump is current).
docker compose up -d app scheduler
curl -fsS http://localhost/healthz
```

Restoring from an **offsite** copy: `rclone copy oss:my-bucket/company-agent/<file>.sql.gz ./backups/` first, then follow the steps above.

---

## 5. Monitoring & logs

- **Health probe:** `GET /healthz` →
  `{"status","db","scheduler","scheduler_stale"}`. Returns **200** while the DB is
  reachable, **503** when it is not. A stale scheduler shows `scheduler_stale:true`
  but keeps the probe green (a scheduler outage must not take the site down).
- **Structured logs:** the app emits one JSON line per request to stdout
  (`request_id`, `account_id`, `method`, `route`, `status`, `latency_ms`) — view
  with `docker compose logs -f app`. Passwords, API keys, webhook secrets, and file
  contents are **never** logged. Every response carries an `X-Request-Id` header to
  correlate a user report with its log line.
- **Error tracking (Sentry):** set `SENTRY_DSN` in `.env` to turn it on; leave it
  unset and Sentry stays completely disabled. PII (request bodies/cookies) is not
  sent.
- **Scheduler heartbeat:** the scheduler stamps `scheduler_heartbeat` every minute;
  `HEARTBEAT_STALE_SECS=120` is the staleness threshold reported by `/healthz`.

## 6. Day-2 commands

```bash
docker compose ps                      # what's running
docker compose logs -f app scheduler   # tail app + scheduler logs
docker compose restart app             # restart just the app
docker compose exec app alembic current# current migration revision
docker compose exec postgres psql -U "$POSTGRES_USER" "$POSTGRES_DB"   # a psql shell
docker compose down                    # stop everything (KEEPS data volumes)
docker compose down -v                 # stop AND delete data volumes (destroys the DB!)
```

## 7. Security & abuse hardening (Phase 8)

Everything here is **optional for local dev** — with all of it unset, SMS codes
print to the app log, files stay plaintext, rate limits use in-process memory, and
spend caps are off, so `python app.py` runs with zero extra config. The toggles
below are what you turn on in production. They change **nothing** for a legitimate
logged-in user beyond the signup / password-reset SMS step.

### 7.1 Environment (set these in `.env`)

```bash
# Phone (SMS) verification — signup + reset send a 6-digit code (5-min expiry).
PHONE_VERIFICATION=1                 # default on; 0 drops the SMS step (not advised)
SMS_PROVIDER=aliyun                  # else "console" (prints code to the log) when keys absent
ALIYUN_SMS_ACCESS_KEY_ID=...         # the four ALIYUN_SMS_* below are read from env only
ALIYUN_SMS_ACCESS_KEY_SECRET=...
ALIYUN_SMS_SIGN_NAME=你的短信签名
ALIYUN_SMS_TEMPLATE_CODE=SMS_123456789

# Encryption at rest (Fernet) — uploads + generated report PDFs are ciphertext on disk.
# Comma-separated, NEWEST first; rotate by prepending a new key (old files still decrypt).
#   Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FERNET_KEYS=newest-key,previous-key

# Platform spend circuit breaker (CRITICAL) — rolling ¥ WE spend on upstream keys.
PLATFORM_HOURLY_CAP_YUAN=200         # 0 (default) = no cap
PLATFORM_DAILY_CAP_YUAN=2000         # 0 (default) = no cap
ACCOUNT_DAILY_CAP_YUAN=200           # per-account/day ceiling (per-row override exists)
ADMIN_EMAILS=you@yourcompany.com     # who gets the alert + can reset the breaker
ADMIN_ALERT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...

# Rate limiting — auth per IP, run-start per account. memory:// is correct for ONE
# process; point at redis for multi-worker gunicorn so the counters are shared.
RATELIMIT_ENABLED=1                  # default on; 0 disables entirely
RATELIMIT_STORAGE_URI=redis://redis:6379
AUTH_RATE_LIMIT=5 per minute
RUN_RATE_LIMIT=10 per hour
```

### 7.2 The spend circuit breaker (and how to reset it)

When rolling upstream spend crosses `PLATFORM_HOURLY_CAP_YUAN` or
`PLATFORM_DAILY_CAP_YUAN`, the app **latches tripped**: new runs are refused with
HTTP 503 and the body `{"error":"系统繁忙，请稍后再试"}`, and **one** admin alert
fires (email to `ADMIN_EMAILS` + a POST to `ADMIN_ALERT_WEBHOOK`). It stays tripped
across restarts (the latch is persisted) until an admin clears it.

```bash
# An admin (an ADMIN_EMAILS address, logged in) opens the breaker page:
#   GET  /admin/circuit-breaker   → current state (TRIPPED / OK) + a reset button
#   POST /admin/circuit-breaker   action=reset   → clears the latch; service resumes
# The reset stamps a "cleared_at"; only spend AFTER that moment counts toward the caps,
# so a reset genuinely restores service rather than immediately re-tripping.
```

Per-account abuse is bounded separately by `ACCOUNT_DAILY_CAP_YUAN` (default ¥200/day;
a single account can be raised or lowered via its `accounts.daily_cap_yuan` column)
— an over-cap account is refused **without** tripping the platform-wide breaker.

### 7.3 Audit log

Security-relevant actions are written to the `audit_log` table (best-effort; a
logging failure never blocks the request). Actions recorded: **login**,
**password_change**, **topup**, **download**, **admin_action**. Each row stores the
account id, action, client IP, a short detail string (truncated to 255 chars), and a
timestamp — **never** a password, API key, file content, or webhook secret.

```bash
# Inspect recent security events:
docker compose exec postgres psql -U "$POSTGRES_USER" "$POSTGRES_DB" \
  -c "SELECT created_at, account_id, action, ip, detail FROM audit_log ORDER BY id DESC LIMIT 50;"
```

### 7.4 Boot-time secret scan

On startup the app walks its served static files and refuses to boot (in production)
if any **known secret value** from the environment appears in one — a guard against
a key being accidentally baked into a shipped asset. It reports only the file path
and the offending env var **name** (e.g. `frontend/app.js←$ANTHROPIC_API_KEY`), never
the value. In dev it warns instead of exiting.

### 7.5 Verify it (acceptance tests)

```bash
.venv/bin/python -m pytest tests/test_phase8_security.py -q
# 5 passed — (1) signup needs a valid SMS code & the code burns after the attempt cap,
# (2) the 11th run-start/hour → 429 + Chinese message, (3) over-cap spend trips the
# breaker + fires one alert + an admin reset restores service, (4) an upload is
# ciphertext on disk but downloads with the original checksum, (5) audit rows are
# written for login + topup + download.
```

## 8. Payments, subscriptions & fapiao (Phase 9)

Real Alipay / WeChat Pay replace the demo top-up. **Zero-config dev:** with no
provider configured, the built-in **Mock provider** takes over — orders "pay" at
a local fake cashier (`/pay/mock/<order_no>`) with HMAC-signed notifies, so the
whole money path (orders → verified notify → credit, subscriptions, refunds,
fapiao, reconciliation) works locally and in CI with zero real money. The moment
a real provider is configured, mock drops out of the available list and its
notify endpoint + cashier go dead automatically.

### 8.1 Environment (set these in `.env`)

```bash
APP_BASE_URL=https://your-domain.example.com   # notify_url base — must be reachable FROM the providers

# Alipay (open platform, RSA2 — keys accept full PEM or the bare base64 body)
ALIPAY_APP_ID=2021000000000000
ALIPAY_APP_PRIVATE_KEY=MIIEv...            # your APPLICATION private key
ALIPAY_PUBLIC_KEY=MIIBIj...                # ALIPAY's public key (verifies notifies)

# WeChat Pay (APIv3, native QR)
WECHATPAY_MCHID=1900000000
WECHATPAY_APPID=wx0000000000000000
WECHATPAY_API_V3_KEY=your-32-char-apiv3-key
WECHATPAY_SERIAL_NO=your-merchant-cert-serial
WECHATPAY_PRIVATE_KEY=MIIEv...             # merchant private key
WECHATPAY_PLATFORM_CERT=MIID...            # WeChat Pay PLATFORM certificate

# Subscriptions (defaults shown) — user-confirmed renewal, never auto-charge
SUB_RENEWAL_LEAD_DAYS=3       # renewal order + pay-link notice at T-3d
SUB_GRACE_DAYS=3              # features keep working this long after expiry
SUB_PAST_DUE_CANCEL_DAYS=30   # locked (past_due) this long, then canceled

# Reconciliation — where ops drops the providers' daily bill CSVs
BILLS_DIR=/srv/company-agent/data/bills
```

Plan prices/quotas/seats live in `agent_team/config.py` `PLANS`
(免费版 10 报告/月 · 基础版 ¥199 100/月 · 专业版 ¥599 500/月).

### 8.2 How the money path stays safe

- **Signature first.** `/pay/notify/alipay` (RSA2 vs the Alipay public key) and
  `/pay/notify/wechat` (platform-cert signature + AES-GCM resource decryption)
  verify BEFORE anything else; an unverifiable notify is a 400 no-op.
- **Idempotent paid transition.** `created→paid` happens in ONE transaction that
  also credits the wallet / activates the subscription. A replayed notify sees
  `paid` and answers success WITHOUT crediting again.
- **Amounts are never client data.** The paid amount comes only from the
  verified notify payload and must equal the stored order amount to the fen;
  a mismatch refuses the notify and logs a warning.
- **Subscription prices are server-side** (`PLANS`); `/api/pay/create` ignores
  any client-sent amount for subscription orders.

### 8.3 Subscriptions (China-style, user-confirmed renewal)

Daily tick (09:30 Beijing, in the scheduler container AND in-process dev): at
**T-3d** it creates ONE renewal order and notifies the user by email + WeChat
webhook with a pay link (no auto-charge); past expiry there are
`SUB_GRACE_DAYS` of grace, then **past_due** — run-starts are locked with a
clear message while downloads/data export stay open — then canceled after
`SUB_PAST_DUE_CANCEL_DAYS`. Paying the renewal order at any point reactivates
immediately. The plan gate also enforces each plan's monthly report quota.

### 8.4 Refunds (admin-only)

`/admin/orders` ($ADMIN_EMAILS gate) lists every order with a full/partial
refund form on paid ones. A **top-up refund deducts the wallet** — and is
REFUSED with a clear error when the credit was already spent below the refund
amount. Every refund calls the provider's refund API first and writes an
`admin_action` audit row.

### 8.5 Reconciliation

Nightly (03:30 Beijing) the scheduler parses each provider's bill CSV from
`BILLS_DIR` (`<provider>-<YYYY-MM-DD>.csv`, Beijing bill date; Alipay GBK and
WeChat backtick formats both parse) and compares it against `orders`. Mismatch
kinds: `missing_order`, `amount_mismatch`, `status_mismatch`,
`missing_in_bill`. Any mismatch fires an admin alert; results live on
`/admin/reconciliation`, which also has a "scan now" button (last 7 days).

### 8.6 Fapiao (发票)

Customers hit 申请发票 on a paid order (抬头 + 税号 + email). The bookkeeper
works from `/admin/fapiao` (+ `export.csv` for 代记账), then marks each row
issued — which emails the customer — or rejected with a note. Actual issuance
is manual at this stage; `payments.issue_electronic_fapiao` is the stable stub
a future e-fapiao API slots into.

### 8.7 Verify it (acceptance tests, MockProvider)

```bash
.venv/bin/python -m pytest tests/test_phase9_payments.py -q
# 5 passed — (1) a paid notify delivered 3× credits the balance exactly once,
# (2) a tampered signature → 400, order stays created, nothing credited,
# (3) subscription lifecycle: pay → active → T-3d renewal order + notice →
#     unpaid → past_due lock → pay → active again,
# (4) a top-up refund deducts the balance and is BLOCKED with a clear error
#     once the credit was spent below the refund amount,
# (5) a fapiao request appears in the admin CSV export with the correct 税号.
```

## 9. Onboarding, support & ops dashboard (Phase 10)

Goal: a stranger signs up and reaches their "first wow" (a finished report)
without you, and you see the business in one screen.

### 9.1 Environment

Everything in this phase works with **zero extra config**. Two optional knobs
for the 联系支持 popup:

```ini
# The floating support button's contacts (both optional; see .env.example):
# SUPPORT_EMAIL falls back to the first ADMIN_EMAILS entry.
SUPPORT_EMAIL=support@your-domain.com
SUPPORT_WECHAT_QR_URL=https://your-cdn/wechat-support-qr.png
```

### 9.2 The onboarding wizard

First login auto-opens a 5-step wizard (skippable per step, resumable — the
state is derived from the `onboarding_events` log, so it survives restarts and
devices):

1. **公司信息** — name + industry; the industry selects the report template
   pack every later run uses (via the company profile).
2. **上传数据** — one Excel/CSV into a persisted upload batch, or
   **用示例数据体验**: the bundled 12-week dataset at
   `assets/demo/示例销售数据.csv` is staged exactly like a real upload
   (encrypted at rest; scheduled jobs read it too).
3. **KPI 列名确认** — deterministic guesses from the file header (rule table in
   `agent_team/onboarding.py`, no LLM call), one click saves them as the
   standing `kpi_mappings` in the company brain.
4. **生成第一份报告** — the SAME gated + billed run pipeline as the Chat bot
   (`balance → breaker → plan gate`), with the live agent feed; the step-4
   funnel event is only recorded when the run SUCCEEDS.
5. **每周定时** — one click creates (idempotently) the `0 8 * * 1` weekly job
   with the chosen delivery channels.

Funnel events land in `onboarding_events` (one row per account/step/action);
the day-3 nudge email deep-links to `/?onboarding=demo`, which reopens the
wizard on the demo step.

### 9.3 Help center, changelog & support

- `/help` → `docs/help/*.md` (快速上手 / 常见问题 / 数据安全说明 / 发票说明),
  re-read per request — edit the markdown, refresh, done. Rendering is the
  escape-first mini-renderer in `agent_team/helpdocs.py` (no new deps).
- `/changelog` → `CHANGELOG.md`. Add a release as a new `## ` heading at the
  top; every account then sees the **新功能** badge in the menu until they view
  the page (per-account `changelog_seen` row clears it).
- 联系支持 floats on the app and the help pages: 企业微信 QR + mailto + a
  message form. Every message is logged to `support_requests` (visible at the
  bottom of `/admin`) and best-effort fanned out via the admin alert channels.

### 9.4 The ops dashboard (/admin)

`ADMIN_EMAILS`-gated (everyone else gets 403). All numbers come from
`admin/queries.py` — one reviewable file, unit-tested against hand-computed
seeds, portable SQL (SQLite dev / Postgres prod):

- **MRR** (Σ active subscriptions × plan price), **churn** (canceled this month
  / active at month start), **activation** (% of 30d signups with a succeeded
  run within 7 days), signups/day sparkline (inline SVG, no chart libs);
- **weekly retention cohorts** (signup week × weeks-with-a-run matrix);
- **per-account economics** (90d): paid-order revenue net of refunds vs the
  account's `runs.total_cost_yuan` upstream model cost — rows with
  **cost > 60% of revenue** are flagged red;
- the **onboarding funnel** (% of wizard-opening accounts completing each step)
  and the latest support requests.

### 9.5 Lifecycle emails

Three touchpoints (plain, short Chinese; sent through the existing delivery
layer; each deduped via `lifecycle_events`, so re-runs/replays NEVER spam):

| When                          | What                                         |
|-------------------------------|----------------------------------------------|
| sign-up                       | welcome + 3-step quickstart                  |
| day 3, still no finished report | "还没生成第一份报告？" + demo deep link     |
| subscription order paid       | receipt (套餐 / ¥ / 订单号 / 有效期)          |

The nudge runs as the `lifecycle-daily` scheduler system job (10:00 Beijing,
registered in both `app.py` and the dedicated `scheduler.py` worker). It only
considers accounts 3–14 days old, so enabling the feature never blasts the
whole historical user base.

### 9.6 Verify it (acceptance tests)

```bash
.venv/bin/python -m pytest tests/test_phase10_onboarding.py -q
# 5 passed — (1) a fresh account reaches a FINISHED first report through the
#     wizard on demo data alone (no upload), ending with the Monday-08:00 job,
# (2) onboarding_events records each step + the admin funnel percentages match
#     hand-computed values (and render on /admin),
# (3) dashboard numbers (signups, activation, cohorts, MRR, churn, economics
#     + the >60% flag) match a hand-computed 3-account seed,
# (4) non-admin → /admin 403; the changelog badge clears after viewing,
# (5) welcome / day-3 nudge / receipt each send exactly once.
```

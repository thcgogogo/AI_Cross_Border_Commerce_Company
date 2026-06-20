"""
scheduler.py — Standalone "定时任务" worker process (Phase 7).

In production the cron engine runs as its OWN container/process, separate from the
web app (gunicorn). This entrypoint:

  * waits for the database schema to exist (the app container runs
    `alembic upgrade head` on Postgres; this worker may start in parallel);
  * runs the SAME ReportScheduler used in dev, whose every fire goes through the
    normal billing pipeline (app._execute_scheduled_job), so a scheduled run is
    metered against the account's ¥ balance exactly like an interactive one;
  * stamps a liveness heartbeat once a minute (read by GET /healthz); and
  * re-syncs its job set from the DB once a minute (the web process edits jobs;
    this worker polls to pick those edits up, since its in-process reload() never
    reaches a separate container).

Double-fire safety has TWO guards:
  1. APScheduler itself (coalesce + max_instances=1) WITHIN this process; and
  2. a Postgres SESSION-LEVEL ADVISORY LOCK keyed on the job id around every fire,
     so even two scheduler processes (e.g. during a rolling deploy) can never run
     the same job at the same instant. On SQLite (local dev) there are no advisory
     locks and only one process, so the wrapper is a transparent pass-through.

Run it with:  python scheduler.py
(In docker-compose this is the `scheduler` service; the web app sets RUN_SCHEDULER=0
so it does NOT also start an in-process scheduler.)
"""

import hashlib
import signal
import sys
import threading
import time

from dotenv import load_dotenv
from sqlalchemy import text

# Make .env authoritative for this process too (same rationale as app.py).
load_dotenv(override=True)

from agent_team import config, db, metrics_store  # noqa: E402
from agent_team.scheduler import ReportScheduler  # noqa: E402

HEARTBEAT_INTERVAL_SECS = 60
SCHEMA_WAIT_TIMEOUT_SECS = 300
SCHEMA_POLL_SECS = 3

# Set by the signal handlers; every blocking wait watches it for prompt shutdown.
_stop = threading.Event()


def _log(msg):
    print(f"  [scheduler] {msg}", flush=True)


def _is_postgres():
    try:
        return db.engine.dialect.name == "postgresql"
    except Exception:
        return False


def _advisory_key(job_id):
    """A stable signed 64-bit int for pg_try_advisory_lock(bigint), derived from
    the job id so it is well-defined whatever the id's runtime type."""
    digest = hashlib.sha256(f"scheduled_job:{job_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _wait_for_schema():
    """Block until the engine is up AND the scheduled_jobs table exists (i.e. the
    migration has run). Returns True when ready, False on timeout (caller exits
    non-zero so the container is restarted). On SQLite the table is created on the
    first init_db(), so this returns almost immediately."""
    deadline = time.time() + SCHEMA_WAIT_TIMEOUT_SECS
    while not _stop.is_set():
        try:
            db.init_db()
            with db.session_scope() as s:
                s.execute(text("SELECT 1 FROM scheduled_jobs LIMIT 1"))
            return True
        except Exception as e:
            if time.time() >= deadline:
                _log(f"database schema not ready after {SCHEMA_WAIT_TIMEOUT_SECS}s "
                     f"({type(e).__name__}: {e}); exiting for restart.")
                return False
            _log("waiting for database schema (alembic upgrade head) …")
            _stop.wait(SCHEMA_POLL_SECS)
    return False


def _make_locked_executor(real_executor):
    """Wrap the pipeline executor in a Postgres session-level advisory lock so the
    same job can never run twice concurrently across processes. On SQLite there is
    no advisory lock (single process), so this is a transparent pass-through."""
    def _run(job):
        job_id = job.get("id")
        if not _is_postgres():
            return real_executor(job)
        key = _advisory_key(job_id)
        try:
            conn = db.engine.connect()
        except Exception as e:
            _log(f"job {job_id}: could not open lock connection "
                 f"({type(e).__name__}); skipping this fire.")
            return
        try:
            got = bool(conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar())
            if not got:
                # Another scheduler process already holds this job's lock — skip,
                # rather than risk a double fire / double bill.
                _log(f"job {job_id}: lock held by another scheduler; skipping.")
                return
            try:
                real_executor(job)
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()
        finally:
            conn.close()
    return _run


def _install_signal_handlers():
    def _handle(signum, _frame):
        _log(f"received signal {signum}; shutting down …")
        _stop.set()
    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def main():
    _install_signal_handlers()
    _log(f"starting (env={config.app_env()}).")
    if not _wait_for_schema():
        sys.exit(1)

    # Import the pipeline executor LAZILY: this boots the full app module (DB init,
    # pack validation, legacy import), which is why we wait for the schema first.
    from app import _execute_scheduled_job

    from agent_team import analytics, lifecycle, payments, reconciliation  # noqa: E402

    scheduler = ReportScheduler(_make_locked_executor(_execute_scheduled_job))
    # Phase 9/10 system jobs (cron in Asia/Shanghai): renewal notices at a humane
    # morning hour; reconciliation after providers cut the previous day's bill;
    # the day-3 onboarding nudge mid-morning.
    scheduler.add_system_job("billing-daily",
                             payments.daily_subscription_tick, "30 9 * * *")
    scheduler.add_system_job("reconciliation-nightly",
                             reconciliation.nightly_reconciliation_tick, "30 3 * * *")
    scheduler.add_system_job("lifecycle-daily",
                             lifecycle.daily_lifecycle_tick, "0 10 * * *")
    # Retention: prune analytics events older than 180 days (default of prune_events).
    scheduler.add_system_job("events-prune-nightly",
                             analytics.prune_events, "0 4 * * *")
    # Retention: prune dormant anonymous Fusion teaser counters (default 90 days).
    scheduler.add_system_job("anon-fusion-prune-nightly",
                             metrics_store.prune_anon_fusion_usage, "15 4 * * *")
    scheduler.start()
    _log(f"running ({len(scheduler.job_ids())} active job(s) loaded); "
         f"heartbeat every {HEARTBEAT_INTERVAL_SECS}s.")

    # Stamp once immediately so /healthz turns green without waiting a full minute.
    metrics_store.write_scheduler_heartbeat()
    try:
        while not _stop.is_set():
            _stop.wait(HEARTBEAT_INTERVAL_SECS)
            if _stop.is_set():
                break
            metrics_store.write_scheduler_heartbeat()
            # Pick up jobs the web process created / edited / deleted since last loop.
            try:
                scheduler.reload()
            except Exception as e:
                _log(f"reload failed ({type(e).__name__}: {e}); will retry next tick.")
    finally:
        scheduler.shutdown(wait=False)
        _log("stopped.")


if __name__ == "__main__":
    main()

"""
scheduler.py — The "定时任务" engine that makes the product show up on its own.

A thin manager around APScheduler's BackgroundScheduler. It deliberately owns NO
business logic: every cron fire just re-reads the job row and hands it to an
injected `executor` callback. That executor (app._execute_scheduled_job) runs the
normal pipeline through the SAME billing path as an interactive run, so a
scheduled fire is metered against the account's ¥ balance exactly like any run.

The scheduler reads the active scheduled_jobs rows at start() and again whenever
reload() is called (after a job is created / edited / deleted), translating each
row's 5-field cron_expr into an APScheduler CronTrigger. A malformed cron string
is skipped, never fatal — one bad row can't stop the others from firing.
"""

import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import metrics_store

# Weekly/monthly presets in the UI are written by a human in their own wall clock,
# so the scheduler runs in China time by default (override via ReportScheduler(...)).
DEFAULT_TIMEZONE = "Asia/Shanghai"


class ReportScheduler:
    """Owns one BackgroundScheduler and keeps its job set in sync with the active
    rows in the deliveries-backed scheduled_jobs table."""

    def __init__(self, executor, *, timezone=DEFAULT_TIMEZONE):
        """executor: a callable taking one job dict (as returned by
        metrics_store.get_scheduled_job / list_active_scheduled_jobs)."""
        self._executor = executor
        self._tz = timezone
        self._sched = BackgroundScheduler(timezone=timezone)
        self._lock = threading.Lock()
        self._started = False
        # Built-in maintenance jobs (Phase 9: the billing tick + nightly
        # reconciliation). Kept in their own dict because _load_all_locked wipes
        # the APScheduler job set on every reload — these are re-added after.
        self._system_jobs = {}

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        """Start the background thread (idempotent) and load the active jobs."""
        with self._lock:
            if not self._started:
                self._sched.start()
                self._started = True
            self._load_all_locked()

    def shutdown(self, wait=False):
        with self._lock:
            if self._started:
                self._sched.shutdown(wait=wait)
                self._started = False

    @property
    def running(self):
        return self._started

    # -- (re)loading jobs ---------------------------------------------------
    def reload(self):
        """Re-sync APScheduler's job set with the active DB rows. Safe to call
        after any create / update / delete of a scheduled job."""
        with self._lock:
            if not self._started:
                return
            self._load_all_locked()

    def _load_all_locked(self):
        # Drop every trigger we registered, then re-add the currently-active rows
        # AND the registered system jobs (the wipe takes those out too).
        self._sched.remove_all_jobs()
        try:
            jobs = metrics_store.list_active_scheduled_jobs()
        except Exception:
            jobs = []
        for job in jobs:
            self._add_locked(job)
        for name, (func, cron) in self._system_jobs.items():
            self._add_system_locked(name, func, cron)

    # -- built-in maintenance jobs (Phase 9) ---------------------------------
    def add_system_job(self, name, func, cron_expr):
        """Register a built-in job (e.g. the subscription billing tick) that
        SURVIVES reload(). Idempotent per `name`; safe before or after start()."""
        with self._lock:
            self._system_jobs[name] = (func, cron_expr)
            if self._started:
                self._add_system_locked(name, func, cron_expr)

    def _add_system_locked(self, name, func, cron_expr):
        try:
            trigger = CronTrigger.from_crontab(cron_expr, timezone=self._tz)
        except Exception:
            return  # a malformed cron must never crash the scheduler
        self._sched.add_job(
            func, trigger=trigger, id="sys-%s" % name, name=name,
            replace_existing=True, coalesce=True, max_instances=1,
            misfire_grace_time=3600)

    def _add_locked(self, job):
        cron = (job.get("cron_expr") or "").strip()
        job_id = job.get("id")
        if not cron or not job_id:
            return
        try:
            trigger = CronTrigger.from_crontab(cron, timezone=self._tz)
        except Exception:
            return  # a malformed cron must never crash the scheduler
        self._sched.add_job(
            self._fire, trigger=trigger, args=[job_id],
            id="job-%s" % job_id, name=job.get("name_cn") or ("job-%s" % job_id),
            replace_existing=True, coalesce=True, max_instances=1,
            misfire_grace_time=3600)

    # -- firing -------------------------------------------------------------
    def _fire(self, job_id):
        """APScheduler calls this when a trigger fires. Re-read the row fresh (so
        an edit between scheduling and firing is honoured), confirm it's still
        active, then hand it to the executor. Any error is swallowed: the executor
        records its own status, and one bad fire must not kill the scheduler."""
        try:
            job = metrics_store.get_scheduled_job(job_id)
        except Exception:
            job = None
        if not job or not job.get("active"):
            return
        try:
            self._executor(job)
        except Exception:
            pass

    # -- introspection (for tests / a future status endpoint) --------------
    def job_ids(self):
        """The APScheduler ids currently registered (e.g. ['job-1', 'job-2'])."""
        with self._lock:
            return [j.id for j in self._sched.get_jobs()]

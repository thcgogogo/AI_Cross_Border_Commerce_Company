"""
analytics.py — First-party, FOUNDER-ONLY operating-data capture (write side).

`track()` appends one `events` row for the head-of-funnel data that has no other
authoritative row: web page views, the anonymous Fusion front door, the pre-signup
funnel. It is BEST-EFFORT and NON-BLOCKING — any failure (DB down, bad input) is
swallowed and returns None, so analytics can never slow or break a real request
(same contract as metrics_store.write_audit). All counts are DB-backed (correct under
multiple gunicorn workers — no in-memory counters). IPs are stored HASHED, never raw.

Read-side aggregation lives in admin/queries.py (founder-gated). This module is
write-only plus a retention pruner.
"""

import hashlib
import json

from . import config, db
from .models import EVENT_KINDS, Event


def track(name, account_id=None, anon_id=None, path=None, ip_hash=None, props=None):
    """Append one analytics event. Returns the new row id, or None on any failure
    (invalid name, DB error). NEVER raises — recording must not affect the request."""
    if name not in EVENT_KINDS:
        return None
    try:
        with db.session_scope() as s:
            row = Event(
                name=name,
                account_id=(str(account_id)[:32] if account_id else None),
                anon_id=(str(anon_id)[:64] if anon_id else None),
                path=(str(path)[:255] if path else None),
                ip_hash=(str(ip_hash)[:64] if ip_hash else None),
                props=(json.dumps(props, ensure_ascii=False) if props else None),
            )
            s.add(row)
            s.flush()
            return row.id
    except Exception:
        return None


def hash_ip(ip):
    """Salted SHA-256 of a client IP for the events table — the raw IP is NEVER
    stored. Salt from config.analytics_ip_salt(). Returns None for a falsy IP."""
    if not ip:
        return None
    try:
        salt = config.analytics_ip_salt()
        return hashlib.sha256((salt + str(ip)).encode("utf-8")).hexdigest()
    except Exception:
        return None


def prune_events(days=180):
    """Delete events older than `days` (retention). Best-effort; returns the number
    of rows removed, or 0 on any failure. Called by the nightly scheduler tick."""
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(days=int(days))
    try:
        with db.session_scope() as s:
            return int(s.query(Event).filter(Event.created_at < cutoff)
                       .delete(synchronize_session=False) or 0)
    except Exception:
        return 0

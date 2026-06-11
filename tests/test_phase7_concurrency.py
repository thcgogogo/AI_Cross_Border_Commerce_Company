"""
test_phase7_concurrency.py — the double-spend guard (Phase 7, acceptance test 3).

Many threads bill the SAME account at the same instant, trying to spend far more
than its balance. Two things must hold:

  1. the balance is NEVER negative — every charge sees a clamped-at-zero balance; and
  2. no charge is LOST — lifetime spend and the request counter reflect *every*
     call, proving the read-modify-write in CustomerStore.record_usage is serialized
     (in-process RLock + SELECT … FOR UPDATE), not racing.

Runs against an isolated SQLite file (the `fresh_db` fixture). On SQLite the row
lock is a no-op, so this exercises the in-process RLock path — exactly the
concurrency a multi-threaded gunicorn worker faces. On Postgres the advisory row
lock adds the same guarantee across processes.
"""

import os
import sys
import threading

import pytest

# Make the project root importable no matter where pytest is launched from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import config, db  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402

MODEL = config.HAIKU
IN_TOK, OUT_TOK = 100_000, 20_000
N_THREADS = 32


@pytest.fixture
def fresh_db(tmp_path):
    """Point the company brain at a throwaway SQLite file for this one test."""
    db.init_db(str(tmp_path / "brain.db"))
    yield


def test_parallel_spend_never_goes_negative(fresh_db, tmp_path):
    store = CustomerStore(store_path=str(tmp_path / "customers.json"))

    # Derive the exact per-call debit (to the fen) via the public API: charge a
    # throwaway probe account once and read back its lifetime spend.
    probe = store.create_customer([], name="Probe", starting_balance=1000.0,
                                  email="probe@corp.com", password="pw")
    probe_id = probe["customer"]["id"]
    store.record_usage(probe_id, MODEL, IN_TOK, OUT_TOK)
    per_call = store.get_by_id(probe_id)["spent"]
    assert per_call > 0

    # The account under test gets room for ~4 calls; 32 concurrent charges blow
    # far past it, so it MUST clamp at zero.
    start_balance = round(per_call * 4, 2)
    created = store.create_customer([], name="Race", starting_balance=start_balance,
                                    email="race@corp.com", password="pw")
    cid = created["customer"]["id"]

    results = []
    errors = []
    # Release every thread at the same moment for maximal contention.
    gate = threading.Barrier(N_THREADS)

    def worker():
        try:
            gate.wait()
            billed = store.record_usage(cid, MODEL, IN_TOK, OUT_TOK)
            results.append(billed)
        except Exception as exc:  # noqa: BLE001 — surface any thread failure to the assert
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"worker threads raised: {errors!r}"
    assert len(results) == N_THREADS

    # (1) No charge ever observed a negative balance.
    for billed in results:
        assert billed["balance"] is not None
        assert billed["balance"] >= 0, f"balance went negative: {billed}"

    rec = store.get_by_id(cid)

    # (2) Final balance is clamped EXACTLY at zero (we massively overspent).
    assert rec["balance"] == 0.0

    # (3) No lost updates on the request counter — every call was applied.
    assert rec["requests"] == N_THREADS

    # (4) No lost updates on lifetime spend either: N × the per-call debit. Each
    # step adds a 2-dp value to a 2-dp running total, so there is no rounding drift.
    expected_spent = round(per_call * N_THREADS, 2)
    assert abs(rec["spent"] - expected_spent) < 0.005, (
        f"spent {rec['spent']} != expected {expected_spent} — a charge was lost")

"""
test_phase11_devices.py — WS03: the per-device token data layer (Phase 1 of the
desktop "Tangent Agent" effort).

Covers the DeviceTokenStore secret discipline (mint once / store sha256 / verify by
hash / revoke = fail closed / field-stripped views) and the 0007 migration
(round-trips a pre-0007 SQLite DB to head, and is reversible).

Hermetic, same conventions as test_phase7_concurrency.py: an isolated SQLite brain
per test, the store instantiated directly, no network / no Anthropic key.
"""
import os
import sqlite3
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import db, devices  # noqa: E402
from agent_team.devices import DeviceTokenStore  # noqa: E402
from agent_team.models import DEVICE_ACTIVE, DEVICE_REVOKED, Device  # noqa: E402

ACCT = "cus_aaaaaaaaaaaa"
OTHER = "cus_bbbbbbbbbbbb"


@pytest.fixture
def fresh_db(tmp_path):
    db.init_db(str(tmp_path / "devices.db"))
    yield


# ---------------------------------------------------------------------------
# Secret discipline + lifecycle
# ---------------------------------------------------------------------------
def test_import_clean_and_prefixes():
    assert devices.DEVICE_TOKEN_PREFIX == "tng-dev-"


def test_mint_exchange_happy_path(fresh_db):
    store = DeviceTokenStore()
    pc = store.create_pairing_code(ACCT, name="Bruce-MBP", platform="darwin")
    assert pc["pairing_code"] and pc["device_id"].startswith("dev_")
    assert pc["account_id"] == ACCT and pc["expires_at"]

    ex = store.exchange_pairing_code(pc["pairing_code"], device_name="Bruce-MBP",
                                     platform="darwin")
    assert ex is not None
    assert ex["device_token"].startswith("tng-dev-")
    assert ex["device_id"] == pc["device_id"]
    assert ex["account_id"] == ACCT
    # The row is now active and the token is NOT stored in plaintext (only its hash).
    with db.session_scope() as s:
        row = s.query(Device).filter(Device.id == pc["device_id"]).one()
        assert row.status == DEVICE_ACTIVE
        assert row.token_hash and row.token_hash != ex["device_token"]
        assert row.pairing_code_hash is None        # burned on exchange


def test_verify_token_resolves_active_device_no_secrets(fresh_db):
    store = DeviceTokenStore()
    pc = store.create_pairing_code(ACCT, name="dev1", platform="win")
    ex = store.exchange_pairing_code(pc["pairing_code"])
    rec = store.verify_token(ex["device_token"])
    assert rec is not None
    assert rec["account_id"] == ACCT
    assert rec["id"] == ex["device_id"]
    assert rec["status"] == "active"
    # NEVER leak secrets or model/¥ machinery in the verify record.
    for forbidden in ("token_hash", "pairing_code_hash", "token", "models",
                      "balance", "model", "key_hash"):
        assert forbidden not in rec


def test_replay_defense_second_exchange_fails(fresh_db):
    store = DeviceTokenStore()
    pc = store.create_pairing_code(ACCT)
    first = store.exchange_pairing_code(pc["pairing_code"])
    assert first is not None
    # A replayed code mints NOTHING (pairing material was burned).
    assert store.exchange_pairing_code(pc["pairing_code"]) is None


def test_expired_pairing_code_cannot_exchange(fresh_db, monkeypatch):
    store = DeviceTokenStore()
    monkeypatch.setattr(devices, "_PAIRING_TTL_SECS", -10)  # expires in the past
    pc = store.create_pairing_code(ACCT)
    assert store.exchange_pairing_code(pc["pairing_code"]) is None


def test_revoke_is_kill_switch_and_account_scoped(fresh_db):
    store = DeviceTokenStore()
    pc = store.create_pairing_code(ACCT, name="laptop", platform="darwin")
    ex = store.exchange_pairing_code(pc["pairing_code"])
    token = ex["device_token"]
    assert store.verify_token(token) is not None

    # A DIFFERENT account cannot revoke this device (cross-account isolation).
    assert store.revoke(ex["device_id"], account_id=OTHER) is None
    assert store.verify_token(token) is not None            # still active

    # The owner revokes → verify fails closed forever.
    view = store.revoke(ex["device_id"], account_id=ACCT)
    assert view is not None and view["status"] == DEVICE_REVOKED
    assert store.verify_token(token) is None


def test_bad_tokens_return_none(fresh_db):
    store = DeviceTokenStore()
    assert store.verify_token("tng-dev-garbage") is None
    assert store.verify_token("") is None
    assert store.verify_token(None) is None
    assert store.exchange_pairing_code("") is None
    assert store.exchange_pairing_code(None) is None


def test_list_for_account_is_field_stripped(fresh_db):
    store = DeviceTokenStore()
    pc = store.create_pairing_code(ACCT, name="d1", platform="darwin")
    store.exchange_pairing_code(pc["pairing_code"])
    rows = store.list_for_account(ACCT)
    assert len(rows) == 1
    r = rows[0]
    assert set(r.keys()) == {"id", "name", "platform", "status", "last_seen_at", "created_at"}
    for forbidden in ("token_hash", "pairing_code_hash", "token", "models", "balance"):
        assert forbidden not in r
    # Another account sees none of this account's devices.
    assert store.list_for_account(OTHER) == []


# ---------------------------------------------------------------------------
# Migration 0007 — round-trip from a simulated pre-0007 SQLite DB
# ---------------------------------------------------------------------------
def _seed_pre_0007(db_path):
    """A minimal database as it looked at revision 0006: companies + runs (without
    origin/device_id) + alembic_version stamped at 0006."""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE companies (
            id INTEGER PRIMARY KEY, account_email VARCHAR(320) NOT NULL,
            company_name VARCHAR(255), industry VARCHAR(255),
            created_at DATETIME NOT NULL);
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, goal_text TEXT,
            started_at DATETIME NOT NULL, finished_at DATETIME,
            status VARCHAR(14) NOT NULL, kind VARCHAR(32) NOT NULL DEFAULT 'report',
            credit_funded BOOLEAN NOT NULL DEFAULT 0,
            total_cost_yuan FLOAT, report_path VARCHAR(255));
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL);
        INSERT INTO alembic_version VALUES ('0006_phase7_single_report');
        INSERT INTO companies VALUES (1, 'legacy@corp.com', 'LegacyCo', NULL,
                                      '2026-01-01');
        INSERT INTO runs (company_id, goal_text, started_at, status)
            VALUES (1, '生成上周经营周报', '2026-01-03', 'succeeded');
    """)
    conn.commit()
    conn.close()


def _alembic(db_path, *args):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT, capture_output=True, text=True,
        env={**os.environ, "DATABASE_URL": f"sqlite:///{db_path}"})


def test_migration_0007_creates_devices_and_runs_columns(tmp_path):
    db_path = tmp_path / "legacy.db"
    _seed_pre_0007(db_path)

    proc = _alembic(db_path, "upgrade", "head")
    assert proc.returncode == 0, proc.stderr

    conn = sqlite3.connect(db_path)
    # devices table created with the expected columns.
    dev_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    assert {"id", "account_id", "token_hash", "pairing_code_hash",
            "pairing_expires_at", "status", "last_seen_at", "created_at"} <= dev_cols
    # the sibling Phase-11 tables exist too.
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"agent_catalog", "agent_install", "agent_run"} <= tables
    # runs gained origin (NOT NULL DEFAULT 'cloud') and device_id.
    runs_cols = {row[1]: row for row in conn.execute("PRAGMA table_info(runs)")}
    assert "origin" in runs_cols and "device_id" in runs_cols
    assert runs_cols["origin"][3] == 1                      # notnull
    assert "cloud" in str(runs_cols["origin"][4])           # default
    # the existing row backfilled to 'cloud'.
    assert conn.execute("SELECT origin FROM runs").fetchone()[0] == "cloud"
    conn.close()


def test_migration_0007_is_reversible(tmp_path):
    db_path = tmp_path / "legacy.db"
    _seed_pre_0007(db_path)

    assert _alembic(db_path, "upgrade", "head").returncode == 0
    down = _alembic(db_path, "downgrade", "-1")
    assert down.returncode == 0, down.stderr
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "devices" not in tables                          # dropped on downgrade
    runs_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    assert "origin" not in runs_cols and "device_id" not in runs_cols
    conn.close()

    up2 = _alembic(db_path, "upgrade", "head")
    assert up2.returncode == 0, up2.stderr
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "devices" in tables                              # re-created cleanly
    conn.close()


def test_fresh_create_all_has_phase11_schema(fresh_db):
    """Model metadata and the migration agree: a fresh create_all DB already has the
    Phase-11 tables and the new runs columns (so the migration no-ops there)."""
    from sqlalchemy import inspect as sa_inspect
    insp = sa_inspect(db.engine)
    names = set(insp.get_table_names())
    assert {"devices", "agent_catalog", "agent_install", "agent_run"} <= names
    runs_cols = {c["name"] for c in insp.get_columns("runs")}
    assert {"origin", "device_id"} <= runs_cols

"""
db.py — The persistent "company brain" database (SQLite locally, PostgreSQL in prod).

Where the per-run SharedMemory (memory.py) is the team's *working* memory for a
single job, THIS is the company's *long-term* memory: it survives across runs so
every new job can read what previous jobs learned (past metrics, the company
profile, glossary terms, and the user's standing report preferences).

Design notes:
  - The DB is chosen at init time (Phase 7):
      * explicit db_path argument  → SQLite at that path (the tests pass a temp
        file / ":memory:" — this ALWAYS wins so test isolation is never affected);
      * else $DATABASE_URL          → any SQLAlchemy URL (postgresql://… in prod);
      * else the local SQLite file  → data/company_brain.db (zero-config dev), or
        $COMPANY_BRAIN_DB if that points elsewhere.
    So local dev and the whole test suite keep working with no env at all.
  - Engines open with pool_pre_ping=True so a connection dropped by Postgres (or a
    proxy / firewall idle-timeout, common on cloud VMs) is detected and replaced
    instead of surfacing as a random mid-run error.
  - Agent jobs run in BACKGROUND threads, so the SQLite engine adds
    check_same_thread=False and hands out short-lived sessions via session_scope()
    that always commit/rollback/close — connections never leak across threads.
  - On SQLite we create the schema directly (create_all) for frictionless dev/test.
    On Postgres the schema is owned by Alembic migrations (`alembic upgrade head`,
    run by the container entrypoint), so we do NOT create_all there — that keeps
    the migration the single source of truth and avoids "table already exists".
  - Written in the version-agnostic "classic declarative" style so it runs on
    SQLAlchemy 1.4 and 2.x alike.
"""

import os
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from . import config

# The project root is the parent of this package, so the DB lives in <root>/data/.
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_PACKAGE_DIR)
DEFAULT_DB_PATH = os.path.join(PROJECT_ROOT, "data", "company_brain.db")

# Every model in models.py subclasses this shared declarative base.
Base = declarative_base()

# Set up by init_db(). Kept as module globals so session_scope() (and therefore
# everything in metrics_store.py) always reads the engine the app most recently
# initialised — which lets the tests point us at an isolated database.
engine = None
SessionLocal = None
DB_PATH = None


def _resolve_db_path(db_path=None):
    if db_path:
        return db_path
    env = (os.environ.get("COMPANY_BRAIN_DB") or "").strip()
    return env or DEFAULT_DB_PATH


def _sqlite_url(path):
    """A SQLAlchemy SQLite URL for `path`, creating the parent dir for file DBs."""
    if path == ":memory:":
        return "sqlite://"  # SQLAlchemy's in-memory URL
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return f"sqlite:///{path}"


def _resolve_engine_target(db_path=None):
    """Decide which database to open. Returns (url, sqlite_path).

    Precedence: an explicit db_path ALWAYS means SQLite at that path (so the test
    suite, which passes a temp file, is never diverted by a stray $DATABASE_URL);
    then $DATABASE_URL (Postgres/prod); then the local SQLite default. `sqlite_path`
    is the on-disk file (or ":memory:") for SQLite, or None for a non-SQLite URL."""
    if db_path:
        return _sqlite_url(db_path), db_path
    url = config.database_url()
    if url:
        return url, None
    path = _resolve_db_path()
    return _sqlite_url(path), path


def init_db(db_path=None):
    """Open the database engine and ensure the schema exists. Returns the engine.

    Idempotent: safe to call on every app start and from each test. On SQLite the
    tables are created directly; on Postgres the schema is owned by Alembic, so we
    skip create_all (the container entrypoint runs `alembic upgrade head` first).
    """
    global engine, SessionLocal, DB_PATH
    url, sqlite_path = _resolve_engine_target(db_path)
    is_sqlite = url.startswith("sqlite")

    engine_kwargs = {
        "future": True,
        # Detect and replace a connection the server/proxy dropped while idle (very
        # common on cloud Postgres) instead of failing the next query.
        "pool_pre_ping": True,
    }
    if is_sqlite:
        # The website reads while a background worker writes — let the one
        # connection pool be shared across threads safely.
        engine_kwargs["connect_args"] = {"check_same_thread": False}

    engine = create_engine(url, **engine_kwargs)
    SessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        # We capture primary keys inside session_scope(); keeping attributes
        # un-expired after commit means returning a value can never trigger a
        # lazy refresh against an already-closed session.
        expire_on_commit=False,
        future=True,
    )
    DB_PATH = sqlite_path

    # Import models so their tables register on Base.metadata, THEN create them
    # (SQLite only — Alembic owns the Postgres schema).
    from . import models  # noqa: F401  (registers tables as a side effect)
    if is_sqlite:
        Base.metadata.create_all(engine)
        _ensure_sqlite_columns(engine)
    return engine


def _ensure_sqlite_columns(engine):
    """Additively bring a pre-existing SQLite DB up to the current model columns.

    create_all() makes any *missing table*, but it never ALTERs a table that already
    exists — so a local dev DB created in an earlier phase is left missing columns we
    added later (e.g. Phase 8's accounts.rpm_limit), which then surfaces as
    "table X has no column named Y" on the next insert. We mirror, SQLite-only, what
    the Alembic migration does for Postgres: inspect each table and ADD COLUMN for any
    column the models declare but the live table lacks. Idempotent — a no-op on a
    fresh DB (and on the tests' temp/:memory: DBs, which create_all built complete)."""
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    live_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in live_tables:
                continue  # create_all just built it with every column present
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    conn.exec_driver_sql(
                        f'ALTER TABLE "{table.name}" ADD COLUMN '
                        + _sqlite_add_column_ddl(col, engine))


def _sqlite_add_column_ddl(col, engine):
    """Render the column fragment for a SQLite ``ALTER TABLE ... ADD COLUMN``.

    SQLite only lets you add a NOT NULL column if you also give a DEFAULT (it needs
    one to backfill existing rows). So we emit NOT NULL only when a default is known;
    otherwise the column is added nullable, which is always safe and the model's
    app-side default still stamps new rows."""
    coltype = col.type.compile(engine.dialect)
    default_sql = None
    if col.server_default is not None:
        arg = getattr(col.server_default, "arg", None)
        if arg is not None:
            default_sql = str(getattr(arg, "text", arg))
    elif col.default is not None and getattr(col.default, "is_scalar", False):
        val = col.default.arg
        default_sql = f"'{val}'" if isinstance(val, str) else str(val)

    parts = [f'"{col.name}"', coltype]
    if default_sql is not None and not col.nullable:
        parts.append("NOT NULL")
    if default_sql is not None:
        parts.append(f"DEFAULT {default_sql}")
    return " ".join(parts)


@contextmanager
def session_scope():
    """Yield a short-lived session that commits on success, rolls back on error,
    and always closes. Auto-initialises the DB on first use if needed."""
    if SessionLocal is None:
        init_db()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

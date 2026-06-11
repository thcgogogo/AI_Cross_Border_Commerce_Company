"""
Alembic migration environment (Phase 7).

The URL is resolved the SAME way the app resolves it (db._resolve_engine_target):
$DATABASE_URL in prod (Postgres), else the local SQLite file. Importing
agent_team.models registers every table on Base.metadata, so target_metadata is
the single source of truth that autogenerate diffs against.
"""

from logging.config import fileConfig

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine

# So a developer running `alembic` locally picks up DATABASE_URL from .env, exactly
# like the app does. In prod the container passes DATABASE_URL as a real env var, so
# this is a no-op there (load_dotenv silently does nothing when there is no .env).
load_dotenv(override=True)

from agent_team import db  # noqa: E402
from agent_team import models  # noqa: E402,F401 — registers all tables on Base.metadata

# Alembic Config object (reads alembic.ini); set up logging if a config file exists.
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = db.Base.metadata


def _database_url():
    # No explicit path → $DATABASE_URL (prod) else the local SQLite default, exactly
    # like the running app, so migrations always target the app's real database.
    url, _sqlite_path = db._resolve_engine_target()
    return url


def run_migrations_offline():
    """Emit SQL to stdout without a live DB connection (`alembic upgrade --sql`)."""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations against a live connection (the normal `alembic upgrade head`)."""
    url = _database_url()
    # pool_pre_ping mirrors the app engine so a dropped cloud-Postgres connection is
    # replaced rather than surfacing as a migration failure.
    engine = create_engine(url, pool_pre_ping=True, future=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=url.startswith("sqlite"),
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

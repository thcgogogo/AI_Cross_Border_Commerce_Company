#!/usr/bin/env python3
"""
migrate_json_accounts.py — one-time, idempotent import of legacy JSON accounts
into the database `accounts` table (Phase 7, Task 1).

Older releases kept accounts in ``memory_store/customers.json``. Phase 7 moved
them into the database. This script absorbs that JSON file into the ``accounts``
table.

It is IDEMPOTENT: an account already present (matched by id, else by email) is
skipped, so running it twice is harmless — the second run imports 0. The app also
performs this same import automatically on every boot (see app.py), so you
normally never need to run this by hand. It exists for an explicit, scriptable
migration — e.g. seeding a fresh Postgres during a server cutover, or verifying a
migration outside the web process.

Usage:
    # Import memory_store/customers.json into whatever DATABASE_URL points at
    # (local SQLite by default, Postgres in production):
    python scripts/migrate_json_accounts.py

    # Import a specific file instead:
    python scripts/migrate_json_accounts.py /path/to/old/customers.json

Against Postgres the schema must already exist (`alembic upgrade head` — the app
container's entrypoint does this for you). Against local SQLite the tables are
created automatically.
"""
import argparse
import os
import sys

# Make the project root importable no matter where this is launched from.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(override=True)  # honor DATABASE_URL / COMPANY_BRAIN_DB from .env

from agent_team import db  # noqa: E402
from agent_team.customers import CustomerStore  # noqa: E402

DEFAULT_JSON = os.path.join(ROOT, "memory_store", "customers.json")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Idempotently import legacy JSON accounts into the database.")
    parser.add_argument(
        "json_path", nargs="?", default=DEFAULT_JSON,
        help=f"Path to the legacy customers.json (default: {DEFAULT_JSON}).")
    args = parser.parse_args(argv)

    # Initialise the DB engine. On SQLite this also creates the tables; on
    # Postgres the schema is owned by Alembic and must already exist.
    db.init_db()

    if not os.path.exists(args.json_path):
        print(f"No legacy account file at {args.json_path} — nothing to import.")
        return 0

    created = CustomerStore(store_path=args.json_path).import_legacy_json()
    if created:
        print(f"Imported {created} legacy account(s) from {args.json_path} "
              f"into the database.")
    else:
        print(f"No new accounts to import from {args.json_path} "
              f"(already migrated — this script is idempotent).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

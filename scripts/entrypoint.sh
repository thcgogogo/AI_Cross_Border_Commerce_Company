#!/usr/bin/env bash
#
# Container entrypoint (Phase 7). Brings the database schema up to head, then execs
# the container's command (gunicorn for the app; `python scheduler.py` for the
# scheduler, which passes RUN_MIGRATIONS=0 so the two containers never race).
set -euo pipefail

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  echo "[entrypoint] applying database migrations (alembic upgrade head) …"
  # Postgres may still be coming up the first time the stack starts; retry rather
  # than crash-loop. compose's depends_on: service_healthy usually makes this a
  # single attempt, but the retry makes a cold start robust.
  attempts=0
  until alembic upgrade head; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 30 ]; then
      echo "[entrypoint] migrations failed after ${attempts} attempts; giving up." >&2
      exit 1
    fi
    echo "[entrypoint] database not ready yet — retrying in 2s (attempt ${attempts}) …"
    sleep 2
  done
  echo "[entrypoint] migrations applied."
fi

exec "$@"

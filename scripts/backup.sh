#!/usr/bin/env bash
#
# backup.sh — nightly Postgres backup (Phase 7).
#
#   pg_dump | gzip  →  backups/YYYY-MM-DD.sql.gz
#   (on Sundays also  →  backups/weekly/YYYY-MM-DD.sql.gz)
#   optional offsite copy via rclone when BACKUP_REMOTE is set (Alibaba OSS /
#   Tencent COS or any S3-compatible target)
#   retention: keep the 14 most recent daily dumps + the 8 most recent weekly dumps
#
# By default the dump runs INSIDE the `postgres` compose service, so the host needs
# no postgresql-client — just Docker. To dump a managed/external Postgres instead,
# set BACKUP_PGDUMP_DIRECT=1 (uses `pg_dump "$DATABASE_URL"`, requires pg_dump on PATH).
#
# Cron example (run as a docker-capable user, 02:30 daily):
#   30 2 * * *  /opt/company-agent/scripts/backup.sh >> /var/log/company-backup.log 2>&1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Load .env so POSTGRES_* / DATABASE_URL / BACKUP_REMOTE are available.
set -a
[ -f .env ] && . ./.env
set +a

BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/backups}"
WEEKLY_DIR="$BACKUP_DIR/weekly"
mkdir -p "$BACKUP_DIR" "$WEEKLY_DIR"

POSTGRES_DB="${POSTGRES_DB:-company}"
POSTGRES_USER="${POSTGRES_USER:-company}"

STAMP="$(date +%F)"               # YYYY-MM-DD (sorts chronologically = lexically)
OUT="$BACKUP_DIR/$STAMP.sql.gz"

echo "[backup] $(date -u +%FT%TZ) dumping database '$POSTGRES_DB' → $OUT"
if [ "${BACKUP_PGDUMP_DIRECT:-0}" = "1" ]; then
  # Direct dump of DATABASE_URL (managed Postgres / host with postgresql-client).
  : "${DATABASE_URL:?BACKUP_PGDUMP_DIRECT=1 needs DATABASE_URL set}"
  pg_dump "$DATABASE_URL" | gzip > "$OUT"
else
  # Dump from inside the running postgres container (-T: no TTY → clean pipe).
  docker compose exec -T postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$OUT"
fi

# A failed/empty dump must NOT masquerade as a good backup.
if [ ! -s "$OUT" ]; then
  echo "[backup] ERROR: dump is empty — removing $OUT" >&2
  rm -f "$OUT"
  exit 1
fi
echo "[backup] wrote $(du -h "$OUT" | cut -f1) → $OUT"

# Sunday (date +%u == 7) → also keep a weekly snapshot.
if [ "$(date +%u)" = "7" ]; then
  cp -f "$OUT" "$WEEKLY_DIR/$STAMP.sql.gz"
  echo "[backup] weekly snapshot → $WEEKLY_DIR/$STAMP.sql.gz"
fi

# Optional offsite upload (only when BACKUP_REMOTE is set). rclone must be
# configured on the host with a remote matching the BACKUP_REMOTE prefix.
if [ -n "${BACKUP_REMOTE:-}" ]; then
  if command -v rclone >/dev/null 2>&1; then
    echo "[backup] uploading $OUT → $BACKUP_REMOTE/"
    rclone copy "$OUT" "$BACKUP_REMOTE/"
    [ -f "$WEEKLY_DIR/$STAMP.sql.gz" ] && rclone copy "$WEEKLY_DIR/$STAMP.sql.gz" "$BACKUP_REMOTE/weekly/"
  else
    echo "[backup] WARNING: BACKUP_REMOTE set but rclone is not installed; skipping upload." >&2
  fi
fi

# Retention: keep the newest N files, delete the rest. Filenames are YYYY-MM-DD so
# a reverse lexical sort is newest-first.
prune_keep() {
  local dir="$1" keep="$2" f
  ls -1 "$dir"/*.sql.gz 2>/dev/null | sort -r | tail -n +"$((keep + 1))" | while read -r f; do
    echo "[backup] pruning old backup: $f"
    rm -f "$f"
  done
}
prune_keep "$BACKUP_DIR" 14
prune_keep "$WEEKLY_DIR" 8

echo "[backup] done."

# syntax=docker/dockerfile:1
#
# One image, two roles (Phase 7): the web app (gunicorn, default CMD) and the
# scheduler worker (command overridden to `python scheduler.py` in compose). All
# Python deps ship as manylinux wheels, so no compiler/system headers are needed.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps FIRST (own layer) so code edits don't bust the pip cache.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# App code (a .dockerignore keeps local DB / venv / secrets out of the image).
COPY . .

# Writable runtime dirs. In compose these are named volumes; create them here so
# the app also works with `docker run` and no mounts.
RUN mkdir -p data data/uploads data/reports outputs memory_store uploads backups \
    && chmod +x scripts/entrypoint.sh scripts/backup.sh

# Run as an unprivileged user (defense in depth); it owns /app so it can write
# the data/output/memory volumes.
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# entrypoint runs `alembic upgrade head` (when RUN_MIGRATIONS=1) then execs CMD.
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["gunicorn", "-w", "4", "-k", "gthread", "--threads", "8", \
     "-b", "0.0.0.0:8000", "--timeout", "120", "app:app"]

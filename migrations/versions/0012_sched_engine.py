"""scheduled_jobs.engine_kind — auto-deliver 跨境财务 reports on a cadence

Adds a nullable `engine_kind` column to scheduled_jobs so a 定时任务 can target a
跨境财务 evidence engine (threeflow/rebate/invoice/vatexempt) instead of the default
report team. None/"" keeps the existing report-team behavior.

IDEMPOTENT BY DESIGN (same contract as 0002–0011): inspect the live columns and add
only if absent. On a FRESH database the 0001 baseline's create_all already builds the
column; on an existing Postgres at 0011 this is the real additive ALTER.

Revision ID: 0012_sched_engine
Revises: 0011_crossborder
Create Date: 2026-06-19
"""
import sqlalchemy as sa
from alembic import op

revision = "0012_sched_engine"
down_revision = "0011_crossborder"
branch_labels = None
depends_on = None


def _cols(insp, table):
    return {c["name"] for c in insp.get_columns(table)} if table in insp.get_table_names() else set()


def upgrade():
    insp = sa.inspect(op.get_bind())
    if "scheduled_jobs" in insp.get_table_names() and "engine_kind" not in _cols(insp, "scheduled_jobs"):
        op.add_column("scheduled_jobs",
                      sa.Column("engine_kind", sa.String(length=32), nullable=True))


def downgrade():
    insp = sa.inspect(op.get_bind())
    if "engine_kind" in _cols(insp, "scheduled_jobs"):
        op.drop_column("scheduled_jobs", "engine_kind")

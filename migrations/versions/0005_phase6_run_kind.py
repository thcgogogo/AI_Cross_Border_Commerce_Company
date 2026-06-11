"""phase 6 (UI redesign) — runs.kind + legacy wizard-cron normalization

Two things:

1. `runs.kind` (TEXT NOT NULL DEFAULT 'report') — separates quota-metered
   report runs from the free 历史数据导入 back-fill (kind='import'). Before
   this, import runs ate the 免费体验 plan's 2-lifetime quota and appeared in
   历史报告 as dead has_content=false rows. Existing import rows are
   recognised by the exact goal_text the import job has always written.

2. Data fix: legacy wizard rows created before the Monday-cron fix —
   scheduled_jobs named 每周经营周报 with cron '0 8 * * 1' — actually fired
   TUESDAY (APScheduler numbers day_of_week 0=Monday, unlike crontab), so they
   are normalized to the named-weekday form '0 8 * * mon'.

IDEMPOTENT BY DESIGN (same contract as 0002–0004). On a FRESH database the
0001 baseline's create_all already builds runs WITH the kind column, so the
add-column step no-ops behind a live-schema inspection; the data fixes are
WHERE-guarded UPDATEs that match nothing on a clean DB. Either way
`alembic upgrade head` ends at a schema identical to the models.

Revision ID: 0005_phase6_run_kind
Revises: 0004_phase10_onboarding
Create Date: 2026-06-11
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0005_phase6_run_kind"
down_revision = "0004_phase10_onboarding"
branch_labels = None
depends_on = None

# The exact goal_text app._run_import_job has written since the import shipped —
# the only reliable marker of pre-`kind` import rows.
_IMPORT_GOAL_TEXT = "历史数据导入 (historical import)"

# The wizard job's name + the broken numeric cron it used to write.
_WIZARD_JOB_NAME = "每周经营周报"
_LEGACY_TUESDAY_CRON = "0 8 * * 1"
_MONDAY_CRON = "0 8 * * mon"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(insp, table, column):
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade():
    insp = _inspector()

    if not _has_column(insp, "runs", "kind"):
        op.add_column(
            "runs",
            sa.Column("kind", sa.String(length=32), nullable=False,
                      server_default="report"),
        )

    # Backfill: rows the import job created before the column existed.
    op.execute(
        sa.text("UPDATE runs SET kind = 'import' WHERE goal_text = :g")
        .bindparams(g=_IMPORT_GOAL_TEXT)
    )

    # Normalize legacy wizard rows that silently fired Tuesday.
    op.execute(
        sa.text("UPDATE scheduled_jobs SET cron_expr = :fixed "
                "WHERE name_cn = :name AND cron_expr = :legacy")
        .bindparams(fixed=_MONDAY_CRON, name=_WIZARD_JOB_NAME,
                    legacy=_LEGACY_TUESDAY_CRON)
    )


def downgrade():
    insp = _inspector()
    if _has_column(insp, "runs", "kind"):
        op.drop_column("runs", "kind")
    # The cron normalization is a data FIX — downgrading must not resurrect the
    # Tuesday bug, so it is deliberately not reversed.

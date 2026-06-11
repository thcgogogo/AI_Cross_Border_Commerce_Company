"""phase 7 (UI redesign) — B4 单次报告 ¥29: report credits + the new order type

Three things:

1. `accounts.report_credits` (INTEGER NOT NULL DEFAULT 0) — unconsumed 单次报告
   purchases. A paid single_report order adds one; the plan-quota gate counts
   them on top of the plan's quota; the run that lands beyond the base quota
   consumes one (metrics_store.consume_report_credit_if_over_quota).

2. `runs.credit_funded` (BOOLEAN NOT NULL DEFAULT 0) — marks the run a credit
   paid for. Credit-funded runs are EXCLUDED from the quota counters (still
   shown in 历史报告): without the mark, a consumed credit's run counts
   against the base quota forever, so a SECOND ¥29 purchase on the 免费体验
   lifetime trial would buy nothing (used=3 ≥ 2+1). (Added in the same
   Phase-7 session that introduced this revision, before any deployment ran
   it — no split into an 0007 needed.)

3. The `single_report` value for orders.type. On SQLite no DDL is needed —
   SQLAlchemy ≥1.4 renders Enum as plain VARCHAR (create_constraint defaults
   to False). On Postgres the native `order_type` enum must learn the value,
   for prod parity: ALTER TYPE … ADD VALUE IF NOT EXISTS is idempotent, and
   on PG ≥ 12 it may run inside Alembic's transaction as long as the new value
   isn't USED in the same transaction (it isn't — no data is written here).

IDEMPOTENT BY DESIGN (same contract as 0002–0005). On a FRESH database the
0001 baseline's create_all already builds accounts WITH report_credits, so the
add-column step no-ops behind a live-schema inspection; ADD VALUE carries its
own IF NOT EXISTS. Either way `alembic upgrade head` ends at a schema
identical to the models.

Revision ID: 0006_phase7_single_report
Revises: 0005_phase6_run_kind
Create Date: 2026-06-11
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_phase7_single_report"
down_revision = "0005_phase6_run_kind"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _column_missing(insp, table, column):
    """True when `table` exists but lacks `column`. An ABSENT table needs no
    work in either direction (a DB that never had it gets the complete table
    from create_all / the baseline later) — same live-schema inspection
    contract as 0002–0005."""
    if table not in insp.get_table_names():
        return False
    return all(c["name"] != column for c in insp.get_columns(table))


def upgrade():
    insp = _inspector()

    if _column_missing(insp, "accounts", "report_credits"):
        op.add_column(
            "accounts",
            sa.Column("report_credits", sa.Integer(), nullable=False,
                      server_default="0"),
        )

    if _column_missing(insp, "runs", "credit_funded"):
        op.add_column(
            "runs",
            sa.Column("credit_funded", sa.Boolean(), nullable=False,
                      server_default="0"),
        )

    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE order_type ADD VALUE IF NOT EXISTS 'single_report'")


def downgrade():
    insp = _inspector()
    tables = insp.get_table_names()
    if "accounts" in tables and not _column_missing(insp, "accounts", "report_credits"):
        op.drop_column("accounts", "report_credits")
    if "runs" in tables and not _column_missing(insp, "runs", "credit_funded"):
        op.drop_column("runs", "credit_funded")
    # Postgres enum VALUES cannot be dropped; leaving 'single_report' behind is
    # harmless (no rows reference it after a downgrade rolls the feature back).

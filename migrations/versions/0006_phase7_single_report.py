"""phase 7 (UI redesign) — B4 单次报告 ¥29: report credits + the new order type

Two things:

1. `accounts.report_credits` (INTEGER NOT NULL DEFAULT 0) — unconsumed 单次报告
   purchases. A paid single_report order adds one; the plan-quota gate counts
   them on top of the plan's quota; the run that lands beyond the base quota
   consumes one (metrics_store.consume_report_credit_if_over_quota).

2. The `single_report` value for orders.type. On SQLite no DDL is needed —
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


def _has_column(insp, table, column):
    # Tolerate an absent table too (a DB that never had `accounts` gets the
    # full table from create_all / the baseline later) — same live-schema
    # inspection contract as 0002–0005.
    if table not in insp.get_table_names():
        return True
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade():
    insp = _inspector()

    if not _has_column(insp, "accounts", "report_credits"):
        op.add_column(
            "accounts",
            sa.Column("report_credits", sa.Integer(), nullable=False,
                      server_default="0"),
        )

    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE order_type ADD VALUE IF NOT EXISTS 'single_report'")


def downgrade():
    insp = _inspector()
    if _has_column(insp, "accounts", "report_credits"):
        op.drop_column("accounts", "report_credits")
    # Postgres enum VALUES cannot be dropped; leaving 'single_report' behind is
    # harmless (no rows reference it after a downgrade rolls the feature back).

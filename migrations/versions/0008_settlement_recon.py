"""对账 / 平台结算核对 (customer settlement reconciliation): settlement_reports + settlement_exceptions

Adds the two customer-scoped settlement-reconciliation tables. Distinct from the
Phase-9 recon_reports table (our OWN internal provider-bill reconciliation) — these
hold one customer's per-run 对账 result and its non-blocking exception worklist.

IDEMPOTENT BY DESIGN (same contract as 0002–0007). On a FRESH database the 0001
baseline's create_all already builds the whole current model metadata, so these
objects exist by the time this revision runs and every step no-ops behind a
live-schema inspection. On an existing Postgres at 0007 the same guards make this the
real, additive migration. runs.kind is String(32), so the new RUN_KIND_RECON value
needs NO ALTER TYPE and no schema change here.

Revision ID: 0008_settlement_recon
Revises: 0007_phase11_devices
Create Date: 2026-06-16
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0008_settlement_recon"
down_revision = "0007_phase11_devices"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(insp, name):
    return name in insp.get_table_names()


def _has_column(insp, table, column):
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade():
    insp = _inspector()

    if not _has_table(insp, "settlement_reports"):
        op.create_table(
            "settlement_reports",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"),
                      nullable=False),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
            sa.Column("account_id", sa.String(length=32), nullable=True),
            sa.Column("platform", sa.String(length=32), nullable=True),
            sa.Column("period_label", sa.String(length=64), nullable=True),
            sa.Column("summary_json", sa.Text(), nullable=True),
            sa.Column("bridge_ok", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("unexplained_amount", sa.Numeric(precision=12, scale=2),
                      nullable=False, server_default="0"),
            sa.Column("unexplained_count", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("company_id", "run_id",
                                name="uq_settlement_report_company_run"),
        )
        op.create_index("ix_settlement_reports_company_id", "settlement_reports",
                        ["company_id"])
        op.create_index("ix_settlement_reports_run_id", "settlement_reports", ["run_id"])
        op.create_index("ix_settlement_reports_account_id", "settlement_reports",
                        ["account_id"])

    if not _has_table(insp, "settlement_exceptions"):
        op.create_table(
            "settlement_exceptions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"),
                      nullable=False),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
            sa.Column("layer", sa.String(length=16), nullable=True),
            sa.Column("bucket", sa.String(length=24), nullable=True),
            sa.Column("cause", sa.String(length=48), nullable=True),
            sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False,
                      server_default="0"),
            sa.Column("platform_ref", sa.String(length=128), nullable=True),
            sa.Column("bank_ref", sa.String(length=128), nullable=True),
            sa.Column("ledger_ref", sa.String(length=128), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False,
                      server_default="open"),
            sa.Column("user_label", sa.String(length=48), nullable=True),
            sa.Column("detail_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_settlement_exceptions_company_id", "settlement_exceptions",
                        ["company_id"])
        op.create_index("ix_settlement_exceptions_run_id", "settlement_exceptions",
                        ["run_id"])


def downgrade():
    insp = _inspector()
    for table in ("settlement_exceptions", "settlement_reports"):
        if _has_table(insp, table):
            op.drop_table(table)

"""现金位置/资金跑道 capstone: cash_position_settings + cash_position_reports

Adds the two tables for the forward-looking 现金位置/资金跑道 capstone (a consumer of the
跨境财务 hub's reconciled outputs): the per-company editable assumptions + manual inputs
(cash_position_settings), and the per-run result (cash_position_reports, mirrors
settlement_reports / mi_reports).

IDEMPOTENT BY DESIGN (same contract as 0002–0018). On a FRESH database the 0001 baseline's
create_all already builds the whole current model metadata, so these objects exist by the
time this revision runs and every step no-ops behind a live-schema inspection. On an existing
Postgres at 0018 the same guards make this the real, additive migration. runs.kind is
String(32), so the new RUN_KIND_CASH_POSITION value needs NO ALTER TYPE. This migration does
NOT touch any of the three existing 工作流 sections' tables.

Revision ID: 0019_cash_position
Revises: 0018_market_intel
Create Date: 2026-06-22
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0019_cash_position"
down_revision = "0018_market_intel"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(insp, name):
    return name in insp.get_table_names()


def upgrade():
    insp = _inspector()

    if not _has_table(insp, "cash_position_settings"):
        op.create_table(
            "cash_position_settings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"),
                      nullable=False),
            sa.Column("assumptions", sa.Text(), nullable=True),
            sa.Column("manual_inputs", sa.Text(), nullable=True),
            sa.Column("prefs", sa.Text(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("company_id", name="uq_cash_settings_company"),
        )
        op.create_index("ix_cash_position_settings_company_id",
                        "cash_position_settings", ["company_id"])

    if not _has_table(insp, "cash_position_reports"):
        op.create_table(
            "cash_position_reports",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"),
                      nullable=False),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
            sa.Column("account_id", sa.String(length=32), nullable=True),
            sa.Column("period_label", sa.String(length=32), nullable=True),
            sa.Column("summary_json", sa.Text(), nullable=True),
            sa.Column("bridge_ok", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("company_id", "run_id",
                                name="uq_cash_report_company_run"),
        )
        op.create_index("ix_cash_position_reports_company_id",
                        "cash_position_reports", ["company_id"])
        op.create_index("ix_cash_position_reports_run_id",
                        "cash_position_reports", ["run_id"])
        op.create_index("ix_cash_position_reports_account_id",
                        "cash_position_reports", ["account_id"])


def downgrade():
    insp = _inspector()
    for table in ("cash_position_reports", "cash_position_settings"):
        if _has_table(insp, table):
            op.drop_table(table)

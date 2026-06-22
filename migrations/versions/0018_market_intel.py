"""市场情报周报 (weekly market-intelligence report): mi_watchlists + mi_intel_pool + mi_reports

Adds the three tables for the new 市场情报周报 workflow (a 4th sibling of 对账/AR/报告):
the per-company watchlist (the paid INPUT), the SHARED weekly intel pool (collected once,
reused per customer — the COGS split), and the per-customer report summary (mirrors
settlement_reports).

IDEMPOTENT BY DESIGN (same contract as 0002–0017). On a FRESH database the 0001 baseline's
create_all already builds the whole current model metadata, so these objects exist by the
time this revision runs and every step no-ops behind a live-schema inspection. On an
existing Postgres at 0017 the same guards make this the real, additive migration.
runs.kind is String(32), so the new RUN_KIND_MARKET_INTEL / RUN_KIND_MI_COLLECT values
need NO ALTER TYPE.

Revision ID: 0018_market_intel
Revises: 0017_fusion_persona
Create Date: 2026-06-21
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0018_market_intel"
down_revision = "0017_fusion_persona"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(insp, name):
    return name in insp.get_table_names()


def upgrade():
    insp = _inspector()

    if not _has_table(insp, "mi_watchlists"):
        op.create_table(
            "mi_watchlists",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"),
                      nullable=False),
            sa.Column("platforms", sa.Text(), nullable=True),
            sa.Column("markets", sa.Text(), nullable=True),
            sa.Column("category", sa.String(length=128), nullable=True),
            sa.Column("competitors", sa.Text(), nullable=True),
            sa.Column("keywords", sa.Text(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("company_id", name="uq_mi_watchlist_company"),
        )
        op.create_index("ix_mi_watchlists_company_id", "mi_watchlists", ["company_id"])

    if not _has_table(insp, "mi_intel_pool"):
        op.create_table(
            "mi_intel_pool",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("period_label", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False,
                      server_default="ready"),
            sa.Column("query_plan_version", sa.String(length=32), nullable=True),
            sa.Column("items_json", sa.Text(), nullable=True),
            sa.Column("collected_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("period_label", name="uq_mi_pool_period"),
        )
        op.create_index("ix_mi_intel_pool_period_label", "mi_intel_pool",
                        ["period_label"])

    if not _has_table(insp, "mi_reports"):
        op.create_table(
            "mi_reports",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"),
                      nullable=False),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
            sa.Column("account_id", sa.String(length=32), nullable=True),
            sa.Column("period_label", sa.String(length=32), nullable=True),
            sa.Column("summary_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("company_id", "run_id", name="uq_mi_report_company_run"),
        )
        op.create_index("ix_mi_reports_company_id", "mi_reports", ["company_id"])
        op.create_index("ix_mi_reports_run_id", "mi_reports", ["run_id"])
        op.create_index("ix_mi_reports_account_id", "mi_reports", ["account_id"])


def downgrade():
    insp = _inspector()
    for table in ("mi_reports", "mi_intel_pool", "mi_watchlists"):
        if _has_table(insp, table):
            op.drop_table(table)

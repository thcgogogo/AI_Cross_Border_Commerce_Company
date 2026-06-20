"""跨境财务 engines: crossborder_reports + crossborder_flags

Adds the two shared, customer-scoped tables behind the new 跨境财务 evidence engines
(三流合一 / 退税核算 / 发票审核 / 无票免税). ONE table pair serves all four — `engine_kind`
distinguishes them — mirroring settlement_reports/settlement_exceptions + ar_reports/ar_flags.

IDEMPOTENT BY DESIGN (same contract as 0002–0010). On a FRESH database the 0001 baseline's
create_all already builds the whole current model metadata, so these objects exist by the
time this revision runs and every step no-ops behind a live-schema inspection. On an
existing Postgres at 0010 the same guards make this the real, additive migration.
runs.kind is String(32), so the new RUN_KIND_CROSSBORDER value needs NO ALTER TYPE.

Revision ID: 0011_crossborder
Revises: 0010_ar
Create Date: 2026-06-19
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0011_crossborder"
down_revision = "0010_ar"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(insp, name):
    return name in insp.get_table_names()


def upgrade():
    insp = _inspector()

    if not _has_table(insp, "crossborder_reports"):
        op.create_table(
            "crossborder_reports",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"),
                      nullable=False),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
            sa.Column("account_id", sa.String(length=32), nullable=True),
            sa.Column("engine_kind", sa.String(length=32), nullable=False),
            sa.Column("period_label", sa.String(length=64), nullable=True),
            sa.Column("summary_json", sa.Text(), nullable=True),
            sa.Column("bridge_ok", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("exception_count", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("company_id", "run_id",
                                name="uq_crossborder_report_company_run"),
        )
        op.create_index("ix_crossborder_reports_company_id", "crossborder_reports",
                        ["company_id"])
        op.create_index("ix_crossborder_reports_run_id", "crossborder_reports",
                        ["run_id"])
        op.create_index("ix_crossborder_reports_account_id", "crossborder_reports",
                        ["account_id"])
        op.create_index("ix_crossborder_reports_engine_kind", "crossborder_reports",
                        ["engine_kind"])

    if not _has_table(insp, "crossborder_flags"):
        op.create_table(
            "crossborder_flags",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"),
                      nullable=False),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
            sa.Column("engine_kind", sa.String(length=32), nullable=False),
            sa.Column("ref", sa.String(length=128), nullable=True),
            sa.Column("cause", sa.String(length=64), nullable=True),
            sa.Column("severity", sa.String(length=8), nullable=True),
            sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False,
                      server_default="0"),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False,
                      server_default="open"),
            sa.Column("user_label", sa.String(length=48), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_crossborder_flags_company_id", "crossborder_flags",
                        ["company_id"])
        op.create_index("ix_crossborder_flags_run_id", "crossborder_flags", ["run_id"])
        op.create_index("ix_crossborder_flags_engine_kind", "crossborder_flags",
                        ["engine_kind"])


def downgrade():
    insp = _inspector()
    for table in ("crossborder_flags", "crossborder_reports"):
        if _has_table(insp, table):
            op.drop_table(table)

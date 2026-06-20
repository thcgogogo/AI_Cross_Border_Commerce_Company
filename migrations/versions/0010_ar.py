"""应收账款管理 + 催收 (AR aging + dunning): ar_reports + ar_flags + dunning_messages

Adds the three customer-scoped AR tables for the new 应收账款 workflow (a sibling of
对账). ar_reports/ar_flags mirror settlement_reports/settlement_exceptions; dunning_messages
is the fresh no-double-dun sent-state table (对账 has no analog).

IDEMPOTENT BY DESIGN (same contract as 0002–0009). On a FRESH database the 0001
baseline's create_all already builds the whole current model metadata, so these objects
exist by the time this revision runs and every step no-ops behind a live-schema
inspection. On an existing Postgres at 0009 the same guards make this the real, additive
migration. runs.kind is String(32), so the new RUN_KIND_AR value needs NO ALTER TYPE.

Revision ID: 0010_ar
Revises: 0009_events
Create Date: 2026-06-19
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0010_ar"
down_revision = "0009_events"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(insp, name):
    return name in insp.get_table_names()


def upgrade():
    insp = _inspector()

    if not _has_table(insp, "ar_reports"):
        op.create_table(
            "ar_reports",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"),
                      nullable=False),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
            sa.Column("account_id", sa.String(length=32), nullable=True),
            sa.Column("format_key", sa.String(length=32), nullable=True),
            sa.Column("period_label", sa.String(length=64), nullable=True),
            sa.Column("summary_json", sa.Text(), nullable=True),
            sa.Column("bridge_ok", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("total_outstanding", sa.Numeric(precision=12, scale=2),
                      nullable=False, server_default="0"),
            sa.Column("overdue_total", sa.Numeric(precision=12, scale=2),
                      nullable=False, server_default="0"),
            sa.Column("dso", sa.Numeric(precision=10, scale=1), nullable=True),
            sa.Column("suspect_count", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("company_id", "run_id",
                                name="uq_ar_report_company_run"),
        )
        op.create_index("ix_ar_reports_company_id", "ar_reports", ["company_id"])
        op.create_index("ix_ar_reports_run_id", "ar_reports", ["run_id"])
        op.create_index("ix_ar_reports_account_id", "ar_reports", ["account_id"])

    if not _has_table(insp, "ar_flags"):
        op.create_table(
            "ar_flags",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"),
                      nullable=False),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
            sa.Column("layer", sa.String(length=16), nullable=True),
            sa.Column("bucket", sa.String(length=24), nullable=True),
            sa.Column("grade", sa.String(length=8), nullable=True),
            sa.Column("cause", sa.String(length=48), nullable=True),
            sa.Column("suspect_reasons", sa.String(length=255), nullable=True),
            sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False,
                      server_default="0"),
            sa.Column("customer_ref", sa.String(length=128), nullable=True),
            sa.Column("invoice_ref", sa.String(length=128), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False,
                      server_default="open"),
            sa.Column("user_label", sa.String(length=48), nullable=True),
            sa.Column("detail_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_ar_flags_company_id", "ar_flags", ["company_id"])
        op.create_index("ix_ar_flags_run_id", "ar_flags", ["run_id"])

    if not _has_table(insp, "dunning_messages"):
        op.create_table(
            "dunning_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"),
                      nullable=False),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=True),
            sa.Column("account_id", sa.String(length=32), nullable=True),
            sa.Column("customer_key", sa.String(length=128), nullable=False),
            sa.Column("invoice_key", sa.String(length=128), nullable=True),
            sa.Column("tier", sa.String(length=16), nullable=False),
            sa.Column("channel", sa.String(length=16), nullable=True),
            sa.Column("grade", sa.String(length=8), nullable=True),
            sa.Column("state", sa.String(length=16), nullable=False,
                      server_default="draft"),
            sa.Column("days_overdue_at_draft", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("amount_at_draft", sa.Numeric(precision=12, scale=2),
                      nullable=False, server_default="0"),
            sa.Column("facts_json", sa.Text(), nullable=True),
            sa.Column("draft_body", sa.Text(), nullable=True),
            sa.Column("edited_body", sa.Text(), nullable=True),
            sa.Column("audit_warning", sa.Text(), nullable=True),
            sa.Column("drafted_at", sa.DateTime(), nullable=False),
            sa.Column("approved_at", sa.DateTime(), nullable=True),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column("delivery_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_dunning_messages_company_id", "dunning_messages",
                        ["company_id"])
        op.create_index("ix_dunning_messages_run_id", "dunning_messages", ["run_id"])
        op.create_index("ix_dunning_messages_account_id", "dunning_messages",
                        ["account_id"])
        op.create_index("ix_dunning_messages_customer_key", "dunning_messages",
                        ["customer_key"])
        op.create_index("ix_dunning_messages_sent_at", "dunning_messages", ["sent_at"])
        op.create_index("ix_dunning_company_customer_tier_sent", "dunning_messages",
                        ["company_id", "customer_key", "tier", "sent_at"])


def downgrade():
    insp = _inspector()
    for table in ("dunning_messages", "ar_flags", "ar_reports"):
        if _has_table(insp, table):
            op.drop_table(table)

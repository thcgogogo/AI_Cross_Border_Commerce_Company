"""phase 9 — payments, subscriptions & fapiao

Adds the Phase 9 tables: orders, subscriptions, fapiao_requests, recon_reports.

IDEMPOTENT BY DESIGN (same contract as 0002). The 0001 baseline builds the WHOLE
current model metadata via create_all, so on a FRESH database these tables already
exist by the time this revision runs — every step is guarded by a live-schema
inspection and becomes a no-op. On a database migrated before Phase 9 (a deployed
Postgres), the same guards make this the real, additive migration. Either way
`alembic upgrade head` ends at a schema identical to the models (zero drift).

Revision ID: 0003_phase9_payments
Revises: 0002_phase8_security
Create Date: 2026-06-10
"""
import sqlalchemy as sa
from alembic import op

from agent_team.models import FAPIAO_STATUSES, ORDER_STATUSES, ORDER_TYPES, SUB_STATUSES

# revision identifiers, used by Alembic.
revision = "0003_phase9_payments"
down_revision = "0002_phase8_security"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(insp, name):
    return name in insp.get_table_names()


def upgrade():
    insp = _inspector()

    if not _has_table(insp, "orders"):
        op.create_table(
            "orders",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("order_no", sa.String(length=64), nullable=False, unique=True),
            sa.Column("account_id", sa.String(length=32), nullable=False),
            sa.Column("type", sa.Enum(*ORDER_TYPES, name="order_type"),
                      nullable=False),
            sa.Column("plan_code", sa.String(length=16), nullable=True),
            sa.Column("amount_yuan", sa.Numeric(precision=12, scale=2),
                      nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("provider_txn_id", sa.String(length=128), nullable=True),
            sa.Column("status", sa.Enum(*ORDER_STATUSES, name="order_status"),
                      nullable=False),
            sa.Column("subject", sa.String(length=255), nullable=True),
            sa.Column("refunded_yuan", sa.Numeric(precision=12, scale=2),
                      nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("paid_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_orders_order_no", "orders", ["order_no"])
        op.create_index("ix_orders_account_id", "orders", ["account_id"])
        op.create_index("ix_orders_status", "orders", ["status"])
        op.create_index("ix_orders_created_at", "orders", ["created_at"])
        op.create_index("ix_orders_paid_at", "orders", ["paid_at"])

    if not _has_table(insp, "subscriptions"):
        op.create_table(
            "subscriptions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.String(length=32), nullable=False,
                      unique=True),
            sa.Column("plan_code", sa.String(length=16), nullable=False),
            sa.Column("status", sa.Enum(*SUB_STATUSES, name="sub_status"),
                      nullable=False),
            sa.Column("current_period_start", sa.DateTime(), nullable=False),
            sa.Column("current_period_end", sa.DateTime(), nullable=False),
            sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False,
                      server_default=sa.text("false")),
            sa.Column("renewal_order_no", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_subscriptions_account_id", "subscriptions",
                        ["account_id"])

    if not _has_table(insp, "fapiao_requests"):
        op.create_table(
            "fapiao_requests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.String(length=32), nullable=False),
            sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"),
                      nullable=False),
            sa.Column("title", sa.String(length=255), nullable=False),
            sa.Column("tax_no", sa.String(length=32), nullable=False),
            sa.Column("email", sa.String(length=320), nullable=True),
            sa.Column("status", sa.Enum(*FAPIAO_STATUSES, name="fapiao_status"),
                      nullable=False),
            sa.Column("note", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_fapiao_requests_account_id", "fapiao_requests",
                        ["account_id"])
        op.create_index("ix_fapiao_requests_order_id", "fapiao_requests",
                        ["order_id"])
        op.create_index("ix_fapiao_requests_status", "fapiao_requests", ["status"])

    if not _has_table(insp, "recon_reports"):
        op.create_table(
            "recon_reports",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("bill_date", sa.String(length=10), nullable=False),
            sa.Column("bill_rows", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("matched", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("mismatch_count", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("mismatches_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("provider", "bill_date",
                                name="uq_recon_provider_date"),
        )


def downgrade():
    insp = _inspector()
    for table in ("recon_reports", "fapiao_requests", "subscriptions", "orders"):
        if _has_table(insp, table):
            op.drop_table(table)

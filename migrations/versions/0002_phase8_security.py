"""phase 8 — security & abuse hardening

Adds the Phase 8 tables (phone_codes, audit_log, platform_spend, platform_breaker)
and two abuse-control columns on accounts (rpm_limit, daily_cap_yuan).

IDEMPOTENT BY DESIGN. The 0001 baseline builds the WHOLE current model metadata via
create_all, so on a FRESH database these objects already exist by the time this
revision runs — every step here is therefore guarded by a live-schema inspection
and becomes a no-op. On a database still at 0001 from BEFORE Phase 8 (e.g. a
deployed Postgres), the same guards make this the real, additive migration that
creates exactly what is missing. Either way `alembic upgrade head` ends at a schema
identical to the models (zero drift).

Revision ID: 0002_phase8_security
Revises: 0001_baseline
Create Date: 2026-06-10
"""
import sqlalchemy as sa
from alembic import op

from agent_team.models import AUDIT_ACTIONS, CODE_PURPOSES

# revision identifiers, used by Alembic.
revision = "0002_phase8_security"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(insp, name):
    return name in insp.get_table_names()


def _has_column(insp, table, column):
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade():
    insp = _inspector()

    if not _has_table(insp, "phone_codes"):
        op.create_table(
            "phone_codes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("phone", sa.String(length=32), nullable=False),
            sa.Column("code_hash", sa.String(length=64), nullable=False),
            sa.Column("purpose", sa.Enum(*CODE_PURPOSES, name="code_purpose"),
                      nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("consumed", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_phone_codes_phone", "phone_codes", ["phone"])
        op.create_index("ix_phone_codes_created_at", "phone_codes", ["created_at"])

    if not _has_table(insp, "audit_log"):
        op.create_table(
            "audit_log",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.String(length=32), nullable=True),
            sa.Column("action", sa.Enum(*AUDIT_ACTIONS, name="audit_action"),
                      nullable=False),
            sa.Column("ip", sa.String(length=64), nullable=True),
            sa.Column("detail", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_audit_log_account_id", "audit_log", ["account_id"])
        op.create_index("ix_audit_log_action", "audit_log", ["action"])
        op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    if not _has_table(insp, "platform_spend"):
        op.create_table(
            "platform_spend",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("hour_bucket", sa.String(length=13), nullable=False),
            sa.Column("provider", sa.String(length=32), nullable=False),
            sa.Column("account_id", sa.String(length=32), nullable=True),
            sa.Column("yuan", sa.Numeric(precision=12, scale=4), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_platform_spend_hour_bucket", "platform_spend",
                        ["hour_bucket"])
        op.create_index("ix_platform_spend_provider", "platform_spend", ["provider"])
        op.create_index("ix_platform_spend_account_id", "platform_spend",
                        ["account_id"])
        op.create_index("ix_platform_spend_created_at", "platform_spend",
                        ["created_at"])

    if not _has_table(insp, "platform_breaker"):
        op.create_table(
            "platform_breaker",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=64), nullable=False, unique=True),
            sa.Column("tripped", sa.Boolean(), nullable=False),
            sa.Column("scope", sa.String(length=16), nullable=True),
            sa.Column("reason", sa.String(length=255), nullable=True),
            sa.Column("tripped_at", sa.DateTime(), nullable=True),
            sa.Column("cleared_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )

    # accounts.rpm_limit — NOT NULL with a server_default so existing rows backfill
    # to 60 (the model carries a Python-side default=60; env.py doesn't compare
    # server defaults, so this leaves no drift).
    if not _has_column(insp, "accounts", "rpm_limit"):
        op.add_column("accounts", sa.Column(
            "rpm_limit", sa.Integer(), nullable=False, server_default="60"))
    if not _has_column(insp, "accounts", "daily_cap_yuan"):
        op.add_column("accounts", sa.Column(
            "daily_cap_yuan", sa.Numeric(precision=12, scale=2), nullable=True))
    # session_token — nullable, no server_default: legacy rows stay NULL (their
    # sessions aren't force-invalidated), new logins stamp a value.
    if not _has_column(insp, "accounts", "session_token"):
        op.add_column("accounts", sa.Column(
            "session_token", sa.String(length=32), nullable=True))


def downgrade():
    insp = _inspector()
    if _has_column(insp, "accounts", "session_token"):
        op.drop_column("accounts", "session_token")
    if _has_column(insp, "accounts", "daily_cap_yuan"):
        op.drop_column("accounts", "daily_cap_yuan")
    if _has_column(insp, "accounts", "rpm_limit"):
        op.drop_column("accounts", "rpm_limit")
    for table in ("platform_breaker", "platform_spend", "audit_log", "phone_codes"):
        if _has_table(insp, table):
            op.drop_table(table)

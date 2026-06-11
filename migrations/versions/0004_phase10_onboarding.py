"""phase 10 — onboarding, support & ops dashboard

Adds the Phase 10 tables: onboarding_events, support_requests, changelog_seen,
lifecycle_events.

IDEMPOTENT BY DESIGN (same contract as 0002/0003). The 0001 baseline builds the
WHOLE current model metadata via create_all, so on a FRESH database these tables
already exist by the time this revision runs — every step is guarded by a
live-schema inspection and becomes a no-op. On a database migrated before
Phase 10 (a deployed Postgres), the same guards make this the real, additive
migration. Either way `alembic upgrade head` ends at a schema identical to the
models (zero drift).

Revision ID: 0004_phase10_onboarding
Revises: 0003_phase9_payments
Create Date: 2026-06-10
"""
import sqlalchemy as sa
from alembic import op

from agent_team.models import LIFECYCLE_KINDS, ONBOARDING_EVENT_KINDS

# revision identifiers, used by Alembic.
revision = "0004_phase10_onboarding"
down_revision = "0003_phase9_payments"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(insp, name):
    return name in insp.get_table_names()


def upgrade():
    insp = _inspector()

    if not _has_table(insp, "onboarding_events"):
        op.create_table(
            "onboarding_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.String(length=32), nullable=False),
            sa.Column("step", sa.Integer(), nullable=False),
            sa.Column("event", sa.Enum(*ONBOARDING_EVENT_KINDS,
                                       name="onboarding_event_kind"),
                      nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_onboarding_events_account_id", "onboarding_events",
                        ["account_id"])
        op.create_index("ix_onboarding_events_step", "onboarding_events", ["step"])
        op.create_index("ix_onboarding_events_created_at", "onboarding_events",
                        ["created_at"])

    if not _has_table(insp, "support_requests"):
        op.create_table(
            "support_requests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.String(length=32), nullable=True),
            sa.Column("email", sa.String(length=320), nullable=True),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("page", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_support_requests_account_id", "support_requests",
                        ["account_id"])
        op.create_index("ix_support_requests_created_at", "support_requests",
                        ["created_at"])

    if not _has_table(insp, "changelog_seen"):
        op.create_table(
            "changelog_seen",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.String(length=32), nullable=False,
                      unique=True),
            sa.Column("last_seen", sa.String(length=64), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_changelog_seen_account_id", "changelog_seen",
                        ["account_id"])

    if not _has_table(insp, "lifecycle_events"):
        op.create_table(
            "lifecycle_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.String(length=32), nullable=False),
            sa.Column("kind", sa.Enum(*LIFECYCLE_KINDS, name="lifecycle_kind"),
                      nullable=False),
            sa.Column("detail", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_lifecycle_events_account_id", "lifecycle_events",
                        ["account_id"])
        op.create_index("ix_lifecycle_events_kind", "lifecycle_events", ["kind"])


def downgrade():
    insp = _inspector()
    for table in ("lifecycle_events", "changelog_seen", "support_requests",
                  "onboarding_events"):
        if _has_table(insp, table):
            op.drop_table(table)

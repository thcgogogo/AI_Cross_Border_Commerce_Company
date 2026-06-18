"""first-party founder-only analytics: events table

Adds the append-only `events` table — the head-of-funnel data (web page views, the
anonymous Fusion front door, the pre-signup funnel) that has no other row. Founder-only
at read time; reports/payments/subscriptions are NOT counted here.

IDEMPOTENT BY DESIGN (same contract as 0002–0008): on a FRESH database the 0001
baseline's create_all already builds the whole current model metadata, so the guards
no-op; on an existing Postgres at 0008 this is the real additive migration.

Revision ID: 0009_events
Revises: 0008_settlement_recon
Create Date: 2026-06-18
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0009_events"
down_revision = "0008_settlement_recon"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(insp, name):
    return name in insp.get_table_names()


def upgrade():
    insp = _inspector()
    if not _has_table(insp, "events"):
        op.create_table(
            "events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("name", sa.Enum("page_view", "fusion_query", "phone_verified",
                                      "account_created", "report_dim", name="event_name"),
                      nullable=False),
            sa.Column("account_id", sa.String(length=32), nullable=True),
            sa.Column("anon_id", sa.String(length=64), nullable=True),
            sa.Column("path", sa.String(length=255), nullable=True),
            sa.Column("ip_hash", sa.String(length=64), nullable=True),
            sa.Column("props", sa.Text(), nullable=True),
        )
        op.create_index("ix_events_created_at", "events", ["created_at"])
        op.create_index("ix_events_name", "events", ["name"])
        op.create_index("ix_events_account_id", "events", ["account_id"])
        op.create_index("ix_events_anon_id", "events", ["anon_id"])
        op.create_index("ix_events_name_created", "events", ["name", "created_at"])


def downgrade():
    insp = _inspector()
    if _has_table(insp, "events"):
        for ix in ("ix_events_name_created", "ix_events_anon_id", "ix_events_account_id",
                   "ix_events_name", "ix_events_created_at"):
            try:
                op.drop_index(ix, table_name="events")
            except Exception:
                pass
        op.drop_table("events")

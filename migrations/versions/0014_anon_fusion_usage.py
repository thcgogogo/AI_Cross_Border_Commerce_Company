"""Anonymous Fusion teaser free-turn counter (server-side)

Adds `anon_fusion_usage` — the per-anon_id free-turn counter behind the anonymous Fusion
teaser. Moves the count off the client-editable session cookie so the gate is atomic
(reserved before the run, so concurrent requests can't all pass) and survives a cleared
cookie. Keyed on the long-lived `anon_id` cookie (a loose ref, no FK — same convention as
fusion_conversations.account_id).

IDEMPOTENT BY DESIGN (same contract as 0002–0013). On a FRESH database the 0001 baseline's
create_all already builds the whole current model metadata, so the table exists by the
time this revision runs and the step no-ops behind a live-schema inspection. On an existing
Postgres at 0013 the same guard makes this the real, additive migration. No ALTER to any
existing table.

Revision ID: 0014_anon_fusion_usage
Revises: 0013_fusion_conversations
Create Date: 2026-06-20
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0014_anon_fusion_usage"
down_revision = "0013_fusion_conversations"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(insp, name):
    return name in insp.get_table_names()


def upgrade():
    insp = _inspector()
    if not _has_table(insp, "anon_fusion_usage"):
        op.create_table(
            "anon_fusion_usage",
            sa.Column("anon_id", sa.String(length=64), primary_key=True),
            sa.Column("used", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )


def downgrade():
    insp = _inspector()
    if _has_table(insp, "anon_fusion_usage"):
        op.drop_table("anon_fusion_usage")

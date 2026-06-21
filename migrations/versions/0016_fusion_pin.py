"""Fusion conversation pinning (Fusion 2.0 R3 — organize)

Adds fusion_conversations.is_pinned so a user can keep important conversations at the top of
the rail. Additive boolean column, default 0. Search (title filter) and export (markdown)
need no schema — they reuse existing columns.

IDEMPOTENT BY DESIGN (same contract as 0002–0015). On a FRESH database the 0001 baseline's
create_all already builds the whole current model metadata, so the column exists by the time
this revision runs and the step no-ops behind a live-schema inspection. On an existing
Postgres at 0015 the guard makes this the real, additive ALTER.

Revision ID: 0016_fusion_pin
Revises: 0015_fusion_attachments
Create Date: 2026-06-21
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0016_fusion_pin"
down_revision = "0015_fusion_attachments"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(insp, table, column):
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade():
    insp = _inspector()
    if "fusion_conversations" in insp.get_table_names() and not _has_column(
            insp, "fusion_conversations", "is_pinned"):
        op.add_column("fusion_conversations",
                      sa.Column("is_pinned", sa.Boolean(), nullable=False,
                                server_default="0"))


def downgrade():
    insp = _inspector()
    if "fusion_conversations" in insp.get_table_names() and _has_column(
            insp, "fusion_conversations", "is_pinned"):
        op.drop_column("fusion_conversations", "is_pinned")

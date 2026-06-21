"""Fusion per-conversation persona (Fusion 2.0 R6)

Adds fusion_conversations.system_prompt — a per-conversation persona (a system instruction)
the council follows. ENCRYPTED at rest by the application layer (crypto.encrypt_string), so
the column is plain Text here. Additive, nullable.

IDEMPOTENT BY DESIGN (same contract as 0002–0016). On a FRESH database the 0001 baseline's
create_all already builds the whole current model metadata, so the column exists by the time
this revision runs and the step no-ops behind a live-schema inspection. On an existing
Postgres at 0016 the guard makes this the real, additive ALTER.

Revision ID: 0017_fusion_persona
Revises: 0016_fusion_pin
Create Date: 2026-06-21
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0017_fusion_persona"
down_revision = "0016_fusion_pin"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_column(insp, table, column):
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade():
    insp = _inspector()
    if "fusion_conversations" in insp.get_table_names() and not _has_column(
            insp, "fusion_conversations", "system_prompt"):
        op.add_column("fusion_conversations",
                      sa.Column("system_prompt", sa.Text(), nullable=True))


def downgrade():
    insp = _inspector()
    if "fusion_conversations" in insp.get_table_names() and _has_column(
            insp, "fusion_conversations", "system_prompt"):
        op.drop_column("fusion_conversations", "system_prompt")

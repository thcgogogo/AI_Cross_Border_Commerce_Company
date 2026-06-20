"""Fusion file attachments (Fusion 2.0 R1 — document understanding)

Adds `fusion_attachments` — files a signed-in user attaches to a Fusion conversation. The
raw file is encrypted at rest by the application layer (crypto.encrypt_file_in_place) and
the extracted text (file_tools.parse_file) is stored encrypted in `parsed_text`, so the
columns are plain Text/String here. Account-owned + conversation-scoped (a loose ref to
fusion_conversations.id with an FK; account_id mirrors the convo owner for a fast
ownership check). `message_index` is NULL while pending and stamped when the turn persists.

IDEMPOTENT BY DESIGN (same contract as 0002–0014). On a FRESH database the 0001 baseline's
create_all already builds the whole current model metadata, so the table exists by the time
this revision runs and the step no-ops behind a live-schema inspection. On an existing
Postgres at 0014 the same guard makes this the real, additive migration. No ALTER to any
existing table.

Revision ID: 0015_fusion_attachments
Revises: 0014_anon_fusion_usage
Create Date: 2026-06-20
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0015_fusion_attachments"
down_revision = "0014_anon_fusion_usage"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(insp, name):
    return name in insp.get_table_names()


def upgrade():
    insp = _inspector()
    if not _has_table(insp, "fusion_attachments"):
        op.create_table(
            "fusion_attachments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("conversation_id", sa.Integer(),
                      sa.ForeignKey("fusion_conversations.id"), nullable=False),
            sa.Column("account_id", sa.String(length=32), nullable=False),
            sa.Column("message_index", sa.Integer(), nullable=True),
            sa.Column("filename", sa.String(length=255), nullable=False),
            sa.Column("media_type", sa.String(length=100), nullable=True),
            sa.Column("kind", sa.String(length=16), nullable=False,
                      server_default="document"),
            sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("content_hash", sa.String(length=64), nullable=True),
            sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("stored_path", sa.String(length=512), nullable=True),
            sa.Column("parsed_text", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False,
                      server_default="ready"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_fusion_attachments_conversation_id", "fusion_attachments",
                        ["conversation_id"])
        op.create_index("ix_fusion_attachments_account_id", "fusion_attachments",
                        ["account_id"])


def downgrade():
    insp = _inspector()
    if _has_table(insp, "fusion_attachments"):
        op.drop_table("fusion_attachments")

"""Fusion chat: fusion_conversations + fusion_messages

Adds the two new tables behind the sign-in-required, multi-turn Fusion chatbot with
per-conversation memory. A conversation is ACCOUNT-owned (account_id, a loose ref to
accounts.id — no SQL FK, mirroring the Company↔Account natural-key convention); each
message's `content` + `answers_json` are encrypted at rest by the application layer
(crypto.encrypt_string), so the columns are plain Text here.

IDEMPOTENT BY DESIGN (same contract as 0002–0012). On a FRESH database the 0001
baseline's create_all already builds the whole current model metadata, so these objects
exist by the time this revision runs and every step no-ops behind a live-schema
inspection. On an existing Postgres at 0012 the same guards make this the real, additive
migration. No ALTER to any existing table.

Revision ID: 0013_fusion_conversations
Revises: 0012_sched_engine
Create Date: 2026-06-20
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0013_fusion_conversations"
down_revision = "0012_sched_engine"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(insp, name):
    return name in insp.get_table_names()


def upgrade():
    insp = _inspector()

    if not _has_table(insp, "fusion_conversations"):
        op.create_table(
            "fusion_conversations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.String(length=32), nullable=False),
            sa.Column("title", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False,
                      server_default="active"),
            sa.Column("model_tier", sa.String(length=16), nullable=True),
            sa.Column("message_count", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("last_message_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_fusion_conversations_account_id", "fusion_conversations",
                        ["account_id"])
        op.create_index("ix_fusion_conversations_last_message_at",
                        "fusion_conversations", ["last_message_at"])

    if not _has_table(insp, "fusion_messages"):
        op.create_table(
            "fusion_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("conversation_id", sa.Integer(),
                      sa.ForeignKey("fusion_conversations.id"), nullable=False),
            sa.Column("message_index", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(length=16), nullable=False),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("answers_json", sa.Text(), nullable=True),
            sa.Column("fused_by", sa.String(length=64), nullable=True),
            sa.Column("fused_by_label", sa.String(length=128), nullable=True),
            sa.Column("fused_note", sa.Text(), nullable=True),
            sa.Column("models_used", sa.String(length=255), nullable=True),
            sa.Column("input_tokens", sa.Integer(), nullable=True),
            sa.Column("output_tokens", sa.Integer(), nullable=True),
            sa.Column("cost_yuan", sa.Numeric(precision=12, scale=6), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False,
                      server_default="ok"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_fusion_messages_conversation_id", "fusion_messages",
                        ["conversation_id"])
        op.create_index("ix_fusion_messages_convo_index", "fusion_messages",
                        ["conversation_id", "message_index"])


def downgrade():
    insp = _inspector()
    for table in ("fusion_messages", "fusion_conversations"):
        if _has_table(insp, table):
            op.drop_table(table)

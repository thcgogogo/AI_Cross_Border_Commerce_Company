"""phase 11 — desktop daemon ("Tangent Agent"): devices + agent catalog/install/run

Adds the Phase 11 tables (devices, agent_catalog, agent_install, agent_run) and two
columns on runs (origin, device_id) so device runs share the runs lifecycle.

IDEMPOTENT BY DESIGN (same contract as 0002–0006). On a FRESH database the 0001
baseline's create_all already builds the WHOLE current model metadata, so on a fresh
DB these objects exist by the time this revision runs and every step here no-ops
behind a live-schema inspection. On a pre-Phase-11 Postgres still at 0006, the same
guards make this the real, additive migration that creates exactly what is missing.
Either way `alembic upgrade head` ends at a schema identical to the models (zero
drift). runs.kind is String(32) (NOT a named enum), so adding 'device_run' needs NO
ALTER TYPE and no schema change here.

Revision ID: 0007_phase11_devices
Revises: 0006_phase7_single_report
Create Date: 2026-06-15
"""
import sqlalchemy as sa
from alembic import op

from agent_team.models import AGENT_RUN_STATUSES, DEVICE_STATUSES

# revision identifiers, used by Alembic.
revision = "0007_phase11_devices"
down_revision = "0006_phase7_single_report"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(insp, name):
    return name in insp.get_table_names()


def _has_column(insp, table, column):
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def _column_missing(insp, table, column):
    """True when `table` exists but lacks `column`. An ABSENT table needs no work
    (create_all / the baseline gives it the complete table) — same contract as 0002–0006."""
    if table not in insp.get_table_names():
        return False
    return all(c["name"] != column for c in insp.get_columns(table))


def _has_index(insp, table, index_name):
    if table not in insp.get_table_names():
        return False
    return any(ix["name"] == index_name for ix in insp.get_indexes(table))


def upgrade():
    insp = _inspector()

    if not _has_table(insp, "devices"):
        op.create_table(
            "devices",
            sa.Column("id", sa.String(length=32), primary_key=True),
            sa.Column("account_id", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=True),
            sa.Column("platform", sa.String(length=32), nullable=True),
            sa.Column("token_hash", sa.String(length=64), nullable=True),
            sa.Column("pairing_code_hash", sa.String(length=64), nullable=True),
            sa.Column("pairing_expires_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.Enum(*DEVICE_STATUSES, name="device_status"),
                      nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_devices_account_id", "devices", ["account_id"])
        op.create_index("ix_devices_token_hash", "devices", ["token_hash"])

    # agent_catalog BEFORE agent_run so the agent_run.agent_catalog_id FK target exists.
    if not _has_table(insp, "agent_catalog"):
        op.create_table(
            "agent_catalog",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("agent_id", sa.String(length=64), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("name_cn", sa.String(length=128), nullable=True),
            sa.Column("manifest_json", sa.Text(), nullable=True),
            sa.Column("content_hash", sa.String(length=71), nullable=True),
            sa.Column("published", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("agent_id", "version",
                                name="uq_agent_catalog_agent_version"),
        )
        op.create_index("ix_agent_catalog_agent_id", "agent_catalog", ["agent_id"])
        op.create_index("ix_agent_catalog_content_hash", "agent_catalog", ["content_hash"])

    if not _has_table(insp, "agent_install"):
        op.create_table(
            "agent_install",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_id", sa.String(length=32), nullable=False),
            sa.Column("agent_id", sa.String(length=64), nullable=False),
            sa.Column("pinned_version", sa.Integer(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("account_id", "agent_id",
                                name="uq_agent_install_account_agent"),
        )
        op.create_index("ix_agent_install_account_id", "agent_install", ["account_id"])
        op.create_index("ix_agent_install_agent_id", "agent_install", ["agent_id"])

    if not _has_table(insp, "agent_run"):
        op.create_table(
            "agent_run",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=True),
            sa.Column("device_id", sa.String(length=32), nullable=True),
            sa.Column("account_id", sa.String(length=32), nullable=True),
            sa.Column("agent_catalog_id", sa.Integer(),
                      sa.ForeignKey("agent_catalog.id"), nullable=True),
            sa.Column("agent_id", sa.String(length=64), nullable=True),
            sa.Column("manifest_hash", sa.String(length=71), nullable=True),
            sa.Column("status", sa.Enum(*AGENT_RUN_STATUSES, name="agent_run_status"),
                      nullable=False),
            sa.Column("cost_yuan", sa.Numeric(precision=12, scale=4), nullable=False,
                      server_default="0"),
            sa.Column("report_summary", sa.Text(), nullable=True),
            sa.Column("report_sha256", sa.String(length=64), nullable=True),
            sa.Column("report_bytes", sa.Integer(), nullable=True),
            sa.Column("deliverable_kind", sa.String(length=32), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_agent_run_run_id", "agent_run", ["run_id"])
        op.create_index("ix_agent_run_device_id", "agent_run", ["device_id"])
        op.create_index("ix_agent_run_account_id", "agent_run", ["account_id"])
        op.create_index("ix_agent_run_agent_catalog_id", "agent_run", ["agent_catalog_id"])
        op.create_index("ix_agent_run_agent_id", "agent_run", ["agent_id"])

    # runs.origin — NOT NULL with a server_default so existing rows backfill to 'cloud'.
    if _column_missing(insp, "runs", "origin"):
        op.add_column("runs", sa.Column("origin", sa.String(length=16),
                                        nullable=False, server_default="cloud"))
    # runs.device_id (+ its index) — only when the column is actually being added, so a
    # fresh create_all DB (which already has both) never double-creates the index.
    if _column_missing(insp, "runs", "device_id"):
        op.add_column("runs", sa.Column("device_id", sa.String(length=32), nullable=True))
        op.create_index("ix_runs_device_id", "runs", ["device_id"])


def downgrade():
    insp = _inspector()
    if _has_index(insp, "runs", "ix_runs_device_id"):
        op.drop_index("ix_runs_device_id", table_name="runs")
    if _has_column(insp, "runs", "device_id"):
        op.drop_column("runs", "device_id")
    if _has_column(insp, "runs", "origin"):
        op.drop_column("runs", "origin")
    for table in ("agent_run", "agent_install", "agent_catalog", "devices"):
        if _has_table(insp, table):
            op.drop_table(table)

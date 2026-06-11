"""baseline schema (Phase 7)

Builds the ENTIRE schema from the SQLAlchemy models in one shot, so a fresh
`alembic upgrade head` on an empty database produces a schema identical to
Base.metadata — zero drift by construction, because the migration literally IS
the models' metadata.

Tables created: companies, runs, metrics, company_profile, glossary,
report_preferences, scheduled_jobs, deliveries, feedback, accounts,
scheduler_heartbeat (plus the named enums: run_status, source_mode,
delivery_status, feedback_vote, account_status).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-10
"""
from alembic import op

from agent_team import db
from agent_team import models  # noqa: F401 — registers every table on Base.metadata

# revision identifiers, used by Alembic.
revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # create_all against the migration's bind builds exactly the model schema, so
    # there is never drift between "upgrade head" and the models.
    db.Base.metadata.create_all(bind=op.get_bind())


def downgrade():
    db.Base.metadata.drop_all(bind=op.get_bind())

"""Add opportunity_assessments (BlackbirdBot screening verdicts)

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-06 00:00:00.000000

One row per posted :mag: Opportunity Assessment. Before this table an assessment
existed only as a Slack message, so the rubric's machine-readable verdict
(Part C.6) had nowhere to live and nothing was queryable or rankable.

Every rubric column is nullable: a sparse verdict must still be recorded.
Downgrade is idempotent (if_exists) per the branch convention (0022/0023/0024).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "opportunity_assessments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("simulation_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_id", sa.String(length=50), nullable=False),
        sa.Column("subject_agent_id", sa.String(length=50), nullable=True),
        sa.Column("channel_name", sa.String(length=100), nullable=False),
        sa.Column("slack_ts", sa.String(length=50), nullable=True),
        sa.Column("company_or_project", sa.Text(), nullable=True),
        sa.Column("funnel_stage", sa.String(length=20), nullable=True),
        sa.Column("recommendation", sa.String(length=30), nullable=True),
        sa.Column("confidence", sa.String(length=20), nullable=True),
        sa.Column("weighted_score", sa.Float(), nullable=True),
        sa.Column("band", sa.String(length=20), nullable=True),
        sa.Column("gating", postgresql.JSONB(), nullable=True),
        sa.Column("scores", postgresql.JSONB(), nullable=True),
        sa.Column("red_flags", postgresql.JSONB(), nullable=True),
        sa.Column("derisking_milestones", postgresql.JSONB(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("raw_verdict", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["simulation_run_id"], ["simulation_runs.id"], ondelete="CASCADE"
        ),
    )
    # Index names must match what SQLAlchemy's index=True generates on the model
    # (ix_<table>_<column>), or autogenerate reports permanent phantom drift.
    op.create_index(
        "ix_opportunity_assessments_simulation_run_id", "opportunity_assessments",
        ["simulation_run_id"],
    )
    op.create_index(
        "ix_opportunity_assessments_agent_id", "opportunity_assessments", ["agent_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_opportunity_assessments_agent_id", "opportunity_assessments",
                  if_exists=True)
    op.drop_index("ix_opportunity_assessments_simulation_run_id",
                  "opportunity_assessments", if_exists=True)
    op.drop_table("opportunity_assessments", if_exists=True)

"""Add assessment_drops (make a lost verdict visible)

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-14 22:30:00.000000

Every way a screening verdict can be lost is currently silent: the concluding
reply is already posted, the thread closes normally, and the only trace is a
WARNING in a container log. That makes an empty /admin/assessments page
indistinguishable from "nothing has been screened yet" — which is exactly the
state this deployment was in, with zero rows across four runs.

One row per dropped verdict, so the admin page can say so.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assessment_drops",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "simulation_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("agent_id", sa.String(50), nullable=False),
        sa.Column("subject_agent_id", sa.String(50), nullable=True),
        sa.Column("thread_id", sa.String(50), nullable=True),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_assessment_drops_simulation_run_id",
        "assessment_drops",
        ["simulation_run_id"],
    )
    op.create_index("ix_assessment_drops_reason", "assessment_drops", ["reason"])


def downgrade() -> None:
    op.drop_index("ix_assessment_drops_reason", table_name="assessment_drops")
    op.drop_index(
        "ix_assessment_drops_simulation_run_id", table_name="assessment_drops"
    )
    op.drop_table("assessment_drops")

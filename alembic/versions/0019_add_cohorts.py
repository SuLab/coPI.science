"""Add cohorts + cohort_memberships tables for agent interaction isolation

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-14 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A cohort is a named group of agents permitted to act on each other's
    # activity during simulation. See specs/cohort-system.md.
    op.create_table(
        "cohorts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=48), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # agent_id is the AgentRegistry slug (no FK — agent rows may not exist at
    # membership-creation time; the app validates at add time).
    op.create_table(
        "cohort_memberships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "cohort_id",
            UUID(as_uuid=True),
            sa.ForeignKey("cohorts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(length=50), nullable=False),
        sa.Column(
            "added_by",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("cohort_id", "agent_id", name="uq_cohort_membership_cohort_agent"),
    )
    op.create_index(
        "ix_cohort_memberships_cohort_id", "cohort_memberships", ["cohort_id"]
    )
    op.create_index(
        "ix_cohort_memberships_agent_id", "cohort_memberships", ["agent_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_cohort_memberships_agent_id", table_name="cohort_memberships")
    op.drop_index("ix_cohort_memberships_cohort_id", table_name="cohort_memberships")
    op.drop_table("cohort_memberships")
    op.drop_table("cohorts")

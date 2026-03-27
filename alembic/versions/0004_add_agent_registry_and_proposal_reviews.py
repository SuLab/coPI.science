"""Add agents registry and proposal_reviews tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-27 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create agents table
    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", sa.String(50), unique=True, nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            unique=True,
            nullable=True,
        ),
        sa.Column("bot_name", sa.String(100), nullable=False),
        sa.Column("pi_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("slack_bot_token", sa.Text, nullable=True),
        sa.Column("slack_app_token", sa.Text, nullable=True),
        sa.Column("slack_user_id", sa.String(50), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "approved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_agents_agent_id", "agents", ["agent_id"])
    op.create_index("ix_agents_user_id", "agents", ["user_id"])

    # 2. Create proposal_reviews table
    op.create_table(
        "proposal_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "thread_decision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("thread_decisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(50), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rating", sa.SmallInteger, nullable=False),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_proposal_reviews_decision_agent",
        "proposal_reviews",
        ["thread_decision_id", "agent_id"],
    )
    op.create_index(
        "ix_proposal_reviews_agent_id", "proposal_reviews", ["agent_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_proposal_reviews_agent_id")
    op.drop_constraint("uq_proposal_reviews_decision_agent", "proposal_reviews")
    op.drop_table("proposal_reviews")

    op.drop_index("ix_agents_user_id")
    op.drop_index("ix_agents_agent_id")
    op.drop_table("agents")

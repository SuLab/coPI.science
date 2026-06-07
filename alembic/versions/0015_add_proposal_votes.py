"""Add proposal_votes (public no-login votes on graph proposals)

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-07 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "proposal_votes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "thread_decision_id",
            UUID(as_uuid=True),
            sa.ForeignKey("thread_decisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("thread_id", sa.String(length=50), nullable=True),
        sa.Column("agent_a", sa.String(length=50), nullable=True),
        sa.Column("agent_b", sa.String(length=50), nullable=True),
        sa.Column("vote", sa.String(length=8), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("voter_token", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint(
            "thread_decision_id", "voter_token",
            name="uq_proposal_vote_decision_voter",
        ),
    )
    op.create_index(
        "ix_proposal_votes_thread_decision_id",
        "proposal_votes",
        ["thread_decision_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_proposal_votes_thread_decision_id", table_name="proposal_votes")
    op.drop_table("proposal_votes")

"""Add matchmaker_proposals table

Revision ID: 0014
Revises: 0013
Create Date: 2026-04-21 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "matchmaker_proposals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pi_a_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "pi_b_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("proposal_md", sa.Text, nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("confidence", sa.String(20), nullable=False),
        sa.Column("llm_model", sa.String(100), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=True),
        sa.Column("output_tokens", sa.Integer, nullable=True),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_matchmaker_proposals_pi_a_id", "matchmaker_proposals", ["pi_a_id"])
    op.create_index("ix_matchmaker_proposals_pi_b_id", "matchmaker_proposals", ["pi_b_id"])


def downgrade() -> None:
    op.drop_index("ix_matchmaker_proposals_pi_b_id", table_name="matchmaker_proposals")
    op.drop_index("ix_matchmaker_proposals_pi_a_id", table_name="matchmaker_proposals")
    op.drop_table("matchmaker_proposals")

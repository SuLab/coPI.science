"""Add podcast_episodes table

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-09 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "podcast_episodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", sa.String(50), nullable=False),
        sa.Column("episode_date", sa.Date, nullable=False),
        sa.Column("pmid", sa.String(100), nullable=False),
        sa.Column("paper_title", sa.String(500), nullable=False),
        sa.Column("paper_authors", sa.String(500), nullable=False),
        sa.Column("paper_journal", sa.String(255), nullable=False),
        sa.Column("paper_year", sa.Integer, nullable=False),
        sa.Column("text_summary", sa.Text, nullable=False),
        sa.Column("audio_file_path", sa.String(500), nullable=True),
        sa.Column("audio_duration_seconds", sa.Integer, nullable=True),
        sa.Column("slack_delivered", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("selection_justification", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_podcast_episodes_agent_id", "podcast_episodes", ["agent_id"])
    op.create_index("ix_podcast_episodes_episode_date", "podcast_episodes", ["episode_date"])
    op.create_unique_constraint(
        "uq_podcast_agent_date", "podcast_episodes", ["agent_id", "episode_date"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_podcast_agent_date", "podcast_episodes")
    op.drop_index("ix_podcast_episodes_episode_date")
    op.drop_index("ix_podcast_episodes_agent_id")
    op.drop_table("podcast_episodes")

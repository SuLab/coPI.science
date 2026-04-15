"""Extend podcast tables to support plain ORCID users (no agent required)

Adds nullable user_id FK to podcast_preferences and podcast_episodes so that
any user who has completed onboarding can receive daily research briefings
without needing an approved AgentRegistry entry.

Changes:
  - podcast_preferences.agent_id: NOT NULL → nullable
  - podcast_preferences.user_id:  new nullable FK → users.id, unique index
  - podcast_episodes.agent_id:    NOT NULL → nullable
  - podcast_episodes.user_id:     new nullable FK → users.id
  - podcast_episodes: partial unique index on (user_id, episode_date) WHERE user_id IS NOT NULL

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-14 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- podcast_preferences ---
    # Make agent_id nullable (existing agent rows keep their values)
    op.alter_column("podcast_preferences", "agent_id", nullable=True)

    # Add user_id FK column
    op.add_column(
        "podcast_preferences",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_podcast_preferences_user_id",
        "podcast_preferences",
        ["user_id"],
        unique=True,
    )

    # --- podcast_episodes ---
    # Make agent_id nullable (existing agent rows keep their values)
    op.alter_column("podcast_episodes", "agent_id", nullable=True)

    # Add user_id FK column
    op.add_column(
        "podcast_episodes",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    # Partial unique index: one episode per user per day (only when user_id is set)
    op.execute(
        "CREATE UNIQUE INDEX ix_podcast_episodes_user_date "
        "ON podcast_episodes (user_id, episode_date) "
        "WHERE user_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_podcast_episodes_user_date")
    op.drop_column("podcast_episodes", "user_id")
    op.alter_column("podcast_episodes", "agent_id", nullable=False)

    op.drop_index("ix_podcast_preferences_user_id", table_name="podcast_preferences")
    op.drop_column("podcast_preferences", "user_id")
    op.alter_column("podcast_preferences", "agent_id", nullable=False)

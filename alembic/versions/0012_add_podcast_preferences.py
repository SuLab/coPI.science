"""Add podcast_preferences table

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-14 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ARRAY

from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "podcast_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", sa.String(50), nullable=False),
        sa.Column("voice_id", sa.String(100), nullable=True),
        sa.Column(
            "extra_keywords",
            ARRAY(sa.String),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "preferred_journals",
            ARRAY(sa.String),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "deprioritized_journals",
            ARRAY(sa.String),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_podcast_preferences_agent_id",
        "podcast_preferences",
        ["agent_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_podcast_preferences_agent_id", table_name="podcast_preferences")
    op.drop_table("podcast_preferences")

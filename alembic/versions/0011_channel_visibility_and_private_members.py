"""Channel visibility + private channel members

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-20 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. agent_channels: visibility, migrated_from_channel_id
    op.add_column(
        "agent_channels",
        sa.Column("visibility", sa.String(20), nullable=False, server_default="public"),
    )
    op.alter_column("agent_channels", "visibility", server_default=None)
    op.add_column(
        "agent_channels",
        sa.Column("migrated_from_channel_id", sa.String(100), nullable=True),
    )

    # 2. agent_messages: visibility (denormalized from agent_channels for fast filtering)
    op.add_column(
        "agent_messages",
        sa.Column("visibility", sa.String(20), nullable=False, server_default="public"),
    )
    op.alter_column("agent_messages", "visibility", server_default=None)

    # 3. thread_decisions: origin_visibility, refined_in_channel
    op.add_column(
        "thread_decisions",
        sa.Column("origin_visibility", sa.String(20), nullable=False, server_default="public"),
    )
    op.alter_column("thread_decisions", "origin_visibility", server_default=None)
    op.add_column(
        "thread_decisions",
        sa.Column("refined_in_channel", sa.String(100), nullable=True),
    )

    # 4. private_channel_members — authoritative membership for collab_private channels
    op.create_table(
        "private_channel_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "agent_channel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_channels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(50), nullable=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column(
            "added_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(agent_id IS NULL) != (user_id IS NULL)",
            name="pcm_exactly_one_of_agent_or_user",
        ),
    )
    # Partial unique indexes: one bot-per-channel row, one user-per-channel row
    op.create_index(
        "ix_pcm_channel_agent",
        "private_channel_members",
        ["agent_channel_id", "agent_id"],
        unique=True,
        postgresql_where=sa.text("agent_id IS NOT NULL"),
    )
    op.create_index(
        "ix_pcm_channel_user",
        "private_channel_members",
        ["agent_channel_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_pcm_channel_user", table_name="private_channel_members")
    op.drop_index("ix_pcm_channel_agent", table_name="private_channel_members")
    op.drop_table("private_channel_members")
    op.drop_column("thread_decisions", "refined_in_channel")
    op.drop_column("thread_decisions", "origin_visibility")
    op.drop_column("agent_messages", "visibility")
    op.drop_column("agent_channels", "migrated_from_channel_id")
    op.drop_column("agent_channels", "visibility")

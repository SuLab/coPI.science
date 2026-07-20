"""Add conversation-content columns to agent_messages (DB becomes primary store)

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-20 00:00:00.000000

Makes the local DB the primary store for agent conversations: agent_messages now
carries the message body and sender metadata (previously only in Slack + the
in-memory MessageLog), plus nullable Slack-mirror mapping columns. See
specs/local-db-conversations.md.
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Content columns — DB is now the durable conversation store.
    op.add_column(
        "agent_messages",
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "agent_messages",
        sa.Column("sender_name", sa.String(100), nullable=False, server_default=""),
    )
    op.add_column(
        "agent_messages",
        sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "agent_messages",
        sa.Column("posted_at", sa.Float(), nullable=False, server_default="0"),
    )
    # Slack-mirror mapping (NULL when Slack is off / message is DB-origin).
    op.add_column("agent_messages", sa.Column("slack_ts", sa.String(50), nullable=True))
    op.add_column("agent_messages", sa.Column("slack_channel_id", sa.String(100), nullable=True))
    op.add_column("agent_messages", sa.Column("slack_thread_ts", sa.String(50), nullable=True))

    # agent_id becomes the sender_agent_id: NULL for human/PI messages.
    op.alter_column("agent_messages", "agent_id", existing_type=sa.String(50), nullable=True)

    # Idempotency + rebuild/mirror indexes.
    op.create_unique_constraint(
        "uq_agent_messages_run_ts", "agent_messages", ["simulation_run_id", "message_ts"]
    )
    op.create_index(
        "ix_agent_messages_run_posted",
        "agent_messages",
        ["simulation_run_id", "posted_at"],
    )
    op.create_index(
        "ix_agent_messages_run_channel_posted",
        "agent_messages",
        ["simulation_run_id", "channel_name", "posted_at"],
    )
    op.create_index(
        "ix_agent_messages_run_slack_ts",
        "agent_messages",
        ["simulation_run_id", "slack_ts"],
        postgresql_where=sa.text("slack_ts IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_agent_messages_run_slack_ts", table_name="agent_messages")
    op.drop_index("ix_agent_messages_run_channel_posted", table_name="agent_messages")
    op.drop_index("ix_agent_messages_run_posted", table_name="agent_messages")
    op.drop_constraint("uq_agent_messages_run_ts", "agent_messages", type_="unique")
    op.alter_column("agent_messages", "agent_id", existing_type=sa.String(50), nullable=False)
    op.drop_column("agent_messages", "slack_thread_ts")
    op.drop_column("agent_messages", "slack_channel_id")
    op.drop_column("agent_messages", "slack_ts")
    op.drop_column("agent_messages", "posted_at")
    op.drop_column("agent_messages", "is_bot")
    op.drop_column("agent_messages", "sender_name")
    op.drop_column("agent_messages", "content")

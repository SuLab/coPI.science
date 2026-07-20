"""Add pi_dm_messages table (durable PI<->bot direct messages)

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-20 00:00:00.000000

DMs never entered the shared message log, so they had no durable home. This
table stores them so a PI can DM their bot (standing instructions, questions)
with Slack fully off. See specs/local-db-conversations.md.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pi_dm_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "simulation_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(50), nullable=False),
        sa.Column("pi_user_id", sa.String(50), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("inbound", "outbound", name="pi_dm_direction_enum"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sender_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("ts", sa.String(50), nullable=False),
        sa.Column("slack_ts", sa.String(50), nullable=True),
        sa.Column("posted_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index(
        "ix_pi_dm_run_agent_posted", "pi_dm_messages",
        ["simulation_run_id", "agent_id", "posted_at"],
    )
    op.create_index(
        "ix_pi_dm_run_direction_posted", "pi_dm_messages",
        ["simulation_run_id", "direction", "posted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pi_dm_run_direction_posted", table_name="pi_dm_messages")
    op.drop_index("ix_pi_dm_run_agent_posted", table_name="pi_dm_messages")
    op.drop_table("pi_dm_messages")
    sa.Enum(name="pi_dm_direction_enum").drop(op.get_bind(), checkfirst=True)

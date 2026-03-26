"""Add thread_decisions table, thread_ts and flexible phase to agent_messages, channel to llm_call_logs

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-26 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create thread_decisions table
    op.create_table(
        "thread_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "simulation_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("thread_id", sa.String(50), nullable=False),
        sa.Column("channel", sa.String(100), nullable=False),
        sa.Column("agent_a", sa.String(50), nullable=False),
        sa.Column("agent_b", sa.String(50), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum("proposal", "no_proposal", "timeout", name="thread_outcome_enum"),
            nullable=False,
        ),
        sa.Column("summary_text", sa.Text, nullable=True),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_thread_decisions_run_id", "thread_decisions", ["simulation_run_id"])

    # 2. Add thread_ts column to agent_messages
    op.add_column("agent_messages", sa.Column("thread_ts", sa.String(50), nullable=True))

    # 3. Change phase column from enum to varchar(30) for flexibility
    # Drop the old enum constraint and alter the column type
    op.alter_column(
        "agent_messages",
        "phase",
        type_=sa.String(30),
        existing_type=sa.Enum("decide", "respond", name="agent_message_phase_enum"),
        existing_nullable=False,
        postgresql_using="phase::text",
    )
    # Drop the old enum type
    op.execute("DROP TYPE IF EXISTS agent_message_phase_enum")

    # 4. Add channel column to llm_call_logs — skip if already exists
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'llm_call_logs' AND column_name = 'channel'"
    ))
    if result.fetchone() is None:
        op.add_column("llm_call_logs", sa.Column("channel", sa.String(100), nullable=True))


def downgrade() -> None:
    # Remove channel from llm_call_logs
    try:
        op.drop_column("llm_call_logs", "channel")
    except Exception:
        pass

    # Restore phase enum
    phase_enum = sa.Enum("decide", "respond", name="agent_message_phase_enum")
    phase_enum.create(op.get_bind(), checkfirst=True)
    op.alter_column(
        "agent_messages",
        "phase",
        type_=phase_enum,
        existing_type=sa.String(30),
        existing_nullable=False,
        postgresql_using="phase::agent_message_phase_enum",
    )

    # Remove thread_ts from agent_messages
    op.drop_column("agent_messages", "thread_ts")

    # Drop thread_decisions
    op.drop_index("ix_thread_decisions_run_id")
    op.drop_table("thread_decisions")
    op.execute("DROP TYPE IF EXISTS thread_outcome_enum")

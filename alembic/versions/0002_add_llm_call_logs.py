"""Add llm_call_logs table

Revision ID: 0002
Revises: 0001

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_call_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "simulation_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(50), nullable=False),
        sa.Column("phase", sa.String(30), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("system_prompt", sa.Text, nullable=False),
        sa.Column("messages_json", postgresql.JSON, nullable=False),
        sa.Column("response_text", sa.Text, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Float, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_llm_call_logs_run_id", "llm_call_logs", ["simulation_run_id"])
    op.create_index("ix_llm_call_logs_agent_id", "llm_call_logs", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_llm_call_logs_agent_id")
    op.drop_index("ix_llm_call_logs_run_id")
    op.drop_table("llm_call_logs")

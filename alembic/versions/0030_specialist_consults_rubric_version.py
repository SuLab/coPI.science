"""Add specialist_consults; stamp assessments with the rubric document version

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-20 00:00:00.000000

Two additive changes from the same plan
(docs/plans/2026-08-20-assessments-rca-ux-specialist-visibility.md):

* ``specialist_consults`` — one row per successful panel consult. Until now the
  panel lived only in engine memory (lost on restart, hence the unverifiable
  ``missing_domains=[]`` state 0029 documented) and in unlinked llm_call_logs
  rows. A durable row per consult is what makes the floor survive a restart and
  gives the admin UI something to show.
* ``opportunity_assessments.rubric_version`` / ``.rubric_content_hash`` — which
  rubric document (prompts/rubric/blackbird-rubric.toml) scored the row. NULL on
  every pre-existing row, deliberately: they were scored by the pre-extraction
  hardcoded rubric.

Both nullable/additive, so old code against the new schema keeps working; the
reverse needs the usual migrate-before-serve ordering once the new code maps
these columns (same reasoning as 0028/0029).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "specialist_consults",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "simulation_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.String(50), nullable=False),
        sa.Column("subject_agent_id", sa.String(50), nullable=True),
        sa.Column("thread_id", sa.String(50), nullable=True),
        sa.Column("channel_name", sa.String(100), nullable=True),
        sa.Column("domain", sa.String(20), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("context_excerpt", sa.Text(), nullable=True),
        sa.Column("verdict_signal", sa.String(10), nullable=False),
        sa.Column("confidence", sa.String(10), nullable=False),
        sa.Column("concerns", postgresql.JSONB(), nullable=True),
        sa.Column("questions_to_ask", postgresql.JSONB(), nullable=True),
        sa.Column("raw_opinion", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_specialist_consults_simulation_run_id",
        "specialist_consults",
        ["simulation_run_id"],
    )
    op.create_index(
        "ix_specialist_consults_thread_id", "specialist_consults", ["thread_id"]
    )
    op.create_index(
        "ix_specialist_consults_subject_agent_id",
        "specialist_consults",
        ["subject_agent_id"],
    )

    op.add_column(
        "opportunity_assessments",
        sa.Column("rubric_version", sa.String(20), nullable=True),
    )
    op.add_column(
        "opportunity_assessments",
        sa.Column("rubric_content_hash", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunity_assessments", "rubric_content_hash")
    op.drop_column("opportunity_assessments", "rubric_version")
    op.drop_index(
        "ix_specialist_consults_subject_agent_id", table_name="specialist_consults"
    )
    op.drop_index("ix_specialist_consults_thread_id", table_name="specialist_consults")
    op.drop_index(
        "ix_specialist_consults_simulation_run_id", table_name="specialist_consults"
    )
    op.drop_table("specialist_consults")

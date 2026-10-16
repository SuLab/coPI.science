"""hub_declines: one row per hub ⏸️ decline that carried no assessment

Owner decision 2026-10-16 (the Hu interview of run e91c35df): a hub decline without an
<assessment_json> sidecar left no opportunity_assessments row, so it appeared neither on
the Assessments page nor in #assessments-summary. This table records it; the engine
writes a row when the hub closes an interview with ⏸️ and no assessment, the Assessments
page lists the rows, and each gets a #assessments-summary headline under the same claim
protocol as an assessment headline (summary_claimed_at, summary_posted_at).

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads the table. NEW CODE ON THE OLD
SCHEMA cannot record a decline: the engine's write is best-effort and logs a WARNING, and
the Assessments page's read raises UndefinedTable, so migrate before the new images serve.

The foreign key to simulation_runs takes a lock on simulation_runs, which a running engine
updates every 30 s in short transactions; a 10 s lock_timeout rollback is harmless, re-run.

Downgrade drops the table and every row.

Revision ID: 0071
Revises: 0070
Create Date: 2026-10-16
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Identical to src.models.hub_decline.REASON_MAX_CHARS (tests pin it); spelled out here
#: because a revision must not change when the model does.
REASON_MAX_CHARS = 4000


def upgrade() -> None:
    op.create_table(
        "hub_declines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "simulation_run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("agent_id", sa.String(50), nullable=False),
        sa.Column("subject_agent_id", sa.String(50), nullable=True),
        sa.Column("channel_name", sa.String(100), nullable=False),
        sa.Column("thread_id", sa.String(50), nullable=False),
        sa.Column("slack_ts", sa.String(50), nullable=True),
        sa.Column("reason_text", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("summary_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary_posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("simulation_run_id", "thread_id", name="uq_hub_declines_run_thread"),
        sa.CheckConstraint(
            f"char_length(reason_text) <= {REASON_MAX_CHARS}",
            name="ck_hub_declines_reason_length",
        ),
    )


def downgrade() -> None:
    op.drop_table("hub_declines")

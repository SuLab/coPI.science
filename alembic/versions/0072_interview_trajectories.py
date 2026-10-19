"""interview_trajectories: one row per run and interview, holding its model-written lines

The Interviews page (/workspace/interviews, staff only) shows one line per post of each
interview. The worker writes those lines once the interview is done and records them here,
keyed by the first chunk's agent_messages.id of each post; the row also carries the claim
status, attempts, the model and the usage ledger of every paid attempt. Past runs are
back-filled by the worker itself, under INTERVIEW_TRAJECTORIES_DAILY_USD_LIMIT.

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads the table. NEW CODE ON THE OLD
SCHEMA fails: the worker's generator raises UndefinedTable into one logged error per idle
tick and the Interviews page's read fails, so migrate before the new images serve.

The foreign key to simulation_runs takes a lock on simulation_runs, which a running engine
updates every 30 s in short transactions; a 10 s lock_timeout rollback is harmless, re-run.

Downgrade drops the table and every row.

Revision ID: 0072
Revises: 0071
Create Date: 2026-10-19
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "interview_trajectories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "simulation_run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("thread_id", sa.String(50), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("lines", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="1", nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("prompt_sha256_12", sa.String(12), nullable=False),
        sa.Column("posts_sha256_12", sa.String(12), nullable=False),
        sa.Column("usage_by_model", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint(
            "simulation_run_id", "thread_id", name="uq_interview_trajectories_run_thread"
        ),
        sa.CheckConstraint(
            "status IN ('ready','failed','refused')", name="ck_interview_trajectories_status"
        ),
    )
    op.create_index("ix_interview_trajectories_updated", "interview_trajectories", ["updated_at"])


def downgrade() -> None:
    op.drop_table("interview_trajectories")

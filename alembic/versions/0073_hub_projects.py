"""hub_projects and its three companions: the BlackbirdBot projects prototype

Once every PROJECT_INTERVAL_HOURS of run time the engine requests one project per live lab
(hub_projects, one row per run, lab and slot), the worker researches it and records each
model call in hub_project_calls (claimed before the call, for the rolling 24 h dollar
ceiling), hub_project_clocks holds each run's run-time clock, and hub_project_reviews holds
staff and reviewer reviews. thread_outcome_enum gains 'project_closed', the outcome a
project thread closes with.

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads the four tables, and the new enum
value is written only by new code. The exception is a ROLLBACK after the first project has
closed: an old image loading that run's thread_decisions through the ORM cannot map
'project_closed' (pinned by
tests/integration/test_migration_0073.py::test_pre_0073_enum_mapping_cannot_read_project_closed),
and Rebuild swallows the error into an empty closed set, so a resumed old engine would
reopen closed threads. A rollback below 0073 is unsafe for a run that holds a
'project_closed' decision.

NEW CODE ON THE OLD SCHEMA fails: the engine's project tick logs one error per minute, the
worker's research step one per idle slot, the Projects page 500s, and a 'project_closed'
decision raises InvalidTextRepresentation and is re-queued. Migrate before the new images
serve, and rebuild the agent image with it (the engine imports src.models).

The foreign keys to simulation_runs take a lock on simulation_runs, which a running engine
updates every 30 s in short transactions; a 10 s lock_timeout rollback is harmless, re-run.

Downgrade drops the four tables and every row, and leaves the enum value.

Revision ID: 0073
Revises: 0072
Create Date: 2026-10-26
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0073"
down_revision: str | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
        ),
    ]


def upgrade() -> None:
    # PG15 allows ADD VALUE inside the chain's one transaction as long as nothing in
    # that transaction uses the value; nothing here does. IF NOT EXISTS keeps ci.sh's
    # up->down->up round trip idempotent, because downgrade() cannot drop an enum
    # value — the same pattern and the same reason as 0063's `abandoned`.
    op.execute("ALTER TYPE thread_outcome_enum ADD VALUE IF NOT EXISTS 'project_closed'")

    op.create_table(
        "hub_projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "simulation_run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("agent_id", sa.String(50), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason_code", sa.String(40), nullable=True),
        sa.Column("requested_run_seconds", sa.Float(), nullable=False),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("research", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("choice", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("posting_text", sa.Text(), nullable=True),
        sa.Column("posting_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("posting_ts", sa.String(50), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reply_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reply_ts", sa.String(50), nullable=True),
        sa.Column("reply_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("final", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_run_seconds", sa.Float(), nullable=True),
        sa.Column("next_step_after", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint(
            "simulation_run_id", "agent_id", "slot", name="uq_hub_projects_run_agent_slot"
        ),
        sa.CheckConstraint(
            "status IN ('requested','researching','ready_to_post','posted','refining',"
            "'ready','not_agreed','not_finished')",
            name="ck_hub_projects_status",
        ),
    )
    op.create_table(
        "hub_project_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hub_projects.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("step", sa.String(40), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("usage_by_model", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('in_progress','done','failed','refused')",
            name="ck_hub_project_calls_status",
        ),
    )
    op.create_table(
        "hub_project_clocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "simulation_run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("run_seconds", sa.Float(), server_default="0", nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint("simulation_run_id", name="uq_hub_project_clocks_run"),
    )
    op.create_table(
        "hub_project_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hub_projects.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "reviewer_user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("reviewer_name", sa.String(255), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), server_default="", nullable=False),
        sa.Column("fund", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint(
            "project_id", "reviewer_user_id", name="uq_hub_project_reviews_project_reviewer"
        ),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_hub_project_reviews_rating"),
    )
    op.create_index(
        "ix_hub_projects_run_reply", "hub_projects", ["simulation_run_id", "reply_ts"]
    )
    op.create_index("ix_hub_project_calls_updated", "hub_project_calls", ["updated_at"])
    op.create_index("ix_hub_project_calls_project", "hub_project_calls", ["project_id"])


def downgrade() -> None:
    op.drop_table("hub_project_reviews")
    op.drop_table("hub_project_calls")
    op.drop_table("hub_project_clocks")
    op.drop_table("hub_projects")
    # The enum value stays. PostgreSQL cannot remove a value from an enum type without
    # rewriting it, and any thread_decisions row already written with 'project_closed'
    # would have nothing to fall back to. The upgrade is idempotent, so an up->down->up
    # round trip is clean.

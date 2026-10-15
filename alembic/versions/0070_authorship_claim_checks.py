"""authorship_claim_checks: one row per authorship-claim check the engine makes

Design: the 2026-10-15 publication-handling design, §11 R6.8 (contract C7).

Every check of a pi_lab or hub post, and every working-memory sweep that would strip a
line, is one row: mode (log, enforce or replay), outcome (no_claim, grounded or rejected),
the reasons as a JSON array of {"code", "text"}, and an excerpt of at most 2,000
characters. simulation_run_id cascades from simulation_runs; it is nullable only so a
replay row is admissible, and nothing writes one.

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads the table. NEW CODE ON THE OLD
SCHEMA writes no row: src/agent/claim_check.py::record_check swallows UndefinedTable with
a WARNING per post, and the post is unaffected. So migrate before the new agent runs.

The foreign key to simulation_runs takes a lock on simulation_runs, which a running engine
updates every 30 s in short transactions; a 10 s lock_timeout rollback is harmless, re-run.

Downgrade drops the table and every row.

Revision ID: 0070
Revises: 0069
Create Date: 2026-10-15
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0070"
down_revision: str | None = "0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Identical to src.models.authorship_claim_check.MODES and OUTCOMES
#: (tests/integration/test_migration_0070.py pins them); spelled out here because a
#: revision must not change when the model does.
MODES = ("log", "enforce", "replay")
OUTCOMES = ("no_claim", "grounded", "rejected")
EXCERPT_MAX_CHARS = 2000


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_table(
        "authorship_claim_checks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "simulation_run_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulation_runs.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("agent_id", sa.String(50), nullable=False),
        sa.Column("thread_key", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column(
            "reasons", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(_in_list("mode", MODES), name="ck_authorship_claim_checks_mode"),
        sa.CheckConstraint(_in_list("outcome", OUTCOMES), name="ck_authorship_claim_checks_outcome"),
        sa.CheckConstraint(
            f"char_length(excerpt) <= {EXCERPT_MAX_CHARS}",
            name="ck_authorship_claim_checks_excerpt_length",
        ),
    )
    op.create_index(
        "ix_authorship_claim_checks_run_agent", "authorship_claim_checks",
        ["simulation_run_id", "agent_id"],
    )
    op.create_index(
        "ix_authorship_claim_checks_created_at", "authorship_claim_checks", ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("authorship_claim_checks")

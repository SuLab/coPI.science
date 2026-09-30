"""Phase 0a remediation columns (docs/specs/2026-09-29-audit-remediation-design.md §10.1)

Five additive nullable columns, none backfilled: NULL means "never asked".

- jobs.not_before: earliest time a job that failed and will be retried may be
  claimed again.
- users.contact_email_unverified: the address a pending-access user typed on
  /access-pending. Never copied to users.email.
- opportunity_assessments.summary_claimed_at: a headline poster's claim, set
  immediately before the post.
- simulation_runs.finalized_at: the run's owed headlines were released and a
  resume is refused.
- simulation_runs.held_at: the run ended holding the headlines of open
  interviews.

OLD CODE ON THE NEW SCHEMA is safe: nothing reads the columns. NEW CODE ON THE OLD
SCHEMA is not: every select of the four models raises UndefinedColumn, and the
engine's best-effort writes swallow that into ERROR lines. Migrate BEFORE the new
code serves, with the WORKER IDLE: a worker mid-pipeline holds its transaction
(and its `jobs` row lock) for the whole run and trips the 10 s lock_timeout.

Revision ID: 0053
Revises: 0052
Create Date: 2026-09-29
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("not_before", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users", sa.Column("contact_email_unverified", sa.String(255), nullable=True)
    )
    op.add_column(
        "opportunity_assessments",
        sa.Column("summary_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "simulation_runs", sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "simulation_runs", sa.Column("held_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("simulation_runs", "held_at")
    op.drop_column("simulation_runs", "finalized_at")
    op.drop_column("opportunity_assessments", "summary_claimed_at")
    op.drop_column("users", "contact_email_unverified")
    op.drop_column("jobs", "not_before")

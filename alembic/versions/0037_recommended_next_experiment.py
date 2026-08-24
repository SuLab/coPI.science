"""opportunity_assessments.recommended_next_experiment — sidecar item 10 as a column.

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-24 00:00:00.000000

One additive nullable Text column. Rubric v2.1.0's sidecar contract adds item 10
— the single experiment Blackbird should fund next, with its readout, pass
threshold, and rough cost/time — and names it the line staff act on. That makes
it a first-class column the assessment detail pages render, not a value staff
dig out of ``raw_verdict``.

NULL for every row written before this revision, deliberately never backfilled:
verdicts emitted under the pre-2.1.0 contract were never asked to name one, and
``raw_verdict`` keeps whatever they did emit verbatim.

Deploy order: additive and nullable, so OLD code against the NEW schema is safe.
The reverse is not — the new code maps the column, so every
``select(OpportunityAssessment)`` (both assessment list pages, both detail
pages, the engine's ``_persist_assessment`` INSERT and ``_flush_persisted``
retry) raises ``UndefinedColumn`` against a pre-0037 database. Build, migrate
from a one-off container, then start — the same ordering as 0028/0030/0036 (see
CLAUDE.md).
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments",
        sa.Column("recommended_next_experiment", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunity_assessments", "recommended_next_experiment")

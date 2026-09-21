"""opportunity_assessments.competitive_landscape / .evidence_maturity — sidecar items 13/14.

Revision ID: 0050
Revises: 0049
Create Date: 2026-09-21

Two additive nullable JSONB columns (scout_hub prompt set 1.7.0):

* ``competitive_landscape`` — the competing and adjacent programs, each with its
  development stage, or a plain statement that a search found none.
* ``evidence_maturity`` — one bullet per axis the verdict rests on (the biology,
  and the enabling chemistry/assay/platform), each naming what IS settled and what
  is NOT.

Both are app-only, the same design as ``score_rationale`` (0048) and
``strengths``/``risks`` (0049): neither is smuggled into ``elevator_pitch`` or the
``#assessments-summary`` headline, which stays exactly the six fields it already
renders.

NULL for every row written before this revision, deliberately never backfilled:
those verdicts were never asked for either field, and a generated one would be
indistinguishable from one the hub wrote. A malformed value also stores NULL —
``normalize_bullets`` in ``src/services/assessment_detail.py`` is the gate, and
``raw_verdict`` keeps whatever the hub emitted either way. Every read path renders
nothing when a column is NULL.

Deploy order: additive and nullable, so OLD code against the NEW schema is safe.
The reverse breaks in both directions. READ — the new code maps both columns, so
every ``select(OpportunityAssessment)`` raises ``UndefinedColumn`` on FOUR
surfaces: both assessment list pages, both detail pages,
``src/services/review_bot.py`` (on the worker, so every review_feedback_analysis
job fails) and ``src/routers/reviews.py`` (so feedback submit/edit 500s). WRITE —
``_persist_assessment`` names both in the INSERT, and that write is best-effort,
so every verdict of a running simulation is lost to one ERROR line in a log nobody
is tailing while the Slack replies keep looking completely normal. Build, migrate
from a one-off container, then start — the same ordering as
0037/0040/0041/0043/0048/0049 (see CLAUDE.md).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments",
        sa.Column("competitive_landscape", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "opportunity_assessments",
        sa.Column("evidence_maturity", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunity_assessments", "evidence_maturity")
    op.drop_column("opportunity_assessments", "competitive_landscape")

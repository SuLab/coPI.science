"""opportunity_assessments.strengths / .risks — sidecar items 11/12 as columns.

Revision ID: 0049
Revises: 0048
Create Date: 2026-09-14

Two additive nullable JSONB columns: the hub's own strengths and risks
bullets for a verdict (scout_hub prompt set 1.5.0). They are NEW, app-only
fields on purpose, the same design as ``score_rationale`` (0048): neither is
ever smuggled into ``elevator_pitch`` or the ``#assessments-summary``
headline, which stays exactly the six fields it already renders.

NULL for every row written before this revision, deliberately never
backfilled: those verdicts were never asked for strengths/risks bullets, and
a generated pair would be indistinguishable from bullets the hub actually
wrote — the same rule ``headline``/``key_points``/``elevator_pitch``/
``score_rationale`` all follow. A malformed value (wrong type, an empty list,
a non-string element) also stores NULL — ``normalize_bullets`` in
``src/services/assessment_detail.py`` is the gate, and ``raw_verdict`` keeps
whatever the hub actually emitted either way. Every read path renders nothing
when a column is NULL.

Deploy order: additive and nullable, so OLD code against the NEW schema is
safe. The reverse breaks in BOTH directions at once. READ — the new code maps
both columns, so every ``select(OpportunityAssessment)`` (both assessment
list pages, both detail pages) raises ``UndefinedColumn`` against a pre-0049
database. WRITE — ``_persist_assessment`` names both in the INSERT, and that
write is best-effort, so every verdict of a running simulation is lost to one
ERROR line in a log nobody is tailing while the Slack replies keep looking
completely normal. Build, migrate from a one-off container, then start — the
same ordering as 0037/0040/0041/0043/0048 (see CLAUDE.md).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0049"
down_revision: Union[str, None] = "0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments",
        sa.Column("strengths", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "opportunity_assessments",
        sa.Column("risks", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunity_assessments", "risks")
    op.drop_column("opportunity_assessments", "strengths")

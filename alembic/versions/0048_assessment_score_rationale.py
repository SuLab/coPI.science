"""opportunity_assessments.score_rationale — sidecar item 10 as a column.

Revision ID: 0048
Revises: 0047
Create Date: 2026-09-14

One additive nullable Text column: the hub's brief account of why the dimension
scores came out where they did. It is a NEW, app-only field on purpose (design
D3): the score rationale must never be smuggled into ``elevator_pitch``,
because the pitch is published to ``#assessments-summary`` and this is not.

NULL for every row written before this revision, deliberately never backfilled:
the 20 existing verdicts were never asked for a score rationale, and a
generated one would be indistinguishable from one the hub wrote — the same rule
``headline``/``key_points``/``elevator_pitch`` follow. Every read path renders
nothing when it is NULL.

Deploy order: additive and nullable, so OLD code against the NEW schema is
safe. The reverse breaks in BOTH directions at once. READ — the new code maps
the column, so every ``select(OpportunityAssessment)`` (both assessment list
pages, both detail pages) raises ``UndefinedColumn`` against a pre-0048
database. WRITE — ``_persist_assessment`` names it in the INSERT, and that
write is best-effort, so every verdict of a running simulation is lost to one
ERROR line in a log nobody is tailing while the Slack replies keep looking
completely normal. Build, migrate from a one-off container, then start — the
same ordering as 0037/0040/0041/0043 (see CLAUDE.md).
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments",
        sa.Column("score_rationale", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunity_assessments", "score_rationale")

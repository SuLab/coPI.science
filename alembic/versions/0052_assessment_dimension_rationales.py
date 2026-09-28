"""opportunity_assessments.dimension_rationales

One additive nullable JSONB column: the hub's one-sentence reason for each
dimension score (sidecar item 2 of scout_hub 1.9.0), so the Evidence summary can
show WHY a dimension scored what it did rather than only the number.

Purely additive — OLD CODE AGAINST THE NEW SCHEMA IS SAFE. The reverse is not:
the new code maps the column, so every `select(OpportunityAssessment)` raises
UndefinedColumn against a pre-0052 database (both assessment list pages, both
detail pages, src/services/review_bot.py on the worker, src/routers/reviews.py
and src/services/directory.py), and `_persist_assessment` names it in the
INSERT — a best-effort write, so every verdict of a running simulation would be
lost to one ERROR line while the Slack replies kept looking normal. Migrate
BEFORE the new code serves.

NULL on every pre-0052 row and deliberately never backfilled: those verdicts
were never asked for per-dimension reasons, and a generated one would be
indistinguishable from one the hub wrote. Both assessment surfaces render
nothing when it is NULL.

Revision ID: 0052
Revises: 0051
Create Date: 2026-09-28
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0052"
down_revision: Union[str, None] = "0051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments",
        sa.Column("dimension_rationales", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunity_assessments", "dimension_rationales")

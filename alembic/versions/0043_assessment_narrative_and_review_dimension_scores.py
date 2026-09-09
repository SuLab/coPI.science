"""assessment narrative fields + per-dimension human review scores

Six additive nullable columns across two tables. No DDL rewrites, no FK
changes, no data repair, no backfill.

  opportunity_assessments : headline, key_points, elevator_pitch
  assessment_reviews      : dimension_scores, rubric_version, rubric_content_hash

Additive and nullable, so OLD CODE AGAINST THE NEW SCHEMA IS SAFE. The reverse
is not, in both directions at once. READ side: the new code MAPS all six, so
every `select(OpportunityAssessment)` (both list pages, both detail pages) and
every `select(AssessmentReview)` (the detail pages' feedback list, review_bot's
own load) raises `UndefinedColumn` against a pre-0043 database. WRITE side:
`_persist_assessment`'s INSERT names headline/key_points/elevator_pitch, so
EVERY verdict write of a running simulation fails — and that write is
best-effort, so the failure is swallowed and one ERROR line is logged while the
Slack replies keep looking completely normal. Migrate BEFORE the new code
serves; see the 0043 deploy box in CLAUDE.md.

Deliberately NOT backfilled. All three narrative fields are NULL on every
pre-0043 assessment row, because those verdicts were never asked for them and a
generated headline would be indistinguishable from one the hub actually wrote —
the same stamp-and-keep rule the rest of this table follows. Every read path
degrades: `headline` falls back to `company_or_project`, and absent
bullets/pitch render nothing.

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-09
"""
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments", sa.Column("headline", sa.Text(), nullable=True)
    )
    op.add_column(
        "opportunity_assessments",
        sa.Column("key_points", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "opportunity_assessments", sa.Column("elevator_pitch", sa.Text(), nullable=True)
    )
    op.add_column(
        "assessment_reviews",
        sa.Column(
            "dimension_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.add_column(
        "assessment_reviews", sa.Column("rubric_version", sa.String(20), nullable=True)
    )
    op.add_column(
        "assessment_reviews",
        sa.Column("rubric_content_hash", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assessment_reviews", "rubric_content_hash")
    op.drop_column("assessment_reviews", "rubric_version")
    op.drop_column("assessment_reviews", "dimension_scores")
    op.drop_column("opportunity_assessments", "elevator_pitch")
    op.drop_column("opportunity_assessments", "key_points")
    op.drop_column("opportunity_assessments", "headline")

"""assessment summary_posted_at — the durable record that a headline posted

Additive and nullable, so OLD CODE AGAINST THE NEW SCHEMA IS SAFE. The reverse
is not: the new code MAPS this column, so every `select(OpportunityAssessment)`
— both assessment list pages, both detail pages — raises `UndefinedColumn`
against a pre-0041 database, and `_persist_assessment`'s INSERT names it, so
every verdict write fails too. Migrate BEFORE the new code serves; see the
deploy box in CLAUDE.md.

Deliberately NOT backfilled. NULL means "no headline has been posted for this
row", and for a pre-0041 row that is unknowable from the database alone — the
only record is the Slack channel. Guessing would manufacture exactly the claim
this column exists to make truthfully. `scripts/backfill_assessment_headlines.py
--stamp-only` is the operator path for marking a row whose headline is already
in Slack.

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-29
"""
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments",
        sa.Column("summary_posted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunity_assessments", "summary_posted_at")

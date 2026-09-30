"""Verdict store: revision, write id and ordinal (spec §8.1, B5/B6)

Four additive nullable columns:

* ``opportunity_assessments.verdict_revision`` — how many verdicts this one
  interview row has held (NULL reads as 1 through COALESCE; never backfilled);
* ``opportunity_assessments.verdict_write_id`` — the id of the write that
  produced the current verdict, so a retried write whose acknowledgement was
  lost is recognised as already applied;
* ``opportunity_assessments.verdict_ordinal`` — the hub reply ordinal that
  produced the current verdict (``thread.message_count + 1``), so a late
  queued write can never overwrite a newer verdict;
* ``assessment_chat_turns.verdict_revision`` — the verdict revision a chat turn
  was asked against (NULL reads as 1), so replay excludes earlier revisions.

OLD CODE AGAINST THE NEW SCHEMA IS SAFE: nothing old names these columns. NEW
CODE AGAINST THE OLD SCHEMA IS NOT: the models map the columns, so every
``select(OpportunityAssessment)`` and every chat read raises UndefinedColumn.
Migrate before the new code serves. The unique constraint comes separately in
0055, after the operator merge script.

Revision ID: 0054
Revises: 0053
Create Date: 2026-09-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0054"
down_revision: Union[str, None] = "0053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments",
        sa.Column("verdict_revision", sa.Integer(), nullable=True),
    )
    op.add_column(
        "opportunity_assessments",
        sa.Column("verdict_write_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "opportunity_assessments",
        sa.Column("verdict_ordinal", sa.Integer(), nullable=True),
    )
    op.add_column(
        "assessment_chat_turns",
        sa.Column("verdict_revision", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("assessment_chat_turns", "verdict_revision")
    op.drop_column("opportunity_assessments", "verdict_ordinal")
    op.drop_column("opportunity_assessments", "verdict_write_id")
    op.drop_column("opportunity_assessments", "verdict_revision")

"""opportunity_assessments.prose_format — write-time stamp gating markdown rendering.

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-28 00:00:00.000000

One additive nullable String(20) column. Rationale and recommended-next-
experiment prose has historically been written as plain text and rendered
``white-space: pre-line`` — some of those rows contain literal ``*`` in
scientific identifiers (e.g. ``HLA-A*02:01``) that a markdown pass would
corrupt by reading as emphasis. Starting with the phase4 prompt change that
ships alongside this migration, the hub is asked to write simple Markdown
instead, and ``_persist_assessment`` stamps ``prose_format="markdown"`` on
every new row so the read path can render markdown ONLY where it is safe to
do so, replaying the stamp rather than re-deriving it.

NULL on every existing row, deliberately never backfilled: those rows were
never written under the markdown contract, so NULL correctly means "render
plain" forever, not "unknown, guess".

Deploy order: additive and nullable, so OLD code against the NEW schema is
safe. The reverse is not — the new code maps the column, so every
``select(OpportunityAssessment)`` (both assessment list pages, both detail
pages) raises ``UndefinedColumn`` against a pre-0040 database, and the
engine's ``_persist_assessment`` INSERT names it, so every verdict write
fails too. Build, migrate from a one-off container, then start — the same
ordering as 0028/0030/0036/0037/0038 (see CLAUDE.md).
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "opportunity_assessments",
        sa.Column("prose_format", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunity_assessments", "prose_format")

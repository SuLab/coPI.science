"""The assessment chat's tables take a landscaping subject

"Ask about this opportunity" on the landscaping pages
(docs/specs/2026-10-30-blackbird-landscaping-design.md §13.5, L63) shares the assessment
chat's tables, as the project chat does since 0076, so one answer in flight per user, the
daily limits and the spend lock cover every chat. The chat is keyed on the STABLE
opportunity, not one of its assessment versions, so a conversation does not split at a new
version (X7):

- assessment_chat_turns: landscape_opportunity_id -> landscape_opportunities ON DELETE
  CASCADE, indexed by ix_assessment_chat_turns_landscape_conversation
  (landscape_opportunity_id, user_id, context_tier, created_at);
  ck_assessment_chat_turns_one_subject becomes
  num_nonnulls(assessment_id, project_id, landscape_opportunity_id) = 1.
- assessment_chat_usage and assessment_chat_opens: landscape_opportunity_id ->
  landscape_opportunities ON DELETE SET NULL, and their subject CHECKs admit 'landscape'.

The three CHECKs are dropped and created again under the same names.

OLD CODE ON THE NEW SCHEMA is safe: it never writes the new column, and the widened checks
admit everything it writes.

NEW CODE ON THE OLD SCHEMA fails: the chat models map landscape_opportunity_id, so every
chat request (assessment, project and landscaping) raises UndefinedColumn. Migrate before
the new web image serves. The engine and the BlackbirdBot worker never read the new column.

ALTER TABLE takes a brief ACCESS EXCLUSIVE lock on each chat table, and the foreign keys
lock landscape_opportunities (written only by the landscaping worker); a 10 s lock_timeout
rollback is harmless, re-run.

Downgrade deletes the landscaping turns (their only subject is the dropped column), sets
the landscaping ledger and opens rows' subject to NULL so the restored 0076 CHECKs hold
(the rows stay: the ledger outlives its subject, and a later re-upgrade reads them as
pre-0076 assessment rows), restores the 0076 CHECKs and drops the index, foreign keys and
columns.

Revision ID: 0078
Revises: 0077
Create Date: 2026-10-30
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUBJECTS = "('assessment', 'project', 'landscape')"
_OLD_SUBJECTS = "('assessment', 'project')"


def _replace_checks(one_subject: str, subjects: str) -> None:
    """Drop the three subject CHECKs and create them again under the same names."""
    op.drop_constraint("ck_assessment_chat_opens_subject", "assessment_chat_opens", type_="check")
    op.drop_constraint("ck_assessment_chat_usage_subject", "assessment_chat_usage", type_="check")
    op.drop_constraint(
        "ck_assessment_chat_turns_one_subject", "assessment_chat_turns", type_="check"
    )
    op.create_check_constraint(
        "ck_assessment_chat_turns_one_subject", "assessment_chat_turns", one_subject,
    )
    op.create_check_constraint(
        "ck_assessment_chat_usage_subject", "assessment_chat_usage", f"subject IN {subjects}",
    )
    op.create_check_constraint(
        "ck_assessment_chat_opens_subject", "assessment_chat_opens", f"subject IN {subjects}",
    )


def upgrade() -> None:
    op.add_column(
        "assessment_chat_turns",
        sa.Column("landscape_opportunity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "assessment_chat_usage",
        sa.Column("landscape_opportunity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "assessment_chat_opens",
        sa.Column("landscape_opportunity_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    # Short names: Postgres truncates an identifier past 63 bytes, and postflight pins these.
    op.create_foreign_key(
        "fk_assessment_chat_turns_landscape_opportunity",
        "assessment_chat_turns", "landscape_opportunities",
        ["landscape_opportunity_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_assessment_chat_usage_landscape_opportunity",
        "assessment_chat_usage", "landscape_opportunities",
        ["landscape_opportunity_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_assessment_chat_opens_landscape_opportunity",
        "assessment_chat_opens", "landscape_opportunities",
        ["landscape_opportunity_id"], ["id"], ondelete="SET NULL",
    )

    op.create_index(
        "ix_assessment_chat_turns_landscape_conversation",
        "assessment_chat_turns",
        ["landscape_opportunity_id", "user_id", "context_tier", "created_at"],
    )

    _replace_checks(
        "num_nonnulls(assessment_id, project_id, landscape_opportunity_id) = 1", _SUBJECTS
    )


def downgrade() -> None:
    # A landscaping turn names no other subject, so the 0076 CHECK cannot come back while one
    # exists. Its ledger row loses turn_id (SET NULL) and stays.
    op.execute("DELETE FROM assessment_chat_turns WHERE landscape_opportunity_id IS NOT NULL")
    op.execute("UPDATE assessment_chat_usage SET subject = NULL WHERE subject = 'landscape'")
    op.execute("UPDATE assessment_chat_opens SET subject = NULL WHERE subject = 'landscape'")

    _replace_checks("num_nonnulls(assessment_id, project_id) = 1", _OLD_SUBJECTS)

    op.drop_index(
        "ix_assessment_chat_turns_landscape_conversation", table_name="assessment_chat_turns"
    )

    op.drop_constraint(
        "fk_assessment_chat_opens_landscape_opportunity", "assessment_chat_opens",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_assessment_chat_usage_landscape_opportunity", "assessment_chat_usage",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_assessment_chat_turns_landscape_opportunity", "assessment_chat_turns",
        type_="foreignkey",
    )

    op.drop_column("assessment_chat_opens", "landscape_opportunity_id")
    op.drop_column("assessment_chat_usage", "landscape_opportunity_id")
    op.drop_column("assessment_chat_turns", "landscape_opportunity_id")

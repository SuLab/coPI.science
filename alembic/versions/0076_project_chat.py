"""The assessment chat's tables take a project subject

"Ask about this project" (docs/specs/2026-10-27-projects-pages-redesign-design.md §8.2)
shares the assessment chat's tables, so one answer in flight per user, the daily limits and
the spend lock cover both chats:

- assessment_chat_turns: project_id -> hub_projects ON DELETE CASCADE, indexed by
  ix_assessment_chat_turns_project_conversation (project_id, user_id, context_tier,
  created_at); assessment_id becomes nullable; ck_assessment_chat_turns_one_subject requires
  exactly one of the two.
- assessment_chat_usage and assessment_chat_opens: project_id -> hub_projects ON DELETE SET
  NULL, indexed, and subject ('assessment' | 'project', checked). NULL subject means the row
  was written before 0076 (every one of them an assessment); never back-filled.

OLD CODE ON THE NEW SCHEMA is safe: it never writes a NULL assessment_id, selects turns by
assessment_id, and leaves the new columns NULL, which the checks allow.

NEW CODE ON THE OLD SCHEMA fails: the chat models map project_id and subject, so every chat
request raises UndefinedColumn. Migrate before the new web image serves. The engine and the
worker never read the new columns, so neither the agent nor the worker image needs a rebuild
for it.

ALTER TABLE takes a brief ACCESS EXCLUSIVE lock on each chat table and the foreign keys lock
hub_projects: run it only when no projects run is live (owner decision D61) and with the
worker idle; a 10 s lock_timeout rollback is harmless, re-run.

Downgrade deletes the project turns (their assessment_id is NULL), drops the checks, indexes,
foreign keys and columns, and restores NOT NULL on assessment_id. Ledger and opens rows stay:
the ledger outlives its subject.

Revision ID: 0076
Revises: 0075
Create Date: 2026-10-27
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0076"
down_revision: str | None = "0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUBJECTS = "('assessment', 'project')"


def upgrade() -> None:
    op.add_column(
        "assessment_chat_turns", sa.Column("project_id", postgresql.UUID(as_uuid=True))
    )
    op.add_column(
        "assessment_chat_usage", sa.Column("project_id", postgresql.UUID(as_uuid=True))
    )
    op.add_column(
        "assessment_chat_usage", sa.Column("subject", sa.String(12), nullable=True)
    )
    op.add_column(
        "assessment_chat_opens", sa.Column("project_id", postgresql.UUID(as_uuid=True))
    )
    op.add_column(
        "assessment_chat_opens", sa.Column("subject", sa.String(12), nullable=True)
    )

    op.create_foreign_key(
        "fk_assessment_chat_turns_project_id_hub_projects",
        "assessment_chat_turns", "hub_projects", ["project_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_assessment_chat_usage_project_id_hub_projects",
        "assessment_chat_usage", "hub_projects", ["project_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_assessment_chat_opens_project_id_hub_projects",
        "assessment_chat_opens", "hub_projects", ["project_id"], ["id"], ondelete="SET NULL",
    )

    op.create_index(
        "ix_assessment_chat_turns_project_conversation",
        "assessment_chat_turns", ["project_id", "user_id", "context_tier", "created_at"],
    )
    op.create_index(
        "ix_assessment_chat_usage_project_id", "assessment_chat_usage", ["project_id"]
    )
    op.create_index(
        "ix_assessment_chat_opens_project_id", "assessment_chat_opens", ["project_id"]
    )

    op.alter_column(
        "assessment_chat_turns", "assessment_id",
        existing_type=postgresql.UUID(as_uuid=True), nullable=True,
    )

    op.create_check_constraint(
        "ck_assessment_chat_turns_one_subject",
        "assessment_chat_turns", "num_nonnulls(assessment_id, project_id) = 1",
    )
    op.create_check_constraint(
        "ck_assessment_chat_usage_subject", "assessment_chat_usage", f"subject IN {_SUBJECTS}",
    )
    op.create_check_constraint(
        "ck_assessment_chat_opens_subject", "assessment_chat_opens", f"subject IN {_SUBJECTS}",
    )


def downgrade() -> None:
    # Project turns have no assessment_id, so NOT NULL cannot come back while they exist.
    # Their ledger rows lose turn_id (SET NULL) and stay, as do project opens; once
    # `subject` and `project_id` are dropped they cannot be told from assessment rows, and
    # after a re-upgrade they read as pre-0076 (NULL subject) assessment rows.
    op.execute("DELETE FROM assessment_chat_turns WHERE project_id IS NOT NULL")

    op.drop_constraint("ck_assessment_chat_opens_subject", "assessment_chat_opens", type_="check")
    op.drop_constraint("ck_assessment_chat_usage_subject", "assessment_chat_usage", type_="check")
    op.drop_constraint(
        "ck_assessment_chat_turns_one_subject", "assessment_chat_turns", type_="check"
    )

    op.drop_index("ix_assessment_chat_opens_project_id", table_name="assessment_chat_opens")
    op.drop_index("ix_assessment_chat_usage_project_id", table_name="assessment_chat_usage")
    op.drop_index(
        "ix_assessment_chat_turns_project_conversation", table_name="assessment_chat_turns"
    )

    op.drop_constraint(
        "fk_assessment_chat_opens_project_id_hub_projects", "assessment_chat_opens",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_assessment_chat_usage_project_id_hub_projects", "assessment_chat_usage",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_assessment_chat_turns_project_id_hub_projects", "assessment_chat_turns",
        type_="foreignkey",
    )

    op.drop_column("assessment_chat_opens", "subject")
    op.drop_column("assessment_chat_opens", "project_id")
    op.drop_column("assessment_chat_usage", "subject")
    op.drop_column("assessment_chat_usage", "project_id")
    op.drop_column("assessment_chat_turns", "project_id")

    op.alter_column(
        "assessment_chat_turns", "assessment_id",
        existing_type=postgresql.UUID(as_uuid=True), nullable=False,
    )

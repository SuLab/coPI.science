"""reviewer role, review tables, review job type

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-28 00:00:00.000000

Foundation for the human-review-feedback feature: a fourth ``users.user_role``
value (``reviewer``), a new ``job_type_enum`` value
(``review_feedback_analysis``), and four new tables — ``assessment_reviews``,
``assessment_review_events``, ``assessment_review_assignments`` and
``prompt_change_suggestions``. See ``src/models/review.py`` for the FK
rationale (A-1/A-2/A-3 in
``docs/plans/2026-08-28-human-review-feedback-adversarial-analysis.md``).

The enum widening is a novel hazard in this repo: no prior migration has run
``ALTER TYPE ... ADD VALUE``. PG15 allows it inside a transaction so long as
the new value is not USED in that same transaction (we don't use it), and
``IF NOT EXISTS`` keeps ``scripts/ci.sh``'s upgrade->downgrade->upgrade round
trip idempotent, because ``downgrade()`` cannot remove an enum value — Postgres
has no ``DROP VALUE``. The old code (pre-``src/models/job.py:20`` update)
would still fail on any SELECT of a row carrying the new value, but nothing
writes one until the corresponding worker/router code ships, so the widened
enum sits inert until then.

Deploy order: BEFORE the new code serves, same as every prior additive
migration in this chain (0028/0030/0036/0037/0038). Old code against the new
schema is safe — the four new tables are simply unused, and the new enum
value/check-constraint value are unused values in an otherwise-untouched
column. The reverse is not: new code that maps ``User.is_reviewer`` or
imports the four review models will fail against a pre-0039 database.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PG15 allows ADD VALUE inside a transaction if the value isn't used in it
    # (we don't use it). IF NOT EXISTS keeps ci.sh's up->down->up idempotent,
    # because downgrade() cannot remove an enum value.
    op.execute("ALTER TYPE job_type_enum ADD VALUE IF NOT EXISTS 'review_feedback_analysis'")

    op.drop_constraint("ck_users_user_role", "users", type_="check")
    op.create_check_constraint(
        "ck_users_user_role",
        "users",
        "user_role IN ('pi', 'manager', 'admin', 'reviewer')",
    )

    op.create_table(
        "assessment_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "assessment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunity_assessments.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "reviewer_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewer_name", sa.String(length=255), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("feedback_mode", sa.String(length=20), nullable=False),
        sa.Column("edited", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("score >= 1 AND score <= 5", name="ck_assessment_reviews_score"),
        sa.CheckConstraint(
            "feedback_mode IN ('learn','log_only')", name="ck_assessment_reviews_mode"
        ),
    )

    op.create_table(
        "assessment_review_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "assessment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunity_assessments.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('approved','disapproved','cleared')",
            name="ck_assessment_review_events_action",
        ),
    )

    op.create_table(
        "assessment_review_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "assessment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunity_assessments.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "assignee_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("assignee_name", sa.String(length=255), nullable=False),
        sa.Column(
            "assigned_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("assigned_by_name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "assessment_id", "assignee_user_id", name="uq_review_assignment_once"
        ),
    )

    op.create_table(
        "prompt_change_suggestions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "assessment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunity_assessments.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("subject_label", sa.Text(), nullable=False, server_default=""),
        sa.Column("assessment_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rubric_version", sa.String(length=20), nullable=True),
        sa.Column("feedback_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("target", sa.String(length=40), nullable=False),
        sa.Column("prompt_files", postgresql.JSONB(), nullable=False),
        sa.Column("suggestion", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("transcript_available", sa.Boolean(), nullable=False),
        sa.Column(
            "input_truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("raw_response", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column(
            "status_set_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status_set_by_name", sa.String(length=255), nullable=True),
        sa.Column("status_set_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('open','dismissed','implemented')",
            name="ck_prompt_change_suggestions_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("prompt_change_suggestions")
    op.drop_table("assessment_review_assignments")
    op.drop_table("assessment_review_events")
    op.drop_table("assessment_reviews")
    # 'pi' is deny-by-default: no staff surface, and PI surfaces still gate on
    # onboarding. 'manager' would grant MORE than reviewer ever had.
    op.execute("UPDATE users SET user_role = 'pi' WHERE user_role = 'reviewer'")
    op.drop_constraint("ck_users_user_role", "users", type_="check")
    op.create_check_constraint(
        "ck_users_user_role",
        "users",
        "user_role IN ('pi', 'manager', 'admin')",
    )
    # job_type_enum keeps the value (cannot drop); harmless at 0038.

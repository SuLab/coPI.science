"""Assessment chat: generated opening questions, drawer openings, question origins

Design: the "Opening questions" section of docs/operations/assessment-chat.md. Additive:

- ``assessment_chat_suggestions``: the drawer's opening questions per (assessment, tier,
  verdict revision), written by the worker (src/services/assessment_chat_suggestions.py).
  Rows CASCADE with the assessment.
- ``assessment_chat_opens``: one content-free row per drawer opening; both foreign keys
  SET NULL, like the usage ledger's.
- ``assessment_chat_usage.question_origin`` (varchar, NULL): typed, or which suggestion
  was clicked. NULL means the question predates 0059; never backfilled.

OLD CODE ON THE NEW SCHEMA is safe: nothing old reads the new tables or the column.
NEW CODE ON THE OLD SCHEMA is not: ``AssessmentChatUsage`` maps ``question_origin``, so
every chat question and every ledger read raises UndefinedColumn, and the detail pages
select ``assessment_chat_suggestions`` (UndefinedTable). Migrate BEFORE the new web app
and worker serve. The engine never reads any of the three, so the agent image needs no
rebuild for this revision.

Downgrade drops both tables, every row in them and the column.

Revision ID: 0059
Revises: 0058
Create Date: 2026-10-05
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIERS = "('staff', 'reviewer')"
_ORIGINS = "('typed', 'drawer_generated', 'drawer_template', 'inline_generated', 'inline_template')"
_VIAS = "('bubble', 'link', 'inline')"
_STATUSES = "('ready', 'failed', 'refused')"


def upgrade() -> None:
    op.add_column(
        "assessment_chat_usage",
        sa.Column("question_origin", sa.String(20), nullable=True),
    )
    op.create_check_constraint(
        "ck_assessment_chat_usage_question_origin",
        "assessment_chat_usage",
        f"question_origin IN {_ORIGINS}",
    )

    op.create_table(
        "assessment_chat_suggestions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "assessment_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunity_assessments.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("context_tier", sa.String(10), nullable=False),
        sa.Column("verdict_revision", sa.Integer, nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("suggestions", postgresql.JSONB, nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("attempts", sa.Integer, server_default=sa.text("1"), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("record_sha256_12", sa.String(12), nullable=False),
        sa.Column("prompt_sha256_12", sa.String(12), nullable=False),
        sa.Column("usage_by_model", postgresql.JSONB, nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "assessment_id", "context_tier", "verdict_revision",
            name="uq_assessment_chat_suggestions_key",
        ),
        sa.CheckConstraint(f"context_tier IN {_TIERS}", name="ck_assessment_chat_suggestions_tier"),
        sa.CheckConstraint(f"status IN {_STATUSES}", name="ck_assessment_chat_suggestions_status"),
    )
    op.create_index(
        "ix_assessment_chat_suggestions_updated", "assessment_chat_suggestions", ["updated_at"],
    )

    op.create_table(
        "assessment_chat_opens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "assessment_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunity_assessments.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("context_tier", sa.String(10), nullable=False),
        sa.Column("opened_via", sa.String(10), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(f"context_tier IN {_TIERS}", name="ck_assessment_chat_opens_tier"),
        sa.CheckConstraint(f"opened_via IN {_VIAS}", name="ck_assessment_chat_opens_via"),
    )
    op.create_index("ix_assessment_chat_opens_user_id", "assessment_chat_opens", ["user_id"])
    op.create_index("ix_assessment_chat_opens_assessment_id", "assessment_chat_opens", ["assessment_id"])
    op.create_index("ix_assessment_chat_opens_created", "assessment_chat_opens", ["created_at"])


def downgrade() -> None:
    op.drop_table("assessment_chat_opens")
    op.drop_table("assessment_chat_suggestions")
    op.drop_constraint(
        "ck_assessment_chat_usage_question_origin", "assessment_chat_usage", type_="check",
    )
    op.drop_column("assessment_chat_usage", "question_origin")

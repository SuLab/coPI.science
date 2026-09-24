"""assessment_chat_turns + assessment_chat_usage

Two new tables for the assessment-detail chat
(docs/specs/2026-09-24-assessment-chat-design.md §7): the private, deletable
conversation turns, and the content-free usage ledger the daily caps read. Purely
additive: OLD CODE AGAINST THE NEW SCHEMA IS SAFE. New code against the old schema
fails only in the three /assessment-chat routes (UndefinedTable) — nothing else
maps these tables. Downgrade drops both tables and every chat they hold.

Revision ID: 0051
Revises: 0050
Create Date: 2026-09-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0051"
down_revision: Union[str, None] = "0050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assessment_chat_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("opportunity_assessments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("context_tier", sa.String(10), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("answer_text", sa.Text, nullable=False, server_default=""),
        sa.Column("answer_segments", postgresql.JSONB, nullable=True),
        sa.Column("citations", postgresql.JSONB, nullable=True),
        sa.Column("allowed_links", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("stop_reason", sa.String(40), nullable=True),
        sa.Column("refusal_category", sa.String(40), nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("fallback_used", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("record_sha256_12", sa.String(12), nullable=False),
        sa.Column("prompt_sha256_12", sa.String(12), nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("context_tier IN ('staff', 'reviewer')", name="ck_assessment_chat_turns_tier"),
        sa.CheckConstraint(
            "status IN ('streaming', 'complete', 'truncated', 'refused', 'failed', 'interrupted')",
            name="ck_assessment_chat_turns_status",
        ),
    )
    op.create_index(
        "ix_assessment_chat_turns_conversation",
        "assessment_chat_turns", ["assessment_id", "user_id", "context_tier", "created_at"],
    )
    op.create_index("ix_assessment_chat_turns_user_id", "assessment_chat_turns", ["user_id"])
    # At most ONE answer in flight per user — the second concurrent question
    # raises IntegrityError and the route answers 409 answer_in_progress.
    op.create_index(
        "uq_assessment_chat_turns_one_streaming_per_user",
        "assessment_chat_turns", ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'streaming'"),
    )

    op.create_table(
        "assessment_chat_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("assessment_chat_turns.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("assessment_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("opportunity_assessments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("context_tier", sa.String(10), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("served_by_model", sa.String(100), nullable=True),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=True),
        sa.Column("output_tokens", sa.Integer, nullable=True),
        sa.Column("cache_read_input_tokens", sa.Integer, nullable=True),
        sa.Column("cache_creation_input_tokens", sa.Integer, nullable=True),
        sa.Column("usage_by_model", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("context_tier IN ('staff', 'reviewer')", name="ck_assessment_chat_usage_tier"),
    )
    op.create_index("ix_assessment_chat_usage_user_created", "assessment_chat_usage", ["user_id", "created_at"])
    op.create_index("ix_assessment_chat_usage_created", "assessment_chat_usage", ["created_at"])
    op.create_index("ix_assessment_chat_usage_turn_id", "assessment_chat_usage", ["turn_id"])
    op.create_index("ix_assessment_chat_usage_assessment_id", "assessment_chat_usage", ["assessment_id"])


def downgrade() -> None:
    op.drop_table("assessment_chat_usage")
    op.drop_table("assessment_chat_turns")

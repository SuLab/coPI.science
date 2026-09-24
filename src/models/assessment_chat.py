"""Assessment chat: one user's private questions about one assessment, and the
content-free ledger that bounds what they cost.

Two tables, split on purpose (docs/specs/2026-09-24-assessment-chat-design.md §7):

* ``assessment_chat_turns`` holds CONTENT — a question, its answer and the answer's
  citations. It is private to one user and deletable: it CASCADEs from the
  assessment (the engine's supersession and any deletion remove it) and from the
  user.
* ``assessment_chat_usage`` holds NO content — tokens per model, a status and two
  timestamps. The daily question cap and the dollar ceilings count it, so every
  foreign key is SET NULL: Clear, an assessment's deletion and a user's deletion
  all leave the cost record behind.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

#: Which page's record answered: `staff` (admin, manager) or `reviewer`. A user
#: whose role moves between the two sees only the current tier's turns (§7.3), so
#: a demoted manager never rereads answers built from staff-only fields.
CHAT_TIER_STAFF = "staff"
CHAT_TIER_REVIEWER = "reviewer"
CHAT_TIERS = (CHAT_TIER_STAFF, CHAT_TIER_REVIEWER)

CHAT_STATUS_STREAMING = "streaming"
CHAT_STATUS_COMPLETE = "complete"
CHAT_STATUS_TRUNCATED = "truncated"
CHAT_STATUS_REFUSED = "refused"
CHAT_STATUS_FAILED = "failed"
CHAT_STATUS_INTERRUPTED = "interrupted"
CHAT_STATUSES = (
    CHAT_STATUS_STREAMING,
    CHAT_STATUS_COMPLETE,
    CHAT_STATUS_TRUNCATED,
    CHAT_STATUS_REFUSED,
    CHAT_STATUS_FAILED,
    CHAT_STATUS_INTERRUPTED,
)
#: Statuses whose answer goes back to the model as history (§5.3) — and then only
#: when the answer text is not blank, because the API rejects an empty text block.
CHAT_REPLAYABLE_STATUSES = (CHAT_STATUS_COMPLETE, CHAT_STATUS_TRUNCATED)

#: The ledger's four token columns, and the token keys of each `usage_by_model` entry.
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

#: The partial unique index behind "one answer in flight per user". The ask route
#: recognises its IntegrityError by this name (§6.2 step 10).
ONE_STREAMING_INDEX = "uq_assessment_chat_turns_one_streaming_per_user"


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class AssessmentChatTurn(Base):
    """One question and its answer, private to ``user_id``."""

    __tablename__ = "assessment_chat_turns"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("opportunity_assessments.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    context_tier: Mapped[str] = mapped_column(String(10), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: Markdown, after the link rewrite and the private-use strip (§5.5).
    answer_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    #: `[{"text", "cites": [n]}]` — the answer's text blocks, in order.
    answer_segments: Mapped[list | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    #: `[{"n", "doc", "anchor", "label", "cited_text"}]`, numbered by first appearance.
    citations: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    #: The answer's URLs found verbatim in this tier's record (D20) — the only links
    #: the drawer ever makes clickable.
    allowed_links: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    refusal_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: The model the request named; `served_by_model` is the one that answered.
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fallback_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: First 12 hex of the record's sha256 when the question was asked (§4.4): the
    #: history flags an answer whose record has changed since.
    record_sha256_12: Mapped[str] = mapped_column(String(12), nullable=False)
    prompt_sha256_12: Mapped[str] = mapped_column(String(12), nullable=False)
    #: Question accepted -> final answer persisted.
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Written by the application at insert (see the plan's clock note); the server
    #: default is a fallback only.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"context_tier IN ({_sql_in(CHAT_TIERS)})", name="ck_assessment_chat_turns_tier"
        ),
        CheckConstraint(
            f"status IN ({_sql_in(CHAT_STATUSES)})", name="ck_assessment_chat_turns_status"
        ),
        Index(
            "ix_assessment_chat_turns_conversation",
            "assessment_id",
            "user_id",
            "context_tier",
            "created_at",
        ),
        # The repo indexes every ondelete FK (issue #25 P1 / 0033).
        Index("ix_assessment_chat_turns_user_id", "user_id"),
        Index(
            ONE_STREAMING_INDEX,
            "user_id",
            unique=True,
            postgresql_where=text(f"status = '{CHAT_STATUS_STREAMING}'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<AssessmentChatTurn {self.id} assessment={self.assessment_id} status={self.status}>"


class AssessmentChatUsage(Base):
    """One question's cost record. It never holds content, and it outlives the turn."""

    __tablename__ = "assessment_chat_usage"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assessment_chat_turns.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("opportunity_assessments.id", ondelete="SET NULL"),
        nullable=True,
    )
    context_tier: Mapped[str] = mapped_column(String(10), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: Mirrors the turn at completion; `streaming` while the answer is in flight.
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    #: Billed iterations summed (§5.7). NULL means never reported.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_creation_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: `[{"model", "billed", <TOKEN_FIELDS>}]`, one entry per API iteration. NULL means
    #: no usage was recorded at all, which the ceilings count at the reserve; `[]`
    #: means the request is known not to have been billed.
    usage_by_model: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"context_tier IN ({_sql_in(CHAT_TIERS)})", name="ck_assessment_chat_usage_tier"
        ),
        Index("ix_assessment_chat_usage_user_created", "user_id", "created_at"),
        Index("ix_assessment_chat_usage_created", "created_at"),
        Index("ix_assessment_chat_usage_turn_id", "turn_id"),
        Index("ix_assessment_chat_usage_assessment_id", "assessment_id"),
    )

    def __repr__(self) -> str:
        return f"<AssessmentChatUsage {self.id} turn={self.turn_id} status={self.status}>"

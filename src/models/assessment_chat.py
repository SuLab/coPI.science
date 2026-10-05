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

Two more (0059), for the drawer's per-assessment opening questions:

* ``assessment_chat_suggestions`` holds the questions the worker generated for one
  (assessment, tier, verdict revision), or why it could not. They are written from that
  tier's record, so they are assessment content, not a user's: they CASCADE from the
  assessment and belong to no user.
* ``assessment_chat_opens`` is content-free like the ledger: one row per drawer opening,
  every foreign key SET NULL.
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
    UniqueConstraint,
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

#: Where a question came from (0059), recorded on the ledger row: typed into the box, or
#: a suggestion clicked in the drawer or beside a page section — generated for this
#: assessment, or the deterministic template set. NULL: asked before 0059, or by a page
#: that did not say.
QUESTION_ORIGIN_TYPED = "typed"
QUESTION_ORIGINS = (
    QUESTION_ORIGIN_TYPED,
    "drawer_generated",
    "drawer_template",
    "inline_generated",
    "inline_template",
)

#: How the drawer was opened (0059): the floating bubble, the list page's `#chat` link,
#: or an inline question beside a page section.
OPEN_VIAS = ("bubble", "link", "inline")

SUGGESTION_STATUS_READY = "ready"
SUGGESTION_STATUS_FAILED = "failed"  # retried, up to the service's attempt cap
SUGGESTION_STATUS_REFUSED = "refused"  # a refusal; never retried for this revision
SUGGESTION_STATUSES = (SUGGESTION_STATUS_READY, SUGGESTION_STATUS_FAILED, SUGGESTION_STATUS_REFUSED)


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
    #: The assessment's verdict revision this turn was asked against (0054).
    #: NULL on pre-0054 turns and never backfilled; replay and the turn cap
    #: compare COALESCE(turn, 1) with COALESCE(assessment, 1) (spec §8.1, B6).
    verdict_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    #: One of QUESTION_ORIGINS (0059). NULL: asked before 0059, or the page did not say.
    question_origin: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"context_tier IN ({_sql_in(CHAT_TIERS)})", name="ck_assessment_chat_usage_tier"
        ),
        CheckConstraint(
            f"question_origin IN ({_sql_in(QUESTION_ORIGINS)})",
            name="ck_assessment_chat_usage_question_origin",
        ),
        Index("ix_assessment_chat_usage_user_created", "user_id", "created_at"),
        Index("ix_assessment_chat_usage_created", "created_at"),
        Index("ix_assessment_chat_usage_turn_id", "turn_id"),
        Index("ix_assessment_chat_usage_assessment_id", "assessment_id"),
        Index(
            "ix_chat_usage_streaming", "created_at",
            postgresql_where=text("status = 'streaming'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<AssessmentChatUsage {self.id} turn={self.turn_id} status={self.status}>"


class AssessmentChatSuggestionSet(Base):
    """The drawer's opening questions for one (assessment, tier, verdict revision), as
    the worker generated them from that tier's record — or why it could not.

    ``suggestions`` is ``[{"text", "anchor", "label"}]``: the question, the page element
    id of the verdict block it is about, and that block's record label. A row that is not
    ``ready`` carries none and the page shows the template set instead. A ``failed`` row
    is retried in place (``attempts`` counts them, ``usage_by_model`` accumulates every
    attempt's usage), so the daily ceiling sees what each one cost.
    """

    __tablename__ = "assessment_chat_suggestions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("opportunity_assessments.id", ondelete="CASCADE"),
        nullable=False,
    )
    context_tier: Mapped[str] = mapped_column(String(10), nullable=False)
    #: COALESCE(assessment.verdict_revision, 1) when generated: a revised verdict gets
    #: its own row, and the page reads only the current revision's.
    verdict_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False)
    suggestions: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    served_by_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    record_sha256_12: Mapped[str] = mapped_column(String(12), nullable=False)
    prompt_sha256_12: Mapped[str] = mapped_column(String(12), nullable=False)
    #: The ledger's shape (`[{"model", "billed", <TOKEN_FIELDS>}]`), every attempt's
    #: entries appended. NULL: no attempt reported usage.
    usage_by_model: Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # Leads with assessment_id, so it is also the ondelete FK's index.
        UniqueConstraint(
            "assessment_id", "context_tier", "verdict_revision",
            name="uq_assessment_chat_suggestions_key",
        ),
        CheckConstraint(
            f"context_tier IN ({_sql_in(CHAT_TIERS)})", name="ck_assessment_chat_suggestions_tier"
        ),
        CheckConstraint(
            f"status IN ({_sql_in(SUGGESTION_STATUSES)})",
            name="ck_assessment_chat_suggestions_status",
        ),
        Index("ix_assessment_chat_suggestions_updated", "updated_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<AssessmentChatSuggestionSet {self.id} assessment={self.assessment_id} "
            f"tier={self.context_tier} status={self.status}>"
        )


class AssessmentChatOpen(Base):
    """One opening of the drawer. Content-free; outlives its user and assessment."""

    __tablename__ = "assessment_chat_opens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
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
    #: One of OPEN_VIAS; NULL when the page did not say.
    opened_via: Mapped[str | None] = mapped_column(String(10), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"context_tier IN ({_sql_in(CHAT_TIERS)})", name="ck_assessment_chat_opens_tier"
        ),
        CheckConstraint(
            f"opened_via IN ({_sql_in(OPEN_VIAS)})", name="ck_assessment_chat_opens_via"
        ),
        Index("ix_assessment_chat_opens_user_id", "user_id"),
        Index("ix_assessment_chat_opens_assessment_id", "assessment_id"),
        Index("ix_assessment_chat_opens_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<AssessmentChatOpen {self.id} assessment={self.assessment_id} via={self.opened_via}>"

"""Human-review-feedback models: reviews, review audit events, review
assignments, and prompt-change suggestions distilled from them.

Every FK here follows the analysis in
``docs/plans/2026-08-28-human-review-feedback-adversarial-analysis.md`` (A-1,
A-2, A-3):

* ``AssessmentReview``, ``AssessmentReviewEvent`` and
  ``AssessmentReviewAssignment`` all CASCADE off ``opportunity_assessments``
  (A-1). The engine's ``_retire_superseded_verdict`` hard-DELETEs a stored
  provisional verdict when a later sidecar supersedes it, minutes apart,
  mid-run — RESTRICT would make that delete raise (swallowed, logged, and
  leaving two rows for one interview, breaking the one-row invariant), and
  SET NULL would orphan a review with nothing to render it against. CASCADE
  means a human's review on a provisional row can be silently lost to
  supersession; that is accepted as the least-bad of the three options
  (H-5), and an engine re-point to the surviving row is the optional
  follow-up A-1 also names.
* Because assessments CASCADE from ``simulation_runs`` (A-2), and the
  standing "never DELETE from simulation_runs" archive rule already treats
  that as a database-level red line, this table set now also stands behind
  human-authored data, not just bot output.
* ``PromptChangeSuggestion.assessment_id`` is SET NULL instead (A-2):
  suggestions are distilled human+bot judgment about the *prompts*, so they
  must outlive both a supersession and any future run purge. The
  ``subject_label``/``assessment_created_at``/``rubric_version`` snapshot
  columns keep a suggestion self-describing once the FK has been nulled.
* Reviewer/actor/assignee "owner" FKs to ``users`` follow A-3: a review or
  event authored *by* a since-deleted staff member survives (SET NULL) with
  its name denormalized at write time (the ``CohortAuditEvent.actor_email``
  precedent, F17) so attribution outlives the account; an assignment made
  *to* a deleted user is meaningless and CASCADEs away instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


class AssessmentReview(Base):
    """One human reviewer's score/comment on a BlackbirdBot verdict.

    ``assessment_id`` CASCADEs (A-1); ``reviewer_user_id`` is SET NULL with
    ``reviewer_name`` denormalized (A-3) so the review stays attributable
    after the reviewer's account is deleted.
    """

    __tablename__ = "assessment_reviews"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("opportunity_assessments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    reviewer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    #: 'learn' / 'log_only' — whether the review-feedback-analysis job should
    #: consider this row.
    feedback_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    edited: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    #: Set when a review-feedback-analysis job has consumed this row
    #: (idempotency ledger). NULL means "not yet consumed".
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<AssessmentReview assessment={self.assessment_id} score={self.score}>"


class AssessmentReviewEvent(Base):
    """Append-only audit trail of approve/disapprove/clear actions.

    ``assessment_id`` CASCADEs (A-1); ``actor_user_id`` is SET NULL with
    ``actor_name`` denormalized (A-3), the same pattern as
    ``CohortAuditEvent.actor_email``.
    """

    __tablename__ = "assessment_review_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("opportunity_assessments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: 'approved' / 'disapproved' / 'cleared'.
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<AssessmentReviewEvent {self.action} assessment={self.assessment_id}>"
        )


class AssessmentReviewAssignment(Base):
    """A staff member assigned to review one assessment.

    ``assessment_id`` CASCADEs (A-1). ``assignee_user_id`` CASCADEs too
    (A-3): an assignment *to* a since-deleted user is meaningless and should
    vanish rather than be orphaned. ``assigned_by_user_id`` is SET NULL with
    ``assigned_by_name`` denormalized, the same reviewer-survives-deletion
    pattern as the other tables here. One assignment per (assessment,
    assignee) — reassigning the same person is a no-op, not a duplicate row.
    """

    __tablename__ = "assessment_review_assignments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("opportunity_assessments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assignee_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    assignee_name: Mapped[str] = mapped_column(String(255), nullable=False)
    assigned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    assigned_by_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "assessment_id", "assignee_user_id", name="uq_review_assignment_once"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AssessmentReviewAssignment assessment={self.assessment_id} "
            f"assignee={self.assignee_user_id}>"
        )


class PromptChangeSuggestion(Base):
    """A review-bot-drafted prompt-edit suggestion, distilled from human feedback.

    ``assessment_id`` is SET NULL, not CASCADE (A-2): the suggestion is
    distilled human+bot judgment about the *prompts*, so it must survive
    both an assessment supersession and any future run-archive purge. The
    snapshot columns (``subject_label``, ``assessment_created_at``,
    ``rubric_version``) keep the row self-describing once the FK is nulled.
    ``prompt_files`` records the sha256[:12] of every prompt file the bot
    read, so a page can badge a suggestion whose target files have since
    changed (the rubric content-hash precedent). Never auto-applied;
    ``status`` is staff-set attribution only.
    """

    __tablename__ = "prompt_change_suggestions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("opportunity_assessments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    subject_label: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    assessment_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rubric_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: The assessment_reviews.id's this suggestion was distilled from
    #: (provenance). NOT NULL: a suggestion always names the feedback that
    #: produced it.
    feedback_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False)
    #: 'scout_hub' / 'pi_lab' / 'specialist:<domain>' / 'out_of_scope'.
    target: Mapped[str] = mapped_column(String(40), nullable=False)
    #: [{path, sha256_12}] for every prompt file the bot read.
    prompt_files: Mapped[list] = mapped_column(JSONB, nullable=False)
    suggestion: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    transcript_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    input_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    raw_response: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    #: 'open' / 'dismissed' / 'implemented'.
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="open")
    status_set_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    status_set_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status_set_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<PromptChangeSuggestion target={self.target} status={self.status}>"
        )

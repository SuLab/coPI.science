"""Review-feedback writes: submit, edit, and the enqueue dedupe that keeps
rapid submissions/edits from buying repeated Opus calls; plus the
approval-status audit trail and reviewer-assignment writes (Task 5).

``review_feedback_analysis`` re-reads ALL unconsumed 'learn' feedback for an
assessment in one pass (a later task), so the job itself is the batching
unit — enqueueing one per submission would turn N rapid reviewer edits into N
redundant model calls over the same rows. ``enqueue_analysis_if_absent`` is
the guard: it only inserts a new job when no pending/processing
``review_feedback_analysis`` job already names this assessment_id.

``record_status_event`` is APPEND-ONLY (never an update — the history is the
point) and ``assign_reviewer``/``unassign_reviewer`` are the reviewer-roster
writes behind it. ``assign_reviewer`` is idempotent via
``pg_insert(...).on_conflict_do_nothing(constraint="uq_review_assignment_once")``
— reassigning the same (assessment, assignee) pair is a no-op, not a
duplicate row or a raised IntegrityError (the named-constraint form has
precedent at ``src/agent/simulation.py:6298``).

``review_columns_for`` (Task 7) is the batched read behind the two list
pages' "Assigned"/"Reviewed by" columns and approval-status chip — see its
own docstring for why it is exactly three ``IN``-clause queries plus a
Python fold, never a ``DISTINCT ON``.
"""

from __future__ import annotations

import uuid
from collections import namedtuple
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    AssessmentReview,
    AssessmentReviewAssignment,
    AssessmentReviewEvent,
    Job,
    OpportunityAssessment,
    User,
)

#: The three approval-status actions a reviewer/staff member may record.
#: Append-only history — see AssessmentReviewEvent.
VALID_STATUS_ACTIONS = ("approved", "disapproved", "cleared")

#: The only two feedback modes a reviewer may submit. 'learn' feeds the
#: prompt-suggestion pipeline (enqueues analysis); 'log_only' is recorded but
#: never analyzed.
VALID_FEEDBACK_MODES = ("learn", "log_only")

#: Comment rows are capped, not rejected — a reviewer pasting an overlong
#: transcript should still get a saved row, just truncated.
_MAX_COMMENT_CHARS = 10_000

#: The list-page columns behind one assessment row's "Assigned"/"Reviewed by"
#: cells and status chip (Task 7). ``status`` is ``None`` for "never
#: reviewed" AND for "reviewed, then cleared" — see ``_CHIP_STATUSES``.
ReviewColumns = namedtuple("ReviewColumns", "assigned_names reviewed_by_names status")

#: What ``review_columns_for`` returns for an assessment id with no
#: assignment, feedback, or status-event rows at all.
EMPTY_REVIEW_COLUMNS = ReviewColumns((), (), None)

#: The only two ``VALID_STATUS_ACTIONS`` that leave an active status for the
#: chip to show. 'cleared' IS a legitimate, storable action — it is how a
#: reviewer undoes a prior approve/disapprove — but its effect is "no active
#: status", the same as never having been reviewed at all, so it folds to
#: ``None`` here rather than becoming a fourth chip. This is a narrower list
#: than ``VALID_STATUS_ACTIONS`` on purpose: that constant governs what may be
#: WRITTEN, this one governs what the chip may SHOW.
_CHIP_STATUSES = ("approved", "disapproved")


def _validate(score: int, feedback_mode: str) -> None:
    if not (1 <= score <= 5):
        raise ValueError("score must be between 1 and 5")
    if feedback_mode not in VALID_FEEDBACK_MODES:
        raise ValueError(f"feedback_mode must be one of {VALID_FEEDBACK_MODES}")


async def enqueue_analysis_if_absent(
    db: AsyncSession, *, assessment_id: uuid.UUID, user_id: uuid.UUID | None
) -> bool:
    """Enqueue one ``review_feedback_analysis`` job for ``assessment_id``,
    unless a pending/processing job for it already exists. Returns whether a
    new job was enqueued."""
    existing = (
        (
            await db.execute(
                select(Job.id)
                .where(
                    Job.type == "review_feedback_analysis",
                    Job.status.in_(("pending", "processing")),
                    Job.payload["assessment_id"].astext == str(assessment_id),
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return False
    db.add(
        Job(
            type="review_feedback_analysis",
            user_id=user_id,
            payload={"assessment_id": str(assessment_id)},
        )
    )
    return True


async def submit_feedback(
    db: AsyncSession,
    *,
    assessment: OpportunityAssessment,
    reviewer: User,
    score: int,
    comment: str,
    feedback_mode: str,
) -> AssessmentReview:
    """Create one ``AssessmentReview`` row. Caller commits.

    Raises ``ValueError`` on an out-of-range score or an unrecognized mode.
    ``reviewer_name`` is denormalized at write time (A-3) so the review stays
    attributable after the reviewer's account is deleted.
    """
    _validate(score, feedback_mode)
    review = AssessmentReview(
        assessment_id=assessment.id,
        reviewer_user_id=reviewer.id,
        reviewer_name=reviewer.name,
        score=score,
        comment=comment[:_MAX_COMMENT_CHARS],
        feedback_mode=feedback_mode,
    )
    db.add(review)
    await db.flush()
    if feedback_mode == "learn":
        await enqueue_analysis_if_absent(
            db, assessment_id=assessment.id, user_id=reviewer.id
        )
    return review


async def edit_feedback(
    db: AsyncSession,
    *,
    review: AssessmentReview,
    score: int,
    comment: str,
    feedback_mode: str,
) -> AssessmentReview:
    """Mutate an existing review in place. Caller commits.

    Author-only is the router's check, not this function's. Resets
    ``consumed_at`` to ``None`` so an edited row is picked back up by the
    next analysis job even if the original had already been consumed.
    """
    _validate(score, feedback_mode)
    review.score = score
    review.comment = comment[:_MAX_COMMENT_CHARS]
    review.feedback_mode = feedback_mode
    review.edited = True
    review.consumed_at = None
    if feedback_mode == "learn":
        await enqueue_analysis_if_absent(
            db, assessment_id=review.assessment_id, user_id=review.reviewer_user_id
        )
    return review


async def record_status_event(
    db: AsyncSession,
    *,
    assessment: OpportunityAssessment,
    actor: User,
    action: str,
) -> AssessmentReviewEvent:
    """Append one ``AssessmentReviewEvent`` row. Caller commits.

    Raises ``ValueError`` on an action outside ``VALID_STATUS_ACTIONS``.
    Append-only: there is no update path, by design — the point of this
    table is the history, not the current status. ``actor_name`` is
    denormalized at write time (A-3), same as ``reviewer_name`` above, so
    the event stays attributable after the actor's account is deleted.
    """
    if action not in VALID_STATUS_ACTIONS:
        raise ValueError(f"action must be one of {VALID_STATUS_ACTIONS}")
    event = AssessmentReviewEvent(
        assessment_id=assessment.id,
        action=action,
        actor_user_id=actor.id,
        actor_name=actor.name,
    )
    db.add(event)
    await db.flush()
    return event


async def assign_reviewer(
    db: AsyncSession,
    *,
    assessment: OpportunityAssessment,
    assignee: User,
    assigned_by: User,
) -> None:
    """Idempotently record ``assignee`` as assigned to review ``assessment``.

    Caller commits. Caller is also responsible for validating that
    ``assignee`` is review-capable and allowed — this function only
    dedupes. Reassigning the same (assessment, assignee) pair is a no-op,
    not a duplicate row or a raised ``IntegrityError``:
    ``on_conflict_do_nothing`` names the unique constraint explicitly rather
    than relying on inferred-columns matching, the same precedent as
    ``src/agent/simulation.py``'s ``AgentMessage`` upsert.
    """
    stmt = (
        pg_insert(AssessmentReviewAssignment.__table__)
        .values(
            assessment_id=assessment.id,
            assignee_user_id=assignee.id,
            assignee_name=assignee.name,
            assigned_by_user_id=assigned_by.id,
            assigned_by_name=assigned_by.name,
        )
        .on_conflict_do_nothing(constraint="uq_review_assignment_once")
    )
    await db.execute(stmt)


async def unassign_reviewer(
    db: AsyncSession,
    *,
    assessment: OpportunityAssessment,
    assignee_user_id: uuid.UUID,
) -> None:
    """Remove the (assessment, assignee) assignment row, if any. Caller
    commits. Silently a no-op when no such assignment exists — unassigning
    someone who was never assigned, or was already removed, is not an
    error."""
    await db.execute(
        delete(AssessmentReviewAssignment).where(
            AssessmentReviewAssignment.assessment_id == assessment.id,
            AssessmentReviewAssignment.assignee_user_id == assignee_user_id,
        )
    )


def _dedup_ordered(names) -> list[str]:
    """First-seen order, deduplicated. Local rather than ``dict.fromkeys``
    spelled out, so the intent reads at the call site."""
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


async def review_columns_for(
    db: AsyncSession, assessment_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, ReviewColumns]:
    """Batch the Assigned/Reviewed-by/status-chip columns for a page of
    assessment rows — one dict lookup per row rather than N+1 queries.

    Exactly THREE ``IN``-clause queries, never a ``DISTINCT ON``: the status
    chip needs only the LATEST status event per assessment, while "reviewed
    by" needs EVERY actor who ever touched the row (a feedback author or a
    status-event actor, unioned and deduplicated) — one ``DISTINCT ON`` query
    returns a single row per assessment and would silently drop every earlier
    actor, which is exactly the information "reviewed by" exists to show.

    Early-returns ``{}`` for an empty ``assessment_ids`` without touching
    ``db`` at all — the zero-query contract for an empty run/page (there is
    no query-count listener in this suite, so the empty return IS the
    assertable contract).

    An assessment id with no assignment, feedback, or status-event row at all
    is simply absent from the returned dict — callers fall back to
    ``EMPTY_REVIEW_COLUMNS`` (see ``directory.list_assessments``), the same
    "missing means untouched" shape whether ``assessment_ids`` names one id
    or a page of them.
    """
    ids = list(assessment_ids)
    if not ids:
        return {}

    assignment_rows = (
        await db.execute(
            select(
                AssessmentReviewAssignment.assessment_id,
                AssessmentReviewAssignment.assignee_name,
            )
            .where(AssessmentReviewAssignment.assessment_id.in_(ids))
            .order_by(
                AssessmentReviewAssignment.assessment_id,
                AssessmentReviewAssignment.created_at,
                AssessmentReviewAssignment.id,
            )
        )
    ).all()

    review_rows = (
        await db.execute(
            select(
                AssessmentReview.assessment_id,
                AssessmentReview.reviewer_name,
                AssessmentReview.created_at,
            )
            .where(AssessmentReview.assessment_id.in_(ids))
            .order_by(
                AssessmentReview.assessment_id,
                AssessmentReview.created_at,
                AssessmentReview.id,
            )
        )
    ).all()

    # Ordered (assessment_id, created_at, id) rather than left to arrive in
    # whatever order Postgres feels like: the LAST row per assessment (by
    # this order) is the status the chip shows, and every row along the way
    # still contributes its actor to "reviewed by". `func.now()` is
    # transaction-start time, so two events written in the same transaction
    # can share a `created_at` — the `id` tiebreak is what keeps "last"
    # deterministic when that happens.
    event_rows = (
        await db.execute(
            select(
                AssessmentReviewEvent.assessment_id,
                AssessmentReviewEvent.actor_name,
                AssessmentReviewEvent.action,
                AssessmentReviewEvent.created_at,
            )
            .where(AssessmentReviewEvent.assessment_id.in_(ids))
            .order_by(
                AssessmentReviewEvent.assessment_id,
                AssessmentReviewEvent.created_at,
                AssessmentReviewEvent.id,
            )
        )
    ).all()

    assigned_by_id: dict[uuid.UUID, list[str]] = {}
    for row in assignment_rows:
        assigned_by_id.setdefault(row.assessment_id, []).append(row.assignee_name)

    # "Reviewed by" = distinct feedback authors UNION every status-event
    # actor, in the order they actually acted — not "every comment, then
    # every status change" — so the two sources are merged by `created_at`
    # before deduplication.
    actor_events_by_id: dict[uuid.UUID, list[tuple]] = {}
    for row in review_rows:
        actor_events_by_id.setdefault(row.assessment_id, []).append(
            (row.created_at, row.reviewer_name)
        )
    for row in event_rows:
        actor_events_by_id.setdefault(row.assessment_id, []).append(
            (row.created_at, row.actor_name)
        )

    reviewed_by_id: dict[uuid.UUID, list[str]] = {}
    for assessment_id, entries in actor_events_by_id.items():
        entries.sort(key=lambda entry: entry[0])
        reviewed_by_id[assessment_id] = _dedup_ordered(name for _, name in entries)

    # The events query above is already ordered ascending per assessment, so
    # a plain overwrite loop leaves the LAST event's action standing — see
    # `_CHIP_STATUSES` for why 'cleared' folds to `None` instead of standing
    # as its own action here.
    status_by_id: dict[uuid.UUID, str | None] = {}
    for row in event_rows:
        status_by_id[row.assessment_id] = (
            row.action if row.action in _CHIP_STATUSES else None
        )

    all_ids = set(assigned_by_id) | set(reviewed_by_id) | set(status_by_id)
    return {
        assessment_id: ReviewColumns(
            tuple(_dedup_ordered(assigned_by_id.get(assessment_id, []))),
            tuple(reviewed_by_id.get(assessment_id, [])),
            status_by_id.get(assessment_id),
        )
        for assessment_id in all_ids
    }

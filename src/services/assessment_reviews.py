"""Review-feedback writes: submit, edit, the MANUAL prompt-suggestion
scheduler, plus the approval-status audit trail and reviewer-assignment
writes.

**Nothing here enqueues a ``review_feedback_analysis`` job as a side effect
of a review write any more** (F2, 2026-09-14, decision D7). Submitting or
editing 'learn' feedback records the row and stops there; a job is created
only when a human presses "Generate suggestions from current reviews"
(``POST /reviews/suggestions/generate`` → ``enqueue_pending_analyses``).
Each job is a 70-90k-token Opus call, and paying for one per submission —
which is what the old auto-enqueue did whenever no pending job happened to
cover the assessment — spent real money on nobody's request.

``review_feedback_analysis`` re-reads ALL unconsumed 'learn' feedback for an
assessment in one pass, so the job is still the batching unit and
``enqueue_analysis_if_absent`` is still the idempotency guard: it only
inserts a new job when no PENDING ``review_feedback_analysis`` job already
names this assessment_id (a processing job has already snapshotted its rows
and cannot cover a later one — see the function's docstring). That is what
makes the new button safe to press twice.

The anti-loss guarantee is UNCHANGED and was never the enqueue's job:
``review_bot.consumed_at_predicates`` refuses to stamp a row that no longer
reads exactly as snapshotted, so an edit made mid-call leaves the row
unconsumed and therefore still eligible for the next manual pass.
``count_pending_analysis_candidates`` is what lets a page say how many
assessments that is.

``record_status_event`` is APPEND-ONLY (never an update — the history is the
point) and ``assign_reviewer``/``unassign_reviewer`` are the reviewer-roster
writes behind it. ``assign_reviewer`` is idempotent via
``pg_insert(...).on_conflict_do_nothing(constraint="uq_review_assignment_once")``
— reassigning the same (assessment, assignee) pair is a no-op, not a
duplicate row or a raised IntegrityError (the named-constraint form has
precedent at ``src/agent/simulation.py:7415``).

``review_columns_for`` is the batched read behind the two list
pages' "Assigned"/"Reviewed by" columns and approval-status chip — see its
own docstring for why it is exactly three ``IN``-clause queries plus a
Python fold, never a ``DISTINCT ON``.

``blackbird_rubric`` is imported here for dimension-key validation. That is
safe because this module is web-tier only — it is imported by
``src/routers/reviews.py``, ``src/routers/manager.py`` and
``src/services/directory.py`` and by nothing the worker loads.
``src/services/review_bot.py``, which DOES run on the worker,
must stay free of ``blackbird_rubric``/``rubric_revisions``/``assessment_detail``/
``assessment_reviews``; see
``tests/unit/test_review_bot.py::test_the_bot_module_stays_free_of_web_tier_rubric_modules``,
an AST import scan in the same idiom as that file's
``test_the_bot_module_imports_no_transport``.
"""

from __future__ import annotations

import uuid
from collections import namedtuple
from collections.abc import Sequence

from sqlalchemy import delete, func, select
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
from src.services.blackbird_rubric import (
    RUBRIC_CONTENT_HASH,
    RUBRIC_VERSION,
    load_rubric,
)

#: The three approval-status actions a reviewer/staff member may record.
#: Append-only history — see AssessmentReviewEvent.
VALID_STATUS_ACTIONS = ("approved", "disapproved", "cleared")

#: The only two feedback modes a reviewer may submit. 'learn' makes the
#: assessment eligible for the prompt-suggestion pipeline (a human then
#: enqueues it); 'log_only' is recorded but never analyzed.
VALID_FEEDBACK_MODES = ("learn", "log_only")

#: Comment rows are capped, not rejected — a reviewer pasting an overlong
#: transcript should still get a saved row, just truncated.
_MAX_COMMENT_CHARS = 10_000

#: The list-page columns behind one assessment row's "Assigned"/"Reviewed by"
#: cells and status chip. ``status`` is ``None`` for "never
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


def _validate(
    score: int,
    feedback_mode: str,
    dimension_scores: dict[str, int] | None = None,
) -> None:
    if not (1 <= score <= 5):
        raise ValueError("score must be between 1 and 5")
    if feedback_mode not in VALID_FEEDBACK_MODES:
        raise ValueError(f"feedback_mode must be one of {VALID_FEEDBACK_MODES}")
    if not dimension_scores:
        return
    # Validated against the LIVE document, which is also what gets stamped on
    # the row — the two cannot disagree, because they are read in the same call.
    rubric = load_rubric()
    valid_keys = {d.key for d in rubric.dimensions}
    for key, value in dimension_scores.items():
        if key not in valid_keys:
            raise ValueError(f"unknown rubric dimension: {key}")
        # `bool` subclasses `int`, so `True` would sail through the range check
        # below and store as a 1 nobody chose. Reject it explicitly.
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"dimension score for {key} must be an integer")
        if not (rubric.scale_min <= value <= rubric.scale_max):
            raise ValueError(
                f"dimension score for {key} must be between "
                f"{rubric.scale_min} and {rubric.scale_max}"
            )


def _normalized_dimension_scores(
    dimension_scores: dict[str, int] | None,
) -> dict[str, int] | None:
    """`{}` and `None` are the same state — "scored no dimensions" — so they
    store as one value. Two encodings of absence on a JSONB column is the
    defect 0031 and 0036 each had to repair once already."""
    return dimension_scores or None


async def enqueue_analysis_if_absent(
    db: AsyncSession, *, assessment_id: uuid.UUID, user_id: uuid.UUID | None
) -> bool:
    """Enqueue one ``review_feedback_analysis`` job for ``assessment_id``,
    unless a PENDING job for it already exists. Returns whether a new job was
    enqueued.

    ``pending`` only — never ``processing``. A processing job has already read
    the feedback rows it will analyze (``execute_review_analysis`` snapshots
    them before the model call), so it cannot cover a row written while it
    waits on the model; counting it here left that row with no job at all
    (audit 2026-09-02, D1). A job that lands in the queue behind a processing
    one simply finds whatever is still unconsumed when its turn comes, and
    completes as a no-op if that is nothing.
    """
    existing = (
        (
            await db.execute(
                select(Job.id)
                .where(
                    Job.type == "review_feedback_analysis",
                    Job.status == "pending",
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


#: Hard cap on how many assessments ONE press of "Generate suggestions from
#: current reviews" may queue. Each queued job is a single Opus call carrying
#: the whole prompt set plus a transcript — on the order of 70-90k input tokens
#: (see ``src/services/review_bot.py``'s cost note) — and the size of the
#: eligible set is controlled by the LEAST-privileged review role: a reviewer
#: makes an assessment eligible by leaving `learn` feedback on it, and
#: ``edit_feedback`` resets an already-consumed row's eligibility, while a
#: reviewer cannot see the suggestions page or press the button at all. So one
#: privileged click could otherwise spend an unbounded amount on work a
#: non-privileged account queued up. Capped rather than rate-limited because
#: the cap is also legible: the page reports "Queued X of Y" and the cap, and
#: a second press picks up where the first stopped.
MAX_ANALYSES_PER_PRESS = 25


async def _eligible_assessment_ids(
    db: AsyncSession, assessment_id: uuid.UUID | None = None
) -> tuple[list[uuid.UUID], int]:
    """``(ids_to_consider, total_eligible)`` — every assessment id with at
    least one UNCONSUMED 'learn' review, oldest feedback first.

    Eligibility is deliberately blind to the job queue: whether an id already
    has a pending job is ``enqueue_analysis_if_absent``'s question, not this
    one's, so the caller can report "eligible" and "enqueued" as two different
    numbers.

    ``total_eligible`` is the UNCAPPED count, so the caller can say how much
    is left. The returned list is the FULL eligible set, ordered by each id's
    oldest unconsumed feedback — the cap is applied by the caller, to what it
    actually enqueues, NOT here to the candidate slice. Capping the candidates
    looked equivalent and was not: the oldest ``N`` keep their pending jobs, so
    the next press re-picked the same ``N``, enqueued nothing, and a backlog
    larger than the cap could never drain. The order still matters for the same
    reason it did then — without it, which assessments get analysed would depend
    on whatever order Postgres felt like.
    """
    stmt = (
        select(
            AssessmentReview.assessment_id,
            func.min(AssessmentReview.created_at).label("oldest"),
        )
        .where(
            AssessmentReview.feedback_mode == "learn",
            AssessmentReview.consumed_at.is_(None),
        )
        .group_by(AssessmentReview.assessment_id)
        .order_by(func.min(AssessmentReview.created_at), AssessmentReview.assessment_id)
    )
    if assessment_id is not None:
        stmt = stmt.where(AssessmentReview.assessment_id == assessment_id)
    rows = (await db.execute(stmt)).all()
    return [r.assessment_id for r in rows], len(rows)


async def enqueue_pending_analyses(
    db: AsyncSession, *, requested_by: User, assessment_id: uuid.UUID | None = None
) -> tuple[int, int]:
    """Enqueue one ``review_feedback_analysis`` job per assessment that has at
    least one unconsumed 'learn' review and no PENDING job. Returns
    ``(enqueued, eligible)``. Caller commits.

    The manual replacement for the auto-enqueue F2 removed. ``enqueued`` can
    be smaller than ``eligible`` for TWO reasons, and neither is a failure:
    an id whose job is already pending is skipped by the dedupe, and the batch
    is capped at ``MAX_ANALYSES_PER_PRESS`` (see that constant for why). So
    pressing the button twice costs nothing the second time for anything
    already queued, and DOES make progress when the backlog exceeded the cap;
    the returned pair is what makes both visible instead of silent. A ``processing`` job deliberately does NOT count as
    covering an id, for the reason ``enqueue_analysis_if_absent``'s docstring
    gives: it has already snapshotted its rows.

    ``assessment_id``, when given, narrows to that one id (the per-assessment
    button). ``user_id=requested_by.id`` on each job so ``/admin/jobs``
    attributes it to whoever pressed the button — not to the reviewer whose
    feedback it will read.
    """
    ids, eligible = await _eligible_assessment_ids(db, assessment_id)
    # The already-pending set in ONE query, so the cap can be applied to ids
    # that will actually be enqueued without walking (and issuing a dedupe
    # SELECT for) every id in a long backlog. `enqueue_analysis_if_absent`
    # still runs per id as the authoritative guard — this only decides which
    # ids are worth offering it.
    already_pending = {
        row[0]
        for row in await db.execute(
            select(Job.payload["assessment_id"].astext).where(
                Job.type == "review_feedback_analysis", Job.status == "pending"
            )
        )
    }
    enqueued = 0
    for eligible_id in ids:
        if enqueued >= MAX_ANALYSES_PER_PRESS:
            break
        if str(eligible_id) in already_pending:
            continue
        if await enqueue_analysis_if_absent(
            db, assessment_id=eligible_id, user_id=requested_by.id
        ):
            enqueued += 1
    return enqueued, eligible


async def count_pending_analysis_candidates(db: AsyncSession) -> int:
    """How many assessments a manual generate would consider right now —
    DISTINCT ``assessment_id`` over unconsumed 'learn' reviews.

    This is the ELIGIBLE count, not the would-be-enqueued count: it does not
    subtract ids that already have a pending job, so a page can say "N
    assessments have feedback waiting" without a second query and without
    pretending to predict the dedupe. It is also the UNCAPPED total — one press
    processes at most ``MAX_ANALYSES_PER_PRESS`` of them — so a page showing
    this number alongside the cap tells a reader how many presses the backlog
    needs.
    """
    return (await _eligible_assessment_ids(db))[1]


async def submit_feedback(
    db: AsyncSession,
    *,
    assessment: OpportunityAssessment,
    reviewer: User,
    score: int,
    comment: str,
    feedback_mode: str,
    dimension_scores: dict[str, int] | None = None,
    recorded_by: User | None = None,
) -> AssessmentReview:
    """Create one ``AssessmentReview`` row. Caller commits.

    Raises ``ValueError`` on an out-of-range score or an unrecognized mode.
    ``reviewer_name`` is denormalized at write time (A-3) so the review stays
    attributable after the reviewer's account is deleted.

    ``recorded_by`` is the real admin when this write happened while
    impersonating (F4, 2026-09-10) — the review is still attributed to
    ``reviewer`` (the impersonated user), and ``recorded_by`` is the second
    signature on the row. ``None`` for an ordinary, non-impersonated write.
    """
    _validate(score, feedback_mode, dimension_scores)
    review = AssessmentReview(
        assessment_id=assessment.id,
        reviewer_user_id=reviewer.id,
        reviewer_name=reviewer.name,
        score=score,
        comment=comment[:_MAX_COMMENT_CHARS],
        feedback_mode=feedback_mode,
        dimension_scores=_normalized_dimension_scores(dimension_scores),
        # Stamped from the module-level constants, not re-read from disk, and
        # unconditionally — whether or not any dimension was scored.
        # `_validate` only calls `load_rubric()` when `dimension_scores` is
        # truthy (it short-circuits on an empty/None map before ever reading
        # the document), but these constants are derived from that same
        # document at import time and cannot change under a running process,
        # so the row is always stamped with the live document either way.
        rubric_version=RUBRIC_VERSION,
        rubric_content_hash=RUBRIC_CONTENT_HASH,
        recorded_by_user_id=recorded_by.id if recorded_by else None,
    )
    db.add(review)
    await db.flush()
    # No enqueue here, deliberately (F2, 2026-09-14): 'learn' feedback makes
    # the assessment ELIGIBLE for analysis, and a human presses the button
    # that pays for it. See the module docstring.
    return review


async def edit_feedback(
    db: AsyncSession,
    *,
    review: AssessmentReview,
    score: int,
    comment: str,
    feedback_mode: str,
    dimension_scores: dict[str, int] | None = None,
    recorded_by: User | None = None,
) -> AssessmentReview:
    """Mutate an existing review in place. Caller commits.

    Author-only is the router's check, not this function's. Resets
    ``consumed_at`` to ``None`` so an edited row becomes eligible again even
    if the original had already been consumed — eligible for the next MANUAL
    generate pass (F2, 2026-09-14): this function no longer enqueues a
    replacement job, and nothing else does on its behalf.

    ``recorded_by`` is the real admin when this edit happened while
    impersonating (F4, 2026-09-10) — same semantics as ``submit_feedback``,
    except it is only ever SET here, never cleared: it records the most
    recent impersonated writer; never cleared by an in-person edit.

    ``dimension_scores`` REPLACES the stored set rather than merging into it —
    the form re-posts every dimension, so a field the reviewer cleared must
    actually clear. The rubric stamp is rewritten unconditionally for the
    same reason — whether or not any dimension was scored THIS time, and
    whether or not the incoming set differs from what was there before: the
    stamp records which document is live at edit time, which is not
    necessarily the one that scored (or stamped) the original.

    A second consequence of that same replace-and-restamp behavior: editing a
    review that was originally scored under an OLDER rubric revision silently
    DROPS whatever dimension keys that older revision used and are no longer
    live — the edit form only ever renders the CURRENT document's dimensions,
    so there is nothing in the submitted payload to carry a retired key
    forward, and the row's stamp moves to the live revision regardless. This
    is defensible, not just tolerated: a retired dimension key would fail
    ``_validate`` against the live rubric anyway, and re-stamping keeps the
    row self-consistent (its ``dimension_scores`` keys always match its own
    ``rubric_version``) rather than leaving a row that claims one revision but
    carries another revision's keys.
    """
    _validate(score, feedback_mode, dimension_scores)
    review.score = score
    review.comment = comment[:_MAX_COMMENT_CHARS]
    review.feedback_mode = feedback_mode
    review.dimension_scores = _normalized_dimension_scores(dimension_scores)
    review.rubric_version = RUBRIC_VERSION
    review.rubric_content_hash = RUBRIC_CONTENT_HASH
    review.edited = True
    review.consumed_at = None
    if recorded_by is not None:
        review.recorded_by_user_id = recorded_by.id
    # No enqueue here either (F2, 2026-09-14) — see the module docstring.
    return review


async def record_status_event(
    db: AsyncSession,
    *,
    assessment: OpportunityAssessment,
    actor: User,
    action: str,
    recorded_by: User | None = None,
) -> AssessmentReviewEvent:
    """Append one ``AssessmentReviewEvent`` row. Caller commits.

    Raises ``ValueError`` on an action outside ``VALID_STATUS_ACTIONS``.
    Append-only: there is no update path, by design — the point of this
    table is the history, not the current status. ``actor_name`` is
    denormalized at write time (A-3), same as ``reviewer_name`` above, so
    the event stays attributable after the actor's account is deleted.

    ``recorded_by`` is the real admin when this write happened while
    impersonating (F4, 2026-09-10) — same semantics as ``submit_feedback``.
    """
    if action not in VALID_STATUS_ACTIONS:
        raise ValueError(f"action must be one of {VALID_STATUS_ACTIONS}")
    event = AssessmentReviewEvent(
        assessment_id=assessment.id,
        action=action,
        actor_user_id=actor.id,
        actor_name=actor.name,
        recorded_by_user_id=recorded_by.id if recorded_by else None,
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

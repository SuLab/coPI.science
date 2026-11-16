"""Landscaping review writes: quick decisions, full reviews, status events, assignments and
expert judgments (spec §13.3, §10.6; L39, L40, L55, D21, D50, D63).

These mirror ``assessment_reviews`` (``submit_feedback``, ``edit_feedback``,
``record_status_event``, ``assign_reviewer``, ``unassign_reviewer``) but are keyed on the stable
opportunity, not one assessment version. Each decision, review and status event records the
``landscape_assessment_id`` shown when it was made, so a later version can be flagged "changed
since your decision" (D50).

Recorded, never learned from (L33, D21): there is no ``feedback_mode`` and no ``consumed_at``,
nothing here enqueues a job, and nothing writes ``prompt_change_suggestions``.

Who may write is the router's check (``src/routers/reviews.py``): ``can_view``, author-only edits,
staff assignment, admin delete. Every function here leaves committing to the caller.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import epochs
from src.landscaping.criteria import load_criteria
from src.landscaping.review_batches import EXPERTS_KEY
from src.models import AppSetting, User
from src.models.landscape import (
    DECISIONS,
    REVIEW_ACTIONS,
    LandscapeAssessment,
    LandscapeDecision,
    LandscapeExpertJudgment,
    LandscapeReview,
    LandscapeReviewAssignment,
    LandscapeReviewEvent,
)
from src.services.assessment_reviews import _validate, _validate_comment
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION, load_rubric

logger = logging.getLogger(__name__)

#: Longest quick-decision reason, in characters (spec §13.3); the column's CHECK holds the same.
MAX_REASON_CHARS = 1_000

#: Landscaping has no feedback mode (D21). This value only satisfies ``_validate``'s membership
#: check, which the dimension-score validation is reused through; nothing stores it.
_NO_FEEDBACK_MODE = "log_only"

#: An expert's gate call per rubric gate, the tri-state S6 and S9 use.
EXPERT_GATE_CALLS = ("met", "not_met", "unconfirmed")
#: An expert's call on each criteria-file deal-breaker, as S6 records them (L24).
EXPERT_DEAL_BREAKER_CALLS = ("confirmed", "cleared")
#: The S9 contract's recommendation vocabulary (``src/landscaping/schemas.py``).
EXPERT_RECOMMENDATIONS = ("advance", "conditional", "pass", "route-to-incubation")


class DuplicateLandscapeDecisionError(Exception):
    """This reviewer already has a quick decision on this opportunity; nothing was written."""


class DuplicateLandscapeReviewError(Exception):
    """This reviewer already has a full review of this opportunity; nothing was written."""


class NotInExpertSample(Exception):
    """The user has no expert-sample item for this opportunity; nothing was written."""


def _lf(text: str) -> str:
    """``text`` with CRLF line breaks as LF. Browsers count ``maxlength`` on the LF form but
    submit CRLF; storing the LF form keeps the stored length the one that was validated, so the
    columns' ``char_length`` CHECKs cannot refuse what validation passed."""
    return text.replace("\r\n", "\n")


def _validate_reason(reason: str) -> str:
    reason = _lf(reason)
    if len(reason) > MAX_REASON_CHARS:
        raise ValueError(f"reason is longer than {MAX_REASON_CHARS} characters")
    if "\x00" in reason:
        raise ValueError("reason contains a NUL character")
    return reason


def _validate_decision(
    decision: str, reason_tags: list[str], reason: str
) -> tuple[list | None, str]:
    """ValueError unless ``decision`` is pursue or pass, every tag is one of the criteria file's
    ``reason_tags`` (L49) and the reason fits; returns ``(tags or None, reason)``."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    allowed = set(load_criteria().reason_tags)
    for tag in reason_tags:
        if tag not in allowed:
            raise ValueError(f"unknown reason tag: {tag}")
    return list(dict.fromkeys(reason_tags)) or None, _validate_reason(reason)


async def shown_assessment_id(
    db: AsyncSession, opportunity_id: uuid.UUID, posted: uuid.UUID | None = None
) -> uuid.UUID | None:
    """The assessment a write is recorded against (D50).

    ``posted`` (the id the form carried, i.e. the assessment the page showed) when it is an
    assessment of this opportunity; otherwise the opportunity's current completed assessment in
    the active epoch, or None when it has none (spec §7.10, X1).
    """
    if posted is not None:
        owner = await db.scalar(
            select(LandscapeAssessment.opportunity_id).where(LandscapeAssessment.id == posted)
        )
        if owner == opportunity_id:
            return posted
    active = await epochs.active_epoch(db)
    if active is None:
        return None
    return await db.scalar(
        select(LandscapeAssessment.id)
        .where(
            LandscapeAssessment.opportunity_id == opportunity_id,
            LandscapeAssessment.epoch_id == active.id,
            LandscapeAssessment.status == "done",
        )
        .order_by(LandscapeAssessment.opportunity_version.desc())
        .limit(1)
    )


async def submit_decision(
    db: AsyncSession,
    *,
    opportunity_id: uuid.UUID,
    reviewer: User,
    decision: str,
    reason_tags: list[str],
    reason: str,
    landscape_assessment_id: uuid.UUID | None,
    recorded_by: User | None = None,
) -> LandscapeDecision:
    """Create the reviewer's one quick decision on the opportunity (L39). Caller commits.

    ``ValueError`` for a decision other than pursue/pass, an unknown reason tag or a reason
    over ``MAX_REASON_CHARS``; ``DuplicateLandscapeDecisionError`` (nothing written) when the
    reviewer already decided, since there is one row per (opportunity, reviewer) and a change
    goes through ``edit_decision``. ``recorded_by`` is the real admin of an impersonated write.
    """
    tags, reason = _validate_decision(decision, reason_tags, reason)
    # ON CONFLICT DO NOTHING on the unique pair, so two racing submits store one row.
    new_id = await db.scalar(
        pg_insert(LandscapeDecision)
        .values(
            opportunity_id=opportunity_id,
            reviewer_id=reviewer.id,
            reviewer_name=reviewer.name,
            decision=decision,
            reason_tags=tags,
            reason=reason,
            landscape_assessment_id=landscape_assessment_id,
            recorded_by_user_id=recorded_by.id if recorded_by else None,
        )
        .on_conflict_do_nothing(constraint="uq_landscape_decisions_opportunity_reviewer")
        .returning(LandscapeDecision.id)
    )
    if new_id is None:
        raise DuplicateLandscapeDecisionError
    return await db.get(LandscapeDecision, new_id)


async def edit_decision(
    db: AsyncSession,
    *,
    row: LandscapeDecision,
    decision: str,
    reason_tags: list[str],
    reason: str,
    landscape_assessment_id: uuid.UUID | None,
    recorded_by: User | None = None,
) -> LandscapeDecision:
    """Change a quick decision in place. Caller commits; author-only is the router's check.

    Re-records ``landscape_assessment_id``: confirming or changing a decision on a newer
    assessment is what clears "changed since your decision" (L60, D50). ``recorded_by`` is only
    ever set here, never cleared, as in ``assessment_reviews.edit_feedback``. ``ValueError`` as
    ``submit_decision``; the row is then unchanged.
    """
    tags, reason = _validate_decision(decision, reason_tags, reason)
    row.decision = decision
    row.reason_tags = tags
    row.reason = reason
    row.landscape_assessment_id = landscape_assessment_id
    row.edited = True
    row.updated_at = datetime.now(UTC)
    if recorded_by is not None:
        row.recorded_by_user_id = recorded_by.id
    await db.flush()
    return row


async def submit_review(
    db: AsyncSession,
    *,
    opportunity_id: uuid.UUID,
    reviewer: User,
    score: int,
    comment: str,
    dimension_scores: dict[str, int] | None,
    landscape_assessment_id: uuid.UUID | None,
    recorded_by: User | None = None,
) -> LandscapeReview:
    """Create the reviewer's one full review of the opportunity (spec §13.3). Caller commits.

    The score, comment and dimension scores are validated as assessment reviews are, the
    dimensions against the live rubric, whose version and hash are stamped on the row.
    ``ValueError`` on a refused value; ``DuplicateLandscapeReviewError`` (nothing written) when
    the reviewer already reviewed it.
    """
    _validate(score, _NO_FEEDBACK_MODE, dimension_scores)
    _validate_comment(comment)
    new_id = await db.scalar(
        pg_insert(LandscapeReview)
        .values(
            opportunity_id=opportunity_id,
            reviewer_id=reviewer.id,
            reviewer_name=reviewer.name,
            score=score,
            comment=_lf(comment),
            dimension_scores=dimension_scores or None,
            rubric_version=RUBRIC_VERSION,
            rubric_content_hash=RUBRIC_CONTENT_HASH,
            landscape_assessment_id=landscape_assessment_id,
            recorded_by_user_id=recorded_by.id if recorded_by else None,
        )
        .on_conflict_do_nothing(constraint="uq_landscape_reviews_opportunity_reviewer")
        .returning(LandscapeReview.id)
    )
    if new_id is None:
        raise DuplicateLandscapeReviewError
    return await db.get(LandscapeReview, new_id)


async def edit_review(
    db: AsyncSession,
    *,
    review: LandscapeReview,
    score: int,
    comment: str,
    dimension_scores: dict[str, int] | None,
    landscape_assessment_id: uuid.UUID | None,
    recorded_by: User | None = None,
) -> LandscapeReview:
    """Change a full review in place. Caller commits; author-only is the router's check.

    ``dimension_scores`` replaces the stored set and the rubric stamp is rewritten, as in
    ``assessment_reviews.edit_feedback``. ``ValueError`` on a refused value; the review is then
    unchanged.
    """
    _validate(score, _NO_FEEDBACK_MODE, dimension_scores)
    _validate_comment(comment)
    review.score = score
    review.comment = _lf(comment)
    review.dimension_scores = dimension_scores or None
    review.rubric_version = RUBRIC_VERSION
    review.rubric_content_hash = RUBRIC_CONTENT_HASH
    review.landscape_assessment_id = landscape_assessment_id
    review.edited = True
    review.updated_at = datetime.now(UTC)
    if recorded_by is not None:
        review.recorded_by_user_id = recorded_by.id
    await db.flush()
    return review


async def record_status_event(
    db: AsyncSession,
    *,
    opportunity_id: uuid.UUID,
    actor: User,
    action: str,
    landscape_assessment_id: uuid.UUID | None,
    recorded_by: User | None = None,
) -> LandscapeReviewEvent:
    """Append one approve, disapprove or clear (append-only). Caller commits; ``ValueError`` for
    any other action."""
    if action not in REVIEW_ACTIONS:
        raise ValueError(f"action must be one of {REVIEW_ACTIONS}")
    event = LandscapeReviewEvent(
        opportunity_id=opportunity_id,
        landscape_assessment_id=landscape_assessment_id,
        action=action,
        actor_id=actor.id,
        actor_name=actor.name,
        recorded_by_user_id=recorded_by.id if recorded_by else None,
    )
    db.add(event)
    await db.flush()
    return event


async def assign_reviewer(
    db: AsyncSession, *, opportunity_id: uuid.UUID, assignee: User, assigned_by: User
) -> None:
    """Idempotently assign ``assignee`` to the opportunity; reassigning is a no-op. Caller
    commits and has validated the assignee (``reviews._load_assignee``)."""
    await db.execute(
        pg_insert(LandscapeReviewAssignment)
        .values(
            opportunity_id=opportunity_id,
            assignee_id=assignee.id,
            assignee_name=assignee.name,
            assigned_by_user_id=assigned_by.id,
            assigned_by_name=assigned_by.name,
        )
        .on_conflict_do_nothing(constraint="uq_landscape_review_assignments_opportunity_assignee")
    )


async def unassign_reviewer(
    db: AsyncSession, *, opportunity_id: uuid.UUID, assignee_user_id: uuid.UUID
) -> None:
    """Remove the (opportunity, assignee) assignment if there is one. Caller commits."""
    await db.execute(
        delete(LandscapeReviewAssignment).where(
            LandscapeReviewAssignment.opportunity_id == opportunity_id,
            LandscapeReviewAssignment.assignee_id == assignee_user_id,
        )
    )


async def is_marked_expert(db: AsyncSession, user: User) -> bool:
    """Whether ``user``'s id is in ``app_settings.landscape_experts`` (L55). An unreadable
    setting marks nobody."""
    raw = await db.scalar(select(AppSetting.value).where(AppSetting.key == EXPERTS_KEY))
    try:
        values = json.loads(raw) if raw else []
    except ValueError:
        logger.warning("%s is not JSON; nobody is a marked expert", EXPERTS_KEY)
        return False
    return isinstance(values, list) and str(user.id) in {str(v) for v in values}


def _validate_calls(
    calls: dict[str, str], keys, values: tuple[str, ...], what: str
) -> dict | None:
    known = set(keys)
    for key, call in calls.items():
        if key not in known:
            raise ValueError(f"unknown {what}: {key}")
        if call not in values:
            raise ValueError(f"{what} call for {key} must be one of {values}")
    return dict(calls) or None


def _validate_expert_scores(scores: dict[str, int]) -> None:
    """Dimension scores against the live rubric, through ``assessment_reviews._validate``. An
    expert gives no overall score, so the 1 passed for it is only a value in range."""
    _validate(1, _NO_FEEDBACK_MODE, scores)


async def submit_expert_judgment(
    db: AsyncSession,
    *,
    opportunity_id: uuid.UUID,
    expert: User,
    gates: dict[str, str],
    scores: dict[str, int],
    deal_breakers: dict[str, str],
    recommendation: str | None,
    comment: str,
) -> LandscapeExpertJudgment:
    """Store an expert's own calls on one of their sample items and set ``submitted_at``, which
    lifts the hiding of S9's and S6's calls on it (L55, D63). Caller commits.

    The item is the expert's newest ``LandscapeExpertJudgment`` row for this opportunity,
    whatever its quarter: the row whose being unsubmitted makes ``landscape_view`` hide the
    calls, in the same order (drawn, then quarter, then id).
    ``NotInExpertSample`` when there is none (the item is outside their own sample);
    ``ValueError`` when it is already submitted (the comparison needs the calls made before S9's
    were visible), or for a gate outside the live rubric, a deal-breaker outside the criteria
    file, a call or recommendation outside its vocabulary, a refused dimension score or comment.
    """
    row = await db.scalar(
        select(LandscapeExpertJudgment)
        .where(
            LandscapeExpertJudgment.opportunity_id == opportunity_id,
            LandscapeExpertJudgment.expert_id == expert.id,
        )
        .order_by(
            LandscapeExpertJudgment.drawn_at.desc(), LandscapeExpertJudgment.quarter.desc(),
            LandscapeExpertJudgment.id.desc(),
        )
        .limit(1)
        .with_for_update()
    )
    if row is None:
        raise NotInExpertSample
    if row.submitted_at is not None:
        raise ValueError("this expert judgment was already submitted")
    gate_calls = _validate_calls(gates, load_rubric().gating, EXPERT_GATE_CALLS, "gate")
    flag_calls = _validate_calls(
        deal_breakers,
        (d.key for d in load_criteria().deal_breakers),
        EXPERT_DEAL_BREAKER_CALLS,
        "deal-breaker",
    )
    _validate_expert_scores(scores)
    if recommendation is not None and recommendation not in EXPERT_RECOMMENDATIONS:
        raise ValueError(f"recommendation must be one of {EXPERT_RECOMMENDATIONS}")
    _validate_comment(comment)
    row.gates = gate_calls
    row.scores = scores or None
    row.deal_breakers = flag_calls
    row.recommendation = recommendation
    row.comment = _lf(comment) or None
    row.submitted_at = row.updated_at = datetime.now(UTC)
    await db.flush()
    return row

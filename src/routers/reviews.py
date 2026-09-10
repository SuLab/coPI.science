"""All review writes. Router-level gate = get_review_user; handlers that are
staff-only or admin-only declare a NARROWER singleton, which is the real gate
for them. Feedback/status writes now ALLOW an impersonated session (F4,
2026-09-10) and attribute the write to the impersonated user, with the real
admin recorded via ``recorded_by``; assign/unassign and suggestion-status
still refuse impersonation outright.

Dependencies are module-level singletons (``_DB``/``_REVIEW``/``_STAFF``/
``_ADMIN``) rather than inline ``Depends(...)`` calls in argument defaults,
same reason as ``src/routers/manager.py``: ruff's B008 flags the latter, and
this router would otherwise chip away at a lint ceiling later tasks still
need headroom under.
"""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from src.database import get_db
from src.dependencies import get_admin_user, get_review_user, get_staff_user
from src.models import AssessmentReview, OpportunityAssessment, PromptChangeSuggestion, User
from src.services.assessment_reviews import (
    assign_reviewer,
    edit_feedback,
    record_status_event,
    submit_feedback,
    unassign_reviewer,
)

#: Mirrors PromptChangeSuggestion.status's docstring (src/models/review.py).
#: Kept local rather than shared with src/routers/manager.py's copy — see
#: that module's comment for why.
_SUGGESTION_STATUSES = frozenset({"open", "dismissed", "implemented"})

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_review_user)])

_DB = Depends(get_db)
_REVIEW = Depends(get_review_user)
_STAFF = Depends(get_staff_user)
_ADMIN = Depends(get_admin_user)


def _refuse_impersonation(current_user: User) -> None:
    if getattr(current_user, "_is_impersonated", False):
        raise HTTPException(
            status_code=403, detail="Review actions are disabled while impersonating."
        )


def _recorded_by(current_user: User) -> User | None:
    """The real admin when the session is impersonating, else None. Review
    writes are attributed to the impersonated user (operator decision
    2026-09-10); this is the second signature on the row."""
    if getattr(current_user, "_is_impersonated", False):
        real = getattr(current_user, "_real_admin", None)
        logger.warning(
            "Review action by admin %s while impersonating %s",
            getattr(real, "id", None), current_user.id,
        )
        return real
    return None


def _assessments_redirect(
    surface: str, current_user: User, assessment_id: uuid.UUID
) -> RedirectResponse:
    """Whitelist lives HERE, not at call sites; admin surface only for
    admins. NEVER build this from a bare "/admin" constant: test_reachability's
    src_strings scan would mark the allowlisted GET /admin entry stale."""
    if surface == "admin" and current_user.is_admin:
        return RedirectResponse(url=f"/admin/assessments/{assessment_id}", status_code=302)
    return RedirectResponse(url=f"/manager/assessments/{assessment_id}", status_code=302)


async def _load_review(db: AsyncSession, feedback_id: uuid.UUID) -> AssessmentReview:
    review = (
        await db.execute(select(AssessmentReview).where(AssessmentReview.id == feedback_id))
    ).scalar_one_or_none()
    if review is None:
        raise HTTPException(status_code=404, detail="Feedback not found")
    return review


async def _load_assessment(
    db: AsyncSession, assessment_id: uuid.UUID
) -> OpportunityAssessment:
    assessment = (
        await db.execute(
            select(OpportunityAssessment).where(OpportunityAssessment.id == assessment_id)
        )
    ).scalar_one_or_none()
    if assessment is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return assessment


async def _load_assignee(db: AsyncSession, assignee_user_id: uuid.UUID) -> User:
    """Load and validate a would-be assignee. 400, never a bare lookup
    failure: an unknown id, a PI, or a non-'allowed' account are all request
    errors, not server errors. Mirrors the last-admin guard's allowed-only
    counting rationale (``admin.py:262-265``): a denied/pending account is
    not actually reachable to do the review, regardless of its role.
    """
    assignee = (
        await db.execute(select(User).where(User.id == assignee_user_id))
    ).scalar_one_or_none()
    if assignee is None:
        raise HTTPException(status_code=400, detail="unknown user")
    if not (
        (assignee.is_staff or assignee.is_reviewer) and assignee.access_status == "allowed"
    ):
        raise HTTPException(
            status_code=400,
            detail="assignee must be a staff member or reviewer with allowed access",
        )
    return assignee


def _parse_assignee_id(assignee_user_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(assignee_user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Malformed assignee id") from exc


#: Prefix for the per-rubric-dimension score fields the Human-review card
#: posts. One field per dimension, named from the rubric document's own key
#: (`dim_scientific_credibility`), so the form and the validator cannot drift.
_DIM_FIELD_PREFIX = "dim_"


def _parse_dimension_scores(form: FormData) -> dict[str, int]:
    """Pull the `dim_<key>` fields out of a posted form.

    An empty value means "not scored" and is DROPPED, never coerced to 0: the
    rubric scale starts at 1, so a stored 0 is a score nobody gave. A
    non-integer is a 400 rather than a silent skip — recording a review that
    quietly omits what the reviewer typed is worse than refusing it. Unknown
    KEYS are not checked here; `assessment_reviews._validate` owns that, against
    the live document.

    A duplicate `dim_<key>` field (the same name posted twice) quietly keeps
    the LAST value seen, matching plain HTML form semantics — the real
    `<select>`-per-dimension form can never produce one, so this is a
    considered default rather than an oversight.
    """
    scores: dict[str, int] = {}
    for field, raw in form.multi_items():
        if not field.startswith(_DIM_FIELD_PREFIX):
            continue
        # A same-named field can arrive as an UploadFile in a crafted
        # multipart/form-data POST rather than a str. A file part is not a
        # score, so treat it the same as any other malformed value: 400,
        # not an unhandled AttributeError from calling .strip() on it.
        if not isinstance(raw, str):
            raise HTTPException(
                status_code=400, detail=f"Malformed dimension score for {field}"
            )
        value = raw.strip()
        if not value:
            continue
        try:
            scores[field[len(_DIM_FIELD_PREFIX) :]] = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail=f"Malformed dimension score for {field}"
            ) from exc
    return scores


@router.post("/assessments/{assessment_id}/feedback")
async def submit_review_feedback(
    assessment_id: uuid.UUID,
    request: Request,
    score: int = Form(...),
    comment: str = Form(""),
    feedback_mode: str = Form(...),
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    assessment = await _load_assessment(db, assessment_id)
    dimension_scores = _parse_dimension_scores(await request.form())
    try:
        await submit_feedback(
            db,
            assessment=assessment,
            reviewer=current_user,
            score=score,
            comment=comment,
            feedback_mode=feedback_mode,
            dimension_scores=dimension_scores,
            recorded_by=_recorded_by(current_user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "Review feedback by %s (%s) on assessment %s: score=%s mode=%s dims=%d",
        current_user.name, current_user.id, assessment_id, score, feedback_mode,
        len(dimension_scores),
    )
    return _assessments_redirect(surface, current_user, assessment_id)


@router.post("/feedback/{feedback_id}/edit")
async def edit_review_feedback(
    feedback_id: uuid.UUID,
    request: Request,
    score: int = Form(...),
    comment: str = Form(""),
    feedback_mode: str = Form(...),
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    review = await _load_review(db, feedback_id)
    if review.reviewer_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the author may edit this feedback")
    # The form re-posts every dimension on each edit, so parsing unconditionally
    # and always passing the result through (even `{}`) is deliberate: a
    # dimension the reviewer cleared must actually clear on the row, not be
    # left at its previous value. See edit_feedback's own docstring (Task 3).
    dimension_scores = _parse_dimension_scores(await request.form())
    try:
        await edit_feedback(
            db,
            review=review,
            score=score,
            comment=comment,
            feedback_mode=feedback_mode,
            dimension_scores=dimension_scores,
            recorded_by=_recorded_by(current_user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "Review feedback %s edited by %s (%s): score=%s mode=%s dims=%d",
        feedback_id, current_user.name, current_user.id, score, feedback_mode,
        len(dimension_scores),
    )
    return _assessments_redirect(surface, current_user, review.assessment_id)


@router.post("/feedback/{feedback_id}/delete")
async def delete_review_feedback(
    feedback_id: uuid.UUID,
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    review = await _load_review(db, feedback_id)
    assessment_id = review.assessment_id
    recorder = _recorded_by(current_user)
    await db.delete(review)
    await db.commit()
    logger.info(
        "Review feedback %s deleted by %s (%s) recorded_by=%s",
        feedback_id, current_user.name, current_user.id, getattr(recorder, "id", None),
    )
    return _assessments_redirect(surface, current_user, assessment_id)


@router.post("/assessments/{assessment_id}/status")
async def set_review_status(
    assessment_id: uuid.UUID,
    action: str = Form(...),
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    assessment = await _load_assessment(db, assessment_id)
    try:
        await record_status_event(
            db, assessment=assessment, actor=current_user, action=action,
            recorded_by=_recorded_by(current_user),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "Review status %s recorded by %s (%s) on assessment %s",
        action, current_user.name, current_user.id, assessment_id,
    )
    return _assessments_redirect(surface, current_user, assessment_id)


@router.post("/assessments/{assessment_id}/assign")
async def assign_review(
    assessment_id: uuid.UUID,
    assignee_user_id: str = Form(...),
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    _refuse_impersonation(current_user)
    assessment = await _load_assessment(db, assessment_id)
    assignee_id = _parse_assignee_id(assignee_user_id)
    assignee = await _load_assignee(db, assignee_id)
    await assign_reviewer(db, assessment=assessment, assignee=assignee, assigned_by=current_user)
    await db.commit()
    logger.info(
        "Assessment %s assigned to %s (%s) by %s (%s)",
        assessment_id, assignee.name, assignee.id, current_user.name, current_user.id,
    )
    return _assessments_redirect(surface, current_user, assessment_id)


@router.post("/assessments/{assessment_id}/unassign")
async def unassign_review(
    assessment_id: uuid.UUID,
    assignee_user_id: str = Form(...),
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    _refuse_impersonation(current_user)
    assessment = await _load_assessment(db, assessment_id)
    assignee_id = _parse_assignee_id(assignee_user_id)
    await unassign_reviewer(db, assessment=assessment, assignee_user_id=assignee_id)
    await db.commit()
    logger.info(
        "Assessment %s unassigned from user %s by %s (%s)",
        assessment_id, assignee_id, current_user.name, current_user.id,
    )
    return _assessments_redirect(surface, current_user, assessment_id)


async def _load_suggestion(
    db: AsyncSession, suggestion_id: uuid.UUID
) -> PromptChangeSuggestion:
    suggestion = (
        await db.execute(
            select(PromptChangeSuggestion).where(PromptChangeSuggestion.id == suggestion_id)
        )
    ).scalar_one_or_none()
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    return suggestion


@router.post("/suggestions/{suggestion_id}/status")
async def set_suggestion_status(
    suggestion_id: uuid.UUID,
    action: str = Form(...),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Task 12: staff-set attribution only — never auto-applied, never touches
    the prompt files themselves. Redirects to the full literal detail path,
    never a bare-prefix constant (the same discipline
    ``_assessments_redirect`` documents), because there is no admin/manager
    surface split to whitelist here: this page lives on /manager only."""
    _refuse_impersonation(current_user)
    if action not in _SUGGESTION_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status action")
    suggestion = await _load_suggestion(db, suggestion_id)
    suggestion.status = action
    suggestion.status_set_by_user_id = current_user.id
    suggestion.status_set_by_name = current_user.name
    suggestion.status_set_at = datetime.now(UTC)
    await db.commit()
    logger.info(
        "Prompt suggestion %s status set to %s by %s (%s)",
        suggestion_id, action, current_user.name, current_user.id,
    )
    return RedirectResponse(
        url=f"/manager/prompt-suggestions/{suggestion_id}", status_code=302
    )

"""All review writes. Router-level gate = get_review_user; handlers that are
staff-only or admin-only declare a NARROWER singleton, which is the real gate
for them. Every handler refuses impersonated sessions.

Dependencies are module-level singletons (``_DB``/``_REVIEW``/``_STAFF``/
``_ADMIN``) rather than inline ``Depends(...)`` calls in argument defaults,
same reason as ``src/routers/manager.py``: ruff's B008 flags the latter, and
this router would otherwise chip away at a lint ceiling later tasks still
need headroom under.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_admin_user, get_review_user, get_staff_user
from src.models import AssessmentReview, OpportunityAssessment, User
from src.services.assessment_reviews import edit_feedback, submit_feedback

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


@router.post("/assessments/{assessment_id}/feedback")
async def submit_review_feedback(
    assessment_id: uuid.UUID,
    score: int = Form(...),
    comment: str = Form(""),
    feedback_mode: str = Form(...),
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    _refuse_impersonation(current_user)
    assessment = (
        await db.execute(
            select(OpportunityAssessment).where(OpportunityAssessment.id == assessment_id)
        )
    ).scalar_one_or_none()
    if assessment is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    try:
        await submit_feedback(
            db,
            assessment=assessment,
            reviewer=current_user,
            score=score,
            comment=comment,
            feedback_mode=feedback_mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "Review feedback by %s (%s) on assessment %s: score=%s mode=%s",
        current_user.name, current_user.id, assessment_id, score, feedback_mode,
    )
    return _assessments_redirect(surface, current_user, assessment_id)


@router.post("/feedback/{feedback_id}/edit")
async def edit_review_feedback(
    feedback_id: uuid.UUID,
    score: int = Form(...),
    comment: str = Form(""),
    feedback_mode: str = Form(...),
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    _refuse_impersonation(current_user)
    review = await _load_review(db, feedback_id)
    if review.reviewer_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the author may edit this feedback")
    try:
        await edit_feedback(
            db, review=review, score=score, comment=comment, feedback_mode=feedback_mode
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    logger.info(
        "Review feedback %s edited by %s (%s): score=%s mode=%s",
        feedback_id, current_user.name, current_user.id, score, feedback_mode,
    )
    return _assessments_redirect(surface, current_user, review.assessment_id)


@router.post("/feedback/{feedback_id}/delete")
async def delete_review_feedback(
    feedback_id: uuid.UUID,
    surface: str = Form("manager"),
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    _refuse_impersonation(current_user)
    review = await _load_review(db, feedback_id)
    assessment_id = review.assessment_id
    await db.delete(review)
    await db.commit()
    logger.info(
        "Review feedback %s deleted by %s (%s)", feedback_id, current_user.name, current_user.id,
    )
    return _assessments_redirect(surface, current_user, assessment_id)

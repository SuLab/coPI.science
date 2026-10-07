"""Role-neutral assessment pages; assignments filter workflow, not access."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_review_user
from src.models import User
from src.services.assessment_chat_suggestions import page_suggestions
from src.services.assessment_detail import build_assessment_detail
from src.services.directory import list_assessments
from src.web.page_context import workspace_context
from src.web.templating import make_templates

router = APIRouter()
templates = make_templates()
_DB = Depends(get_db)
_REVIEW = Depends(get_review_user)


@router.get("/assessments", response_class=HTMLResponse, name="workspace_assessments")
async def workspace_assessments(
    request: Request,
    run_id: str | None = None,
    sort: str | None = None,
    lab: str | None = None,
    review: str | None = None,
    assignment: str = "all",
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    assignment = "mine" if assignment == "mine" else "all"
    view = await list_assessments(
        db,
        run_id,
        sort=sort,
        lab=lab,
        review=review,
        assignee_user_id=current_user.id if assignment == "mine" else None,
    )
    return templates.TemplateResponse(
        request,
        "workspace/assessments.html",
        workspace_context(
            request, current_user, section="assessments", assignment=assignment, **view
        ),
    )


@router.get(
    "/assessments/{assessment_id}", response_class=HTMLResponse, name="workspace_assessment_detail"
)
async def workspace_assessment_detail(
    assessment_id: uuid.UUID,
    request: Request,
    run_id: str | None = None,
    sort: str | None = None,
    lab: str | None = None,
    review: str | None = None,
    assignment: str = "all",
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    detail = await build_assessment_detail(
        db, assessment_id, admin_view=current_user.is_admin, viewer_is_staff=current_user.is_staff
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    questions = await page_suggestions(
        db,
        detail,
        current_user,
        chat_enabled=getattr(request.app.state, "assessment_chat_enabled", False) is True,
    )
    return templates.TemplateResponse(
        request,
        "workspace/assessment_detail.html",
        workspace_context(
            request,
            current_user,
            section="assessments",
            chat_suggestions=questions,
            run_id=run_id,
            sort=sort,
            lab_filter=lab,
            review=review,
            assignment="mine" if assignment == "mine" else "all",
            **detail,
        ),
    )

"""The Projects list and details pages (admins, managers and reviewers).

Read-only: the review forms post to ``/reviews/...`` (``src/routers/reviews.py``), which
holds every review write.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_review_user
from src.models import User
from src.services.project_chat_record import project_page_suggestions
from src.services.projects_view import (
    build_project_detail,
    build_projects_list,
    project_list_state,
)
from src.web.identity import identity_for
from src.web.page_context import workspace_context
from src.web.templating import make_templates

router = APIRouter()
templates = make_templates()
_DB = Depends(get_db)
_REVIEW = Depends(get_review_user)


def _chat_enabled(request: Request) -> bool:
    """Both switches on: the assessment chat's and the project chat's (spec §8.3)."""
    state = request.app.state
    return (
        getattr(state, "assessment_chat_enabled", False) is True
        and getattr(state, "project_chat_enabled", False) is True
    )


@router.get("/projects", response_class=HTMLResponse, name="workspace_projects")
async def workspace_projects(
    request: Request,
    run_id: str | None = None,
    lab: str | None = None,
    outcome: str | None = None,
    review: str | None = None,
    sort: str | None = None,
    mine: str | None = None,
    assignment: str | None = None,
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    impersonating = identity_for(current_user).is_impersonating
    view = await build_projects_list(
        db,
        viewer=current_user,
        impersonating=impersonating,
        run_id=run_id,
        lab=lab,
        outcome=outcome,
        review=review,
        sort=sort,
        mine=mine == "1",
        assignment=assignment,
    )
    return templates.TemplateResponse(
        request,
        "workspace/projects.html",
        workspace_context(
            request,
            current_user,
            section="projects",
            card_chat_available=_chat_enabled(request) and not impersonating,
            **view,
        ),
    )


@router.get(
    "/projects/{project_id}", response_class=HTMLResponse, name="workspace_project_detail"
)
async def workspace_project_detail(
    request: Request,
    project_id: uuid.UUID,
    run_id: str | None = None,
    lab: str | None = None,
    outcome: str | None = None,
    review: str | None = None,
    sort: str | None = None,
    mine: str | None = None,
    assignment: str | None = None,
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    """The details page; the list's state rides along for the back link and the forms."""
    identity = identity_for(current_user)
    view = await build_project_detail(
        db,
        project_id,
        viewer=current_user,
        is_admin=identity.permissions.admin,
        impersonating=identity.is_impersonating,
    )
    if view is None:
        raise HTTPException(status_code=404, detail="Project not found")
    list_state = project_list_state(
        run_id=run_id,
        lab=lab,
        outcome=outcome,
        review=review,
        sort=sort,
        mine=mine,
        assignment=assignment,
    )
    chat_enabled = _chat_enabled(request)
    # Open projects get no chat: their record would change under the conversation (§8.1).
    chat_available = (
        chat_enabled and not identity.is_impersonating and view["project"]["is_finished"]
    )
    return templates.TemplateResponse(
        request,
        "workspace/project_detail.html",
        workspace_context(
            request,
            current_user,
            section="projects",
            list_state=list_state,
            chat_enabled=chat_enabled,
            chat_available=chat_available,
            chat_suggestions=project_page_suggestions(
                view, current_user, chat_enabled=chat_available
            ),
            **view,
        ),
    )

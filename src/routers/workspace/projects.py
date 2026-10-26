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
from src.services.projects_view import build_project_detail, build_projects_list
from src.web.identity import identity_for
from src.web.page_context import workspace_context
from src.web.templating import make_templates

router = APIRouter()
templates = make_templates()
_DB = Depends(get_db)
_REVIEW = Depends(get_review_user)


@router.get("/projects", response_class=HTMLResponse, name="workspace_projects")
async def workspace_projects(
    request: Request,
    run_id: str | None = None,
    lab: str | None = None,
    outcome: str | None = None,
    mine: str | None = None,
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    view = await build_projects_list(
        db,
        viewer=current_user,
        impersonating=identity_for(current_user).is_impersonating,
        run_id=run_id,
        lab=lab,
        outcome=outcome,
        mine=mine == "1",
    )
    return templates.TemplateResponse(
        request,
        "workspace/projects.html",
        workspace_context(request, current_user, section="projects", **view),
    )


@router.get(
    "/projects/{project_id}", response_class=HTMLResponse, name="workspace_project_detail"
)
async def workspace_project_detail(
    request: Request,
    project_id: uuid.UUID,
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
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
    return templates.TemplateResponse(
        request,
        "workspace/project_detail.html",
        workspace_context(request, current_user, section="projects", **view),
    )

"""Shared staff activity pages with effective-admin diagnostics."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_staff_user
from src.models import User
from src.services.directory import MAX_PAGE, build_run_detail, list_runs_overview
from src.services.headline_claims import held_headline_counts, list_in_doubt
from src.web.page_context import workspace_context
from src.web.templating import make_templates

router = APIRouter()
templates = make_templates()
_DB = Depends(get_db)
_STAFF = Depends(get_staff_user)
_PAGE = Query(1, ge=1, le=MAX_PAGE)


@router.get("/activity", response_class=HTMLResponse, name="workspace_activity")
async def workspace_activity(request: Request, db: AsyncSession = _DB, current_user: User = _STAFF):
    overview = await list_runs_overview(db)
    return templates.TemplateResponse(
        request,
        "workspace/activity.html",
        workspace_context(request, current_user, section="activity", **overview),
    )


@router.get("/activity/{run_id}", response_class=HTMLResponse, name="workspace_activity_detail")
async def workspace_activity_detail(
    run_id: uuid.UUID,
    request: Request,
    page: int = _PAGE,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    detail = await build_run_detail(db, run_id, page=page)
    if detail is None:
        raise HTTPException(status_code=404, detail="Run not found")
    held = None
    doubtful = []
    if current_user.is_admin:
        held = await held_headline_counts(db, run_id) if detail["run"].status == "stopped" else None
        doubtful = await list_in_doubt(db, run_id)
    return templates.TemplateResponse(
        request,
        "workspace/activity_detail.html",
        workspace_context(
            request, current_user, section="activity", held_counts=held, in_doubt=doubtful, **detail
        ),
    )

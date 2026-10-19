"""Staff interview trajectories: one line per post of each interview."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_staff_user
from src.models import User
from src.services.interview_trajectories import build_interviews_view
from src.web.page_context import workspace_context
from src.web.templating import make_templates

router = APIRouter()
templates = make_templates()
_DB = Depends(get_db)
_STAFF = Depends(get_staff_user)


@router.get("/interviews", response_class=HTMLResponse, name="workspace_interviews")
async def workspace_interviews(
    request: Request,
    run_id: str | None = None,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    view = await build_interviews_view(db, run_id)
    return templates.TemplateResponse(
        request,
        "workspace/interviews.html",
        workspace_context(request, current_user, section="interviews", **view),
    )

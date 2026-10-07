"""Admin discussions view and export."""

from fastapi import Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import User
from src.routers.admin._common import _ADMIN, _DB, router
from src.routers.workspace.compatibility import redirect
from src.routers.workspace.discussions import workspace_discussions
from src.services.directory import MAX_PAGE

_PAGE = Query(1, ge=1, le=MAX_PAGE)


_AGENT_FILTER = Query(default=[])

@router.get("/discussions", response_class=HTMLResponse, name="legacy_admin_discussions")
async def admin_discussions(request: Request, run_id: str | None = None, channel_filter: str | None = None,
                            status_filter: str | None = None, agent_filter: list[str] = _AGENT_FILTER,
                            export: str = "", page: int = _PAGE,
                            db: AsyncSession = _DB, current_user: User = _ADMIN):
    if export:
        return await workspace_discussions(request, run_id=run_id, channel_filter=channel_filter,
            status_filter=status_filter, agent_filter=agent_filter, export=export,
            page=page, db=db, current_user=current_user)
    return redirect(request, current_user, "discussions", "workspace_discussions")

"""Admin profile-job queue page."""

from fastapi import Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database import get_db
from src.dependencies import get_admin_user
from src.models import Job, User
from src.routers.admin._common import _template_context, router, templates
from src.services.directory import JOBS_PAGE_SIZE, MAX_PAGE

_PAGE = Query(1, ge=1, le=MAX_PAGE)


@router.get("/jobs", response_class=HTMLResponse)
async def admin_jobs(
    request: Request,
    status_filter: str | None = None,
    type_filter: str | None = None,
    page: int = _PAGE,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Job queue overview."""
    query = select(Job).options(selectinload(Job.user)).order_by(Job.enqueued_at.desc())
    if status_filter:
        query = query.where(Job.status == status_filter)
    if type_filter:
        query = query.where(Job.type == type_filter)
    total = (
        await db.execute(select(func.count()).select_from(query.order_by(None).subquery()))
    ).scalar_one()
    jobs = (
        await db.execute(query.limit(JOBS_PAGE_SIZE).offset((page - 1) * JOBS_PAGE_SIZE))
    ).scalars().unique().all()

    # Summary counts: every job by status, whatever the filters
    counts = dict((await db.execute(select(Job.status, func.count()).group_by(Job.status))).all())

    return templates.TemplateResponse(
        request,
        "admin/jobs.html",
        _template_context(
            request,
            current_user,
            active_admin="jobs",
            jobs=jobs,
            counts=counts,
            status_filter=status_filter,
            type_filter=type_filter,
            total=total,
            page=page,
            page_count=max(1, -(-total // JOBS_PAGE_SIZE)),
        ),
    )

"""Admin access-request and allowlist routes."""

import logging
import uuid

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_admin_user
from src.models import AccessAllowlist, ResearcherProfile, User
from src.models.job import INTERACTIVE_PRIORITY
from src.routers.admin._common import _template_context, router, templates
from src.services.profile_jobs import enqueue_profile_job_if_absent

logger = logging.getLogger("src.routers.admin")


# ---------------------------------------------------------------------------
# Access requests + allowlist
# ---------------------------------------------------------------------------


@router.get("/access-requests", response_class=HTMLResponse)
async def admin_access_requests(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """List pending/allowed/denied users and manage the allowlist."""
    pending_result = await db.execute(
        select(User).where(User.access_status == "pending").order_by(User.created_at.desc())
    )
    pending = pending_result.scalars().all()

    denied_result = await db.execute(
        select(User).where(User.access_status == "denied").order_by(User.created_at.desc())
    )
    denied = denied_result.scalars().all()

    recent_allowed_result = await db.execute(
        select(User)
        .where(User.access_status == "allowed")
        .order_by(User.updated_at.desc())
        .limit(25)
    )
    recent_allowed = recent_allowed_result.scalars().all()

    allowlist_result = await db.execute(
        select(AccessAllowlist).order_by(AccessAllowlist.created_at.desc())
    )
    allowlist = allowlist_result.scalars().all()

    return templates.TemplateResponse(
        request,
        "admin/access_requests.html",
        _template_context(
            request,
            current_user,
            active_admin="access",
            pending=pending,
            denied=denied,
            recent_allowed=recent_allowed,
            allowlist=allowlist,
        ),
    )



@router.post("/access-requests/{user_id}/approve")
async def admin_approve_access(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Approve a pending user; enqueue profile job if needed."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.access_status = "allowed"

    profile_result = await db.execute(
        select(ResearcherProfile.id).where(ResearcherProfile.user_id == user.id)
    )
    if profile_result.scalar_one_or_none() is None:
        await enqueue_profile_job_if_absent(db, user, priority=INTERACTIVE_PRIORITY)

    await db.commit()
    logger.info("Admin %s approved access for user %s", current_user.name, user.id)
    return RedirectResponse(url="/admin/access-requests", status_code=302)



@router.post("/access-requests/{user_id}/deny")
async def admin_deny_access(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Deny a pending user."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.access_status = "denied"
    await db.commit()
    logger.info("Admin %s denied access for user %s", current_user.name, user.id)
    return RedirectResponse(url="/admin/access-requests", status_code=302)



@router.post("/access-allowlist/add")
async def admin_allowlist_add(
    request: Request,
    orcid: str = Form(...),
    note: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Add an ORCID to the allowlist."""
    orcid_clean = orcid.strip()
    if not orcid_clean:
        return RedirectResponse(url="/admin/access-requests", status_code=302)

    existing = await db.execute(
        select(AccessAllowlist).where(AccessAllowlist.orcid == orcid_clean)
    )
    if existing.scalar_one_or_none() is None:
        db.add(
            AccessAllowlist(
                orcid=orcid_clean,
                note=note.strip() or None,
                added_by_user_id=current_user.id,
            )
        )

    # If a user with this ORCID already exists and is pending, promote them.
    user_result = await db.execute(select(User).where(User.orcid == orcid_clean))
    user = user_result.scalar_one_or_none()
    if user and user.access_status != "allowed":
        user.access_status = "allowed"
        profile_result = await db.execute(
            select(ResearcherProfile.id).where(ResearcherProfile.user_id == user.id)
        )
        if profile_result.scalar_one_or_none() is None:
            await enqueue_profile_job_if_absent(db, user, priority=INTERACTIVE_PRIORITY)

    await db.commit()
    return RedirectResponse(url="/admin/access-requests", status_code=302)



@router.post("/access-allowlist/{entry_id}/remove")
async def admin_allowlist_remove(
    entry_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Remove an ORCID from the allowlist."""
    result = await db.execute(
        select(AccessAllowlist).where(AccessAllowlist.id == entry_id)
    )
    entry = result.scalar_one_or_none()
    if entry:
        await db.delete(entry)
        await db.commit()
    return RedirectResponse(url="/admin/access-requests", status_code=302)

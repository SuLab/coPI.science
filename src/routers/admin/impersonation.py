"""Admin impersonation start/stop routes.

The impersonation is held in the signed session (``IMPERSONATE_KEY`` in
src/dependencies.py, spec 2026-10-01 §6.7). It used to be an unsigned
``copi-impersonate`` cookie, which anything able to set a cookie for this host
could point at any user for an admin's browser (A-05).
"""

from fastapi import Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import (
    end_impersonation,
    get_admin_user,
    get_current_user,
    start_impersonation,
)
from src.models import User
from src.routers.admin._common import router
from src.services.pi_onboarding import normalize_orcid


@router.post("/impersonate")
async def impersonate_user(
    request: Request,
    orcid: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Start impersonating a user by ORCID."""
    orcid = normalize_orcid(orcid)
    result = await db.execute(select(User).where(User.orcid == orcid))
    target = result.scalar_one_or_none()
    if target is None:
        # A-16: impersonation looks an account up; it never creates one. An unknown
        # ORCID used to mint a pending PI and enqueue a profile job.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No user has that ORCID iD"
        )

    start_impersonation(request, target.id)
    return RedirectResponse(url="/", status_code=302)



@router.post("/impersonate/stop")
async def stop_impersonating(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Stop impersonating — drop the impersonation from the session."""
    end_impersonation(request)
    return RedirectResponse(url="/admin/users", status_code=302)

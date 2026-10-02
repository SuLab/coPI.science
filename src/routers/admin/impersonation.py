"""Admin impersonation start/stop routes.

The impersonation is held in the signed session (``IMPERSONATE_KEY`` in
src/dependencies.py, spec 2026-10-01 §6.7). It used to be an unsigned
``copi-impersonate`` cookie, which anything able to set a cookie for this host
could point at any user for an admin's browser (A-05).
"""

import logging

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
from src.services.pi_onboarding import find_or_create_pi_by_orcid

logger = logging.getLogger("src.routers.admin")


@router.post("/impersonate")
async def impersonate_user(
    request: Request,
    orcid: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Start impersonating a user by ORCID."""
    # Security: this route requires admin
    orcid = orcid.strip()

    result = await db.execute(select(User).where(User.orcid == orcid))
    target = result.scalar_one_or_none()

    if not target:
        try:
            target = await find_or_create_pi_by_orcid(db, orcid)
            await db.commit()
        except ValueError as exc:
            logger.error("Failed to fetch ORCID profile for impersonation: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ORCID {orcid} not found",
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

"""Admin impersonation start/stop routes."""

import logging

from fastapi import Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database import get_db
from src.dependencies import get_admin_user, get_current_user
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

    response = RedirectResponse(url="/", status_code=302)
    # httpOnly cookie, 24h expiry
    response.set_cookie(
        "copi-impersonate",
        str(target.id),
        max_age=86400,
        httponly=True,
        samesite="lax",
        # Same switch the session cookie uses in src/main.py. This used to read
        # `request.app.state.allow_http`, guarded by a hasattr — but nothing in
        # src/ has ever SET app.state.allow_http (the setting is called
        # allow_http_sessions and lives on Settings), so the hasattr was always
        # False and the whole ternary was a constant secure=False. Production
        # runs ALLOW_HTTP_SESSIONS=false, so this cookie was shipping without
        # Secure beside a session cookie that requires HTTPS (E1.5).
        secure=not get_settings().allow_http_sessions,
    )
    return response



@router.post("/impersonate/stop")
async def stop_impersonating(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Stop impersonating — clear the impersonate cookie."""
    response = RedirectResponse(url="/admin/users", status_code=302)
    response.delete_cookie("copi-impersonate")
    return response

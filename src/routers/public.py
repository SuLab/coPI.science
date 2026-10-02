"""Public-facing routes: the root redirect and the access-pending page."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models import User
from src.services.validators import is_valid_email
from src.web.templating import make_templates

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()


@router.get("/")
async def root(request: Request):
    """Site root: signed-in users go to their profile, everyone else to login.
    This instance has no marketing landing page."""
    if request.session.get("user_id"):
        return RedirectResponse(url="/profile", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@router.get("/access-pending", response_class=HTMLResponse)
async def access_pending(request: Request):
    """Shown after ORCID login when the user is not yet approved."""
    pending_info = request.session.get("pending_access") or {}
    return templates.TemplateResponse(
        request,
        "access_pending.html",
        {
            "request": request,
            "pending_info": pending_info,
        },
    )


@router.post("/access-pending/email", response_class=HTMLResponse)
async def access_pending_email(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Record an address a pending-access user typed (users.contact_email_unverified; never users.email)."""
    pending_info = request.session.get("pending_access") or {}
    user_id = pending_info.get("user_id")
    if not user_id:
        return RedirectResponse(url="/", status_code=302)

    email_clean = (email or "").strip().lower()
    if not is_valid_email(email_clean):
        return templates.TemplateResponse(
            request,
            "access_pending.html",
            {
                "request": request,
                "pending_info": pending_info,
                "email_error": "Please enter a valid email address.",
            },
            status_code=400,
        )

    import uuid as _uuid

    result = await db.execute(select(User).where(User.id == _uuid.UUID(user_id)))
    user = result.scalar_one_or_none()
    if user and not user.email and user.access_status == "pending":
        # Only while the request is still pending: a stale session after an
        # approval or denial must not rewrite what admins see.
        # Unverified by construction (anyone holding this browser session can
        # type any address), so it lands in its own column for admins to see
        # and is never copied to users.email.
        user.contact_email_unverified = email_clean
        await db.commit()
        pending_info["email"] = email_clean
        request.session["pending_access"] = pending_info

    return templates.TemplateResponse(
        request,
        "access_pending.html",
        {
            "request": request,
            "pending_info": pending_info,
            "email_saved": True,
        },
    )

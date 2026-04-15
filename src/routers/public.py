"""Public-facing routes: landing page, waitlist, access-pending."""

import logging
import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models import User, WaitlistSignup

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """Public landing page. Logged-in users redirect to their profile."""
    if request.session.get("user_id"):
        return RedirectResponse(url="/profile", status_code=302)
    return templates.TemplateResponse(request, "landing.html", {"request": request})


@router.post("/waitlist", response_class=HTMLResponse)
async def waitlist_submit(
    request: Request,
    email: str = Form(...),
    name: str = Form(""),
    institution: str = Form(""),
    note: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """Accept a waitlist signup. Upserts on email."""
    email_clean = (email or "").strip().lower()
    if not EMAIL_RE.match(email_clean):
        return templates.TemplateResponse(
            request,
            "landing.html",
            {
                "request": request,
                "waitlist_error": "Please enter a valid email address.",
                "form_values": {
                    "email": email,
                    "name": name,
                    "institution": institution,
                    "note": note,
                },
            },
            status_code=400,
        )

    result = await db.execute(
        select(WaitlistSignup).where(WaitlistSignup.email == email_clean)
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.name = name.strip() or existing.name
        existing.institution = institution.strip() or existing.institution
        existing.note = note.strip() or existing.note
    else:
        db.add(
            WaitlistSignup(
                email=email_clean,
                name=name.strip() or None,
                institution=institution.strip() or None,
                note=note.strip() or None,
            )
        )
    await db.commit()
    logger.info("Waitlist signup: %s", email_clean)

    return templates.TemplateResponse(
        request,
        "landing.html",
        {"request": request, "waitlist_success": True},
    )


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
    """Capture an email for a pending-access user who didn't share one via ORCID."""
    pending_info = request.session.get("pending_access") or {}
    user_id = pending_info.get("user_id")
    if not user_id:
        return RedirectResponse(url="/", status_code=302)

    email_clean = (email or "").strip().lower()
    if not EMAIL_RE.match(email_clean):
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
    if user and not user.email:
        user.email = email_clean
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

"""User settings router — email notification preferences and unsubscribe."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user
from src.models import EmailEngagementTracker, User
from src.services.email_notifications import _verify_unsubscribe_token

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

VALID_FREQUENCIES = {"daily", "twice_weekly", "weekly", "biweekly", "off"}

FREQUENCY_LABELS = {
    "daily": "Daily",
    "twice_weekly": "Twice a week (Mon & Thu)",
    "weekly": "Weekly (Monday)",
    "biweekly": "Every two weeks",
}


def _template_context(request: Request, user: User, **kwargs) -> dict:
    impersonated = getattr(user, "_is_impersonated", False)
    real_admin = getattr(user, "_real_admin", None)
    ctx = {
        "request": request,
        "current_user": real_admin if impersonated else user,
        "user": user,
        "impersonation_banner": user if impersonated else None,
        "active_page": "settings",
    }
    ctx.update(kwargs)
    return ctx


@router.get("", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """User settings page."""
    # Get engagement tracker for status display
    tracker_result = await db.execute(
        select(EmailEngagementTracker).where(
            EmailEngagementTracker.user_id == current_user.id
        )
    )
    tracker = tracker_result.scalar_one_or_none()

    return templates.TemplateResponse(
        request,
        "settings.html",
        _template_context(
            request,
            current_user,
            tracker=tracker,
            frequency_labels=FREQUENCY_LABELS,
        ),
    )


@router.post("/save")
async def settings_save(
    request: Request,
    email_notifications_on: str = Form("0"),
    email_notification_frequency: str = Form("weekly"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save settings."""
    if email_notifications_on != "1":
        email_notification_frequency = "off"
    elif email_notification_frequency not in VALID_FREQUENCIES or email_notification_frequency == "off":
        email_notification_frequency = "weekly"

    current_user.email_notification_frequency = email_notification_frequency

    # If user is re-enabling after system pause, clear the pause flag
    if email_notification_frequency != "off":
        current_user.email_notifications_paused_by_system = False

    # Reset engagement tracker when frequency changes
    tracker_result = await db.execute(
        select(EmailEngagementTracker).where(
            EmailEngagementTracker.user_id == current_user.id
        )
    )
    tracker = tracker_result.scalar_one_or_none()
    if tracker:
        tracker.consecutive_missed = 0

    await db.commit()

    return RedirectResponse(url="/settings?saved=1", status_code=302)


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe(
    request: Request,
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """One-click unsubscribe from email notifications. No auth required."""
    user_id_str = _verify_unsubscribe_token(token)
    if not user_id_str:
        return templates.TemplateResponse(
            request,
            "unsubscribe.html",
            {"request": request, "success": False, "error": "Invalid or expired link."},
        )

    result = await db.execute(select(User).where(User.id == user_id_str))
    user = result.scalar_one_or_none()
    if not user:
        return templates.TemplateResponse(
            request,
            "unsubscribe.html",
            {"request": request, "success": False, "error": "User not found."},
        )

    user.email_notification_frequency = "off"
    await db.commit()

    return templates.TemplateResponse(
        request,
        "unsubscribe.html",
        {"request": request, "success": True, "error": None},
    )


@router.post("/unsubscribe/{token}")
async def unsubscribe_post(
    request: Request,
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """RFC 8058 one-click unsubscribe via POST."""
    user_id_str = _verify_unsubscribe_token(token)
    if not user_id_str:
        return HTMLResponse("Invalid token", status_code=400)

    result = await db.execute(select(User).where(User.id == user_id_str))
    user = result.scalar_one_or_none()
    if not user:
        return HTMLResponse("User not found", status_code=404)

    user.email_notification_frequency = "off"
    await db.commit()

    return HTMLResponse("Unsubscribed successfully", status_code=200)

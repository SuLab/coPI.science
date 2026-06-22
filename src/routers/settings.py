"""User settings router — email notification preferences and unsubscribe."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user
from src.models import EmailNotificationPreference, EmailEngagementTracker, User
from src.services.email_notifications import (
    CATEGORY_DEFAULTS,
    get_or_create_pref,
    _verify_unsubscribe_token,
)

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

VALID_FREQUENCIES = {"daily", "twice_weekly", "weekly", "biweekly", "monthly", "off"}

FREQUENCY_LABELS = {
    "daily": "Daily",
    "twice_weekly": "Twice a week (Mon & Thu)",
    "weekly": "Weekly (Monday)",
    "biweekly": "Every two weeks",
    "monthly": "Monthly",
}

# Table-backed categories shown on the settings page (proposal_review is on User).
PREF_CATEGORIES = ("status_overview", "new_proposal", "news_updates")


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

    # Read table-backed category prefs (falling back to defaults, no insert on GET)
    prefs = {}
    for cat in PREF_CATEGORIES:
        row = (
            await db.execute(
                select(EmailNotificationPreference).where(
                    EmailNotificationPreference.user_id == current_user.id,
                    EmailNotificationPreference.category == cat,
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            prefs[cat] = {"enabled": row.enabled, "frequency": row.frequency}
        else:
            prefs[cat] = dict(CATEGORY_DEFAULTS[cat])

    return templates.TemplateResponse(
        request,
        "settings.html",
        _template_context(
            request,
            current_user,
            tracker=tracker,
            frequency_labels=FREQUENCY_LABELS,
            prefs=prefs,
        ),
    )


def _resolve_frequency(on: str, freq: str) -> str:
    """Normalize a toggle + frequency pair into a stored frequency string."""
    if on != "1":
        return "off"
    if freq not in VALID_FREQUENCIES or freq == "off":
        return "weekly"
    return freq


@router.post("/save")
async def settings_save(
    request: Request,
    # proposal_review (backed by User.email_notification_frequency)
    proposal_review_on: str = Form("0"),
    proposal_review_frequency: str = Form("weekly"),
    # status_overview (table-backed, periodic digest)
    status_overview_on: str = Form("0"),
    status_overview_frequency: str = Form("weekly"),
    # new_proposal (table-backed, event-driven; no frequency)
    new_proposal_on: str = Form("0"),
    # news_updates (table-backed, on/off; no frequency)
    news_updates_on: str = Form("0"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save per-category notification preferences."""
    # --- proposal_review ---
    new_freq = _resolve_frequency(proposal_review_on, proposal_review_frequency)
    if new_freq != current_user.email_notification_frequency:
        # Reset the missed counter when the user changes this preference
        tracker_result = await db.execute(
            select(EmailEngagementTracker).where(
                EmailEngagementTracker.user_id == current_user.id
            )
        )
        tracker = tracker_result.scalar_one_or_none()
        if tracker:
            tracker.consecutive_missed = 0
    current_user.email_notification_frequency = new_freq
    if new_freq != "off":
        # Re-enabling clears a system pause
        current_user.email_notifications_paused_by_system = False

    # --- status_overview ---
    so_pref = await get_or_create_pref(current_user.id, "status_overview", db)
    so_pref.enabled = status_overview_on == "1"
    so_pref.frequency = _resolve_frequency(status_overview_on, status_overview_frequency)

    # --- new_proposal (event-driven, no frequency) ---
    np_pref = await get_or_create_pref(current_user.id, "new_proposal", db)
    np_pref.enabled = new_proposal_on == "1"

    # --- news_updates (on/off, no frequency) ---
    news_pref = await get_or_create_pref(current_user.id, "news_updates", db)
    news_pref.enabled = news_updates_on == "1"

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

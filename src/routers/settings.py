"""User settings router — email notification preferences, unsubscribe, and the Microsoft
sign-in section (shown while the feature is on; the user can unlink there)."""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import account_of, get_current_user, impersonating, refuse_impersonation
from src.models import EmailNotificationPreference, EmailEngagementTracker, User
from src.services.email_notifications import (
    CATEGORY_DEFAULTS,
    get_or_create_pref,
    _verify_unsubscribe_token,
)
from src.services.microsoft_identity import link_view, message_for, unlink
from src.services.session_epoch import bump_session_epoch
from src.sign_in_session import fresh_orcid_sign_in
from src.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter()
_DB = Depends(get_db)
_USER = Depends(get_current_user)

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


def _microsoft_on(request: Request) -> bool:
    return getattr(request.app.state, "microsoft_sign_in", None) is not None


async def _microsoft_section(request: Request, db: AsyncSession, user: User) -> dict | None:
    """What the Microsoft section shows; None hides it.

    Hidden when the feature is off and while impersonating, either because the session
    resolved to an impersonated user or because the session's own admin carries an
    impersonation cookie: an admin's fresh ORCID sign-in must never count for someone else.
    """
    if (
        not _microsoft_on(request)
        or getattr(user, "_is_impersonated", False)
        or impersonating(request, account_of(user))
    ):
        return None
    return {
        "link": await link_view(db, user.id),
        "can_link": fresh_orcid_sign_in(request.session),
    }


@router.get("", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """User settings page."""
    flash = message_for(request.query_params.get("microsoft"))
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
            microsoft=await _microsoft_section(request, db, current_user),
            flash_message=flash[0] if flash else None,
            flash_type=flash[1] if flash else None,
        ),
    )


@router.post("/microsoft/unlink")
async def microsoft_unlink(request: Request, current_user: User = _USER, db: AsyncSession = _DB):
    """Remove the user's own Microsoft sign-in and end every session, this one included."""
    if not _microsoft_on(request):
        raise HTTPException(status_code=404, detail="Not Found")
    refuse_impersonation(
        current_user, "Microsoft sign-in changes are disabled while impersonating."
    )
    user_id = current_user.id
    removed = await unlink(db, user_id)
    if removed is None:
        return RedirectResponse(url="/settings?microsoft=nothing_linked", status_code=302)
    # Unconditional: this request already passed the epoch check, and a session issued after
    # it (a sign-in through the link being removed, after a logout elsewhere moved the epoch)
    # must end too.
    await bump_session_epoch(db, user_id)
    await db.commit()
    logger.info("User %s removed their Microsoft sign-in (tid=%s)", user_id, removed.tenant_id)
    request.session.clear()
    return RedirectResponse(url="/login?error=microsoft_unlinked", status_code=302)


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
    """Show the unsubscribe confirmation page. No auth required.

    This GET is intentionally read-only. Email-security scanners (Outlook
    SafeLinks, Defender, Proofpoint, etc.) auto-fetch every link in a message,
    so a state-mutating GET would silently unsubscribe recipients. The actual
    change happens only on the POST below (confirmation button or RFC 8058
    one-click). See specs/ and the project memory on the unsubscribe landmine.
    """
    user_id_str = _verify_unsubscribe_token(token)
    if not user_id_str:
        return templates.TemplateResponse(
            request,
            "unsubscribe.html",
            {"request": request, "state": "error", "error": "Invalid or expired link."},
        )

    result = await db.execute(select(User).where(User.id == user_id_str))
    user = result.scalar_one_or_none()
    if not user:
        return templates.TemplateResponse(
            request,
            "unsubscribe.html",
            {"request": request, "state": "error", "error": "User not found."},
        )

    return templates.TemplateResponse(
        request,
        "unsubscribe.html",
        {"request": request, "state": "confirm", "error": None, "token": token},
    )


@router.post("/unsubscribe/{token}")
async def unsubscribe_post(
    request: Request,
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Perform the unsubscribe. Reached via the confirmation button on the GET
    page or via RFC 8058 one-click (List-Unsubscribe-Post)."""
    user_id_str = _verify_unsubscribe_token(token)
    if not user_id_str:
        return templates.TemplateResponse(
            request,
            "unsubscribe.html",
            {"request": request, "state": "error", "error": "Invalid or expired link."},
            status_code=400,
        )

    result = await db.execute(select(User).where(User.id == user_id_str))
    user = result.scalar_one_or_none()
    if not user:
        return templates.TemplateResponse(
            request,
            "unsubscribe.html",
            {"request": request, "state": "error", "error": "User not found."},
            status_code=404,
        )

    user.email_notification_frequency = "off"
    await db.commit()

    return templates.TemplateResponse(
        request,
        "unsubscribe.html",
        {"request": request, "state": "success", "error": None},
    )

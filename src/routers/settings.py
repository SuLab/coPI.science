"""User settings router — the account page, and the Microsoft sign-in section for staff
(spec docs/specs/2026-10-13-microsoft-staff-sign-in-design.md §5.3, §5.7).

The email-notification preferences and the unsubscribe links were retired with
PI notification email. Their tables stay.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user, refuse_impersonation
from src.models import User
from src.services.microsoft_identity import link_view, may_use_microsoft_sign_in, unlink
from src.services.session_epoch import bump_session_epoch, epoch_in_session
from src.web.flash import flash
from src.web.page_context import page_context
from src.web.sign_in_session import fresh_orcid_sign_in
from src.web.templating import make_templates

router = APIRouter()
templates = make_templates()
_DB = Depends(get_db)
_USER = Depends(get_current_user)


def _template_context(request: Request, user: User, **kwargs) -> dict:
    return page_context(request, user, active_page="settings", user=user, **kwargs)


def _microsoft_on(request: Request) -> bool:
    return getattr(request.app.state, "microsoft_sign_in", None) is not None


async def _microsoft_section(request: Request, db: AsyncSession, user: User) -> dict | None:
    """What the Microsoft section shows; None hides it (feature off, role not eligible, or
    impersonating: the admin's own fresh sign-in must never count for someone else, S6)."""
    if (not _microsoft_on(request) or getattr(user, "_is_impersonated", False)
            or not may_use_microsoft_sign_in(user)):
        return None
    return {"link": await link_view(db, user.id), "can_link": fresh_orcid_sign_in(request.session)}


@router.get("", response_class=HTMLResponse)
async def settings_page(request: Request, current_user: User = _USER, db: AsyncSession = _DB):
    """User settings page: the account section, and Microsoft sign-in for staff."""
    return templates.TemplateResponse(
        request, "settings.html",
        _template_context(request, current_user,
                          microsoft=await _microsoft_section(request, db, current_user)),
    )


@router.post("/microsoft/unlink")
async def microsoft_unlink(request: Request, current_user: User = _USER, db: AsyncSession = _DB):
    """Remove the user's own Microsoft sign-in and end every session, this one included (D11)."""
    if not _microsoft_on(request):
        raise HTTPException(status_code=404, detail="Not Found")
    refuse_impersonation(current_user, "Microsoft sign-in changes are disabled while impersonating.")
    user_id = current_user.id
    if await unlink(db, user_id) is None:
        flash(request, "No Microsoft account is linked.", "info")
        return RedirectResponse(url="/settings", status_code=302)
    await bump_session_epoch(db, user_id, expected_epoch=epoch_in_session(request.session))
    await db.commit()
    request.session.clear()
    flash(request, "Microsoft sign-in removed. Please sign in again.", "success")
    return RedirectResponse(url="/login", status_code=302)

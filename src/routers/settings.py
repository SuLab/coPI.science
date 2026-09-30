"""User settings router — the account page.

The email-notification preferences and the unsubscribe links were retired with
PI notification email. Their tables stay.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from src.dependencies import get_current_user
from src.models import User

router = APIRouter()
templates = Jinja2Templates(directory="templates")


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
    current_user: User = Depends(get_current_user),
):
    """User settings page: the account section."""
    return templates.TemplateResponse(
        request, "settings.html", _template_context(request, current_user)
    )

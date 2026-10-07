"""User settings router — the account page.

The email-notification preferences and the unsubscribe links were retired with
PI notification email. Their tables stay.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from src.dependencies import get_current_user
from src.models import User
from src.web.templating import make_templates
from src.web.page_context import page_context

router = APIRouter()
templates = make_templates()


def _template_context(request: Request, user: User, **kwargs) -> dict:
    return page_context(request, user, active_page="settings", user=user, **kwargs)


@router.get("", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """User settings page: the account section."""
    return templates.TemplateResponse(
        request, "settings.html", _template_context(request, current_user)
    )

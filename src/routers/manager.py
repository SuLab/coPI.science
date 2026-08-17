"""Manager dashboard router — global, strictly read-only.

Every route here is GET, and the router carries its gate as a router-level
dependency rather than a per-handler one. /admin declares Depends(get_admin_user)
on 34 separate handlers (F5), which means a route added there without the
declaration is open to any logged-in user. A router-level dependency makes that
mistake impossible for this surface: a new route is gated by construction.

Query logic lives in src/services/directory.py and is shared with /admin.

Dependencies are module-level singletons (``_DB``, ``_STAFF``) rather than
inline ``Depends(...)`` calls in argument defaults: ruff's B008 flags the
latter, and with ~2 per handler this router would otherwise chip away at a
lint ceiling (231, currently sitting at 225) that later tasks still need
headroom under.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_staff_user
from src.models import USER_ROLE_PI, User
from src.services.directory import list_pi_directory, load_user_detail

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(get_staff_user)])
templates = Jinja2Templates(directory="templates")

_DB = Depends(get_db)
_STAFF = Depends(get_staff_user)


def _template_context(
    request: Request, current_user: User, active_manager: str = "", **kwargs
) -> dict:
    ctx = {
        "request": request,
        "current_user": current_user,
        "active_page": "manager",
        "active_manager": active_manager,
    }
    ctx.update(kwargs)
    return ctx


@router.get("", response_class=HTMLResponse)
async def manager_root():
    """Bare-prefix landing. The top nav links here; the sub-nav links the children."""
    return RedirectResponse(url="/manager/pis", status_code=302)


@router.get("/pis", response_class=HTMLResponse)
async def manager_pis(
    request: Request,
    status_filter: str | None = None,
    institution_filter: str | None = None,
    claimed_filter: str | None = None,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """PI directory. Unclaimed stubs included (D11) so recruitment coverage is
    visible; staff accounts excluded so the admin roster is not enumerable."""
    user_data = await list_pi_directory(
        db,
        status_filter=status_filter,
        institution_filter=institution_filter,
        claimed_filter=claimed_filter,
        roles=(USER_ROLE_PI,),
    )
    return templates.TemplateResponse(
        request,
        "manager/pis.html",
        _template_context(
            request,
            current_user,
            active_manager="pis",
            user_data=user_data,
            status_filter=status_filter,
            claimed_filter=claimed_filter,
        ),
    )


@router.get("/pis/{user_id}", response_class=HTMLResponse)
async def manager_pi_detail(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """One PI's record. 404s on a non-PI account so a manager cannot read an
    admin's row by guessing or harvesting a UUID."""
    detail = await load_user_detail(db, user_id)
    if detail is None or detail["user"].user_role != USER_ROLE_PI:
        raise HTTPException(status_code=404, detail="PI not found")
    return templates.TemplateResponse(
        request,
        "manager/pi_detail.html",
        _template_context(
            request,
            current_user,
            active_manager="pis",
            target_user=detail["user"],
            profile=detail["profile"],
            publications=detail["publications"],
            jobs=detail["jobs"],
        ),
    )

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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_staff_user
from src.models import USER_ROLE_PI, User
from src.services.directory import (
    build_discussions_view,
    build_run_detail,
    list_assessments,
    list_pi_directory,
    list_runs_overview,
    load_user_detail,
)

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(get_staff_user)])
templates = Jinja2Templates(directory="templates")

_DB = Depends(get_db)
_STAFF = Depends(get_staff_user)
_AGENT_FILTER = Query(default=[])


def _template_context(
    request: Request, current_user: User, active_manager: str = "", **kwargs
) -> dict:
    """Build the template context, surfacing the impersonation banner.

    ``current_user`` here is the *effective* user from ``get_staff_user`` —
    the impersonated user when an admin is impersonating a manager (see that
    dependency's docstring: it 403s an admin impersonating a PI, but an admin
    impersonating a *manager* satisfies ``is_staff`` and reaches this router).
    Without this, every /manager/* page rendered with no banner and no Stop
    button, and because the effective user is a manager, `is_admin` is false
    on the nav too — stranding the admin with no visible route back to
    /admin. Mirrors the same pattern in onboarding.py / profile.py /
    agent_page.py / settings.py.

    None of the manager templates key page data off `current_user` (they are
    read-only listings driven entirely by `**kwargs`), so swapping it for the
    real admin here only affects the banner/nav, not what data is shown.
    """
    impersonated = getattr(current_user, "_is_impersonated", False)
    real_admin = getattr(current_user, "_real_admin", None)
    ctx = {
        "request": request,
        "current_user": real_admin if impersonated else current_user,
        "impersonation_banner": current_user if impersonated else None,
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


@router.get("/assessments", response_class=HTMLResponse)
async def manager_assessments(
    request: Request,
    run_id: str | None = None,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """BlackbirdBot's screening verdicts. Same data and same run-scoping as
    /admin/assessments; read-only, and it has no export path."""
    view = await list_assessments(db, run_id)
    return templates.TemplateResponse(
        request,
        "manager/assessments.html",
        _template_context(request, current_user, active_manager="assessments", **view),
    )


@router.get("/discussions", response_class=HTMLResponse)
async def manager_discussions(
    request: Request,
    run_id: str | None = None,
    channel_filter: str | None = None,
    status_filter: str | None = None,
    agent_filter: list[str] = _AGENT_FILTER,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Thread-level view of what each lab's bot did.

    Carries no `export` parameter: the export branch is admin-only, and this
    router is strictly read-only-and-render (D12).

    Per D5 this includes threads from collab_private channels. That is a
    deliberate policy decision recorded in the spec, not an oversight — no
    visibility filter exists anywhere in this code path (F12).
    """
    view = await build_discussions_view(
        db,
        run_id=run_id,
        channel_filter=channel_filter,
        status_filter=status_filter,
        agent_filter=agent_filter,
    )
    return templates.TemplateResponse(
        request,
        "manager/discussions.html",
        _template_context(request, current_user, active_manager="discussions", **view),
    )


@router.get("/activity", response_class=HTMLResponse)
async def manager_activity(
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Simulation-run overview."""
    view = await list_runs_overview(db)
    return templates.TemplateResponse(
        request,
        "manager/activity.html",
        _template_context(request, current_user, active_manager="activity", **view),
    )


@router.get("/activity/{run_id}", response_class=HTMLResponse)
async def manager_activity_detail(
    run_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """One run's per-agent and per-channel stats. There is deliberately no
    llm-calls drill-down here (D10)."""
    view = await build_run_detail(db, run_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return templates.TemplateResponse(
        request,
        "manager/activity_detail.html",
        _template_context(request, current_user, active_manager="activity", **view),
    )

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

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_staff_user
from src.models import USER_ROLE_PI, AgentRegistry, User
from src.services.agent_mute import set_agent_mute_state
from src.services.assessment_detail import build_assessment_detail
from src.services.directory import (
    build_discussions_view,
    build_run_detail,
    list_assessments,
    list_pi_directory,
    list_runs_overview,
    load_user_detail,
)
from src.services.jhu_rules import get_tenure_start
from src.services.pi_onboarding import (
    create_pending_agent_for,
    find_or_create_pi_by_orcid,
)
from src.services.profile_edit import apply_profile_edits
from src.services.thread_panel import panel_cards_by_thread

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
    agent = detail["user"].agent
    tenure_start = await get_tenure_start(
        db, user_id, agent_id=agent.agent_id if agent else None
    )
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
            tenure_start=tenure_start,
        ),
    )


def _create_pi_error_code(exc: ValueError) -> str:
    """Map service failures to canned codes — the raw exception text used to
    be interpolated into the redirect Location unescaped (it embeds the
    submitted ORCID string and upstream httpx internals; audit L1)."""
    text = str(exc)
    if "Invalid ORCID" in text:
        return "invalid_orcid"
    if "already exists" in text:
        return "exists"
    if "Could not fetch" in text:
        return "fetch_failed"
    return "create_failed"


@router.post("/pis")
async def manager_create_pi(
    orcid: str = Form(...),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Create a PI via the ORCID pipeline (design D5/D6) — no manual profile
    form exists anywhere in the app; every profile is ORCID/publication
    derived. Rejects if the ORCID already belongs to anyone, any role.

    2026-08-24 auto-flow: one POST creates the User, the generate_profile
    Job, a PENDING (inert) AgentRegistry row and the employment-derived JHU
    tenure entry, in ONE commit — so the worker can never claim the job
    before the agent row exists (the ordering gap that silently skipped
    exports/revisions for seeded PIs; scripts/backfill_agents.py repairs it
    after the fact). The pending row is provisioned and activated by an
    admin on /admin/agents, never here (D1: no new manager write route; D7:
    the row belongs to the new PI, never the manager)."""
    try:
        pi = await find_or_create_pi_by_orcid(db, orcid)
        await create_pending_agent_for(db, pi)
        await db.commit()
    except ValueError as exc:
        return RedirectResponse(
            url=f"/manager/pis?error={_create_pi_error_code(exc)}",
            status_code=302,
        )
    except IntegrityError:
        # Two managers adding same-surname PIs can race the identity
        # derivation's SELECT-then-INSERT; the loser rolls the WHOLE creation
        # back (User + Job + agent together — the atomicity is the feature).
        await db.rollback()
        logger.warning(
            "Add-PI race on agent identity for ORCID %r; rolled back",
            orcid.strip()[:40],
        )
        return RedirectResponse(
            url="/manager/pis?error=agent_conflict", status_code=302
        )
    return RedirectResponse(url=f"/manager/pis/{pi.id}", status_code=302)


@router.post("/pis/{user_id}/profile")
async def manager_edit_pi_profile(
    user_id: uuid.UUID,
    name: str = Form(""),
    email: str = Form(""),
    institution: str = Form(""),
    department: str = Form(""),
    research_summary: str = Form(""),
    techniques: str = Form(""),
    experimental_models: str = Form(""),
    disease_areas: str = Form(""),
    key_targets: str = Form(""),
    keywords: str = Form(""),
    jhu_tenure_start: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Edit a PI's profile fields (design D8) — same fields as the PI's own
    /profile/save, attributed to the acting manager via changed_by_user_id,
    plus the JHU tenure-start year (manager-only field; blank = unchanged)."""
    detail = await load_user_detail(db, user_id)
    if detail is None or detail["user"].user_role != USER_ROLE_PI:
        raise HTTPException(status_code=404, detail="PI not found")

    error = await apply_profile_edits(
        db, target_user=detail["user"], changed_by_user_id=current_user.id,
        name=name, email=email, institution=institution, department=department,
        research_summary=research_summary, techniques=techniques,
        experimental_models=experimental_models, disease_areas=disease_areas,
        key_targets=key_targets, keywords=keywords,
        jhu_tenure_start=jhu_tenure_start,
    )
    if error:
        return RedirectResponse(url=f"/manager/pis/{user_id}?error={error}", status_code=302)
    return RedirectResponse(url=f"/manager/pis/{user_id}?saved=1", status_code=302)


async def _manager_set_mute(
    user_id: uuid.UUID, db: AsyncSession, current_user: User, *, muted: bool,
) -> RedirectResponse:
    detail = await load_user_detail(db, user_id)
    if detail is None or detail["user"].user_role != USER_ROLE_PI:
        raise HTTPException(status_code=404, detail="PI not found")

    agent = (
        await db.execute(select(AgentRegistry).where(AgentRegistry.user_id == user_id))
    ).scalar_one_or_none()
    if agent is None:
        return RedirectResponse(
            url=f"/manager/pis/{user_id}?error=no_agent", status_code=302
        )

    ok = await set_agent_mute_state(
        db, agent=agent, muted=muted, actor_user_id=current_user.id,
    )
    if not ok:
        return RedirectResponse(
            url=f"/manager/pis/{user_id}?error=agent_not_mutable", status_code=302
        )
    return RedirectResponse(url=f"/manager/pis/{user_id}", status_code=302)


@router.post("/pis/{user_id}/mute")
async def manager_mute_pi(
    user_id: uuid.UUID, db: AsyncSession = _DB, current_user: User = _STAFF,
):
    """Mute a PI's agent — maps to status='inactive' (design D2), not a new
    status value. No-ops (redirects with an error) if the agent doesn't
    exist or isn't currently active/inactive."""
    return await _manager_set_mute(user_id, db, current_user, muted=True)


@router.post("/pis/{user_id}/unmute")
async def manager_unmute_pi(
    user_id: uuid.UUID, db: AsyncSession = _DB, current_user: User = _STAFF,
):
    return await _manager_set_mute(user_id, db, current_user, muted=False)


@router.get("/assessments", response_class=HTMLResponse)
async def manager_assessments(
    request: Request,
    run_id: str | None = None,
    sort: str | None = None,
    lab: str | None = None,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """BlackbirdBot's screening verdicts. Same data, same run-scoping and the
    same sort/lab controls as /admin/assessments; read-only, and it has no
    export path."""
    view = await list_assessments(db, run_id, sort=sort, lab=lab)
    return templates.TemplateResponse(
        request,
        "manager/assessments.html",
        _template_context(request, current_user, active_manager="assessments", **view),
    )


@router.get("/assessments/{assessment_id}", response_class=HTMLResponse)
async def manager_assessment_detail(
    assessment_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """One verdict in full, plus the interview that produced it.

    Same page as /admin/assessments/{id} with ``admin_view=False``, which is
    what keeps the LLM drill-down admin-only (D10): no tool activity, and no
    specialist's verbatim opinion text. The panel's substance — domain, signal,
    confidence, concerns, questions_to_ask — IS shown; that split is plan
    decision 2, and the redaction happens in the service, not just in the
    template (see ``src.services.assessment_detail``).
    """
    detail = await build_assessment_detail(db, assessment_id, admin_view=False)
    if detail is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return templates.TemplateResponse(
        request,
        "manager/assessment_detail.html",
        _template_context(request, current_user, active_manager="assessments", **detail),
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
    # What the panel was asked, and what it said, per thread — the same cards
    # /admin/discussions shows, minus the verbatim reply. A domain, a signal, a
    # confidence, the concerns and the questions_to_ask are verdict substance,
    # not LLM drill-down, so they are not admin-only (plan decision 2); the
    # specialist's raw text is, and ``admin_view=False`` is what withholds it —
    # in the service, so it never reaches the page source at all.
    panel = await panel_cards_by_thread(
        db,
        view["selected_run_id"],
        [t["message_ts"] for t in view["threads"]],
        admin_view=False,
    )
    return templates.TemplateResponse(
        request,
        "manager/discussions.html",
        _template_context(
            request,
            current_user,
            active_manager="discussions",
            panel_by_thread=panel.by_thread,
            # A capped panel read must not look like an unconsulted page.
            panel_truncated=panel.truncated,
            panel_row_limit=panel.limit,
            admin_view=False,
            **view,
        ),
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

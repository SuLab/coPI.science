"""Manager dashboard router — global read access, with an explicit write
allowlist (D1) and, since Task 3, three read tiers instead of one.

The router-level dependency (``Depends(get_review_user)``) is deliberately
the WIDEST audience this surface admits — admin, manager, or reviewer — and
it exists so a route added here with no dependency of its own is still
gated by construction, unlike /admin, which declares Depends(get_admin_user)
on 34 separate handlers (F5) and is therefore only as safe as every
individual declaration. But the router-level dependency is NOT the real
gate for any individual handler: it only proves a caller is one of the
three roles above, never which one. The per-handler singleton — ``_STAFF``
(admin/manager) or ``_REVIEW`` (+ reviewer) — is what actually decides who
is admitted to a given route, and every write handler stays on ``_STAFF``
regardless of what the router-level dependency alone would allow through.
Exactly four GETs use ``_REVIEW``: ``manager_pis``, ``manager_pi_detail``,
``manager_assessments`` and ``manager_assessment_detail``; ``manager_root``
takes no per-handler dependency at all (its only job is a redirect to a
route that is itself reviewer-reachable). Every other handler — the four
POSTs, ``manager_discussions`` and ``manager_activity``/
``manager_activity_detail`` — stays on ``_STAFF``, so a reviewer reaches
none of them. This docstring is not what enforces that split;
``tests/integration/test_reviewer_role.py``'s
``test_reviewer_manager_surface_is_exactly_the_read_slice`` enumerates the
live router for both methods against an explicit expectation map and fails
loudly the moment a route and the map disagree.

Query logic lives in src/services/directory.py and is shared with /admin.

Dependencies are module-level singletons (``_DB``, ``_STAFF``, ``_REVIEW``)
rather than inline ``Depends(...)`` calls in argument defaults: ruff's B008
flags the latter, and with ~2 per handler this router would otherwise chip
away at a lint ceiling that later tasks still need headroom under (see
``scripts/ci.sh``'s ``SRC_LINT_MAX``).
"""

import hashlib
import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_review_user, get_staff_user
from src.models import USER_ROLE_PI, AgentRegistry, PromptChangeSuggestion, User
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
router = APIRouter(dependencies=[Depends(get_review_user)])
templates = Jinja2Templates(directory="templates")

_DB = Depends(get_db)
_STAFF = Depends(get_staff_user)      # manager|admin — writes, discussions, activity
_REVIEW = Depends(get_review_user)    # + reviewer — the four read handlers only
_AGENT_FILTER = Query(default=[])

#: Task 12: the review-bot-drafted prompt-change queue. Read-only display cap
#: — a reviewer never reaches this pair (get_staff_user, not get_review_user):
#: a suggestion can quote an unpublished PI disclosure verbatim (it is
#: distilled from the same interview transcript the sidecar protects), so it
#: gets the same staff-only audience as discussions/activity, not the four
#: reviewer-visible reads.
SUGGESTIONS_LIMIT = 200

#: Mirrors PromptChangeSuggestion.status's docstring (src/models/review.py).
#: Kept local rather than imported: the write-side validation lives on the
#: reviews router, which does not import from this one.
_SUGGESTION_STATUSES = frozenset({"open", "dismissed", "implemented"})


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

    Templates DO now key controls off the user (Task 3): pi_detail.html's
    mute buttons and Edit Profile form, and pis.html's Add-PI form, are all
    gated on `effective_user.is_staff and not impersonation_banner` in the
    template, never on `current_user` — because an admin CAN impersonate a
    reviewer (the impersonate cookie carries no role restriction), and under
    impersonation this dict's `current_user` is swapped back to the real
    admin. A `current_user.is_staff` gate would therefore render write forms
    for an admin impersonating a reviewer that then 403 on submission.
    `effective_user` (base.html's `impersonation_banner or current_user`) is
    what those controls gate on; `current_user` here still only decides the
    banner/nav, and the swap above is unchanged.
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
    current_user: User = _REVIEW,
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
    current_user: User = _REVIEW,
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
    current_user: User = _REVIEW,
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
    current_user: User = _REVIEW,
):
    """One verdict in full, plus the interview that produced it.

    Same page as /admin/assessments/{id} with ``admin_view=False``, which is
    what keeps the LLM drill-down admin-only (D10): no tool activity, and no
    specialist's verbatim opinion text. The panel's substance — domain, signal,
    confidence, concerns, questions_to_ask — IS shown; that split is plan
    decision 2, and the redaction happens in the service, not just in the
    template (see ``src.services.assessment_detail``).
    """
    detail = await build_assessment_detail(
        db, assessment_id, admin_view=False, viewer_is_staff=current_user.is_staff
    )
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


def _prompt_file_status(entry: dict) -> dict:
    """Current-hash comparison for one recorded ``prompt_files`` entry.

    Computed here, at RENDER time, not stored: a suggestion's staleness is a
    fact about the *live* prompt set, and freezing it at write time would go
    stale itself the moment anything under ``prompts/`` changed.

    A stored ``sha256_12`` of ``None`` means the bot's own read failed when it
    ran (``review_bot._render_prompt_files``'s ``FileNotFoundError`` branch)
    — there is nothing to diff against, so that's reported as "missing" the
    same way a file that has since been deleted is, rather than invented as
    a false "stale".
    """
    path = entry.get("path", "")
    stored_hash = entry.get("sha256_12")
    try:
        current_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    except FileNotFoundError:
        current_hash = None
    if stored_hash is None or current_hash is None:
        badge = "missing"
    elif current_hash != stored_hash:
        badge = "stale"
    else:
        badge = None
    return {
        "path": path,
        "stored_hash": stored_hash,
        "current_hash": current_hash,
        "badge": badge,
    }


@router.get("/prompt-suggestions", response_class=HTMLResponse)
async def manager_prompt_suggestions(
    request: Request,
    status: str | None = None,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """The review bot's (Task 10) drafted prompt-edit queue. Read-only triage:
    the only write this surface offers is the status action, which lives on
    ``POST /reviews/suggestions/{id}/status`` (D1-style split — every write
    stays off this router except the four-route allowlist)."""
    status_filter = status if status in _SUGGESTION_STATUSES else None
    query = select(PromptChangeSuggestion)
    if status_filter:
        query = query.where(PromptChangeSuggestion.status == status_filter)
    total_count = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    query = query.order_by(PromptChangeSuggestion.created_at.desc()).limit(SUGGESTIONS_LIMIT)
    suggestions = (await db.execute(query)).scalars().all()
    return templates.TemplateResponse(
        request,
        "manager/prompt_suggestions.html",
        _template_context(
            request,
            current_user,
            active_manager="prompt-suggestions",
            suggestions=suggestions,
            status_filter=status_filter,
            total_count=total_count,
            suggestions_limit=SUGGESTIONS_LIMIT,
        ),
    )


@router.get("/prompt-suggestions/{suggestion_id}", response_class=HTMLResponse)
async def manager_prompt_suggestion_detail(
    suggestion_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """One suggestion in full: the feedback it was distilled from, the
    interview/assessment it came from (if that row still exists — Task 1's
    ``assessment_id`` is SET NULL, not CASCADE, on deletion), and per-file
    staleness against the prompt set on disk right now."""
    suggestion = (
        await db.execute(
            select(PromptChangeSuggestion).where(PromptChangeSuggestion.id == suggestion_id)
        )
    ).scalar_one_or_none()
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    file_status = [_prompt_file_status(entry) for entry in suggestion.prompt_files]
    return templates.TemplateResponse(
        request,
        "manager/prompt_suggestion_detail.html",
        _template_context(
            request,
            current_user,
            active_manager="prompt-suggestions",
            suggestion=suggestion,
            file_status=file_status,
        ),
    )

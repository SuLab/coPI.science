"""Admin agent registry routes."""

import re
import uuid

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.role_capabilities import requires_linked_user, star_role
from src.agent.roles import available_roles
from src.database import get_db
from src.dependencies import get_admin_user, get_staff_user
from src.models import AgentRegistry, Job, ResearcherProfile, User
from src.routers.admin._common import (
    _ADMIN,
    _DB,
    VALID_AGENT_STATUSES,
    _template_context,
    router,
    templates,
)
from src.services.agent_activation import (
    activate_agent,
    activation_blockers,
    ensure_activation_allowed,
)
from src.services.agent_form import agent_form_version
from src.services.jhu_rules import get_tenure_start
from src.web.flash import flash

#: The slug names profile files (profiles/public/<slug>.md) and must pass
#: user_deletion's _SAFE_AGENT_ID: a slug outside it could escape the profiles
#: directory, and its agent could never be deleted.
_SLUG_RE = re.compile(r"[a-z0-9_-]{1,50}")


@router.get("/agents", response_class=HTMLResponse)
async def admin_agents(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Agent registry management."""
    result = await db.execute(
        select(AgentRegistry).order_by(AgentRegistry.requested_at.desc())
    )
    agents = result.scalars().all()

    # One users query serves both the linked-name map and the linking dropdown.
    users_result = await db.execute(select(User).order_by(User.name))
    all_users = users_result.scalars().all()
    user_map = {str(u.id): u.name for u in all_users}

    # Which agents have a usable bot token (DB column preferred, .env fallback).
    from src.services.slack_tokens import token_for_agent_row
    env_token_agents = {
        a.agent_id for a in agents if token_for_agent_row(a)
    }

    pending = [a for a in agents if a.status == "pending"]
    active = [a for a in agents if a.status == "active"]
    suspended = [a for a in agents if a.status == "suspended"]
    inactive = [a for a in agents if a.status == "inactive"]

    return templates.TemplateResponse(
        request,
        "admin/agents.html",
        _template_context(
            request,
            current_user,
            active_admin="agents",
            pending=pending,
            active=active,
            suspended=suspended,
            inactive=inactive,
            user_map=user_map,
            all_users=all_users,
            env_token_agents=env_token_agents,
        ),
    )



@router.get("/agents/{agent_id}", response_class=HTMLResponse)
async def admin_agent_detail(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Agent detail / approval form."""
    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Get linked user
    linked_user = None
    if agent.user_id:
        u_result = await db.execute(select(User).where(User.id == agent.user_id))
        linked_user = u_result.scalar_one_or_none()

    # Evidence panel + gate preview (audit H4/FD-7): what stands behind this
    # lab, shown beside the Approve button — profile groundedness, the newest
    # generation job, the JHU tenure entry, and the exact blockers the gate
    # would refuse activation for.
    profile = None
    latest_gen_job = None
    tenure_start = None
    if agent.user_id:
        profile = (
            await db.execute(
                select(ResearcherProfile).where(
                    ResearcherProfile.user_id == agent.user_id
                )
            )
        ).scalar_one_or_none()
        latest_gen_job = (
            await db.execute(
                select(Job)
                .where(Job.user_id == agent.user_id, Job.type == "generate_profile")
                .order_by(Job.enqueued_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        tenure_start = await get_tenure_start(
            db, agent.user_id, agent_id=agent.agent_id
        )
    blockers = await activation_blockers(db, agent)

    # Star-spoke state for the evidence panel: "missing" means a run started
    # with this agent active would fail _validate_star_topology at startup.
    spoke_state = None
    if star_role(agent.role) == "spoke" and agent.status != "suspended":
        from src.services.star_topology import ensure_star_spokes

        try:
            plan = await ensure_star_spokes(
                db, apply=False, only={agent.agent_id}
            )
            spoke_state = (
                "missing"
                if (plan.created_cohorts or plan.added_members)
                else "present"
            )
        except ValueError:
            # No single scout_hub on the roster — the wire buttons will refuse
            # too; the panel says so rather than 500ing the page.
            spoke_state = "unknown"

    return templates.TemplateResponse(
        request,
        "admin/agent_detail.html",
        _template_context(
            request,
            current_user,
            active_admin="agents",
            agent=agent,
            linked_user=linked_user,
            profile=profile,
            latest_gen_job=latest_gen_job,
            tenure_start=tenure_start,
            blockers=blockers,
            activation_blocked=request.query_params.get("activation_blocked"),
            valid_statuses=VALID_AGENT_STATUSES,
            available_roles=available_roles(),
            form_error=request.query_params.get("error"),
            form_version=agent_form_version(agent),
            slack_ok=request.query_params.get("slack_ok"),
            role_error=request.query_params.get("role_error"),
            spoke_state=spoke_state,
            requires_linked_user=requires_linked_user(agent.role),
            spoke_ok=request.query_params.get("spoke_ok"),
            spoke_error=request.query_params.get("spoke_error"),
        ),
    )



@router.post("/agents/{agent_id}/ensure-spoke")
async def admin_ensure_agent_spoke(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Wire ONE lab into the hub-and-spoke topology (the per-agent button).

    Scoped twin of POST /admin/cohorts/ensure-star-spokes; the click is
    attributed to the acting admin in cohort_audit_events.
    """
    from urllib.parse import quote

    from src.services.star_topology import ensure_star_spokes

    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if star_role(agent.role) != "spoke":
        return RedirectResponse(
            url=f"/admin/agents/{agent_id}?spoke_error="
            + quote("Only pi_lab agents have star spokes."),
            status_code=302,
        )
    try:
        report = await ensure_star_spokes(
            db, apply=True, actor=current_user, only={agent.agent_id}
        )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/admin/agents/{agent_id}?spoke_error={quote(str(exc)[:200])}",
            status_code=302,
        )
    await db.commit()
    if report.anomalies:
        return RedirectResponse(
            url=f"/admin/agents/{agent_id}?spoke_error="
            + quote("; ".join(report.anomalies)[:300]),
            status_code=302,
        )
    return RedirectResponse(
        url=f"/admin/agents/{agent_id}?spoke_ok=1", status_code=302
    )



@router.post("/agents/{agent_id}/approve")
async def admin_approve_agent(
    agent_id: uuid.UUID,
    request: Request,
    agent_slug: str = Form(""),
    bot_name: str = Form(...),
    replace_slack_bot_token: str = Form(""),
    agent_status: str = Form(None),
    activation_override: str = Form(""),
    form_version: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Approve a pending agent request, or save edits to an existing agent.

    Pending agents are approved straight to ``active`` (stamping the approver),
    matching the original one-click flow. For an agent that's already been
    approved, the edit form's status dropdown drives ``status`` — letting an
    admin park (``inactive``), ``suspended``, or re-``active``ate it. A running
    simulation picks the change up live via ``_sync_roster_from_db``.

    ACTIVATION IS GATED (audit H4 / coverage plan P3): flipping any pi_lab
    agent to ``active`` — through either branch — is refused when its profile
    is missing or ungrounded or its newest generation job is dead, unless the
    explicit (logged) override checkbox was posted. Auto-created pending rows
    (the manager Add-PI flow) made "pending" stop implying "profile exists",
    and an active agent with no exported profile is the Kavran-class failure.
    A refusal applies NO edits at all — the form's slug/name/token changes
    roll back with it, so what the admin sees stays what the DB holds.

    The form is guarded against staleness (RA-02): the row is locked, and a
    ``form_version`` that no longer matches ``agent_form_version`` means another
    writer changed it after the page rendered, so nothing is written. The slug is
    editable only while the agent is pending (RA-14), and the Slack bot token is
    write-only: a blank ``replace_slack_bot_token`` keeps the stored one (RA-03).

    ``agent_status=pending`` is refused for an agent that is no longer pending
    (C-04). A hub-role activation is refused while another hub is active, override or not
    (D14).
    """
    result = await db.execute(
        select(AgentRegistry)
        .where(AgentRegistry.id == agent_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if form_version != agent_form_version(agent):
        return RedirectResponse(url=f"/admin/agents/{agent_id}?error=stale_form", status_code=302)
    if agent_status == "pending" and agent.status != "pending":
        # C-04: `pending` re-opens the slug rename and the auto-activation branch.
        return RedirectResponse(
            url=f"/admin/agents/{agent_id}?error=pending_not_allowed", status_code=302
        )
    new_slug = agent_slug.strip().lower()
    if new_slug and new_slug != agent.agent_id:
        if agent.status != "pending":
            return RedirectResponse(
                url=f"/admin/agents/{agent_id}?error=slug_read_only", status_code=302
            )
        if not _SLUG_RE.fullmatch(new_slug):
            return RedirectResponse(
                url=f"/admin/agents/{agent_id}?error=invalid_slug", status_code=302
            )
        taken = await db.scalar(
            select(AgentRegistry.id).where(
                AgentRegistry.agent_id == new_slug, AgentRegistry.id != agent.id
            )
        )
        if taken is not None:
            return RedirectResponse(
                url=f"/admin/agents/{agent_id}?error=slug_taken", status_code=302
            )

    activating = agent.status == "pending" or (
        agent.status != "active" and agent_status == "active"
    )
    if activating:
        blockers = await activate_agent(
            db, agent, actor=current_user,
            override=bool(activation_override.strip()),
        )
        if blockers:
            flash(request, "Activation refused: " + "; ".join(blockers), "error")
            return RedirectResponse(
                url=f"/admin/agents/{agent_id}?activation_blocked=1",
                status_code=302,
            )

    if new_slug:
        agent.agent_id = new_slug  # pending only, by the check above
    agent.bot_name = bot_name.strip()
    # No path clears the token from this form; user_deletion and provisioning own that.
    if replace_slack_bot_token.strip():
        agent.slack_bot_token = replace_slack_bot_token.strip()

    if not activating and agent_status in VALID_AGENT_STATUSES:
        agent.status = agent_status

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()  # a concurrent rename took the slug
        return RedirectResponse(url=f"/admin/agents/{agent_id}?error=slug_taken", status_code=302)

    return RedirectResponse(url="/admin/agents", status_code=302)



@router.post("/agents/{agent_id}/reject")
async def admin_reject_agent(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Reject an agent request."""
    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent.status = "suspended"
    await db.commit()

    return RedirectResponse(url="/admin/agents", status_code=302)



@router.post("/agents/{agent_id}/slack/provision")
async def admin_provision_slack(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Create a Slack app for this agent and redirect to Slack's install/consent
    screen. Slack redirects back to the callback below, which saves the token."""
    from src.services.admin_provisioning import ProvisioningError, start_provisioning

    agent = (
        await db.execute(select(AgentRegistry).where(AgentRegistry.id == agent_id))
    ).scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        oauth_url = await start_provisioning(db, agent, initiated_by=current_user)
    except ProvisioningError as exc:
        flash(request, f"Slack provisioning failed: {exc}", "error")
        return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)
    return RedirectResponse(url=oauth_url, status_code=302)



#: Slack OAuth ``error`` values the callback names; any other value gets a fixed sentence.
_SLACK_OAUTH_ERRORS = {"access_denied": "the installation was cancelled in Slack"}


@router.get("/agents/slack/callback")
async def admin_provision_slack_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_staff_user),
):
    """OAuth redirect target: exchange the code for a bot token and store it on
    the agent, then return to the caller's own surface.

    The path stays under /admin even though a manager may now reach it: it is
    baked into the ``redirect_uri`` of every Slack app manifest already issued,
    and moving it would break every install link in flight. Only the GATE
    widened (admin -> staff, F2), and every redirect out of here branches on
    the caller's role so a manager is never dropped on an /admin page they
    cannot load. ``complete_provisioning`` separately refuses to finish an
    install a different account started (migration 0046).
    """
    from src.services.admin_provisioning import ProvisioningError, complete_provisioning

    # An impersonated session is admitted (operator decision 2026-09-11: the
    # /manager/pis PI-management controls, this callback included, are not
    # refused while impersonating; reviewer assign/unassign, prompt-suggestion
    # generate/status, assessment chat and both account deletes still are).
    # `current_user` is the impersonated account, which is also who
    # `start_provisioning` recorded as initiator, so the initiator check below
    # still lines up.
    is_admin = bool(current_user.is_admin)

    def surface_error(msg: str) -> RedirectResponse:
        flash(request, f"Slack provisioning failed: {msg}", "error")
        return RedirectResponse(
            url="/admin/agents" if is_admin else "/manager/pis", status_code=302
        )

    if error:
        # A cross-site GET can set ``error`` to any text, so it is never echoed.
        return surface_error(_SLACK_OAUTH_ERRORS.get(error, "Slack reported an error"))
    if not code or not state:
        return surface_error("Missing code or state from Slack")

    try:
        agent = await complete_provisioning(
            db, state, code, completing_user=current_user
        )
    except ProvisioningError as exc:
        return surface_error(str(exc))

    if is_admin:
        return RedirectResponse(
            url=f"/admin/agents/{agent.id}?slack_ok=1", status_code=302
        )
    if agent.user_id is None:
        return RedirectResponse(url="/manager/pis?slack_ok=1", status_code=302)
    return RedirectResponse(
        url=f"/manager/pis/{agent.user_id}?slack_ok=1", status_code=302
    )



@router.post("/agents/{agent_id}/link")
async def admin_link_agent(
    agent_id: uuid.UUID,
    request: Request,
    user_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Link an agent to a user account."""
    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent.user_id = uuid.UUID(user_id) if user_id else None
    await db.commit()

    return RedirectResponse(url="/admin/agents", status_code=302)



@router.post("/agents/{agent_id}/role")
async def admin_set_agent_role(
    agent_id: uuid.UUID,
    request: Request,
    role: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Set an agent's role — selects its per-role prompt overrides and tool
    allow-list (src/agent/roles.py). Validated against the same role set the
    admin's <select> was built from, so a stale or hand-crafted form can never
    write a role the runtime does not know how to resolve.

    On an ACTIVE agent the new role must pass the activation gate,
    including the one-active-hub limit (D14); an inactive agent may take any valid role.
    """
    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if role not in available_roles():
        return RedirectResponse(
            url=f"/admin/agents/{agent_id}?role_error=Unknown+role", status_code=302
        )

    if role != agent.role and agent.status == "active":
        blockers = await ensure_activation_allowed(
            db, agent, new_role=role, new_status="active"
        )
        if blockers:
            flash(request, "Role not changed: " + "; ".join(blockers), "error")
            return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)

    agent.role = role
    await db.commit()

    return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)

"""Admin agent registry routes."""

import logging
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
from src.dependencies import get_admin_user, get_staff_user, impersonation_note
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
    persona_blockers,
)
from src.services.agent_form import agent_form_version
from src.services.jhu_rules import get_tenure_start
from src.services.persona_lifecycle import export_after_lifecycle, rename_persona_files
from src.services.pi_companies import move_companies_file
from src.services.star_topology import ensure_lab_spoke
from src.web.flash import flash

logger = logging.getLogger("src.routers.admin")

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
    hard_blockers = await persona_blockers(db, agent)

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
            hard_blockers=hard_blockers,
            activation_blocked=request.query_params.get("activation_blocked"),
            valid_statuses=VALID_AGENT_STATUSES,
            available_roles=available_roles(),
            form_error=request.query_params.get("error"),
            form_version=agent_form_version(agent),
            spoke_state=spoke_state,
            requires_linked_user=requires_linked_user(agent.role),
            spoke_ok=request.query_params.get("spoke_ok"),
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
    from src.services.star_topology import ensure_star_spokes

    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if star_role(agent.role) != "spoke":
        flash(request, "Star spoke: Only pi_lab agents have star spokes.", "error")
        return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)
    try:
        report = await ensure_star_spokes(
            db, apply=True, actor=current_user, only={agent.agent_id}
        )
    except ValueError as exc:
        flash(request, "Star spoke: " + str(exc)[:200], "error")
        return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)
    await db.commit()
    if report.anomalies:
        flash(request, "Star spoke: " + "; ".join(report.anomalies)[:300], "error")
        return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)
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
    is missing, ungrounded or summary-less or its newest generation job is dead,
    unless the explicit (logged) override checkbox was posted; and, override or not,
    when its persona file ``profiles/public/<slug>.md`` is missing or has no Research
    Summary, or its owner may not use the PI surfaces
    (``agent_activation.persona_blockers``, spec 2026-10-05 §6.4). Auto-created pending
    rows (the manager Add-PI flow) made "pending" stop implying "profile exists",
    and an active agent with no exported profile is the Kavran-class failure.
    Without a rename, a refusal applies NO edits at all — the form's name/token
    changes roll back with it, so what the admin sees stays what the DB holds; a
    pending agent's rename is committed first and survives a refused activation
    (``_rename_then_activate``). After an activation commits, the persona is
    re-exported (``persona_lifecycle.export_after_lifecycle``).

    The form is guarded against staleness (RA-02): the row is locked, and a
    ``form_version`` that no longer matches ``agent_form_version`` means another
    writer changed it after the page rendered, so nothing is written. The slug is
    editable only while the agent is pending (RA-14; a rename moves the PI's persona and
    companies files, ``persona_lifecycle.rename_persona_files``), and the Slack bot token is
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
    actor_id = current_user.id
    override = bool(activation_override.strip())

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
        return await _rename_then_activate(
            request, db, agent, current_user, actor_id=actor_id, new_slug=new_slug,
            bot_name=bot_name, replace_token=replace_slack_bot_token, override=override,
        )

    activating = agent.status == "pending" or (
        agent.status != "active" and agent_status == "active"
    )
    if activating:
        blockers = await activate_agent(db, agent, actor=current_user, override=override)
        if blockers:
            flash(request, "Activation refused: " + "; ".join(blockers), "error")
            return RedirectResponse(
                url=f"/admin/agents/{agent_id}?activation_blocked=1",
                status_code=302,
            )

    owner_id = agent.user_id
    agent_role = agent.role
    agent.bot_name = bot_name.strip()
    # No path clears the token from this form; user_deletion and provisioning own that.
    if replace_slack_bot_token.strip():
        agent.slack_bot_token = replace_slack_bot_token.strip()

    if not activating and agent_status in VALID_AGENT_STATUSES:
        agent.status = agent_status

    try:
        # The lab's hub spoke goes in the same commit as the activation (ensure_lab_spoke).
        # A rename never reaches here (_rename_then_activate); the IntegrityError guard
        # stays for any unique conflict its flush surfaces.
        spoke_problems = (
            await ensure_lab_spoke(
                db, agent_id=agent.agent_id, role=agent.role, actor=current_user
            )
            if activating else []
        )
        if not spoke_problems:
            await db.commit()
    except IntegrityError:
        await db.rollback()  # a concurrent rename took the slug
        return RedirectResponse(url=f"/admin/agents/{agent_id}?error=slug_taken", status_code=302)
    if spoke_problems:
        await db.rollback()
        flash(
            request,
            "Activation refused: this lab could not be connected to the hub: "
            + "; ".join(spoke_problems)[:300],
            "error",
        )
        return RedirectResponse(
            url=f"/admin/agents/{agent_id}?activation_blocked=1", status_code=302
        )

    if activating and owner_id is not None and requires_linked_user(agent_role):
        await export_after_lifecycle(db, owner_id, event="Agent activated", actor_id=actor_id)

    return RedirectResponse(url="/admin/agents", status_code=302)


async def _rename_then_activate(
    request: Request, db: AsyncSession, agent: AgentRegistry, current_user: User, *,
    actor_id: uuid.UUID, new_slug: str, bot_name: str, replace_token: str, override: bool,
) -> RedirectResponse:
    """A pending agent's rename and activation from one POST, as two transactions (spec
    2026-10-05 §6.4, D31). The rename, with the bot-name and token edits, commits first; the
    persona and companies files then move to the new slug (``rename_persona_files``), so
    the activation gate reads the new-slug file. A refused activation keeps the rename."""
    # Every value used after a rollback is read here first: a rollback expires the
    # session's objects, and an expired attribute read is sync IO.
    agent_pk, old_slug, owner_id, role = agent.id, agent.agent_id, agent.user_id, agent.role
    agent.agent_id = new_slug
    agent.bot_name = bot_name.strip()
    # No path clears the token from this form; user_deletion and provisioning own that.
    if replace_token.strip():
        agent.slack_bot_token = replace_token.strip()
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()  # a concurrent rename took the slug
        return RedirectResponse(url=f"/admin/agents/{agent_pk}?error=slug_taken", status_code=302)
    if owner_id is not None and requires_linked_user(role):
        await rename_persona_files(db, owner_id, old_agent_id=old_slug, actor_id=actor_id)
    elif owner_id is not None:
        # The hub reads profiles/private/companies/<agent_id>.md: move it to the new id.
        await move_companies_file(db, user_id=owner_id, old_agent_id=old_slug)
    agent = (await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_pk).with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if agent is None or agent.status != "pending":
        flash(request, f"Renamed to {new_slug} (saved). The agent changed meanwhile; "
                       "it was not activated.", "error")
        return RedirectResponse(url=f"/admin/agents/{agent_pk}", status_code=302)
    blockers = await activate_agent(db, agent, actor=current_user, override=override)
    spoke = [] if blockers else await ensure_lab_spoke(
        db, agent_id=new_slug, role=role, actor=current_user,
    )
    if blockers or spoke:
        await db.rollback()
        reason = ("; ".join(blockers) if blockers
                  else "this lab could not be connected to the hub: " + "; ".join(spoke))
        flash(request, f"Renamed to {new_slug} (saved). Activation refused: {reason}"[:300],
              "error")
        return RedirectResponse(
            url=f"/admin/agents/{agent_pk}?activation_blocked=1", status_code=302,
        )
    await db.commit()
    if owner_id is not None and requires_linked_user(role):
        await export_after_lifecycle(db, owner_id, event="Agent activated", actor_id=actor_id)
    return RedirectResponse(url="/admin/agents", status_code=302)



@router.post("/agents/{agent_id}/reject")
async def admin_reject_agent(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Reject a pending agent request. Any other status is refused (C-29): an
    approved agent is parked or suspended from its edit form, not by "Reject"."""
    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    if agent.status != "pending":
        flash(
            request,
            "Only a pending request can be rejected; change an approved agent's status "
            "on its edit page.",
            "error",
        )
        return RedirectResponse(url="/admin/agents", status_code=302)
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
        flash(request, f"Slack provisioning failed: {msg[:200]}", "error")
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

    # A GET write (A-19) reachable under impersonation: the token row has no note
    # column, so the note goes on this line (A-10).
    logger.info(
        "Slack bot token stored for agent %s by %s (%s)",
        agent.agent_id, current_user.id, impersonation_note(current_user) or "direct",
    )

    if is_admin:
        flash(
            request,
            "Slack bot provisioned — the token is saved on the agent (it is not shown).",
            "success",
        )
        return RedirectResponse(url=f"/admin/agents/{agent.id}", status_code=302)
    flash(
        request,
        "Slack bot installed — the token is saved on this PI's agent. "
        "Activate the agent to bring it live.",
        "success",
    )
    if agent.user_id is None:
        return RedirectResponse(url="/manager/pis", status_code=302)
    return RedirectResponse(url=f"/manager/pis/{agent.user_id}", status_code=302)



_LINK_USER_FORM = Form("")


@router.post("/agents/{agent_id}/link")
async def admin_link_agent(
    agent_id: uuid.UUID,
    request: Request,
    user_id: str = _LINK_USER_FORM,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Link an agent to a user account (C-06).

    Refused with a flash and nothing written unless ``user_id`` names an existing
    account whose role may own a lab and that no other agent is linked to; see
    ``_link_target``. A malformed or unknown id used to surface as a 500.

    After the commit the new owner's persona is published and a file left at the slug
    archived (``persona_lifecycle.export_after_lifecycle``).
    """
    actor_id = current_user.id
    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    refusal, user = await _link_target(db, agent, user_id)
    if refusal is not None:
        flash(request, refusal, "error")
        return RedirectResponse(url="/admin/agents", status_code=302)
    agent.user_id = user.id
    try:
        await db.commit()
    except IntegrityError:
        # agents.user_id is unique: a concurrent link of the same user won.
        await db.rollback()
        flash(request, "That user is already linked to another agent.", "error")
    else:
        if requires_linked_user(agent.role):
            # The file at this slug was written for the agent's previous owner, if anyone
            # (spec 2026-10-05 §6.4, D31): archive it and publish the new owner's persona.
            await export_after_lifecycle(
                db, user.id, event="Agent linked", actor_id=actor_id, replace_leftover=True,
            )
    return RedirectResponse(url="/admin/agents", status_code=302)


async def _link_target(
    db: AsyncSession, agent: AgentRegistry, raw_user_id: str
) -> tuple[str | None, User | None]:
    """``(refusal, user)`` for the link form; exactly one is None. The role rule is
    ``User.may_use_pi_surfaces`` (PI or admin): a manager or reviewer has no lab (D7)."""
    raw = raw_user_id.strip()
    if not raw:
        return "Choose a user to link.", None
    try:
        target_id = uuid.UUID(raw)
    except ValueError:
        return "That user id is not valid.", None
    user = (
        await db.execute(select(User).where(User.id == target_id))
    ).scalar_one_or_none()
    if user is None:
        return "No such user.", None
    if not user.may_use_pi_surfaces:
        return "That account's role cannot own a lab.", None
    linked_elsewhere = await db.scalar(
        select(AgentRegistry.id).where(
            AgentRegistry.user_id == user.id, AgentRegistry.id != agent.id
        )
    )
    if linked_elsewhere is not None:
        return "That user is already linked to another agent.", None
    return None, user



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
        flash(request, "Role not changed: unknown role", "error")
        return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)

    if role != agent.role and agent.status == "active":
        blockers = await ensure_activation_allowed(
            db, agent, new_role=role, new_status="active"
        )
        if blockers:
            flash(request, "Role not changed: " + "; ".join(blockers), "error")
            return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)

    agent.role = role
    if agent.status == "active":
        spoke_problems = await ensure_lab_spoke(
            db, agent_id=agent.agent_id, role=role, actor=current_user
        )
        if spoke_problems:
            await db.rollback()
            flash(
                request,
                "Role not changed: this lab could not be connected to the hub: "
                + "; ".join(spoke_problems)[:300],
                "error",
            )
            return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)
    await db.commit()

    return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)

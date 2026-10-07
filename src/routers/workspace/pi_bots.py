"""PI-only Slack bot workspace page and write registrations."""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.role_capabilities import roles_requiring_user
from src.database import get_db
from src.dependencies import get_staff_user
from src.models import USER_ROLE_PI, AgentRegistry, User
from src.services import directory
from src.services.admin_provisioning import ProvisioningError, start_provisioning
from src.services.agent_activation import ensure_activation_allowed
from src.services.agent_mute import set_agent_mute_state
from src.services.persona_lifecycle import export_after_lifecycle
from src.services.slack_tokens import token_for_agent_row
from src.services.star_topology import ensure_lab_spoke
from src.web.flash import flash
from src.web.page_context import workspace_context
from src.web.templating import make_templates
from src.web.urls import page_url

from ._pi_common import _pending_pi_agent

router = APIRouter()
templates = make_templates()
_DB = Depends(get_db)
_STAFF = Depends(get_staff_user)


@router.get("/slack-bots", response_class=HTMLResponse, name="workspace_slack_bots")
async def workspace_slack_bots(
    request: Request, db: AsyncSession = _DB, current_user: User = _STAFF
):
    rows = (
        await db.execute(
            select(AgentRegistry, User)
            .outerjoin(User, User.id == AgentRegistry.user_id)
            .where(AgentRegistry.role.in_(roles_requiring_user()))
            .order_by(AgentRegistry.status, AgentRegistry.bot_name)
        )
    ).all()
    bots = [
        {
            "agent": agent,
            "pi": pi if pi is not None and pi.user_role == USER_ROLE_PI else None,
            "has_token": bool(token_for_agent_row(agent)),
        }
        for agent, pi in rows
    ]
    counts = {"active": 0, "pending": 0, "inactive": 0, "other": 0}
    for bot in bots:
        counts[bot["agent"].status if bot["agent"].status in counts else "other"] += 1
    return templates.TemplateResponse(
        request,
        "workspace/slack_bots.html",
        workspace_context(request, current_user, section="slack-bots", bots=bots, counts=counts),
    )


async def _manager_set_mute(
    request: Request,
    user_id: uuid.UUID,
    db: AsyncSession,
    current_user: User,
    *,
    muted: bool,
) -> RedirectResponse:
    detail = await directory.load_pi_target(db, user_id)
    if detail is None or detail["user"].user_role != USER_ROLE_PI:
        raise HTTPException(status_code=404, detail="PI not found")

    agent = (
        await db.execute(select(AgentRegistry).where(AgentRegistry.user_id == user_id))
    ).scalar_one_or_none()
    if agent is None:
        return RedirectResponse(
            url=page_url(
                request, "workspace_pi_detail", user_id=user_id, query={"error": "no_agent"}
            ),
            status_code=302,
        )

    # Read before the call: a refusal rolls ``db`` back, expiring ``current_user``.
    actor_id = current_user.id
    refusal = await set_agent_mute_state(db, agent=agent, muted=muted, actor=current_user)
    if refusal is not None:
        return RedirectResponse(
            url=page_url(request, "workspace_pi_detail", user_id=user_id, query={"error": refusal}),
            status_code=302,
        )
    if not muted:
        # The unmuted lab serves its persona again (spec 2026-10-05 §6.4, D31).
        await export_after_lifecycle(db, user_id, event="Agent unmuted", actor_id=actor_id)
    return RedirectResponse(
        url=page_url(request, "workspace_pi_detail", user_id=user_id), status_code=302
    )


@router.post("/pis/{user_id}/mute")
async def workspace_mute_pi(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Mute a PI's agent — maps to status='inactive' (design D2), not a new
    status value. No-ops (redirects with an error) if the agent doesn't
    exist or isn't currently active/inactive."""
    return await _manager_set_mute(request, user_id, db, current_user, muted=True)


@router.post("/pis/{user_id}/unmute")
async def workspace_unmute_pi(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    return await _manager_set_mute(request, user_id, db, current_user, muted=False)


@router.post("/pis/{user_id}/slack/provision")
async def workspace_provision_slack(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Create this PI's Slack app and bounce to Slack's install screen.

    Slack redirects back to ``/admin/agents/slack/callback`` — the path is
    baked into every issued manifest and cannot move — which branches its own
    redirect back to /workspace/pis for a manager caller.
    """
    agent = await _pending_pi_agent(db, user_id)
    try:
        url = await start_provisioning(db, agent, initiated_by=current_user)
    except ProvisioningError as exc:
        flash(request, f"Slack provisioning failed: {exc}", "error")
        return RedirectResponse(
            url=page_url(request, "workspace_pi_detail", user_id=user_id), status_code=302
        )
    return RedirectResponse(url=url, status_code=302)


@router.post("/pis/{user_id}/activate")
async def workspace_activate_agent(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Flip this PI's pending agent to ``active``, subject to the same gate
    ``admin_approve_agent`` applies.

    There is deliberately NO override here: the admin's "activate anyway"
    checkbox is a logged, admin-only escape hatch, and offering it on the
    manager surface would make the gate advisory for the role most likely to
    be working through a bulk onboarding list.

    The status write is ``UPDATE … WHERE status = 'pending'`` (D-07). The token
    check is ``token_for_agent_row`` (D-11), the predicate /manager/slack-bots shows.

    The gate includes the persona checks no override waives
    (``agent_activation.persona_blockers``); the persona is re-exported after the commit.
    """
    actor_id = current_user.id
    agent = await _pending_pi_agent(db, user_id)
    if not token_for_agent_row(agent):
        flash(request, "Slack provisioning failed: Install the Slack bot first.", "error")
        return RedirectResponse(
            url=page_url(request, "workspace_pi_detail", user_id=user_id), status_code=302
        )
    blockers = await ensure_activation_allowed(db, agent, new_role=agent.role, new_status="active")
    if blockers:
        # Kept from Phase 1 Task 1C-6: the hub limit and profile checks name themselves.
        flash(request, "Activation refused: " + "; ".join(blockers), "error")
        return RedirectResponse(
            url=page_url(
                request, "workspace_pi_detail", user_id=user_id, query={"activation_blocked": "1"}
            ),
            status_code=302,
        )
    # D-07: the write is conditional on the row still being pending, so a suspend
    # (or any status change) committed after _pending_pi_agent read it is never
    # overwritten. approved_at/approved_by are stamped here because this is the
    # pending -> active transition (see activate_agent's docstring).
    activated = await db.execute(
        update(AgentRegistry)
        .where(AgentRegistry.id == agent.id, AgentRegistry.status == "pending")
        .values(status="active", approved_at=datetime.now(UTC), approved_by=current_user.id)
        .execution_options(synchronize_session=False)
    )
    if activated.rowcount != 1:
        await db.rollback()
        flash(
            request,
            "This agent changed while you were activating it (someone else acted first). "
            "Reload the page and check its status.",
            "error",
        )
        return RedirectResponse(
            url=page_url(request, "workspace_pi_detail", user_id=user_id), status_code=302
        )
    # The lab's hub-{slug} spoke, in the same commit as the status flip: without it
    # the lab is isolated mid-run and the next run start fails (ensure_lab_spoke).
    spoke_problems = await ensure_lab_spoke(
        db, agent_id=agent.agent_id, role=agent.role, actor=current_user
    )
    if spoke_problems:
        await db.rollback()
        flash(
            request,
            "Activation refused: this lab could not be connected to the hub: "
            + "; ".join(spoke_problems)[:300],
            "error",
        )
        return RedirectResponse(
            url=page_url(request, "workspace_pi_detail", user_id=user_id), status_code=302
        )
    await db.commit()
    await export_after_lifecycle(db, user_id, event="Agent activated", actor_id=actor_id)
    return RedirectResponse(
        url=page_url(request, "workspace_pi_detail", user_id=user_id, query={"activated": "1"}),
        status_code=302,
    )

"""Invitation acceptance router."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models import AgentDelegate, AgentRegistry, DelegateInvitation, User

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/invite/{token}", response_class=HTMLResponse)
async def accept_invite(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Accept a delegate invitation."""
    # Look up invitation
    result = await db.execute(
        select(DelegateInvitation).where(DelegateInvitation.token == token)
    )
    invitation = result.scalar_one_or_none()

    if not invitation:
        return templates.TemplateResponse(
            request,
            "invite/error.html",
            {"request": request, "error": "This invitation link is invalid."},
        )

    # Check expiry
    if invitation.expires_at < datetime.now(timezone.utc):
        if invitation.status == "pending":
            invitation.status = "expired"
            await db.commit()
        return templates.TemplateResponse(
            request,
            "invite/error.html",
            {"request": request, "error": "This invitation has expired. Ask the PI to send a new one."},
        )

    if invitation.status != "pending":
        messages = {
            "accepted": "This invitation has already been accepted.",
            "revoked": "This invitation has been revoked by the PI.",
            "expired": "This invitation has expired. Ask the PI to send a new one.",
        }
        return templates.TemplateResponse(
            request,
            "invite/error.html",
            {"request": request, "error": messages.get(invitation.status, "This invitation is no longer valid.")},
        )

    # Valid invitation — check if user is logged in
    user_id_str = request.session.get("user_id")
    if not user_id_str:
        # Store token and redirect to login
        request.session["pending_invite_token"] = token
        return RedirectResponse(url="/login/start", status_code=302)

    # User is logged in — check onboarding
    user_result = await db.execute(
        select(User).where(User.id == user_id_str)
    )
    user = user_result.scalar_one_or_none()
    if not user:
        request.session["pending_invite_token"] = token
        return RedirectResponse(url="/login/start", status_code=302)

    if not user.onboarding_complete:
        request.session["pending_invite_token"] = token
        return RedirectResponse(url="/onboarding", status_code=302)

    # Process the acceptance
    return await _accept_invitation(invitation, user, db, request)


async def _accept_invitation(
    invitation: DelegateInvitation,
    user: User,
    db: AsyncSession,
    request: Request,
) -> RedirectResponse:
    """Create the delegation relationship and mark invitation accepted."""
    # Check if already a delegate
    existing = await db.execute(
        select(AgentDelegate).where(
            AgentDelegate.agent_registry_id == invitation.agent_registry_id,
            AgentDelegate.user_id == user.id,
        )
    )
    if existing.scalar_one_or_none():
        # Already a delegate — just mark invitation and redirect
        invitation.status = "accepted"
        invitation.accepted_by_user_id = user.id
        invitation.accepted_at = datetime.now(timezone.utc)
        await db.commit()

        # Get agent_id for redirect
        agent_result = await db.execute(
            select(AgentRegistry.agent_id).where(
                AgentRegistry.id == invitation.agent_registry_id
            )
        )
        agent_id = agent_result.scalar_one()
        return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)

    # Create delegation
    delegate = AgentDelegate(
        agent_registry_id=invitation.agent_registry_id,
        user_id=user.id,
        invitation_id=invitation.id,
    )
    db.add(delegate)

    # Mark invitation accepted
    invitation.status = "accepted"
    invitation.accepted_by_user_id = user.id
    invitation.accepted_at = datetime.now(timezone.utc)

    # Try Slack sync
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == invitation.agent_registry_id)
    )
    agent = agent_result.scalar_one()

    if user.email:
        try:
            from slack_sdk import WebClient
            from src.routers.agent_page import _get_bot_token
            bot_token = _get_bot_token()
            if bot_token:
                client = WebClient(token=bot_token)
                slack_result = client.users_lookupByEmail(email=user.email)
                sid = slack_result["user"]["id"]
                current_ids = list(agent.delegate_slack_ids or [])
                if sid not in current_ids:
                    current_ids.append(sid)
                    agent.delegate_slack_ids = current_ids
        except Exception:
            pass  # Slack sync is best-effort

    await db.commit()

    logger.info(
        "Delegate %s accepted invitation for agent %s",
        user.id, agent.agent_id,
    )

    return RedirectResponse(url=f"/agent/{agent.agent_id}/dashboard", status_code=302)

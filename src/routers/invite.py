"""Invitation acceptance router."""

import logging
from datetime import UTC, datetime

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

_INVITE_EMAIL_MISMATCH_MSG = (
    "This invitation was sent to a different email address. Please sign in with "
    "the ORCID account whose email matches the invitation."
)


def _invite_matches_user(invitation: DelegateInvitation, user: User) -> bool:
    """True only if the logged-in user's email matches the invited email.

    Binds acceptance to the invited address so a forwarded or leaked invite link
    cannot let a different logged-in account claim delegate access (read/write on
    the PI's proposals and profile). Fails closed when either address is missing.
    See SEC-6.
    """
    invited = (invitation.email or "").strip().lower()
    account = (getattr(user, "email", None) or "").strip().lower()
    return bool(invited) and bool(account) and invited == account


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
    if invitation.expires_at < datetime.now(UTC):
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

    # Bind the invite to the address it was sent to — a forwarded/leaked link
    # opened by a different account must not reach the acceptance page.
    if not _invite_matches_user(invitation, user):
        logger.warning(
            "Invite %s (for %r) opened by user %s (%r) — email mismatch",
            invitation.id, invitation.email, user.id, user.email,
        )
        return templates.TemplateResponse(
            request,
            "invite/error.html",
            {"request": request, "error": _INVITE_EMAIL_MISMATCH_MSG},
        )

    # Show confirmation page (no onboarding required for delegates)
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == invitation.agent_registry_id)
    )
    agent = agent_result.scalar_one()

    return templates.TemplateResponse(
        request,
        "invite/accept.html",
        {
            "request": request,
            "pi_name": agent.pi_name,
            "bot_name": agent.bot_name,
            "token": token,
            "invitation_email": invitation.email,
        },
    )


@router.post("/invite/{token}/accept", response_class=HTMLResponse)
async def confirm_accept_invite(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Process explicit acceptance of a delegate invitation."""
    result = await db.execute(
        select(DelegateInvitation).where(DelegateInvitation.token == token)
    )
    invitation = result.scalar_one_or_none()

    if not invitation or invitation.status != "pending":
        return templates.TemplateResponse(
            request,
            "invite/error.html",
            {"request": request, "error": "This invitation is no longer valid."},
        )

    if invitation.expires_at < datetime.now(UTC):
        invitation.status = "expired"
        await db.commit()
        return templates.TemplateResponse(
            request,
            "invite/error.html",
            {"request": request, "error": "This invitation has expired. Ask the PI to send a new one."},
        )

    user_id_str = request.session.get("user_id")
    if not user_id_str:
        return RedirectResponse(url=f"/invite/{token}", status_code=302)

    user_result = await db.execute(select(User).where(User.id == user_id_str))
    user = user_result.scalar_one_or_none()
    if not user:
        return RedirectResponse(url=f"/invite/{token}", status_code=302)

    return await _accept_invitation(invitation, user, db, request)


async def _accept_invitation(
    invitation: DelegateInvitation,
    user: User,
    db: AsyncSession,
    request: Request,
) -> HTMLResponse | RedirectResponse:
    """Create the delegation relationship and mark invitation accepted."""
    # Enforce the email binding at the mutation chokepoint (defense in depth
    # behind the GET-side check): never grant delegate access to an account
    # whose email differs from the invited address. See SEC-6.
    if not _invite_matches_user(invitation, user):
        logger.warning(
            "Rejecting invite acceptance: invitation %s for %r, user %s has %r",
            invitation.id, invitation.email, user.id, user.email,
        )
        return templates.TemplateResponse(
            request,
            "invite/error.html",
            {"request": request, "error": _INVITE_EMAIL_MISMATCH_MSG},
        )

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
        invitation.accepted_at = datetime.now(UTC)
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
    invitation.accepted_at = datetime.now(UTC)

    # Try Slack sync
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == invitation.agent_registry_id)
    )
    agent = agent_result.scalar_one()

    if user.email:
        try:
            from src.services.slack_tokens import token_for_agent_row
            from src.services.slack_web import lookup_user_by_email

            bot_token = token_for_agent_row(agent)
            if bot_token:
                sid = lookup_user_by_email(bot_token, user.email)
                if sid:
                    current_ids = list(agent.delegate_slack_ids or [])
                    if sid not in current_ids:
                        current_ids.append(sid)
                        agent.delegate_slack_ids = current_ids
        except Exception as exc:
            # Best-effort by design (specs/web-delegates.md §Slack Linkage): a
            # delegate is useful without a Slack id. But LOG it — a bare `pass`
            # here hid an ImportError for an unknown length of time, and the
            # whole sync was dead code with nothing to show for it.
            logger.warning(
                "Delegate Slack-ID sync failed for agent %s: %s", agent.agent_id, exc
            )

    await db.commit()

    logger.info(
        "Delegate %s accepted invitation for agent %s",
        user.id, agent.agent_id,
    )

    return RedirectResponse(url=f"/agent/{agent.agent_id}/dashboard", status_code=302)

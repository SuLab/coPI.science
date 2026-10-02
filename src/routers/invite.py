"""Invitation acceptance router."""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user, refuse_impersonation
from src.models import AgentDelegate, AgentRegistry, DelegateInvitation, User
from src.web.templating import make_templates

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()

_INVITE_EMAIL_MISMATCH_MSG = (
    "This invitation was sent to a different email address. Please sign in with "
    "the ORCID account whose email matches the invitation."
)
_INVITE_UNVERIFIED_MSG = (
    "An administrator must verify your email address before you can accept this "
    "invitation."
)
_INVITE_NOT_PI_MSG = "Only a PI account can accept a delegate invitation."
_INVITE_IMPERSONATION_DETAIL = "Invitations cannot be accepted while impersonating."


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


def _invite_refusal(invitation: DelegateInvitation, user: User) -> str | None:
    """Why ``user`` may not accept ``invitation``, or None when they may.

    Spec 2026-10-01 §6.6: a PI-surface account (D-06: a manager or reviewer has no
    lab to delegate into), holding the invited address (SEC-6), verified by an
    administrator or manager (A-04: ``users.email`` is user-editable and the
    verification is cleared whenever it changes). Every branch refuses; the order
    only picks the most useful message.
    """
    if not user.may_use_pi_surfaces:
        return _INVITE_NOT_PI_MSG
    if not _invite_matches_user(invitation, user):
        return _INVITE_EMAIL_MISMATCH_MSG
    if user.email_verified_at is None:
        return _INVITE_UNVERIFIED_MSG
    return None


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

    # Valid invitation. An anonymous visitor signs in first and is brought back
    # here: the token survives the login's session reset
    # (src/routers/auth.py::_start_fresh_session).
    if not request.session.get("user_id"):
        request.session["pending_invite_token"] = token
        return RedirectResponse(url="/login/start", status_code=302)

    # A signed-in visitor is resolved through get_current_user (A-09), so a
    # denied, pending, signed-out-elsewhere or deleted account is bounced exactly
    # as on every other page rather than read from the raw session.
    user = await get_current_user(request, db)
    refuse_impersonation(user, _INVITE_IMPERSONATION_DETAIL)

    # Bind the invite to the address it was sent to — a forwarded/leaked link
    # opened by a different account must not reach the acceptance page.
    refusal = _invite_refusal(invitation, user)
    if refusal is not None:
        logger.warning(
            "Invite %s (for %r) refused for user %s (%r): %s",
            invitation.id, invitation.email, user.id, user.email, refusal,
        )
        return templates.TemplateResponse(
            request,
            "invite/error.html",
            {"request": request, "error": refusal},
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

    if not request.session.get("user_id"):
        return RedirectResponse(url=f"/invite/{token}", status_code=302)

    user = await get_current_user(request, db)
    refuse_impersonation(user, _INVITE_IMPERSONATION_DETAIL)

    return await _accept_invitation(invitation, user, db, request)


async def _accept_invitation(
    invitation: DelegateInvitation,
    user: User,
    db: AsyncSession,
    request: Request,
) -> HTMLResponse | RedirectResponse:
    """Create the delegation relationship and mark invitation accepted."""
    # Enforce the binding at the mutation chokepoint (defense in depth behind the
    # GET-side check): a PI-surface account whose verified address is the invited
    # one, or nothing. See SEC-6 and spec 2026-10-01 §6.6.
    refusal = _invite_refusal(invitation, user)
    if refusal is not None:
        logger.warning(
            "Rejecting invite acceptance: invitation %s for %r, user %s (%r): %s",
            invitation.id, invitation.email, user.id, user.email, refusal,
        )
        return templates.TemplateResponse(
            request,
            "invite/error.html",
            {"request": request, "error": refusal},
        )

    # Claim the invitation with a conditional UPDATE: of two racing accepts only one
    # matches status = 'pending'. The delegate insert is ON CONFLICT DO NOTHING on
    # the (agent, user) unique constraint, so a user who is already a delegate via
    # another invitation is not an error either (RB-10).
    claimed = (await db.execute(
        update(DelegateInvitation)
        .where(DelegateInvitation.id == invitation.id, DelegateInvitation.status == "pending")
        .values(status="accepted", accepted_by_user_id=user.id, accepted_at=datetime.now(UTC))
        .returning(DelegateInvitation.id)
    )).scalar_one_or_none()
    agent = (await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == invitation.agent_registry_id)
    )).scalar_one()
    if claimed is None:
        # Another request accepted (or revoked) it first; the delegation, if any, exists.
        # Read the slug before the rollback, which expires every loaded attribute.
        agent_slug = agent.agent_id
        await db.rollback()
        return RedirectResponse(url=f"/agent/{agent_slug}/dashboard", status_code=302)
    await db.execute(
        pg_insert(AgentDelegate)
        .values(id=uuid.uuid4(), agent_registry_id=invitation.agent_registry_id,
                user_id=user.id, invitation_id=invitation.id)
        .on_conflict_do_nothing(constraint="uq_agent_delegate_agent_user")
    )
    await db.commit()

    logger.info(
        "Delegate %s accepted invitation for agent %s",
        user.id, agent.agent_id,
    )

    return RedirectResponse(url=f"/agent/{agent.agent_id}/dashboard", status_code=302)

"""My Agent page router."""

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database import get_db
from src.dependencies import get_agent_with_access, get_current_user
from src.models import (
    AgentDelegate,
    AgentMessage,
    AgentRegistry,
    ProposalReview,
    ResearcherProfile,
    ThreadDecision,
    User,
)
from src.services.profile_export import export_private_profile

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

PROFILES_DIR = Path("profiles")
SLACK_INVITE_URL = (
    "https://join.slack.com/t/labbot-workspace/shared_invite/"
    "zt-3sxfrrisw-t4hRz4aMfZZPxThxUaTGKA"
)


def _template_context(request: Request, user: User, **kwargs) -> dict:
    impersonated = getattr(user, "_is_impersonated", False)
    real_admin = getattr(user, "_real_admin", None)
    ctx = {
        "request": request,
        "current_user": real_admin if impersonated else user,
        "user": user,
        "impersonation_banner": user if impersonated else None,
        "active_page": "agent",
    }
    ctx.update(kwargs)
    return ctx


# --------------------------------------------------------------------------
# Landing page — agent listing / auto-redirect
# --------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def agent_landing(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Agent landing page — lists all agents the user has access to."""
    # Own agent
    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    own_agent = result.scalar_one_or_none()

    # Delegated agents
    delegated_result = await db.execute(
        select(AgentRegistry)
        .join(AgentDelegate, AgentDelegate.agent_registry_id == AgentRegistry.id)
        .where(AgentDelegate.user_id == current_user.id)
    )
    delegated_agents = delegated_result.scalars().all()

    # Collect all accessible agents
    all_agents = []
    if own_agent:
        all_agents.append(own_agent)
    all_agents.extend(delegated_agents)

    # Auto-redirect if exactly one agent and it's active
    if len(all_agents) == 1 and all_agents[0].status == "active":
        return RedirectResponse(
            url=f"/agent/{all_agents[0].agent_id}/dashboard", status_code=302
        )

    # No agents at all — show request page
    if not all_agents:
        has_profile = (
            current_user.onboarding_complete
            and current_user.profile
            and current_user.profile.research_summary
        )
        return templates.TemplateResponse(
            request,
            "agent/request.html",
            _template_context(
                request, current_user, agent=None, has_profile=has_profile
            ),
        )

    # Single agent but pending — show request page
    if len(all_agents) == 1 and own_agent and own_agent.status == "pending":
        return templates.TemplateResponse(
            request,
            "agent/request.html",
            _template_context(request, current_user, agent=own_agent),
        )

    # Multiple agents (or single delegated) — show listing
    return templates.TemplateResponse(
        request,
        "agent/listing.html",
        _template_context(
            request,
            current_user,
            own_agent=own_agent,
            delegated_agents=delegated_agents,
        ),
    )


# --------------------------------------------------------------------------
# Agent dashboard
# --------------------------------------------------------------------------


@router.get("/{agent_id}/dashboard", response_class=HTMLResponse)
async def agent_dashboard(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Agent dashboard — shows stats, proposals, and settings."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)

    if agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    aid = agent.agent_id
    slack_error = request.query_params.get("slack_error")

    # Stats
    posts_count_result = await db.execute(
        select(func.count(AgentMessage.id)).where(
            AgentMessage.agent_id == aid,
            AgentMessage.phase == "new_post",
        )
    )
    posts_count = posts_count_result.scalar() or 0

    threads_count_result = await db.execute(
        select(func.count(distinct(AgentMessage.thread_ts))).where(
            AgentMessage.agent_id == aid,
            AgentMessage.phase == "thread_reply",
        )
    )
    threads_count = threads_count_result.scalar() or 0

    # Proposals where this agent is involved
    proposals_result = await db.execute(
        select(ThreadDecision)
        .where(
            ThreadDecision.outcome == "proposal",
            (ThreadDecision.agent_a == aid) | (ThreadDecision.agent_b == aid),
        )
        .order_by(ThreadDecision.decided_at.desc())
    )
    proposals = proposals_result.scalars().all()

    # Get existing reviews by this agent
    reviewed_ids_result = await db.execute(
        select(ProposalReview.thread_decision_id).where(
            ProposalReview.agent_id == aid
        )
    )
    reviewed_ids = {r[0] for r in reviewed_ids_result}

    # Separate into reviewed and unreviewed
    unreviewed = []
    reviewed = []
    for p in proposals:
        other = p.agent_b if p.agent_a == aid else p.agent_a
        entry = {"proposal": p, "other_agent": other}
        if p.id in reviewed_ids:
            rev_result = await db.execute(
                select(ProposalReview).where(
                    ProposalReview.thread_decision_id == p.id,
                    ProposalReview.agent_id == aid,
                )
            )
            entry["review"] = rev_result.scalar_one_or_none()
            reviewed.append(entry)
        else:
            unreviewed.append(entry)

    # Private profile path
    private_profile_path = PROFILES_DIR / "private" / f"{aid}.md"
    has_private_profile = private_profile_path.exists()

    # Resolve delegate display names (legacy Slack-only delegates)
    delegates = []
    if agent.delegate_slack_ids:
        delegates = _resolve_delegate_names(agent.delegate_slack_ids)

    # Pending invitations (for PI view)
    from src.models import DelegateInvitation
    pending_invitations = []
    if is_owner:
        pending_result = await db.execute(
            select(DelegateInvitation).where(
                DelegateInvitation.agent_registry_id == agent.id,
                DelegateInvitation.status == "pending",
            ).order_by(DelegateInvitation.created_at.desc())
        )
        pending_invitations = pending_result.scalars().all()

    # Web delegates
    web_delegates_result = await db.execute(
        select(AgentDelegate)
        .options(selectinload(AgentDelegate.user))
        .where(AgentDelegate.agent_registry_id == agent.id)
    )
    web_delegates = web_delegates_result.scalars().all()

    # Check if current delegate user has Slack linked
    delegate_has_slack = True
    if not is_owner:
        delegate_slack_ids = agent.delegate_slack_ids or []
        # Check if any of the delegate's possible Slack IDs are in the list
        # For now, we check by trying to find their user in the web delegates
        delegate_has_slack = any(
            _user_slack_id_in_list(wd.user, delegate_slack_ids)
            for wd in web_delegates
            if wd.user_id == current_user.id
        )

    return templates.TemplateResponse(
        request,
        "agent/dashboard.html",
        _template_context(
            request,
            current_user,
            agent=agent,
            is_owner=is_owner,
            posts_count=posts_count,
            threads_count=threads_count,
            proposals_total=len(proposals),
            unreviewed=unreviewed,
            reviewed=reviewed,
            has_private_profile=has_private_profile,
            slack_invite_url=SLACK_INVITE_URL,
            slack_error=slack_error,
            delegates=delegates,
            web_delegates=web_delegates,
            pending_invitations=pending_invitations,
            delegate_has_slack=delegate_has_slack,
            delegate_error=request.query_params.get("delegate_error"),
        ),
    )


def _user_slack_id_in_list(user: User, slack_ids: list[str]) -> bool:
    """Check if a user's email maps to any Slack ID in the list (heuristic)."""
    # We can't check without calling Slack API, so for now always return False
    # This gets properly resolved in Step 5 (Slack sync)
    return False


# --------------------------------------------------------------------------
# Request an agent
# --------------------------------------------------------------------------


@router.post("/request")
async def request_agent(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Submit an agent request."""
    if not current_user.onboarding_complete or not current_user.profile:
        raise HTTPException(status_code=400, detail="Complete your profile first")

    existing = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    if existing.scalar_one_or_none():
        return RedirectResponse(url="/agent", status_code=302)

    last_name = current_user.name.split()[-1].lower()
    agent_id = "".join(c for c in last_name if c.isalpha())

    collision = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
    )
    if collision.scalar_one_or_none():
        first_initial = current_user.name[0].lower()
        agent_id = f"{first_initial}{agent_id}"

    agent = AgentRegistry(
        agent_id=agent_id,
        user_id=current_user.id,
        bot_name=f"{current_user.name.split()[-1]}Bot",
        pi_name=current_user.name,
        status="pending",
    )
    db.add(agent)
    await db.commit()

    return RedirectResponse(url="/agent", status_code=302)


# --------------------------------------------------------------------------
# Proposal review
# --------------------------------------------------------------------------


@router.post("/{agent_id}/proposals/{thread_decision_id}/review")
async def review_proposal(
    agent_id: str,
    thread_decision_id: uuid.UUID,
    request: Request,
    rating: int = Form(...),
    comment: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Rate a proposal (1-4)."""
    if rating < 1 or rating > 4:
        raise HTTPException(status_code=400, detail="Rating must be 1-4")

    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)

    td_result = await db.execute(
        select(ThreadDecision).where(ThreadDecision.id == thread_decision_id)
    )
    td = td_result.scalar_one_or_none()
    if not td:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if agent.agent_id not in (td.agent_a, td.agent_b):
        raise HTTPException(status_code=403, detail="Not your proposal")

    existing = await db.execute(
        select(ProposalReview).where(
            ProposalReview.thread_decision_id == thread_decision_id,
            ProposalReview.agent_id == agent.agent_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Already reviewed")

    review = ProposalReview(
        thread_decision_id=thread_decision_id,
        agent_id=agent.agent_id,
        user_id=agent.user_id,  # Always the PI
        delegate_user_id=current_user.id if not is_owner else None,
        rating=rating,
        comment=comment.strip() or None,
    )
    db.add(review)
    await db.commit()

    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)


@router.post("/{agent_id}/proposals/{thread_decision_id}/reopen")
async def reopen_proposal(
    agent_id: str,
    thread_decision_id: uuid.UUID,
    request: Request,
    guidance: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reopen a proposal thread with PI guidance posted via the bot."""
    guidance = guidance.strip()
    if not guidance:
        raise HTTPException(status_code=400, detail="Guidance text is required")

    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)

    td_result = await db.execute(
        select(ThreadDecision).where(ThreadDecision.id == thread_decision_id)
    )
    td = td_result.scalar_one_or_none()
    if not td:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if agent.agent_id not in (td.agent_a, td.agent_b):
        raise HTTPException(status_code=403, detail="Not your proposal")

    # Post guidance to Slack thread via the agent's bot token
    try:
        from slack_sdk import WebClient
        from src.config import get_settings
        settings = get_settings()
        env_tokens = settings.get_slack_tokens()
        bot_token = env_tokens.get(agent.agent_id, {}).get("bot")

        if not bot_token or bot_token.startswith("xoxb-placeholder"):
            raise HTTPException(status_code=500, detail="No bot token available")

        client = WebClient(token=bot_token)

        channels_result = client.conversations_list(types="public_channel,private_channel", limit=200)
        channel_id = None
        for ch in channels_result.get("channels", []):
            if ch["name"] == td.channel:
                channel_id = ch["id"]
                break

        if not channel_id:
            raise HTTPException(status_code=500, detail=f"Channel #{td.channel} not found")

        message = f"*PI guidance from {current_user.name}:*\n\n{guidance}"
        client.chat_postMessage(
            channel=channel_id,
            text=message,
            thread_ts=td.thread_id,
        )
        logger.info(
            "PI %s posted guidance in proposal thread %s via %s",
            current_user.name, td.thread_id, agent.agent_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to post PI guidance to Slack: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to post to Slack: {str(exc)[:100]}")

    existing = await db.execute(
        select(ProposalReview).where(
            ProposalReview.thread_decision_id == thread_decision_id,
            ProposalReview.agent_id == agent.agent_id,
        )
    )
    if not existing.scalar_one_or_none():
        review = ProposalReview(
            thread_decision_id=thread_decision_id,
            agent_id=agent.agent_id,
            user_id=agent.user_id,  # Always the PI
            delegate_user_id=current_user.id if not is_owner else None,
            rating=0,  # 0 = reopened with guidance, not a rating
            comment=f"[Reopened] {guidance[:500]}",
        )
        db.add(review)
        await db.commit()

    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)


# --------------------------------------------------------------------------
# Private profile view/edit
# --------------------------------------------------------------------------


@router.get("/{agent_id}/profile", response_class=HTMLResponse)
async def view_private_profile(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """View agent's private profile."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    profile_path = PROFILES_DIR / "private" / f"{agent.agent_id}.md"
    content = profile_path.read_text() if profile_path.exists() else ""

    return templates.TemplateResponse(
        request,
        "agent/profile.html",
        _template_context(
            request, current_user, agent=agent, is_owner=is_owner,
            profile_content=content, editing=False,
        ),
    )


@router.get("/{agent_id}/profile/edit", response_class=HTMLResponse)
async def edit_private_profile(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit agent's private profile."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    profile_path = PROFILES_DIR / "private" / f"{agent.agent_id}.md"
    content = profile_path.read_text() if profile_path.exists() else ""

    return templates.TemplateResponse(
        request,
        "agent/profile.html",
        _template_context(
            request, current_user, agent=agent, is_owner=is_owner,
            profile_content=content, editing=True,
        ),
    )


@router.post("/{agent_id}/profile/save")
async def save_private_profile(
    agent_id: str,
    request: Request,
    content: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save private profile to disk and database."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    profile_path = PROFILES_DIR / "private" / f"{agent.agent_id}.md"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(content)

    # Persist to DB — use the PI's user_id, not the delegate's
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == agent.user_id)
    )
    profile = profile_result.scalar_one_or_none()
    if profile:
        profile.private_profile_md = content.strip() or None
        await db.commit()

    return RedirectResponse(url=f"/agent/{agent_id}/profile", status_code=302)


# --------------------------------------------------------------------------
# Slack connection (PI only)
# --------------------------------------------------------------------------


@router.post("/{agent_id}/slack")
async def connect_slack(
    agent_id: str,
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Look up the PI's Slack user ID from their email address."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if not is_owner:
        raise HTTPException(status_code=403, detail="Only the PI can connect Slack")

    email = email.strip()
    slack_user_id = None
    error = None

    try:
        from slack_sdk import WebClient
        from src.config import get_settings
        settings = get_settings()
        env_tokens = settings.get_slack_tokens()

        bot_token = None
        for tokens in env_tokens.values():
            t = tokens.get("bot", "")
            if t and not t.startswith("xoxb-placeholder"):
                bot_token = t
                break

        if not bot_token:
            error = "No Slack bot token available to perform lookup."
        else:
            client = WebClient(token=bot_token)
            result = client.users_lookupByEmail(email=email)
            slack_user_id = result["user"]["id"]
    except Exception as exc:
        error_msg = str(exc)
        if "users_not_found" in error_msg:
            error = f"No Slack user found with email {email}. Have you joined the workspace first?"
        else:
            logger.warning("Slack lookup failed for %s: %s", email, exc)
            error = f"Slack lookup failed: {error_msg[:100]}"

    if slack_user_id:
        agent.slack_user_id = slack_user_id
        await db.commit()
        return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)

    return RedirectResponse(
        url=f"/agent/{agent_id}/dashboard?slack_error=" + (error or "Unknown error"),
        status_code=302,
    )


def _get_bot_token() -> str | None:
    """Get the first valid Slack bot token for API calls."""
    from src.config import get_settings
    settings = get_settings()
    env_tokens = settings.get_slack_tokens()
    for tokens in env_tokens.values():
        t = tokens.get("bot", "")
        if t and not t.startswith("xoxb-placeholder"):
            return t
    return None


def _resolve_delegate_names(slack_ids: list[str]) -> list[dict]:
    """Resolve Slack user IDs to display names."""
    from slack_sdk import WebClient
    bot_token = _get_bot_token()
    if not bot_token:
        return [{"slack_id": sid, "name": sid} for sid in slack_ids]

    client = WebClient(token=bot_token)
    delegates = []
    for sid in slack_ids:
        try:
            info = client.users_info(user=sid)
            name = info["user"].get("real_name") or info["user"].get("name") or sid
            delegates.append({"slack_id": sid, "name": name})
        except Exception:
            delegates.append({"slack_id": sid, "name": sid})
    return delegates


# --------------------------------------------------------------------------
# Delegate Slack connection
# --------------------------------------------------------------------------


@router.post("/{agent_id}/delegates/connect-slack")
async def delegate_connect_slack(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Let a delegate link their Slack account to this agent."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)

    if not current_user.email:
        return RedirectResponse(
            url=f"/agent/{agent_id}/dashboard?slack_error=No email on your account.",
            status_code=302,
        )

    error = None
    try:
        from slack_sdk import WebClient
        bot_token = _get_bot_token()
        if not bot_token:
            error = "No Slack bot token available."
        else:
            client = WebClient(token=bot_token)
            result = client.users_lookupByEmail(email=current_user.email)
            sid = result["user"]["id"]
            current_ids = list(agent.delegate_slack_ids or [])
            if sid not in current_ids:
                current_ids.append(sid)
                agent.delegate_slack_ids = current_ids
                await db.commit()
            return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)
    except Exception as exc:
        error_msg = str(exc)
        if "users_not_found" in error_msg:
            error = f"No Slack account found for {current_user.email}. Please join the workspace first."
        else:
            error = f"Slack lookup failed: {error_msg[:100]}"

    return RedirectResponse(
        url=f"/agent/{agent_id}/dashboard?slack_error=" + (error or "Unknown error"),
        status_code=302,
    )


# --------------------------------------------------------------------------
# Delegate management — invitation-based
# --------------------------------------------------------------------------


@router.post("/{agent_id}/delegates/invite")
async def invite_delegate(
    agent_id: str,
    request: Request,
    emails: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send delegate invitation(s) by email."""
    import re
    import secrets
    from datetime import datetime, timedelta, timezone

    from src.config import get_settings
    from src.models import DelegateInvitation
    from src.services.email import send_delegate_invitation

    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if not is_owner:
        raise HTTPException(status_code=403, detail="Only the PI can manage delegates")
    if agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    settings = get_settings()

    # Parse comma/newline-separated emails
    email_list = [
        e.strip().lower()
        for e in re.split(r"[,\n]+", emails)
        if e.strip()
    ]

    errors = []
    sent_count = 0
    for email in email_list:
        # Basic validation
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            errors.append(f"Invalid email: {email}")
            continue

        # Don't invite yourself
        if current_user.email and email == current_user.email.lower():
            errors.append("You can't invite yourself.")
            continue

        # Check if already an active delegate
        existing_delegate = await db.execute(
            select(AgentDelegate)
            .join(User, AgentDelegate.user_id == User.id)
            .where(
                AgentDelegate.agent_registry_id == agent.id,
                func.lower(User.email) == email,
            )
        )
        if existing_delegate.scalar_one_or_none():
            errors.append(f"{email} is already a delegate.")
            continue

        # Check for pending invitation
        existing_invite = await db.execute(
            select(DelegateInvitation).where(
                DelegateInvitation.agent_registry_id == agent.id,
                DelegateInvitation.email == email,
                DelegateInvitation.status == "pending",
            )
        )
        if existing_invite.scalar_one_or_none():
            errors.append(f"Invitation already pending for {email}.")
            continue

        # Create invitation
        token = secrets.token_urlsafe(48)
        invitation = DelegateInvitation(
            agent_registry_id=agent.id,
            invited_by_user_id=current_user.id,
            email=email,
            token=token,
            status="pending",
            expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        db.add(invitation)
        await db.flush()  # Get the ID

        # Send email (non-blocking — invitation is created regardless)
        invite_url = f"{settings.base_url}/invite/{token}"
        send_delegate_invitation(email, agent.pi_name, agent.bot_name, invite_url)
        sent_count += 1

    await db.commit()

    error_msg = "; ".join(errors) if errors else ""
    if error_msg:
        return RedirectResponse(
            url=f"/agent/{agent_id}/dashboard?delegate_error={error_msg}",
            status_code=302,
        )
    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)


@router.post("/{agent_id}/delegates/{invitation_id}/revoke")
async def revoke_invitation(
    agent_id: str,
    invitation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Revoke a pending delegate invitation."""
    from src.models import DelegateInvitation

    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if not is_owner:
        raise HTTPException(status_code=403, detail="Only the PI can manage delegates")

    result = await db.execute(
        select(DelegateInvitation).where(
            DelegateInvitation.id == invitation_id,
            DelegateInvitation.agent_registry_id == agent.id,
            DelegateInvitation.status == "pending",
        )
    )
    invitation = result.scalar_one_or_none()
    if invitation:
        invitation.status = "revoked"
        await db.commit()

    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)


@router.post("/{agent_id}/delegates/{delegate_id}/remove")
async def remove_delegate(
    agent_id: str,
    delegate_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove an active delegate."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if not is_owner:
        raise HTTPException(status_code=403, detail="Only the PI can manage delegates")

    result = await db.execute(
        select(AgentDelegate)
        .options(selectinload(AgentDelegate.user))
        .where(
            AgentDelegate.id == delegate_id,
            AgentDelegate.agent_registry_id == agent.id,
        )
    )
    delegate = result.scalar_one_or_none()
    if delegate:
        # Remove Slack ID if present
        if delegate.user.email and agent.delegate_slack_ids:
            try:
                from slack_sdk import WebClient
                bot_token = _get_bot_token()
                if bot_token:
                    client = WebClient(token=bot_token)
                    slack_result = client.users_lookupByEmail(email=delegate.user.email)
                    sid = slack_result["user"]["id"]
                    current_ids = list(agent.delegate_slack_ids or [])
                    if sid in current_ids:
                        current_ids.remove(sid)
                        agent.delegate_slack_ids = current_ids if current_ids else None
            except Exception:
                pass  # Slack sync is best-effort

        await db.delete(delegate)
        await db.commit()
        logger.info(
            "Delegate %s removed from agent %s by %s",
            delegate.user_id, agent.agent_id, current_user.name,
        )

    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)

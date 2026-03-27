"""My Agent page router."""

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user
from src.models import (
    AgentMessage,
    AgentRegistry,
    ProposalReview,
    ThreadDecision,
    User,
)

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
# Main dashboard
# --------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def my_agent(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """My Agent page — dispatches to one of three states."""
    # Look up agent record for this user
    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    agent = result.scalar_one_or_none()

    if not agent:
        # State 1: No agent
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

    if agent.status == "pending":
        # State 2: Pending approval
        return templates.TemplateResponse(
            request,
            "agent/request.html",
            _template_context(request, current_user, agent=agent),
        )

    # State 3: Active agent — show dashboard
    aid = agent.agent_id

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
            # Get the review
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

    return templates.TemplateResponse(
        request,
        "agent/dashboard.html",
        _template_context(
            request,
            current_user,
            agent=agent,
            posts_count=posts_count,
            threads_count=threads_count,
            proposals_total=len(proposals),
            unreviewed=unreviewed,
            reviewed=reviewed,
            has_private_profile=has_private_profile,
            slack_invite_url=SLACK_INVITE_URL,
        ),
    )


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
    # Must have complete profile
    if not current_user.onboarding_complete or not current_user.profile:
        raise HTTPException(status_code=400, detail="Complete your profile first")

    # Check if already has an agent
    existing = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    if existing.scalar_one_or_none():
        return RedirectResponse(url="/agent", status_code=302)

    # Create pending agent request
    agent = AgentRegistry(
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


@router.post("/proposals/{thread_decision_id}/review")
async def review_proposal(
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

    # Get agent for current user
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="No agent found")

    # Verify the thread decision exists and involves this agent
    td_result = await db.execute(
        select(ThreadDecision).where(ThreadDecision.id == thread_decision_id)
    )
    td = td_result.scalar_one_or_none()
    if not td:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if agent.agent_id not in (td.agent_a, td.agent_b):
        raise HTTPException(status_code=403, detail="Not your proposal")

    # Check for existing review
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
        user_id=current_user.id,
        rating=rating,
        comment=comment.strip() or None,
    )
    db.add(review)
    await db.commit()

    return RedirectResponse(url="/agent", status_code=302)


# --------------------------------------------------------------------------
# Private profile view/edit
# --------------------------------------------------------------------------


@router.get("/profile", response_class=HTMLResponse)
async def view_private_profile(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """View agent's private profile."""
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent or agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    profile_path = PROFILES_DIR / "private" / f"{agent.agent_id}.md"
    content = profile_path.read_text() if profile_path.exists() else ""

    return templates.TemplateResponse(
        request,
        "agent/profile.html",
        _template_context(
            request, current_user, agent=agent, profile_content=content, editing=False
        ),
    )


@router.get("/profile/edit", response_class=HTMLResponse)
async def edit_private_profile(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit agent's private profile."""
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent or agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    profile_path = PROFILES_DIR / "private" / f"{agent.agent_id}.md"
    content = profile_path.read_text() if profile_path.exists() else ""

    return templates.TemplateResponse(
        request,
        "agent/profile.html",
        _template_context(
            request, current_user, agent=agent, profile_content=content, editing=True
        ),
    )


@router.post("/profile/save")
async def save_private_profile(
    request: Request,
    content: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save private profile to disk."""
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent or agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    profile_path = PROFILES_DIR / "private" / f"{agent.agent_id}.md"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(content)

    return RedirectResponse(url="/agent/profile", status_code=302)


# --------------------------------------------------------------------------
# Slack username
# --------------------------------------------------------------------------


@router.post("/slack")
async def save_slack_username(
    request: Request,
    slack_user_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save the PI's Slack username/ID."""
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    agent = agent_result.scalar_one_or_none()
    if not agent:
        return RedirectResponse(url="/agent", status_code=302)

    agent.slack_user_id = slack_user_id.strip()
    await db.commit()

    return RedirectResponse(url="/agent", status_code=302)

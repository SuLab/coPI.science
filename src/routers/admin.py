"""Admin dashboard router."""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database import get_db
from src.dependencies import get_admin_user, get_current_user
from src.models import (
    AgentChannel,
    AgentMessage,
    Job,
    Publication,
    ResearcherProfile,
    SimulationRun,
    User,
)
from src.services.orcid import fetch_orcid_profile

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _template_context(request: Request, current_user: User, **kwargs) -> dict:
    ctx = {
        "request": request,
        "current_user": current_user,
        "active_page": "admin",
    }
    ctx.update(kwargs)
    return ctx


@router.get("", response_class=HTMLResponse)
@router.get("/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    status_filter: str | None = None,
    institution_filter: str | None = None,
    claimed_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Admin users overview."""
    query = select(User).options(selectinload(User.profile), selectinload(User.jobs))

    result = await db.execute(query)
    users = result.scalars().unique().all()

    # Get publication counts
    pub_counts_result = await db.execute(
        select(Publication.user_id, func.count(Publication.id).label("count"))
        .group_by(Publication.user_id)
    )
    pub_counts = {str(r.user_id): r.count for r in pub_counts_result}

    user_data = []
    for user in users:
        profile = user.profile
        pub_count = pub_counts.get(str(user.id), 0)

        # Profile status
        if not profile:
            profile_status = "no_profile"
        elif profile.pending_profile:
            profile_status = "pending_update"
        elif profile.research_summary:
            profile_status = "complete"
        else:
            # Check if there's a running job
            active_jobs = [j for j in user.jobs if j.status in ("pending", "processing")]
            profile_status = "generating" if active_jobs else "no_profile"

        # Apply filters
        if status_filter and profile_status != status_filter:
            continue
        if institution_filter and (not user.institution or institution_filter.lower() not in user.institution.lower()):
            continue
        if claimed_filter == "claimed" and not user.claimed_at:
            continue
        if claimed_filter == "unclaimed" and user.claimed_at:
            continue

        user_data.append({
            "user": user,
            "profile": profile,
            "profile_status": profile_status,
            "pub_count": pub_count,
        })

    return templates.TemplateResponse(
        "admin/users.html",
        _template_context(
            request,
            current_user,
            user_data=user_data,
            status_filter=status_filter,
            institution_filter=institution_filter,
            claimed_filter=claimed_filter,
        ),
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def admin_user_detail(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Admin user detail page."""
    result = await db.execute(
        select(User)
        .where(User.id == user_id)
        .options(selectinload(User.profile), selectinload(User.jobs))
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    pub_result = await db.execute(
        select(Publication)
        .where(Publication.user_id == user_id)
        .order_by(Publication.year.desc())
    )
    publications = pub_result.scalars().all()

    return templates.TemplateResponse(
        "admin/user_detail.html",
        _template_context(
            request,
            current_user,
            target_user=user,
            profile=user.profile,
            publications=publications,
            jobs=sorted(user.jobs, key=lambda j: j.enqueued_at, reverse=True),
        ),
    )


@router.get("/jobs", response_class=HTMLResponse)
async def admin_jobs(
    request: Request,
    status_filter: str | None = None,
    type_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Job queue overview."""
    query = select(Job).options(selectinload(Job.user)).order_by(Job.enqueued_at.desc())
    result = await db.execute(query)
    all_jobs = result.scalars().unique().all()

    # Filter
    jobs = []
    for job in all_jobs:
        if status_filter and job.status != status_filter:
            continue
        if type_filter and job.type != type_filter:
            continue
        jobs.append(job)

    # Summary counts
    counts = {}
    for job in all_jobs:
        counts[job.status] = counts.get(job.status, 0) + 1

    return templates.TemplateResponse(
        "admin/jobs.html",
        _template_context(
            request,
            current_user,
            jobs=jobs,
            counts=counts,
            status_filter=status_filter,
            type_filter=type_filter,
        ),
    )


@router.get("/activity", response_class=HTMLResponse)
async def admin_activity(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Agent activity overview."""
    runs_result = await db.execute(
        select(SimulationRun).order_by(SimulationRun.started_at.desc())
    )
    runs = runs_result.scalars().all()

    # Summary stats
    total_messages_result = await db.execute(
        select(func.sum(SimulationRun.total_messages))
    )
    total_messages = total_messages_result.scalar() or 0

    total_channels_result = await db.execute(
        select(func.count(AgentChannel.id))
    )
    total_channels = total_channels_result.scalar() or 0

    # Most active agent
    agent_count_result = await db.execute(
        select(AgentMessage.agent_id, func.count(AgentMessage.id).label("count"))
        .group_by(AgentMessage.agent_id)
        .order_by(func.count(AgentMessage.id).desc())
        .limit(1)
    )
    most_active = agent_count_result.first()

    return templates.TemplateResponse(
        "admin/activity.html",
        _template_context(
            request,
            current_user,
            runs=runs,
            total_runs=len(runs),
            total_messages=total_messages,
            total_channels=total_channels,
            most_active_agent=most_active.agent_id if most_active else None,
            most_active_count=most_active.count if most_active else 0,
        ),
    )


@router.get("/activity/{run_id}", response_class=HTMLResponse)
async def admin_activity_detail(
    run_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Simulation run detail."""
    run_result = await db.execute(
        select(SimulationRun).where(SimulationRun.id == run_id)
    )
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Messages for this run
    messages_result = await db.execute(
        select(AgentMessage)
        .where(AgentMessage.simulation_run_id == run_id)
        .order_by(AgentMessage.created_at)
    )
    messages = messages_result.scalars().all()

    # Channels for this run
    channels_result = await db.execute(
        select(AgentChannel).where(AgentChannel.simulation_run_id == run_id)
    )
    channels = channels_result.scalars().all()

    # Aggregate by agent
    agent_stats: dict[str, dict] = {}
    for msg in messages:
        if msg.agent_id not in agent_stats:
            agent_stats[msg.agent_id] = {"count": 0, "total_length": 0}
        agent_stats[msg.agent_id]["count"] += 1
        agent_stats[msg.agent_id]["total_length"] += msg.message_length

    for agent_id, stats in agent_stats.items():
        stats["avg_length"] = (
            stats["total_length"] // stats["count"] if stats["count"] > 0 else 0
        )

    # Aggregate by channel
    channel_stats: dict[str, dict] = {}
    for msg in messages:
        if msg.channel_name not in channel_stats:
            channel_stats[msg.channel_name] = {"count": 0, "agents": set()}
        channel_stats[msg.channel_name]["count"] += 1
        channel_stats[msg.channel_name]["agents"].add(msg.agent_id)

    return templates.TemplateResponse(
        "admin/activity_detail.html",
        _template_context(
            request,
            current_user,
            run=run,
            messages=messages,
            channels=channels,
            agent_stats=agent_stats,
            channel_stats=channel_stats,
        ),
    )


@router.post("/api/admin/impersonate")
async def impersonate_user(
    request: Request,
    orcid: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Start impersonating a user by ORCID."""
    # Security: this route requires admin
    orcid = orcid.strip()

    result = await db.execute(select(User).where(User.orcid == orcid))
    target = result.scalar_one_or_none()

    if not target:
        # Try to fetch from ORCID and create unclaimed record
        try:
            profile_data = await fetch_orcid_profile(orcid)
            target = User(
                orcid=orcid,
                name=profile_data.get("name", orcid),
                email=profile_data.get("email"),
                institution=profile_data.get("institution"),
                department=profile_data.get("department"),
            )
            db.add(target)
            await db.commit()
        except Exception as exc:
            logger.error("Failed to fetch ORCID profile for impersonation: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"User with ORCID {orcid} not found",
            )

    response = RedirectResponse(url="/", status_code=302)
    # httpOnly cookie, 24h expiry
    response.set_cookie(
        "copi-impersonate",
        str(target.id),
        max_age=86400,
        httponly=True,
        samesite="lax",
        secure=not request.app.state.allow_http if hasattr(request.app.state, "allow_http") else False,
    )
    return response


@router.post("/api/admin/impersonate/stop")
async def stop_impersonating(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Stop impersonating — clear the impersonate cookie."""
    response = RedirectResponse(url="/admin/users", status_code=302)
    response.delete_cookie("copi-impersonate")
    return response

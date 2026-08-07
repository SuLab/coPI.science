"""Admin dashboard router."""

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.agent.roles import available_roles
from src.config import get_settings
from src.database import get_db
from src.dependencies import get_admin_user, get_current_user
from src.models import (
    COHORT_ACTION_AGENT_ADDED,
    COHORT_ACTION_AGENT_REMOVED,
    COHORT_ACTION_CREATED,
    COHORT_ACTION_DELETED,
    COHORT_ACTION_TOPOLOGY_SNAPSHOT,
    AccessAllowlist,
    AgentChannel,
    AgentMessage,
    AgentRegistry,
    Cohort,
    CohortAuditEvent,
    CohortMembership,
    Job,
    LlmCallLog,
    OpportunityAssessment,
    Publication,
    ResearcherProfile,
    SimulationRun,
    ThreadDecision,
    User,
    WaitlistSignup,
)
from src.services.blackbird_rubric import RUBRIC_WEIGHTS
from src.services.cohorts import (
    compute_gates,
    record_cohort_audit_event,
    summarise_gates,
)
from src.services.orcid import fetch_orcid_profile
from src.services.validators import csv_safe_cell

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

# Valid AgentRegistry.status values (see src/models/agent_registry.py). Admins
# can move an already-approved agent between these from the edit page; the sim
# runs status=='active' agents (others are parked/excluded, reversibly).
VALID_AGENT_STATUSES = ("active", "inactive", "suspended", "pending")


def _template_context(
    request: Request, current_user: User, active_admin: str = "", **kwargs
) -> dict:
    ctx = {
        "request": request,
        "current_user": current_user,
        "active_page": "admin",
        "active_admin": active_admin,
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
    query = select(User).options(selectinload(User.profile), selectinload(User.jobs), selectinload(User.agent))

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

        # Agent status
        if not user.agent:
            agent_status = "not_requested"
        elif user.agent.status == "pending":
            agent_status = "awaiting_token"
        else:
            agent_status = user.agent.status  # "active" or "suspended"

        user_data.append({
            "user": user,
            "profile": profile,
            "profile_status": profile_status,
            "pub_count": pub_count,
            "agent_status": agent_status,
        })

    return templates.TemplateResponse(
        request,
        "admin/users.html",
        _template_context(
            request,
            current_user,
            active_admin="users",
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
        request,
        "admin/user_detail.html",
        _template_context(
            request,
            current_user,
            active_admin="users",
            target_user=user,
            profile=user.profile,
            publications=publications,
            jobs=sorted(user.jobs, key=lambda j: j.enqueued_at, reverse=True),
        ),
    )


@router.post("/users/{user_id}/delete")
async def admin_delete_user(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Delete a user account (admin only)."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    name = user.name
    await db.delete(user)
    await db.commit()
    logger.info("Admin %s deleted user %s (%s)", current_user.name, name, user_id)
    return RedirectResponse(url="/admin/users", status_code=302)


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
        request,
        "admin/jobs.html",
        _template_context(
            request,
            current_user,
            active_admin="jobs",
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
        request,
        "admin/activity.html",
        _template_context(
            request,
            current_user,
            active_admin="activity",
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
    #
    # The agent add is None-guarded: `agent_id` is nullable on agent_messages
    # and really is NULL in production — _rebuild_state_from_slack records a
    # real Slack message whose sender maps to no known bot as
    # `is_bot=True, agent_id=NULL`. This set is sorted() in the template
    # (activity_detail.html), so an unguarded add of a single None took the
    # whole page down with "'<' not supported between instances of
    # 'NoneType' and 'str'" — the same bug class fixed for /admin/discussions
    # in 73a78c3.
    channel_stats: dict[str, dict] = {}
    for msg in messages:
        if msg.channel_name not in channel_stats:
            channel_stats[msg.channel_name] = {"count": 0, "agents": set()}
        channel_stats[msg.channel_name]["count"] += 1
        if msg.agent_id:
            channel_stats[msg.channel_name]["agents"].add(msg.agent_id)

    return templates.TemplateResponse(
        request,
        "admin/activity_detail.html",
        _template_context(
            request,
            current_user,
            active_admin="activity",
            run=run,
            messages=messages,
            channels=channels,
            agent_stats=agent_stats,
            channel_stats=channel_stats,
        ),
    )


@router.get("/activity/{run_id}/llm-calls", response_class=HTMLResponse)
async def admin_llm_calls(
    run_id: uuid.UUID,
    request: Request,
    agent: str | None = None,
    phase: str | None = None,
    model: str | None = None,
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """View LLM call logs for a simulation run."""
    # Verify run exists
    run_result = await db.execute(
        select(SimulationRun).where(SimulationRun.id == run_id)
    )
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Build filtered query
    query = select(LlmCallLog).where(LlmCallLog.simulation_run_id == run_id)
    if agent:
        query = query.where(LlmCallLog.agent_id == agent)
    if phase:
        query = query.where(LlmCallLog.phase == phase)
    if model:
        query = query.where(LlmCallLog.model.contains(model))

    # Total count for pagination
    from sqlalchemy import func as sa_func

    count_query = select(sa_func.count()).select_from(query.subquery())
    total_count = (await db.execute(count_query)).scalar() or 0

    # Paginate
    page_size = 50
    offset = (page - 1) * page_size
    query = query.order_by(LlmCallLog.created_at).offset(offset).limit(page_size)
    logs_result = await db.execute(query)
    logs = logs_result.scalars().all()

    total_pages = max(1, (total_count + page_size - 1) // page_size)

    # Summary stats for this run (unfiltered)
    stats_result = await db.execute(
        select(
            sa_func.count(LlmCallLog.id).label("total_calls"),
            sa_func.sum(LlmCallLog.input_tokens).label("total_input_tokens"),
            sa_func.sum(LlmCallLog.output_tokens).label("total_output_tokens"),
            sa_func.avg(LlmCallLog.latency_ms).label("avg_latency_ms"),
        ).where(LlmCallLog.simulation_run_id == run_id)
    )
    stats = stats_result.first()

    # Model breakdown
    model_breakdown_result = await db.execute(
        select(LlmCallLog.model, sa_func.count(LlmCallLog.id).label("count"))
        .where(LlmCallLog.simulation_run_id == run_id)
        .group_by(LlmCallLog.model)
    )
    model_breakdown = {r.model: r.count for r in model_breakdown_result}

    # Distinct agents and phases for filter dropdowns
    agents_result = await db.execute(
        select(LlmCallLog.agent_id)
        .where(LlmCallLog.simulation_run_id == run_id)
        .distinct()
    )
    available_agents = sorted([r[0] for r in agents_result])

    phases_result = await db.execute(
        select(LlmCallLog.phase)
        .where(LlmCallLog.simulation_run_id == run_id)
        .distinct()
    )
    available_phases = sorted([r[0] for r in phases_result])

    return templates.TemplateResponse(
        request,
        "admin/llm_calls.html",
        _template_context(
            request,
            current_user,
            active_admin="activity",
            run=run,
            logs=logs,
            total_count=total_count,
            page=page,
            total_pages=total_pages,
            page_size=page_size,
            total_calls=stats.total_calls or 0,
            total_input_tokens=stats.total_input_tokens or 0,
            total_output_tokens=stats.total_output_tokens or 0,
            avg_latency_ms=round(stats.avg_latency_ms or 0, 1),
            model_breakdown=model_breakdown,
            available_agents=available_agents,
            available_phases=available_phases,
            filter_agent=agent,
            filter_phase=phase,
            filter_model=model,
        ),
    )


@router.get("/discussions", response_class=HTMLResponse)
async def admin_discussions(
    request: Request,
    run_id: str | None = None,
    channel_filter: str | None = None,
    status_filter: str | None = None,
    agent_filter: list[str] = Query(default=[]),
    export: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Discussion summary: threads grouped by status."""

    # Pick which simulation run to show
    runs_result = await db.execute(
        select(SimulationRun).order_by(SimulationRun.started_at.desc())
    )
    runs = runs_result.scalars().all()

    show_all_runs = run_id == "all"
    selected_run_id = "all" if show_all_runs else None
    if not show_all_runs and run_id:
        try:
            selected_run_id = uuid.UUID(run_id)
        except ValueError:
            pass
    if not selected_run_id and runs:
        selected_run_id = runs[0].id

    if not selected_run_id:
        return templates.TemplateResponse(
            request,
            "admin/discussions.html",
            _template_context(
                request,
                current_user,
                active_admin="discussions",
                runs=runs,
                selected_run_id=None,
                threads=[],
                counts={},
                channels=[],
                channel_filter=channel_filter,
                status_filter=status_filter,
            ),
        )

    # Get all root posts (new_post phase, no thread_ts)
    roots_query = select(AgentMessage).where(
        AgentMessage.phase == "new_post",
        AgentMessage.thread_ts.is_(None),
    )
    if not show_all_runs:
        roots_query = roots_query.where(AgentMessage.simulation_run_id == selected_run_id)
    roots_result = await db.execute(roots_query.order_by(AgentMessage.created_at)
    )
    root_posts = roots_result.scalars().all()

    # Get reply counts and replier agent IDs per thread
    reply_query = select(
        AgentMessage.thread_ts,
        func.count(AgentMessage.id).label("reply_count"),
    ).where(AgentMessage.phase == "thread_reply")
    if not show_all_runs:
        reply_query = reply_query.where(AgentMessage.simulation_run_id == selected_run_id)
    reply_counts_result = await db.execute(reply_query.group_by(AgentMessage.thread_ts))
    reply_count_map = {r.thread_ts: r.reply_count for r in reply_counts_result}

    # Get distinct replier agent IDs per thread
    replier_query = select(AgentMessage.thread_ts, AgentMessage.agent_id).where(
        AgentMessage.phase == "thread_reply",
    )
    if not show_all_runs:
        replier_query = replier_query.where(AgentMessage.simulation_run_id == selected_run_id)
    repliers_result = await db.execute(replier_query.distinct())
    replier_map: dict[str, set[str]] = {}
    for r in repliers_result:
        replier_map.setdefault(r.thread_ts, set()).add(r.agent_id)

    # Get thread decisions
    decisions_query = select(ThreadDecision)
    if not show_all_runs:
        decisions_query = decisions_query.where(ThreadDecision.simulation_run_id == selected_run_id)
    decisions_result = await db.execute(decisions_query.order_by(ThreadDecision.decided_at))
    all_decisions = decisions_result.scalars().all()

    # Build a map: thread_id -> final outcome (last decision wins)
    decision_map: dict[str, ThreadDecision] = {}
    for d in all_decisions:
        decision_map[d.thread_id] = d

    # Build thread list
    threads = []
    available_channels = set()
    for post in root_posts:
        ts = post.message_ts
        available_channels.add(post.channel_name)
        reply_count = reply_count_map.get(ts, 0)
        repliers = replier_map.get(ts, set())
        decision = decision_map.get(ts)

        # Find the other agent (replier who isn't the poster)
        other_agents = repliers - {post.agent_id}
        replier = next(iter(other_agents), None) if other_agents else None

        if decision:
            if decision.outcome == "proposal":
                thread_status = "proposal"
            elif decision.outcome == "no_proposal":
                thread_status = "no_proposal"
            elif decision.outcome == "timeout":
                thread_status = "timeout"
            else:
                thread_status = decision.outcome
        elif reply_count > 0:
            thread_status = "active"
        else:
            thread_status = "no_replies"

        threads.append({
            "message_ts": ts,
            "channel_name": post.channel_name,
            "agent_id": post.agent_id,
            "created_at": post.created_at,
            "reply_count": reply_count,
            "replier": replier,
            "status": thread_status,
            "decision": decision,
        })

    # Apply filters
    if channel_filter:
        threads = [t for t in threads if t["channel_name"] == channel_filter]
    if status_filter:
        threads = [t for t in threads if t["status"] == status_filter]

    # Get proposal reviews
    from src.models import ProposalReview as PR
    reviews_query = select(PR).join(ThreadDecision, PR.thread_decision_id == ThreadDecision.id)
    if not show_all_runs:
        reviews_query = reviews_query.where(ThreadDecision.simulation_run_id == selected_run_id)
    reviews_result = await db.execute(reviews_query.order_by(PR.reviewed_at))
    all_reviews = reviews_result.scalars().all()
    reviews_by_decision: dict[str, list] = {}
    for rev in all_reviews:
        reviews_by_decision.setdefault(str(rev.thread_decision_id), []).append(rev)

    # Attach reviews to threads
    for t in threads:
        if t["decision"]:
            t["reviews"] = reviews_by_decision.get(str(t["decision"].id), [])
        else:
            t["reviews"] = []

    # Add orphaned decisions (thread_decisions with no matching root post in agent_messages)
    known_thread_ids = {t["message_ts"] for t in threads}
    for td in all_decisions:
        if td.thread_id not in known_thread_ids:
            other_agents = replier_map.get(td.thread_id, set())
            poster_id = td.agent_a
            replier = td.agent_b if td.agent_a == poster_id else td.agent_a
            threads.append({
                "message_ts": td.thread_id,
                "channel_name": td.channel,
                "agent_id": poster_id,
                "created_at": td.decided_at,
                "reply_count": reply_count_map.get(td.thread_id, 0),
                "replier": replier,
                "status": td.outcome,
                "decision": td,
                "reviews": reviews_by_decision.get(str(td.id), []),
            })
            known_thread_ids.add(td.thread_id)
            available_channels.add(td.channel)

    # Count by status (before filtering)
    counts: dict[str, int] = {}
    for t in threads:
        s = t["status"]
        counts[s] = counts.get(s, 0) + 1

    # Collect available agents from threads.
    #
    # Every add is None-guarded, including the poster's. `agent_id` is nullable
    # on agent_messages and really is NULL in production: _rebuild_state_from_slack
    # records a real Slack message whose sender maps to no known bot as
    # `is_bot=True, agent_id=NULL` (measured: 7 rows, all from one raw Slack user
    # id). This set is sorted() below, so a single None took the whole page down
    # with "'<' not supported between instances of 'NoneType' and 'str'". The
    # replier and decision adds were already guarded; the poster's was not.
    available_agents = set()
    for t in threads:
        for candidate in (
            t["agent_id"],
            t.get("replier"),
            t["decision"].agent_a if t.get("decision") else None,
            t["decision"].agent_b if t.get("decision") else None,
        ):
            if candidate:
                available_agents.add(candidate)

    # Apply filters
    if channel_filter:
        threads = [t for t in threads if t["channel_name"] == channel_filter]
    if status_filter:
        threads = [t for t in threads if t["status"] == status_filter]
    if agent_filter:
        agent_set = set(agent_filter)
        threads = [
            t for t in threads
            if t["agent_id"] in agent_set
            or (t.get("replier") and t["replier"] in agent_set)
            or (t.get("decision") and (
                t["decision"].agent_a in agent_set or t["decision"].agent_b in agent_set
            ))
        ]

    if export:
        proposals = []
        for t in threads:
            d = t.get("decision")
            if not d or not d.summary_text:
                continue
            proposals.append({
                "channel": t["channel_name"],
                "agent_a": d.agent_a,
                "agent_b": d.agent_b,
                "outcome": d.outcome,
                "date": d.decided_at.strftime("%Y-%m-%d %H:%M UTC"),
                "summary": d.summary_text.strip(),
            })

        if export == "html":
            return templates.TemplateResponse(
                request,
                "admin/discussions_export.html",
                {"request": request, "proposals": proposals},
                headers={"Content-Disposition": "attachment; filename=proposals.html"},
            )

        # Default: plain text
        from fastapi.responses import PlainTextResponse
        lines = []
        for p in proposals:
            lines.append(f"{'=' * 72}")
            lines.append(f"Channel: #{p['channel']}")
            lines.append(f"Agents: {p['agent_a'].capitalize()}Bot + {p['agent_b'].capitalize()}Bot")
            lines.append(f"Outcome: {p['outcome']}")
            lines.append(f"Date: {p['date']}")
            lines.append("")
            lines.append(p["summary"])
            lines.append("")
        if not lines:
            lines.append("No proposals found with current filters.")
        return PlainTextResponse(
            "\n".join(lines),
            headers={"Content-Disposition": "attachment; filename=proposals.txt"},
        )

    return templates.TemplateResponse(
        request,
        "admin/discussions.html",
        _template_context(
            request,
            current_user,
            active_admin="discussions",
            runs=runs,
            selected_run_id=selected_run_id,
            threads=threads,
            counts=counts,
            channels=sorted(available_channels),
            agents=sorted(available_agents),
            channel_filter=channel_filter,
            status_filter=status_filter,
            agent_filter=agent_filter or [],
        ),
    )


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

    # Get linked user names
    user_map = {}
    for agent in agents:
        if agent.user_id:
            u_result = await db.execute(select(User).where(User.id == agent.user_id))
            u = u_result.scalar_one_or_none()
            if u:
                user_map[str(agent.user_id)] = u.name

    # Get all users for the linking dropdown
    users_result = await db.execute(select(User).order_by(User.name))
    all_users = users_result.scalars().all()

    # Which agents have a usable bot token (DB column preferred, .env fallback).
    from src.services.slack_tokens import token_for_agent_row
    env_token_agents = {
        a.agent_id for a in agents if token_for_agent_row(a)
    }

    # Count unreviewed proposals per agent
    from src.models import ProposalReview
    proposal_counts: dict[str, int] = {}
    review_counts: dict[str, int] = {}
    for agent in agents:
        aid = agent.agent_id
        total_result = await db.execute(
            select(func.count(ThreadDecision.id)).where(
                ThreadDecision.outcome == "proposal",
                (ThreadDecision.agent_a == aid) | (ThreadDecision.agent_b == aid),
            )
        )
        proposal_counts[aid] = total_result.scalar() or 0
        rev_result = await db.execute(
            select(func.count(ProposalReview.id)).where(
                ProposalReview.agent_id == aid,
            )
        )
        review_counts[aid] = rev_result.scalar() or 0

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
            proposal_counts=proposal_counts,
            review_counts=review_counts,
        ),
    )


# Hard cap on rows fetched for one render of the triage queue (B1). Scoped to
# the current run this is rarely close to binding — a single run's worth of
# :mag: assessments — but "All Runs" accumulates across every run this
# instance has ever done, and the table has no other bound. Capped rather
# than paginated because this is a triage queue: the highest-scoring rows
# (the ones that matter) are always first under the existing ORDER BY, so a
# cap only ever drops the least-actionable tail, and the "N of TOTAL" note
# below says so rather than hiding the truncation.
_ASSESSMENTS_LIMIT = 500


@router.get("/assessments", response_class=HTMLResponse)
async def admin_assessments(
    request: Request,
    run_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """BlackbirdBot's screening verdicts against the Blackbird investment rubric.

    Ordered by weighted score descending (NULLs last), then most-recent-first,
    so the advance/conditional candidates are what a human sees on arrival —
    this page is a triage queue, not a log.

    Defaults to the CURRENT simulation run (the most recently started
    ``SimulationRun``) — ``?run_id=all`` or picking an older run from the
    dropdown reaches everything else; nothing is ever deleted from this view,
    only filtered. This is deliberate, not incidental: ``--fresh``
    (``src/agent/main.py``) wipes ``agent_messages``/``agent_channels`` but
    NEVER ``opportunity_assessments`` — a screening verdict is a durable
    record and losing one is worse than keeping a stale one — so after a
    fresh restart, old assessments whose Slack messages no longer exist would
    otherwise sit on this page with nothing to distinguish them from current
    ones. Scoping to the latest run excludes those by construction (their
    ``simulation_run_id`` is the run that got wiped), while the "All Runs"
    escape hatch and the per-run dropdown keep every row reachable. Mirrors
    the run-selector pattern already used by ``admin_discussions``.
    """
    runs_result = await db.execute(
        select(SimulationRun).order_by(SimulationRun.started_at.desc())
    )
    runs = runs_result.scalars().all()

    show_all_runs = run_id == "all"
    selected_run_id: uuid.UUID | str | None = "all" if show_all_runs else None
    if not show_all_runs and run_id:
        try:
            selected_run_id = uuid.UUID(run_id)
        except ValueError:
            pass
    if not selected_run_id and runs:
        selected_run_id = runs[0].id

    query = select(OpportunityAssessment)
    if not show_all_runs and selected_run_id:
        query = query.where(OpportunityAssessment.simulation_run_id == selected_run_id)

    total_count = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar() or 0

    # NULLS LAST needs saying: a bare .desc() puts NULLs FIRST in Postgres,
    # which would float every not-yet-scored assessment to the top of a
    # triage queue instead of to the bottom.
    query = query.order_by(
        OpportunityAssessment.weighted_score.desc().nullslast(),
        OpportunityAssessment.created_at.desc(),
    ).limit(_ASSESSMENTS_LIMIT)

    result = await db.execute(query)
    assessments = result.scalars().all()

    return templates.TemplateResponse(
        request,
        "admin/assessments.html",
        _template_context(
            request,
            current_user,
            active_admin="assessments",
            assessments=assessments,
            # Passed so the detail row can render the nine dimensions in
            # descending rubric weight rather than dict order, and can show an
            # unscored dimension as a gap. An unscored dimension counts as zero
            # in the weighted score (src/services/blackbird_rubric.py), so a
            # reader needs to see which ones were never answered.
            rubric_weights=RUBRIC_WEIGHTS,
            runs=runs,
            runs_by_id={r.id: r for r in runs},
            selected_run_id=selected_run_id,
            show_all_runs=show_all_runs,
            total_count=total_count,
            assessments_limit=_ASSESSMENTS_LIMIT,
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

    return templates.TemplateResponse(
        request,
        "admin/agent_detail.html",
        _template_context(
            request,
            current_user,
            active_admin="agents",
            agent=agent,
            linked_user=linked_user,
            valid_statuses=VALID_AGENT_STATUSES,
            available_roles=available_roles(),
            slack_error=request.query_params.get("slack_error"),
            slack_ok=request.query_params.get("slack_ok"),
            role_error=request.query_params.get("role_error"),
        ),
    )


@router.post("/agents/{agent_id}/approve")
async def admin_approve_agent(
    agent_id: uuid.UUID,
    request: Request,
    agent_slug: str = Form(...),
    bot_name: str = Form(...),
    slack_bot_token: str = Form(""),
    agent_status: str = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Approve a pending agent request, or save edits to an existing agent.

    Pending agents are approved straight to ``active`` (stamping the approver),
    matching the original one-click flow. For an agent that's already been
    approved, the edit form's status dropdown drives ``status`` — letting an
    admin park (``inactive``), ``suspended``, or re-``active``ate it. A running
    simulation picks the change up live via ``_sync_roster_from_db``.
    """
    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent.agent_id = agent_slug.strip().lower()
    agent.bot_name = bot_name.strip()
    agent.slack_bot_token = slack_bot_token.strip() or None

    if agent.status == "pending":
        agent.status = "active"
        agent.approved_at = datetime.now(UTC)
        agent.approved_by = current_user.id
    elif agent_status in VALID_AGENT_STATUSES:
        agent.status = agent_status

    await db.commit()

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
        oauth_url = await start_provisioning(db, agent)
    except ProvisioningError as exc:
        return RedirectResponse(
            url=f"/admin/agents/{agent_id}?slack_error={str(exc)[:200]}",
            status_code=302,
        )
    return RedirectResponse(url=oauth_url, status_code=302)


@router.get("/agents/slack/callback")
async def admin_provision_slack_callback(
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """OAuth redirect target: exchange the code for a bot token and store it on
    the agent, then return to the agent's approve page with the token filled."""
    from src.services.admin_provisioning import ProvisioningError, complete_provisioning

    if error:
        return RedirectResponse(
            url=f"/admin/agents?slack_error=Slack returned: {error}", status_code=302
        )
    if not code or not state:
        return RedirectResponse(
            url="/admin/agents?slack_error=Missing code or state from Slack",
            status_code=302,
        )

    try:
        agent = await complete_provisioning(db, state, code)
    except ProvisioningError as exc:
        return RedirectResponse(
            url=f"/admin/agents?slack_error={str(exc)[:200]}", status_code=302
        )
    return RedirectResponse(url=f"/admin/agents/{agent.id}?slack_ok=1", status_code=302)


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

    agent.role = role
    await db.commit()

    return RedirectResponse(url=f"/admin/agents/{agent_id}", status_code=302)


@router.post("/impersonate")
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
            await db.flush()  # get target.id
            job = Job(
                type="generate_profile",
                user_id=target.id,
                payload={"user_id": str(target.id), "orcid": orcid},
            )
            db.add(job)
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


@router.post("/impersonate/stop")
async def stop_impersonating(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Stop impersonating — clear the impersonate cookie."""
    response = RedirectResponse(url="/admin/users", status_code=302)
    response.delete_cookie("copi-impersonate")
    return response


# ---------------------------------------------------------------------------
# Access requests + allowlist
# ---------------------------------------------------------------------------


@router.get("/access-requests", response_class=HTMLResponse)
async def admin_access_requests(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """List pending/allowed/denied users and manage the allowlist."""
    pending_result = await db.execute(
        select(User).where(User.access_status == "pending").order_by(User.created_at.desc())
    )
    pending = pending_result.scalars().all()

    denied_result = await db.execute(
        select(User).where(User.access_status == "denied").order_by(User.created_at.desc())
    )
    denied = denied_result.scalars().all()

    recent_allowed_result = await db.execute(
        select(User)
        .where(User.access_status == "allowed")
        .order_by(User.updated_at.desc())
        .limit(25)
    )
    recent_allowed = recent_allowed_result.scalars().all()

    allowlist_result = await db.execute(
        select(AccessAllowlist).order_by(AccessAllowlist.created_at.desc())
    )
    allowlist = allowlist_result.scalars().all()

    return templates.TemplateResponse(
        request,
        "admin/access_requests.html",
        _template_context(
            request,
            current_user,
            active_admin="access",
            pending=pending,
            denied=denied,
            recent_allowed=recent_allowed,
            allowlist=allowlist,
        ),
    )


@router.post("/access-requests/{user_id}/approve")
async def admin_approve_access(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Approve a pending user; enqueue profile job if needed."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.access_status = "allowed"

    profile_result = await db.execute(
        select(ResearcherProfile.id).where(ResearcherProfile.user_id == user.id)
    )
    if profile_result.scalar_one_or_none() is None:
        db.add(
            Job(
                type="generate_profile",
                user_id=user.id,
                payload={"user_id": str(user.id), "orcid": user.orcid},
            )
        )

    await db.commit()
    logger.info("Admin %s approved access for user %s", current_user.name, user.id)
    return RedirectResponse(url="/admin/access-requests", status_code=302)


@router.post("/access-requests/{user_id}/deny")
async def admin_deny_access(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Deny a pending user."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.access_status = "denied"
    await db.commit()
    logger.info("Admin %s denied access for user %s", current_user.name, user.id)
    return RedirectResponse(url="/admin/access-requests", status_code=302)


@router.post("/access-allowlist/add")
async def admin_allowlist_add(
    request: Request,
    orcid: str = Form(...),
    note: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Add an ORCID to the allowlist."""
    orcid_clean = orcid.strip()
    if not orcid_clean:
        return RedirectResponse(url="/admin/access-requests", status_code=302)

    existing = await db.execute(
        select(AccessAllowlist).where(AccessAllowlist.orcid == orcid_clean)
    )
    if existing.scalar_one_or_none() is None:
        db.add(
            AccessAllowlist(
                orcid=orcid_clean,
                note=note.strip() or None,
                added_by_user_id=current_user.id,
            )
        )

    # If a user with this ORCID already exists and is pending, promote them.
    user_result = await db.execute(select(User).where(User.orcid == orcid_clean))
    user = user_result.scalar_one_or_none()
    if user and user.access_status != "allowed":
        user.access_status = "allowed"
        profile_result = await db.execute(
            select(ResearcherProfile.id).where(ResearcherProfile.user_id == user.id)
        )
        if profile_result.scalar_one_or_none() is None:
            db.add(
                Job(
                    type="generate_profile",
                    user_id=user.id,
                    payload={"user_id": str(user.id), "orcid": user.orcid},
                )
            )

    await db.commit()
    return RedirectResponse(url="/admin/access-requests", status_code=302)


@router.post("/access-allowlist/{entry_id}/remove")
async def admin_allowlist_remove(
    entry_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Remove an ORCID from the allowlist."""
    result = await db.execute(
        select(AccessAllowlist).where(AccessAllowlist.id == entry_id)
    )
    entry = result.scalar_one_or_none()
    if entry:
        await db.delete(entry)
        await db.commit()
    return RedirectResponse(url="/admin/access-requests", status_code=302)


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------


@router.get("/waitlist", response_class=HTMLResponse)
async def admin_waitlist(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """List waitlist signups."""
    result = await db.execute(
        select(WaitlistSignup).order_by(WaitlistSignup.created_at.desc())
    )
    signups = result.scalars().all()

    return templates.TemplateResponse(
        request,
        "admin/waitlist.html",
        _template_context(
            request,
            current_user,
            active_admin="waitlist",
            signups=signups,
        ),
    )


@router.get("/waitlist/export")
async def admin_waitlist_export(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """CSV export of waitlist signups."""
    import csv
    import io

    from fastapi.responses import Response

    result = await db.execute(
        select(WaitlistSignup).order_by(WaitlistSignup.created_at.desc())
    )
    signups = result.scalars().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["email", "name", "institution", "note", "created_at", "contacted_at"])
    for s in signups:
        # All four text fields are attacker-controlled (public waitlist form),
        # so neutralize CSV formula/DDE injection before export (SEC-20). The
        # timestamps are app-generated ISO strings and safe as-is.
        writer.writerow(
            [
                csv_safe_cell(s.email),
                csv_safe_cell(s.name or ""),
                csv_safe_cell(s.institution or ""),
                csv_safe_cell((s.note or "").replace("\n", " ")),
                s.created_at.isoformat() if s.created_at else "",
                s.contacted_at.isoformat() if s.contacted_at else "",
            ]
        )

    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=waitlist.csv"},
    )


@router.post("/waitlist/{signup_id}/mark-contacted")
async def admin_waitlist_mark_contacted(
    signup_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Mark a waitlist signup as contacted."""
    result = await db.execute(
        select(WaitlistSignup).where(WaitlistSignup.id == signup_id)
    )
    signup = result.scalar_one_or_none()
    if signup:
        signup.contacted_at = datetime.now(UTC)
        await db.commit()
    return RedirectResponse(url="/admin/waitlist", status_code=302)


# ---------------------------------------------------------------------------
# Cohorts — admin-managed groups gating which agents interact during simulation.
#
# The gate is an agent-BEHAVIOUR filter, never access control: it changes what an
# agent acts on, never what a human can read. Nothing in this section may be reused
# to scope a PI-facing view. See .notes/cohort-system-v2.md §6.2.
#
# Enforcement only happens in the running simulation, and only when
# settings.cohort_isolation_enabled is True. Membership edits are picked up live on
# the engine's roster-sync cadence (~30s) — no restart. Filtering is forward-only:
# adding an agent to a cohort does not reveal the backlog it missed while excluded,
# because the agent's cursor has already advanced past it (v2 §6.3).
# ---------------------------------------------------------------------------

# Cohort name: lowercase alphanumeric + hyphens, max 48 chars (slug style).
_COHORT_NAME_RE = re.compile(r"^[a-z0-9-]{1,48}$")

# Starlette's request.form() defaults to max_fields=1000. The topology matrix
# posts one marker per rendered row and column plus one value per ticked cell,
# so the payload is agents + cohorts + ticked — but "ticked" alone will pass
# 1,000 on a large enough roster, so the limit is raised rather than relied on.
_TOPOLOGY_MAX_FIELDS = 50_000


async def _cohort_gate_context(db: AsyncSession) -> dict[str, Any]:
    """Preview of the gate the engine will compute from the current topology.

    Uses the same ``compute_gates`` the engine uses, so the preview cannot drift from
    the behaviour. The roster is AgentRegistry's *active* agents — what the engine
    loads — so an inactive agent shows as absent rather than as unrestricted.
    See v2 §12.
    """
    settings = get_settings()
    active = (await db.execute(
        select(AgentRegistry.agent_id, AgentRegistry.bot_name)
        .where(AgentRegistry.status == "active")
        .order_by(AgentRegistry.bot_name)
    )).all()
    agent_ids = [r.agent_id for r in active]
    rows = (await db.execute(
        select(CohortMembership.cohort_id, CohortMembership.agent_id)
    )).all()
    cohort_count = (await db.execute(
        select(func.count()).select_from(Cohort)
    )).scalar() or 0

    gates, preflight_error = compute_gates(
        membership_rows=[(r[0], r[1]) for r in rows],
        agent_ids=agent_ids,
        isolation_enabled=settings.cohort_isolation_enabled,
        policy=settings.cohort_default_policy,
        cohort_count=cohort_count,
        has_db=True,
    )

    # Most recent topology snapshot written by a running engine — the only way this
    # process can see the engine's in-memory counters (v2 §9.4 / §13.1).
    snapshot = (await db.execute(
        select(CohortAuditEvent)
        .where(CohortAuditEvent.action == COHORT_ACTION_TOPOLOGY_SNAPSHOT)
        .order_by(CohortAuditEvent.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    return {
        "isolation_enabled": settings.cohort_isolation_enabled,
        "default_policy": settings.cohort_default_policy,
        "preflight_error": preflight_error,
        "preview": {
            aid: (None if g is None else sorted(g)) for aid, g in gates.items()
        },
        "summary": summarise_gates(gates),
        "bot_names": {r.agent_id: r.bot_name for r in active},
        "snapshot": snapshot,
    }


@router.get("/cohorts", response_class=HTMLResponse)
async def admin_cohorts(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """List all cohorts with member counts, plus the live gate preview."""
    result = await db.execute(
        select(Cohort).options(selectinload(Cohort.memberships)).order_by(Cohort.name)
    )
    cohorts = result.scalars().unique().all()

    # Creator names for display
    creator_map: dict[str, str] = {}
    creator_ids = {c.created_by for c in cohorts if c.created_by}
    if creator_ids:
        u_result = await db.execute(select(User).where(User.id.in_(creator_ids)))
        for u in u_result.scalars().all():
            creator_map[str(u.id)] = u.name

    return templates.TemplateResponse(
        request,
        "admin/cohorts.html",
        _template_context(
            request,
            current_user,
            active_admin="cohorts",
            cohorts=cohorts,
            creator_map=creator_map,
            error=request.query_params.get("error"),
            notice=request.query_params.get("notice"),
            gate=await _cohort_gate_context(db),
        ),
    )


@router.post("/cohorts/create")
async def admin_cohort_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Create a new cohort."""
    name = name.strip().lower()
    if not _COHORT_NAME_RE.match(name):
        return RedirectResponse(
            url="/admin/cohorts?error=Invalid+name+(lowercase+letters,+numbers,+hyphens;+max+48)",
            status_code=302,
        )
    existing = await db.execute(select(Cohort).where(Cohort.name == name))
    if existing.scalar_one_or_none():
        return RedirectResponse(
            url="/admin/cohorts?error=A+cohort+with+that+name+already+exists",
            status_code=302,
        )
    cohort = Cohort(
        name=name,
        description=description.strip() or None,
        created_by=current_user.id,
    )
    db.add(cohort)
    await db.flush()
    await record_cohort_audit_event(
        db,
        action=COHORT_ACTION_CREATED,
        cohort_id=cohort.id,
        cohort_name=cohort.name,
        actor=current_user,
    )
    await db.commit()
    return RedirectResponse(url=f"/admin/cohorts/{cohort.id}", status_code=302)


@router.get("/cohorts/topology", response_class=HTMLResponse)
async def admin_cohort_topology(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Agent x cohort matrix — edit the whole topology in one pass.

    Granular control: every (agent, cohort) pair is a checkbox, so an admin can move
    several agents across several cohorts in one save instead of walking the
    per-cohort add/remove forms. The resulting per-agent gate is shown alongside,
    computed with the engine's own logic. See v2 §12.

    Registered before /cohorts/{cohort_id} so "topology" is not swallowed as a UUID
    path parameter.
    """
    cohorts = (await db.execute(
        select(Cohort).order_by(Cohort.name)
    )).scalars().all()
    agents = (await db.execute(
        select(AgentRegistry).order_by(AgentRegistry.bot_name)
    )).scalars().all()
    rows = (await db.execute(
        select(CohortMembership.cohort_id, CohortMembership.agent_id)
    )).all()
    membership_set = {f"{c}:{a}" for c, a in rows}

    return templates.TemplateResponse(
        request,
        "admin/cohort_topology.html",
        _template_context(
            request,
            current_user,
            active_admin="cohorts",
            cohorts=cohorts,
            agents=agents,
            membership_set=membership_set,
            error=request.query_params.get("error"),
            notice=request.query_params.get("notice"),
            gate=await _cohort_gate_context(db),
        ),
    )


@router.post("/cohorts/topology")
async def admin_cohort_topology_save(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Apply a whole-matrix edit as a diff against the cells that were rendered.

    The form posts one ``cell`` value per ticked box (``{cohort_id}:{agent_id}``),
    one ``present_agent`` per rendered row and one ``present_cohort`` per rendered
    column; the rendered cell set is their cross product, which is what the
    template renders (an unconditional nested loop). Sending markers instead of one
    hidden input per cell keeps the payload at agents+cohorts fields rather than
    agents*cohorts — 60x56 posted 3,528 fields and hit Starlette's
    ``max_fields=1000``, which is why the matrix could not be saved at all.

    Diffing against ``rendered`` rather than against the whole table means a stale
    or partial form can never delete memberships for a cohort or agent it did not
    display — the usual checkbox-matrix data-loss bug. Unknown cohort/agent ids are
    ignored, never written. Every add and remove is audited individually.

    ``present_cohort``/``present_agent`` are filtered down to ids that still exist
    *before* the cross product is built, not after: the product of two
    attacker-controlled lists is multiplicative, so crossing them first and
    validating each resulting cell afterward (the naive approach) lets a payload
    well within ``_TOPOLOGY_MAX_FIELDS`` build a cross product many orders of
    magnitude larger than either list — e.g. 25,000 garbage ids on each side is
    50,000 form fields (under the cap) but a 625-million-entry ``rendered`` set.
    Filtering first bounds the product by the real ``Cohort``/``AgentRegistry`` row
    counts instead. ``ticked`` is filtered the same way for the same reason, and so
    that a ticked cell naming an id that no longer exists is silently ignored
    (as it always was) rather than tripping the "malformed submission" guard below,
    which is reserved for a cell that names two otherwise-valid ids but was never
    part of the rendered cross product at all.
    """
    form = await request.form(max_fields=_TOPOLOGY_MAX_FIELDS)
    ticked = {v for v in form.getlist("cell") if isinstance(v, str)}
    present_agents = {v for v in form.getlist("present_agent") if isinstance(v, str)}
    present_cohorts = {v for v in form.getlist("present_cohort") if isinstance(v, str)}
    # Checked on the raw, unfiltered marker sets: a genuinely empty submission (no
    # rows or no columns rendered at all) is an error, but a submission naming only
    # since-deleted rows/columns is not — that is just every cell turning out inert,
    # handled below by the (empty) diff loop, not by this guard.
    if not present_agents or not present_cohorts:
        return RedirectResponse(
            url="/admin/cohorts/topology?error=Nothing+to+save", status_code=302
        )

    cohorts_by_id = {
        str(c.id): c for c in (await db.execute(select(Cohort))).scalars().all()
    }
    valid_agents = {
        r[0] for r in (await db.execute(select(AgentRegistry.agent_id))).all()
    }

    def _known_cell(cell: str) -> bool:
        cid, _, aid = cell.partition(":")
        return bool(cid) and bool(aid) and cid in cohorts_by_id and aid in valid_agents

    # Filter BEFORE crossing: bounds the cross product by the current table sizes
    # rather than by the (attacker-controlled) lengths of the submitted lists.
    present_cohorts &= cohorts_by_id.keys()
    present_agents &= valid_agents
    ticked = {t for t in ticked if _known_cell(t)}

    rendered = {f"{cid}:{aid}" for cid in present_cohorts for aid in present_agents}
    if ticked - rendered:
        return RedirectResponse(
            url="/admin/cohorts/topology?error=Malformed+submission", status_code=302
        )

    existing = {
        (str(cid), aid): mid
        for mid, cid, aid in (await db.execute(
            select(CohortMembership.id, CohortMembership.cohort_id,
                   CohortMembership.agent_id)
        )).all()
    }

    added = removed = 0
    for cell in sorted(rendered):
        cid, _, aid = cell.partition(":")
        if not cid or not aid or cid not in cohorts_by_id or aid not in valid_agents:
            continue  # stale form referencing something that no longer exists
        want = cell in ticked
        have = (cid, aid) in existing
        if want and not have:
            db.add(CohortMembership(
                cohort_id=uuid.UUID(cid), agent_id=aid, added_by=current_user.id,
            ))
            await record_cohort_audit_event(
                db,
                action=COHORT_ACTION_AGENT_ADDED,
                cohort_id=uuid.UUID(cid),
                cohort_name=cohorts_by_id[cid].name,
                agent_id=aid,
                actor=current_user,
            )
            added += 1
        elif have and not want:
            await db.execute(
                sa_delete(CohortMembership).where(
                    CohortMembership.id == existing[(cid, aid)]
                )
            )
            await record_cohort_audit_event(
                db,
                action=COHORT_ACTION_AGENT_REMOVED,
                cohort_id=uuid.UUID(cid),
                cohort_name=cohorts_by_id[cid].name,
                agent_id=aid,
                actor=current_user,
            )
            removed += 1

    if added or removed:
        await db.commit()
    return RedirectResponse(
        url=f"/admin/cohorts/topology?notice={added}+added,+{removed}+removed",
        status_code=302,
    )


@router.get("/cohorts/{cohort_id}", response_class=HTMLResponse)
async def admin_cohort_detail(
    cohort_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Cohort detail: members, add-agent picker, agent->cohort map, audit log."""
    result = await db.execute(
        select(Cohort).options(selectinload(Cohort.memberships)).where(Cohort.id == cohort_id)
    )
    cohort = result.scalar_one_or_none()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found")

    # All agents, for the add-agent picker and status display.
    agents_result = await db.execute(
        select(AgentRegistry).order_by(AgentRegistry.bot_name)
    )
    all_agents = agents_result.scalars().all()
    agent_by_id = {a.agent_id: a for a in all_agents}

    member_ids = {m.agent_id for m in cohort.memberships}
    available_agents = [a for a in all_agents if a.agent_id not in member_ids]

    # Adder names for the members table.
    adder_map: dict[str, str] = {}
    adder_ids = {m.added_by for m in cohort.memberships if m.added_by}
    if adder_ids:
        u_result = await db.execute(select(User).where(User.id.in_(adder_ids)))
        for u in u_result.scalars().all():
            adder_map[str(u.id)] = u.name

    # Read-only agent -> cohorts map (all memberships across all cohorts).
    all_memberships = (await db.execute(
        select(CohortMembership.agent_id, Cohort.name)
        .join(Cohort, CohortMembership.cohort_id == Cohort.id)
    )).all()
    agent_cohort_map: dict[str, list[str]] = {}
    for aid, cname in all_memberships:
        agent_cohort_map.setdefault(aid, []).append(cname)

    # Audit log for this cohort. Matched on cohort_id, which outlives the cohort
    # row; a recreated cohort with the same name gets a new id and so a fresh
    # trail, which is the honest reading.
    audit_events = (await db.execute(
        select(CohortAuditEvent)
        .where(CohortAuditEvent.cohort_id == cohort_id)
        .order_by(CohortAuditEvent.created_at.desc())
        .limit(200)
    )).scalars().all()

    return templates.TemplateResponse(
        request,
        "admin/cohort_detail.html",
        _template_context(
            request,
            current_user,
            active_admin="cohorts",
            cohort=cohort,
            agent_by_id=agent_by_id,
            available_agents=available_agents,
            adder_map=adder_map,
            all_agents=all_agents,
            agent_cohort_map=agent_cohort_map,
            audit_events=audit_events,
            error=request.query_params.get("error"),
            notice=request.query_params.get("notice"),
            gate=await _cohort_gate_context(db),
        ),
    )


@router.post("/cohorts/{cohort_id}/delete")
async def admin_cohort_delete(
    cohort_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Delete a cohort. Refused while it still has members.

    A server-side guard, not just a disabled button: deleting a populated cohort
    cascades its memberships away, silently reshaping the interaction topology of a
    running simulation. Remove the members first so each removal is an audited,
    individually reversible step. See v2 §12.

    A cohort id that does not exist is a 404, matching every other route in this
    module whose path-addressed row is missing (and ``admin_cohort_detail`` for
    this very id). It used to be a bare redirect to the list, which said nothing
    at all — a double-submitted delete looked like it had done the work.
    """
    result = await db.execute(
        select(Cohort).options(selectinload(Cohort.memberships)).where(Cohort.id == cohort_id)
    )
    cohort = result.scalar_one_or_none()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found")
    if cohort.memberships:
        return RedirectResponse(
            url=f"/admin/cohorts/{cohort_id}?error=Remove+all+"
                f"{len(cohort.memberships)}+members+before+deleting+this+cohort",
            status_code=302,
        )
    name = cohort.name
    await record_cohort_audit_event(
        db,
        action=COHORT_ACTION_DELETED,
        cohort_id=cohort_id,
        cohort_name=name,
        actor=current_user,
    )
    await db.delete(cohort)
    await db.commit()
    return RedirectResponse(
        url=f"/admin/cohorts?notice=Deleted+cohort+{name}", status_code=302
    )


@router.post("/cohorts/{cohort_id}/add-agent")
async def admin_cohort_add_agent(
    cohort_id: uuid.UUID,
    agent_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Add an agent to the cohort."""
    result = await db.execute(select(Cohort).where(Cohort.id == cohort_id))
    cohort = result.scalar_one_or_none()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found")

    agent_id = agent_id.strip().lower()
    # Validate the agent exists in the registry.
    agent_exists = await db.execute(
        select(AgentRegistry.id).where(AgentRegistry.agent_id == agent_id)
    )
    if not agent_exists.scalar_one_or_none():
        return RedirectResponse(
            url=f"/admin/cohorts/{cohort_id}?error=Unknown+agent",
            status_code=302,
        )
    # Reject duplicate membership.
    dup = await db.execute(
        select(CohortMembership.id).where(
            CohortMembership.cohort_id == cohort_id,
            CohortMembership.agent_id == agent_id,
        )
    )
    if dup.scalar_one_or_none():
        return RedirectResponse(
            url=f"/admin/cohorts/{cohort_id}?error=Agent+is+already+a+member",
            status_code=302,
        )
    db.add(CohortMembership(
        cohort_id=cohort_id,
        agent_id=agent_id,
        added_by=current_user.id,
    ))
    await record_cohort_audit_event(
        db,
        action=COHORT_ACTION_AGENT_ADDED,
        cohort_id=cohort_id,
        cohort_name=cohort.name,
        agent_id=agent_id,
        actor=current_user,
    )
    await db.commit()
    return RedirectResponse(url=f"/admin/cohorts/{cohort_id}", status_code=302)


@router.post("/cohorts/{cohort_id}/remove-agent")
async def admin_cohort_remove_agent(
    cohort_id: uuid.UUID,
    agent_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Remove an agent from the cohort.

    An unknown cohort id is a 404, as everywhere else in this module: the old
    behaviour redirected to ``/admin/cohorts/{cohort_id}``, a detail page that
    then 404s itself — so the user paid for two requests to be told nothing.
    Removing an agent that is not a member is a different case and stays a quiet
    redirect back to the (real) detail page: a stale Remove button is a race the
    admin cannot act on, and the page it returns to already shows the truth.
    """
    cohort = (await db.execute(
        select(Cohort).where(Cohort.id == cohort_id)
    )).scalar_one_or_none()
    if not cohort:
        raise HTTPException(status_code=404, detail="Cohort not found")
    result = await db.execute(
        select(CohortMembership).where(
            CohortMembership.cohort_id == cohort_id,
            CohortMembership.agent_id == agent_id.strip().lower(),
        )
    )
    membership = result.scalar_one_or_none()
    if membership:
        await record_cohort_audit_event(
            db,
            action=COHORT_ACTION_AGENT_REMOVED,
            cohort_id=cohort_id,
            cohort_name=cohort.name,
            agent_id=membership.agent_id,
            actor=current_user,
        )
        await db.delete(membership)
        await db.commit()
    return RedirectResponse(url=f"/admin/cohorts/{cohort_id}", status_code=302)

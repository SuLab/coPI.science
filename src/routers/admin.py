"""Admin dashboard router."""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
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
    AgentRegistry,
    Job,
    LlmCallLog,
    Publication,
    ResearcherProfile,
    SimulationRun,
    ThreadDecision,
    User,
)
from src.services.orcid import fetch_orcid_profile

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")


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
    channel_stats: dict[str, dict] = {}
    for msg in messages:
        if msg.channel_name not in channel_stats:
            channel_stats[msg.channel_name] = {"count": 0, "agents": set()}
        channel_stats[msg.channel_name]["count"] += 1
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
    page: int = 1,
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
    export: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Discussion summary: threads grouped by status."""
    from sqlalchemy import case, distinct, literal, text

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

    # Collect available agents from threads
    available_agents = set()
    for t in threads:
        available_agents.add(t["agent_id"])
        if t.get("replier"):
            available_agents.add(t["replier"])
        if t.get("decision"):
            available_agents.add(t["decision"].agent_a)
            available_agents.add(t["decision"].agent_b)

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
        from fastapi.responses import PlainTextResponse
        lines = []
        for t in threads:
            d = t.get("decision")
            if not d or not d.summary_text:
                continue
            lines.append(f"{'=' * 72}")
            lines.append(f"Channel: #{t['channel_name']}")
            lines.append(f"Agents: {d.agent_a.capitalize()}Bot + {d.agent_b.capitalize()}Bot")
            lines.append(f"Outcome: {d.outcome}")
            lines.append(f"Date: {d.decided_at.strftime('%Y-%m-%d %H:%M UTC')}")
            lines.append("")
            lines.append(d.summary_text.strip())
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

    # Check which agents have .env tokens
    from src.config import get_settings
    settings = get_settings()
    env_tokens = settings.get_slack_tokens()
    env_token_agents = {
        aid for aid, tokens in env_tokens.items()
        if tokens.get("bot") and not tokens["bot"].startswith("xoxb-placeholder")
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
            user_map=user_map,
            all_users=all_users,
            env_token_agents=env_token_agents,
            proposal_counts=proposal_counts,
            review_counts=review_counts,
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
        ),
    )


@router.post("/agents/{agent_id}/approve")
async def admin_approve_agent(
    agent_id: uuid.UUID,
    request: Request,
    agent_slug: str = Form(...),
    bot_name: str = Form(...),
    slack_bot_token: str = Form(""),
    slack_app_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Approve an agent request."""
    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent.agent_id = agent_slug.strip().lower()
    agent.bot_name = bot_name.strip()
    agent.slack_bot_token = slack_bot_token.strip() or None
    agent.slack_app_token = slack_app_token.strip() or None
    agent.status = "active"
    agent.approved_at = datetime.now(timezone.utc)
    agent.approved_by = current_user.id
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

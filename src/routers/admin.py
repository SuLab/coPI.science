"""Admin dashboard router."""

import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.agent.roles import available_roles
from src.agent.run_marker import _template_body, parse_announce_channels, validate_template
from src.agent.specialists import parse_opinion
from src.config import get_settings
from src.database import get_db
from src.dependencies import get_admin_user, get_current_user
from src.models import (
    COHORT_ACTION_AGENT_ADDED,
    COHORT_ACTION_AGENT_REMOVED,
    COHORT_ACTION_CREATED,
    COHORT_ACTION_DELETED,
    COHORT_ACTION_TOPOLOGY_SNAPSHOT,
    USER_ROLE_ADMIN,
    VALID_USER_ROLES,
    AccessAllowlist,
    AdminAuditEvent,
    AgentRegistry,
    AppSetting,
    Cohort,
    CohortAuditEvent,
    CohortMembership,
    Job,
    LlmCallLog,
    ResearcherProfile,
    SimulationCommand,
    SimulationRun,
    ThreadDecision,
    User,
    WaitlistSignup,
)
from src.services.assessment_detail import build_assessment_detail
from src.services.cohorts import (
    compute_gates,
    record_cohort_audit_event,
    summarise_gates,
)
from src.services.directory import (
    build_discussions_view,
    build_run_detail,
    list_assessments,
    list_pi_directory,
    list_runs_overview,
    load_user_detail,
)
from src.services.llm import is_truncated_stop
from src.services.agent_activation import activation_blockers
from src.services.jhu_rules import get_tenure_start
from src.services.pi_onboarding import find_or_create_pi_by_orcid
from src.services.simulation_control import (
    derive_panel_state,
    enqueue_command,
    read_status,
    record_audit,
)
from src.services.thread_panel import panel_cards_by_thread
from src.services.user_deletion import delete_user_account
from src.services.validators import csv_safe_cell

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

# "Did this API call stop before it finished?" — registered as a Jinja TEST so
# `admin/llm_calls.html` can `selectattr('stop_reason', 'truncated_stop')` and
# reach the real predicate rather than re-listing its stop reasons.
#
# The template used to test `stop_reason == 'max_tokens'` on its own, which
# rendered a `refusal`-truncated turn as complete on the one page an operator
# opens to audit truncation. `is_truncated_stop` (src/services/llm.py) is the
# single definition — the engine, the specialist floor and the Slack posting
# path all read it — so a third stop reason added there reaches this page for
# free. A test, not a filter or a global: `selectattr` takes a test name.
templates.env.tests["truncated_stop"] = is_truncated_stop

# Valid AgentRegistry.status values (see src/models/agent_registry.py). Admins
# can move an already-approved agent between these from the edit page; the sim
# runs status=='active' agents (others are parked/excluded, reversibly).
VALID_AGENT_STATUSES = ("active", "inactive", "suspended", "pending")

# Module-level dependency singletons, the pattern src/routers/manager.py
# documents: ruff's B008 flags a `Depends(...)` call sitting in an argument
# default, and the src/ lint ratchet (scripts/ci.sh's SRC_LINT_MAX) has ~3
# findings of headroom against 140 existing B008s in this file alone. New
# handlers here take these instead of adding to that debt.
_DB = Depends(get_db)
_ADMIN = Depends(get_admin_user)


def _template_context(
    request: Request, current_user: User, active_admin: str = "", **kwargs
) -> dict:
    """Build the template context, surfacing the impersonation banner.

    ``current_user`` here is the *effective* user from `get_admin_user`, which
    is the impersonated user when one admin impersonates another (both
    satisfy `is_admin`, so `get_admin_user` lets it through). Without this,
    every /admin/* page rendered with no banner and no Stop button. Mirrors
    the same pattern in onboarding.py / profile.py / agent_page.py /
    settings.py / manager.py.
    """
    impersonated = getattr(current_user, "_is_impersonated", False)
    real_admin = getattr(current_user, "_real_admin", None)
    ctx = {
        "request": request,
        "current_user": real_admin if impersonated else current_user,
        "impersonation_banner": current_user if impersonated else None,
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
    user_data = await list_pi_directory(
        db,
        status_filter=status_filter,
        institution_filter=institution_filter,
        claimed_filter=claimed_filter,
    )

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
    detail = await load_user_detail(db, user_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="User not found")

    return templates.TemplateResponse(
        request,
        "admin/user_detail.html",
        _template_context(
            request,
            current_user,
            active_admin="users",
            target_user=detail["user"],
            profile=detail["profile"],
            publications=detail["publications"],
            jobs=detail["jobs"],
            valid_user_roles=VALID_USER_ROLES,
        ),
    )


@router.post("/users/{user_id}/delete")
async def admin_delete_user(
    user_id: uuid.UUID,
    request: Request,
    remove_from_allowlist: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Delete a user account (admin only) — through the full teardown."""
    if getattr(current_user, "_is_impersonated", False):
        # Same guard as POST /profile/delete-account (deletion audit F8/D6):
        # under impersonation `current_user` is the impersonated admin, so the
        # self-delete check below compares against the WRONG identity and the
        # log line would attribute the deletion to someone who never acted.
        # Drop impersonation first; then delete.
        raise HTTPException(
            status_code=403,
            detail="Account deletion is disabled while impersonating.",
        )
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    name = user.name
    report = await delete_user_account(
        db, user, remove_from_allowlist=bool(remove_from_allowlist)
    )
    logger.info(
        "Admin %s deleted user %s (%s): %s",
        current_user.name, name, user_id, report.summary(),
    )
    return RedirectResponse(url="/admin/users", status_code=302)


@router.post("/users/{user_id}/role")
async def admin_set_user_role(
    user_id: uuid.UUID,
    request: Request,
    user_role: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Set a user's account type (admin only).

    Named for users, not agents: POST /agents/{agent_id}/role already exists
    and sets a BOT role (pi_lab / scout_hub), which is a different thing.
    """
    if user_role not in VALID_USER_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {user_role}")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Mirrors the self-delete guard above: an admin editing their own row is
    # how you lose your own access mid-session.
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    # Demoting the last admin locks every human out of /admin; the only way
    # back is `python -m src.cli role:set` from a container shell. This branch
    # is unreachable over HTTP while the self-change guard above stands —
    # demoting the last admin X requires an actor with admin rights who is not
    # X, and if X is the last admin no such actor exists — but it stays as a
    # backstop for if that guard is ever relaxed, and it is what
    # tests/integration/test_role_appointment.py exercises by calling this
    # function directly with a non-admin actor.
    # access_status is part of the count on purpose. The invariant being
    # defended is "at least one admin can still LOG IN", and auth.py refuses a
    # user whose access_status is not 'allowed' — so a denied/pending admin is
    # not a way back in. Counting them anyway inflates the number, which makes
    # `<= 1` fire LESS often and therefore makes demotion EASIER: admins X
    # (denied) and Y (allowed) count as 2, Y is demotable, and zero loginable
    # admins remain. An earlier note recorded the unfiltered count as "more
    # conservative"; that was backwards.
    if user.user_role == USER_ROLE_ADMIN and user_role != USER_ROLE_ADMIN:
        admin_count = await db.scalar(
            select(func.count(User.id)).where(
                User.user_role == USER_ROLE_ADMIN,
                User.access_status == "allowed",
            )
        )
        if (admin_count or 0) <= 1:
            raise HTTPException(
                status_code=400, detail="Cannot demote the last remaining admin"
            )

    previous = user.user_role
    user.user_role = user_role
    await db.commit()
    logger.info(
        "Admin %s changed role of %s (%s) from %s to %s",
        current_user.name, user.name, user_id, previous, user_role,
    )
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=302)


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
    overview = await list_runs_overview(db)

    return templates.TemplateResponse(
        request,
        "admin/activity.html",
        _template_context(
            request,
            current_user,
            active_admin="activity",
            runs=overview["runs"],
            total_runs=overview["total_runs"],
            total_messages=overview["total_messages"],
            total_channels=overview["total_channels"],
            most_active_agent=overview["most_active_agent"],
            most_active_count=overview["most_active_count"],
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
    detail = await build_run_detail(db, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Run not found")

    return templates.TemplateResponse(
        request,
        "admin/activity_detail.html",
        _template_context(
            request,
            current_user,
            active_admin="activity",
            run=detail["run"],
            messages=detail["messages"],
            channels=detail["channels"],
            agent_stats=detail["agent_stats"],
            channel_stats=detail["channel_stats"],
        ),
    )


@router.get("/activity/{run_id}/llm-calls", response_class=HTMLResponse)
async def admin_llm_calls(
    run_id: uuid.UUID,
    request: Request,
    agent: str | None = None,
    phase: str | None = None,
    model: str | None = None,
    channel: str | None = None,
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """View LLM call logs for a simulation run.

    ``channel`` is what groups ONE INTERVIEW's calls together: the hub's
    ``thread_reply`` rows have always carried the channel, and consult rows
    carry it as of the consult ``log_meta`` change, so filtering by channel is
    the closest this page gets to "show me this conversation". Consult rows
    written BEFORE that change have ``channel`` NULL and are therefore excluded
    by any channel filter — they are still reachable with the filter cleared,
    which is why the dropdown never offers a "(none)" option that would look
    like a real grouping.
    """
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
    if channel:
        query = query.where(LlmCallLog.channel == channel)

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

    # Non-NULL only: `channel` is nullable and NULL on every non-channel call
    # (memory, decide) as well as on pre-log_meta consults, so a NULL option
    # would read as a grouping when it is really "unattributed".
    channels_result = await db.execute(
        select(LlmCallLog.channel)
        .where(
            LlmCallLog.simulation_run_id == run_id,
            LlmCallLog.channel.is_not(None),
        )
        .distinct()
    )
    available_channels = sorted([r[0] for r in channels_result])

    # A consult's verdict signal — the one thing worth scanning a page of
    # consults for — was only readable by opening the row and reading the JSON.
    # Parsed server-side here, and ONLY for `consult_` rows: running
    # parse_opinion over 50 arbitrary responses would be 50 wasted json.loads.
    # `parse_opinion` never raises and degrades an unreadable reply to `gap`,
    # exactly as the engine does — a specialist we could not read has not met
    # any bar. `allow_historical=True` is one of only TWO places that opt into
    # the pre-2026-08-28 `caution`/`clear` (`_READABLE_SIGNALS`,
    # src/agent/specialists.py), and this is why the flag exists: the page
    # re-parses STORED text, so every consult logged before the rename would
    # otherwise be relabelled `gap` on every page view. It must stay OFF on the
    # live consult path, which shares this same function — see that constant.
    consult_signals = {
        str(log.id): parse_opinion(
            log.response_text,
            domain=log.phase.removeprefix("consult_"),
            allow_historical=True,
        ).verdict_signal
        for log in logs
        if log.phase.startswith("consult_")
    }

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
            available_channels=available_channels,
            consult_signals=consult_signals,
            filter_agent=agent,
            filter_phase=phase,
            filter_model=model,
            filter_channel=channel,
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
    view = await build_discussions_view(
        db,
        run_id=run_id,
        channel_filter=channel_filter,
        status_filter=status_filter,
        agent_filter=agent_filter,
    )

    # No simulation runs exist at all: render the normal HTML page and return
    # here, BEFORE the `if export:` branch below. This ordering is
    # deliberate and matches the pre-extraction code — an export request
    # must never swallow the no-runs page (e.g. GET
    # /admin/discussions?export=true with zero SimulationRun rows previously
    # returned the HTML page, not a "No proposals found" text attachment).
    if view["selected_run_id"] is None:
        return templates.TemplateResponse(
            request,
            "admin/discussions.html",
            _template_context(
                request,
                current_user,
                active_admin="discussions",
                runs=view["runs"],
                selected_run_id=view["selected_run_id"],
                threads=view["threads"],
                counts=view["counts"],
                channels=view["channels"],
                agents=view["agents"],
                channel_filter=view["channel_filter"],
                status_filter=view["status_filter"],
                agent_filter=view["agent_filter"],
                # No run selected means no threads, so no panel to summarize —
                # but the keys must still be present: the shared threads body
                # reads them on every render. Nothing was read, so nothing was
                # capped; `panel_row_limit` is only ever printed inside the
                # truncation notice, which this cannot reach.
                panel_by_thread={},
                panel_truncated=False,
                panel_row_limit=0,
                admin_view=True,
            ),
        )

    if export:
        proposals = []
        for t in view["threads"]:
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

    # What the panel was asked, and what it said, per thread. Keyed on
    # thread_id, which is the ROOT message's ts — the same value the threads
    # list calls `message_ts` (src/services/directory.py::build_discussions_view).
    #
    # ONE query serves both the compact per-row indicator and the cards in the
    # expanded row: the cards carry domain and verdict_signal, which is all the
    # indicator reads. It is scoped to the threads this render is actually
    # showing, so the filters above narrow the consult read too.
    panel = await panel_cards_by_thread(
        db,
        view["selected_run_id"],
        [t["message_ts"] for t in view["threads"]],
        admin_view=True,
    )

    return templates.TemplateResponse(
        request,
        "admin/discussions.html",
        _template_context(
            request,
            current_user,
            active_admin="discussions",
            runs=view["runs"],
            selected_run_id=view["selected_run_id"],
            threads=view["threads"],
            counts=view["counts"],
            channels=view["channels"],
            agents=view["agents"],
            channel_filter=view["channel_filter"],
            status_filter=view["status_filter"],
            agent_filter=view["agent_filter"],
            panel_by_thread=panel.by_thread,
            # A capped panel read must not look like an unconsulted page.
            panel_truncated=panel.truncated,
            panel_row_limit=panel.limit,
            # Unlocks the verbatim specialist reply inside each panel card.
            # /manager/discussions renders the same shared body with it False;
            # the value is also withheld server-side (see
            # src/services/thread_panel.py).
            admin_view=True,
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


@router.get("/assessments", response_class=HTMLResponse)
async def admin_assessments(
    request: Request,
    run_id: str | None = None,
    sort: str | None = None,
    lab: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """BlackbirdBot's screening verdicts against the Blackbird investment rubric.

    See ``src.services.directory.list_assessments`` for the run-scoping,
    truncation and sort/filter semantics — including why an unrecognized
    ``sort`` or ``lab`` renders the default view instead of an error.
    """
    view = await list_assessments(db, run_id, sort=sort, lab=lab)

    return templates.TemplateResponse(
        request,
        "admin/assessments.html",
        _template_context(
            request,
            current_user,
            active_admin="assessments",
            assessments=view["assessments"],
            # The band thresholds and the decline label the legend states, from
            # the rubric document rather than as template literals — the page
            # and the scorer must never be able to disagree about where the
            # "advance" line sits.
            banding=view["banding"],
            rubric_version=view["rubric_version"],
            runs=view["runs"],
            runs_by_id=view["runs_by_id"],
            selected_run_id=view["selected_run_id"],
            show_all_runs=view["show_all_runs"],
            # The sort/lab controls' own state. Forwarded explicitly because
            # this handler allowlists every key it passes (unlike
            # manager_assessments' `**view` splat) — a key added to
            # list_assessments and not added here simply never reaches the
            # page, and Jinja's Undefined would render the control as if no
            # filter were applied.
            sort=view["sort"],
            sort_options=view["sort_options"],
            lab_filter=view["lab_filter"],
            lab_options=view["lab_options"],
            pi_user_ids=view["pi_user_ids"],
            total_count=view["total_count"],
            assessments_limit=view["assessments_limit"],
            drop_counts=view["drop_counts"],
            drops_total=view["drops_total"],
            incomplete_panel_count=view["incomplete_panel_count"],
            dimension_stats=view["dimension_stats"],
            band_counts=view["band_counts"],
            assessment_counts_by_run=view["assessment_counts_by_run"],
            off_rubric_count=view["off_rubric_count"],
        ),
    )


@router.get("/assessments/{assessment_id}", response_class=HTMLResponse)
async def admin_assessment_detail(
    assessment_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """One verdict in full, plus the interview that produced it.

    ``admin_view=True`` is what unlocks the two admin-only strands of this page
    — the hub's tool activity (parsed out of ``llm_call_logs``) and each
    specialist's verbatim opinion. /manager/assessments/{id} renders the same
    shared body with it False; see ``src.services.assessment_detail``.
    """
    detail = await build_assessment_detail(
        db, assessment_id, admin_view=True, viewer_is_staff=current_user.is_staff
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return templates.TemplateResponse(
        request,
        "admin/assessment_detail.html",
        _template_context(request, current_user, active_admin="assessments", **detail),
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

    # Star-spoke state for the evidence panel: "missing" means a run started
    # with this agent active would fail _validate_star_topology at startup.
    spoke_state = None
    if agent.role == "pi_lab" and agent.status != "suspended":
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
            activation_blocked=request.query_params.get("activation_blocked"),
            valid_statuses=VALID_AGENT_STATUSES,
            available_roles=available_roles(),
            slack_error=request.query_params.get("slack_error"),
            slack_ok=request.query_params.get("slack_ok"),
            role_error=request.query_params.get("role_error"),
            spoke_state=spoke_state,
            spoke_ok=request.query_params.get("spoke_ok"),
            spoke_error=request.query_params.get("spoke_error"),
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
    from urllib.parse import quote

    from src.services.star_topology import ensure_star_spokes

    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.role != "pi_lab":
        return RedirectResponse(
            url=f"/admin/agents/{agent_id}?spoke_error="
            + quote("Only pi_lab agents have star spokes."),
            status_code=302,
        )
    try:
        report = await ensure_star_spokes(
            db, apply=True, actor=current_user, only={agent.agent_id}
        )
    except ValueError as exc:
        return RedirectResponse(
            url=f"/admin/agents/{agent_id}?spoke_error={quote(str(exc)[:200])}",
            status_code=302,
        )
    await db.commit()
    if report.anomalies:
        return RedirectResponse(
            url=f"/admin/agents/{agent_id}?spoke_error="
            + quote("; ".join(report.anomalies)[:300]),
            status_code=302,
        )
    return RedirectResponse(
        url=f"/admin/agents/{agent_id}?spoke_ok=1", status_code=302
    )


@router.post("/agents/{agent_id}/approve")
async def admin_approve_agent(
    agent_id: uuid.UUID,
    request: Request,
    agent_slug: str = Form(...),
    bot_name: str = Form(...),
    slack_bot_token: str = Form(""),
    agent_status: str = Form(None),
    activation_override: str = Form(""),
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
    is missing or ungrounded or its newest generation job is dead, unless the
    explicit (logged) override checkbox was posted. Auto-created pending rows
    (the manager Add-PI flow) made "pending" stop implying "profile exists",
    and an active agent with no exported profile is the Kavran-class failure.
    A refusal applies NO edits at all — the form's slug/name/token changes
    roll back with it, so what the admin sees stays what the DB holds.
    """
    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    activating = agent.status == "pending" or (
        agent.status != "active" and agent_status == "active"
    )
    if activating:
        blockers = await activation_blockers(db, agent)
        if blockers and not activation_override.strip():
            logger.warning(
                "Refused activation of agent %s (%s): %s",
                agent.agent_id, agent.id, "; ".join(blockers),
            )
            return RedirectResponse(
                url=f"/admin/agents/{agent_id}?activation_blocked=1",
                status_code=302,
            )
        if blockers:
            logger.warning(
                "Activation OVERRIDE by admin %s for agent %s (%s) despite: %s",
                current_user.id, agent.agent_id, agent.id, "; ".join(blockers),
            )

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
        try:
            target = await find_or_create_pi_by_orcid(db, orcid)
            await db.commit()
        except ValueError as exc:
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
        # Same switch the session cookie uses in src/main.py. This used to read
        # `request.app.state.allow_http`, guarded by a hasattr — but nothing in
        # src/ has ever SET app.state.allow_http (the setting is called
        # allow_http_sessions and lives on Settings), so the hasattr was always
        # False and the whole ternary was a constant secure=False. Production
        # runs ALLOW_HTTP_SESSIONS=false, so this cookie was shipping without
        # Secure beside a session cookie that requires HTTPS (E1.5).
        secure=not get_settings().allow_http_sessions,
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

    # Dry-run the star-spoke maintainer so the page can offer the wire-up
    # button exactly when a pi_lab is missing its hub-and-spoke cohort (the
    # state that fails _validate_star_topology at run startup).
    from src.services.star_topology import ensure_star_spokes

    try:
        spoke_plan = await ensure_star_spokes(db, apply=False)
        spokes_missing = len(
            set(spoke_plan.created_cohorts)
            | {name for name, _ in spoke_plan.added_members}
        )
    except ValueError:
        spokes_missing = None  # no single scout_hub; the button would refuse too

    return templates.TemplateResponse(
        request,
        "admin/cohorts.html",
        _template_context(
            request,
            current_user,
            active_admin="cohorts",
            cohorts=cohorts,
            creator_map=creator_map,
            spokes_missing=spokes_missing,
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


@router.post("/cohorts/ensure-star-spokes")
async def admin_ensure_star_spokes(
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """One click: wire every pi_lab into the hub-and-spoke topology.

    Same service the CLI (scripts/ensure_star_spokes.py) drives, with the
    click attributed to the acting admin in cohort_audit_events. Registered
    before /cohorts/{cohort_id} so the literal path is matched, not parsed as
    a UUID. Additive only — anomalies (lab-to-lab contamination, overlong
    slugs) are surfaced in the banner, never "fixed" by deletion.
    """
    from urllib.parse import quote

    from src.services.star_topology import ensure_star_spokes

    try:
        report = await ensure_star_spokes(db, apply=True, actor=current_user)
    except ValueError as exc:
        return RedirectResponse(
            url=f"/admin/cohorts?error={quote(str(exc)[:200])}", status_code=302
        )
    await db.commit()
    notice = (
        f"Star spokes: {len(report.created_cohorts)} cohort(s) created, "
        f"{len(report.added_members)} membership(s) added, "
        f"{len(report.complete)} already complete"
    )
    url = f"/admin/cohorts?notice={quote(notice)}"
    if report.anomalies:
        url += "&error=" + quote("; ".join(report.anomalies)[:300])
    return RedirectResponse(url=url, status_code=302)


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


# ---------------------------------------------------------------------------
# /admin/simulation — the control-plane panel (Task 7 of the 2026-08-30
# simulation-control-panel plan). Command/heartbeat/audit primitives live in
# src.services.simulation_control; this module stays thin — read the panel
# state, shape a form, ask the service a question, render or redirect. The
# supervisor (src/agent/supervisor.py) and the running engine's own
# `_poll_control_plane` are the only consumers of the rows written here.
# ---------------------------------------------------------------------------

#: Slack's own channel-name shape (lowercase letters, digits, hyphens,
#: underscores, up to 80 chars) — checked per name AFTER
#: parse_announce_channels has already split/deduped/trimmed the raw input,
#: so an admin typo is caught here rather than surfacing later as a silent
#: channel_not_found from Slack at run-start time.
_ANNOUNCE_CHANNEL_RE = re.compile(r"^[a-z0-9_-]{1,80}$")

_KEY_ANNOUNCE_CHANNELS = "run_start_announce_channels"
_KEY_ANNOUNCE_TEMPLATE = "run_start_announcement_template"


def _hash12(text: str | None) -> str | None:
    """First 12 hex chars of a sha256 digest, or None for a None input.

    The ONLY form the announce-template audit payload ever records of a
    template body — never the full text (it may echo an unpublished PI
    disclosure's placeholder shape, or simply be long).
    """
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


async def _kv_get(db: AsyncSession, key: str) -> str | None:
    return (await db.execute(select(AppSetting.value).where(AppSetting.key == key))).scalar_one_or_none()


async def _kv_upsert(db: AsyncSession, key: str, value: str) -> None:
    stmt = pg_insert(AppSetting).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value})
    await db.execute(stmt)


async def _kv_delete(db: AsyncSession, key: str) -> None:
    await db.execute(sa_delete(AppSetting).where(AppSetting.key == key))


async def _simulation_context(
    db: AsyncSession,
    request: Request,
    current_user: User,
    *,
    msg: str | None = None,
    error: str | None = None,
    template_error: str | None = None,
    template_value_override: str | None = None,
) -> dict:
    """Assemble every value templates/admin/simulation.html renders.

    Shared by the GET route and by the announce-template POST's inline-error
    re-render (the one POST here that renders the page directly instead of
    redirecting — see that handler's docstring for why).
    """
    now = datetime.now(UTC)
    status_row = await read_status(db)
    panel_state = derive_panel_state(status_row, now)

    pending_result = await db.execute(
        select(SimulationCommand)
        .where(SimulationCommand.status == "pending")
        .order_by(SimulationCommand.created_at)
    )
    pending_commands = pending_result.scalars().all()
    pending_start = next((c for c in pending_commands if c.command == "start"), None)

    recent_result = await db.execute(
        select(SimulationCommand).order_by(SimulationCommand.created_at.desc()).limit(20)
    )
    recent_commands = recent_result.scalars().all()

    latest_run = (
        await db.execute(select(SimulationRun).order_by(SimulationRun.started_at.desc()).limit(1))
    ).scalar_one_or_none()

    channels_kv = await _kv_get(db, _KEY_ANNOUNCE_CHANNELS)
    channels_value = channels_kv if channels_kv is not None else get_settings().run_start_announce_channels

    if template_value_override is not None:
        template_value = template_value_override
    else:
        template_kv = await _kv_get(db, _KEY_ANNOUNCE_TEMPLATE)
        template_value = template_kv if template_kv is not None else _template_body()

    audit_result = await db.execute(
        select(AdminAuditEvent).order_by(AdminAuditEvent.created_at.desc()).limit(20)
    )
    audit_events = audit_result.scalars().all()

    return _template_context(
        request,
        current_user,
        active_admin="simulation",
        panel_state=panel_state,
        status_row=status_row,
        latest_run=latest_run,
        pending_commands=pending_commands,
        pending_start=pending_start,
        recent_commands=recent_commands,
        channels_value=channels_value,
        template_value=template_value,
        audit_events=audit_events,
        msg=msg,
        error=error,
        template_error=template_error,
    )


@router.get("/simulation", response_class=HTMLResponse)
async def admin_simulation(
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """The simulation control panel: status card, start/stop forms, the
    announce-channels + announce-template editors, and recent command/audit
    history. Stats sections are appended by Task 11 — see the marker comment
    inside the template."""
    ctx = await _simulation_context(
        db,
        request,
        current_user,
        msg=request.query_params.get("msg"),
        error=request.query_params.get("error"),
    )
    return templates.TemplateResponse(request, "admin/simulation.html", ctx)


@router.post("/simulation/start")
async def admin_simulation_start(
    request: Request,
    fresh: bool = Form(False),
    max_runtime: int = Form(0),
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Enqueue a `start` command for the supervisor to claim.

    Refused (no row written) when `derive_panel_state` already reads
    `running`/`starting` — a live engine, CLI-launched or panel-launched
    alike, since the heartbeat is state-source-agnostic — or when a `start`
    is already pending (the common double-click case, caught here before
    ever reaching the database). The 0042 partial unique index
    (`uq_simulation_commands_one_pending`) is the second, race-proof layer:
    an `IntegrityError` from `enqueue_command` is caught and rendered as the
    same refusal. DECLARED v1 decision: this two-field form plus these
    refusals ARE the confirmation step — no JS confirm dialog — and the
    sharp CLI-only flags (`--all-agents`/`--reset-cursors`) are deliberately
    not exposed here; the page links no substitute.
    """
    now = datetime.now(UTC)
    status_row = await read_status(db)
    panel_state = derive_panel_state(status_row, now)
    pending_start = (
        await db.execute(
            select(SimulationCommand).where(
                SimulationCommand.status == "pending",
                SimulationCommand.command == "start",
            )
        )
    ).scalar_one_or_none()
    if panel_state in ("running", "starting") or pending_start is not None:
        return RedirectResponse(
            url=f"/admin/simulation?error={quote('A run is already starting or in progress.')}",
            status_code=302,
        )
    payload = {"fresh": fresh, "max_runtime": max_runtime}
    try:
        await enqueue_command(
            db, command="start", payload=payload, requested_by_user_id=current_user.id
        )
    except IntegrityError:
        await db.rollback()
        return RedirectResponse(
            url=f"/admin/simulation?error={quote('A start is already pending.')}",
            status_code=302,
        )
    await record_audit(
        db, action="simulation_start_requested", actor_user_id=current_user.id, payload=payload
    )
    return RedirectResponse(
        url=f"/admin/simulation?msg={quote('Start requested.')}", status_code=302
    )


@router.post("/simulation/stop")
async def admin_simulation_stop(
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Enqueue a `stop` command.

    Refused when the panel does not currently read `running` — the same
    predicate the supervisor itself uses (src/agent/supervisor.py) to decide
    whether a claimed stop has a live engine to reach; anything else, the
    supervisor would immediately finish a stop as "nothing running" on its
    own next poll, so refusing here gives the same answer without writing a
    row nobody will act on. The IntegrityError catch is the same
    double-click/race guard as the start route.
    """
    now = datetime.now(UTC)
    status_row = await read_status(db)
    panel_state = derive_panel_state(status_row, now)
    if panel_state != "running":
        return RedirectResponse(
            url=f"/admin/simulation?error={quote('Nothing is running.')}", status_code=302
        )
    try:
        await enqueue_command(db, command="stop", payload=None, requested_by_user_id=current_user.id)
    except IntegrityError:
        await db.rollback()
        return RedirectResponse(
            url=f"/admin/simulation?error={quote('A stop is already pending.')}",
            status_code=302,
        )
    await record_audit(
        db, action="simulation_stop_requested", actor_user_id=current_user.id, payload=None
    )
    return RedirectResponse(url=f"/admin/simulation?msg={quote('Stop requested.')}", status_code=302)


@router.post("/simulation/announce-settings")
async def admin_simulation_announce_settings(
    request: Request,
    channels: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Persist (or clear) the DB override for the run-start announcement's
    channel list.

    `src.agent.run_marker.parse_announce_channels` does the split/dedup/trim;
    each resulting name is then checked against Slack's own channel-name
    shape (`_ANNOUNCE_CHANNEL_RE`) before anything is written. An empty
    input clears the override (falls back to the `Settings` default).
    """
    names = parse_announce_channels(channels)
    bad = [n for n in names if not _ANNOUNCE_CHANNEL_RE.match(n)]
    if bad:
        return RedirectResponse(
            url=f"/admin/simulation?error={quote('Invalid channel name(s): ' + ', '.join(bad))}",
            status_code=302,
        )
    old_value = await _kv_get(db, _KEY_ANNOUNCE_CHANNELS)
    new_value = ",".join(names) or None
    if new_value is None:
        await _kv_delete(db, _KEY_ANNOUNCE_CHANNELS)
    else:
        await _kv_upsert(db, _KEY_ANNOUNCE_CHANNELS, new_value)
    await record_audit(
        db,
        action="simulation_announce_channels_updated",
        actor_user_id=current_user.id,
        payload={"old": old_value, "new": new_value},
    )
    return RedirectResponse(
        url=f"/admin/simulation?msg={quote('Announce channels updated.')}", status_code=302
    )


@router.post("/simulation/announce-template")
async def admin_simulation_announce_template(
    request: Request,
    body: str = Form(""),
    reset: bool = Form(False),
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Save (or reset) the DB override for the run-start announcement body.

    `reset` deletes the KV row outright and is never validated (deleting
    can't introduce a bad template) — it falls back to the template FILE,
    not to `DEFAULT_TEMPLATE` directly (see `run_marker._template_body`).
    Otherwise the body is checked with `run_marker.validate_template` BEFORE
    anything is written: a bad body re-renders this same page with the
    error inline and the just-submitted text still in the textarea, rather
    than redirecting — the one route here that isn't a plain
    POST-then-redirect, because the error needs the submitted body back in
    front of the admin, not a fresh KV read. The audit payload never stores
    the template text itself, only a sha256[:12] fingerprint of the old and
    new bodies.
    """
    old_value = await _kv_get(db, _KEY_ANNOUNCE_TEMPLATE)

    if reset:
        if old_value is not None:
            await _kv_delete(db, _KEY_ANNOUNCE_TEMPLATE)
            await record_audit(
                db,
                action="simulation_announce_template_reset",
                actor_user_id=current_user.id,
                payload={"old_hash": _hash12(old_value), "new_hash": None},
            )
        return RedirectResponse(
            url=f"/admin/simulation?msg={quote('Template reset to file default.')}",
            status_code=302,
        )

    error = validate_template(body)
    if error:
        ctx = await _simulation_context(
            db, request, current_user, template_error=error, template_value_override=body
        )
        return templates.TemplateResponse(request, "admin/simulation.html", ctx)

    await _kv_upsert(db, _KEY_ANNOUNCE_TEMPLATE, body)
    await record_audit(
        db,
        action="simulation_announce_template_updated",
        actor_user_id=current_user.id,
        payload={"old_hash": _hash12(old_value), "new_hash": _hash12(body)},
    )
    return RedirectResponse(url=f"/admin/simulation?msg={quote('Template saved.')}", status_code=302)

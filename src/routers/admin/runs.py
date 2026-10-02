"""Admin run activity and LLM-call pages."""

import uuid

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.specialists import parse_opinion
from src.database import get_db
from src.dependencies import get_admin_user
from src.models import LlmCallLog, SimulationRun, User
from src.routers.admin._common import _template_context, router, templates
from src.services.directory import MAX_PAGE, build_run_detail, list_runs_overview
from src.services.headline_claims import held_headline_counts, list_in_doubt

_PAGE = Query(1, ge=1, le=MAX_PAGE)


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
    page: int = _PAGE,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Simulation run detail."""
    detail = await build_run_detail(db, run_id, page=page)
    if detail is None:
        raise HTTPException(status_code=404, detail="Run not found")
    held_counts = (
        await held_headline_counts(db, run_id) if detail["run"].status == "stopped" else None
    )
    in_doubt = await list_in_doubt(db, run_id)

    return templates.TemplateResponse(
        request,
        "admin/activity_detail.html",
        _template_context(
            request,
            current_user,
            active_admin="activity",
            run=detail["run"],
            messages=detail["messages"],
            message_total=detail["message_total"],
            page=detail["page"],
            page_count=detail["page_count"],
            channels=detail["channels"],
            agent_stats=detail["agent_stats"],
            channel_stats=detail["channel_stats"],
            held_counts=held_counts,
            in_doubt=in_doubt,
            msg=request.query_params.get("msg"),
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

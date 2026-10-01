"""The /admin/simulation Live tab's view model and SVG composition, for ONE run (spec §7.3; moved from the admin router)."""

import uuid
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.role_capabilities import hub_role_names, star_role
from src.models import (
    AgentRegistry,
    LlmCallLog,
    OpportunityAssessment,
    SimulationProcessStatus,
    SimulationRun,
)
from src.services import display_format as fmt
from src.services.build_info import API_CALL_UNITS_NOTE
from src.services.runs import runs_ordered
from src.services.simulation_control import HEARTBEAT_STALE_SECONDS
from src.services.simulation_stats import (
    CALL_STATS_ROW_LIMIT,
    consult_fanout,
    cost_by_call_kind,
    cost_by_specialist,
    cost_by_stage,
    cost_per_interview,
    cost_summary,
    funnel,
    hourly_activity,
    hub_lab_burn,
    interview_timeline,
    latency_percentiles,
    per_agent,
    run_overview,
    specialist_mix,
    stop_reason_taxonomy,
)
from src.services.svg_charts import (
    CATEGORICAL_COLORS,
    DIVERGING_MID,
    DIVERGING_NEG,
    DIVERGING_POS,
    diverging_hbar,
    gantt,
    hbar_list,
    legend,
    line_chart,
    meter,
    stacked_hbar,
    stat_tile,
    table_twin,
)

# ---------------------------------------------------------------------------
# Live tab — wires src.services.simulation_stats + svg_charts into
# the control panel. Strictly single-run: every figure below comes from ONE
# `simulation_run_id`, chosen by `_resolve_selected_run`, and nothing here
# ever sums across runs — the run selector exists so an operator can compare
# runs side by side, never so the page can blend them.
# ---------------------------------------------------------------------------


async def _rubric_stamps_by_run(
    db: AsyncSession, run_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[str]]:
    """FALLBACK ONLY, for the runs in `run_ids` (legacy rows opened before
    `_stamp_run_config` started recording `SimulationRun.config["rubric_version"]`
    — see `_run_selector_labels`, which prefers the config stamp and only
    calls this for a run missing one). Derives a stamp from whatever distinct
    (rubric_version, rubric_content_hash) an OpportunityAssessment row in that
    run was stamped with, most-common first — a run that straddled a rubric
    bump shows both stamps rather than picking one. Scoped to `run_ids` rather
    than scanning every run's assessments on every page load."""
    if not run_ids:
        return {}
    rows = (
        await db.execute(
            select(
                OpportunityAssessment.simulation_run_id,
                OpportunityAssessment.rubric_version,
                OpportunityAssessment.rubric_content_hash,
                func.count(),
            )
            .where(OpportunityAssessment.simulation_run_id.in_(run_ids))
            .group_by(
                OpportunityAssessment.simulation_run_id,
                OpportunityAssessment.rubric_version,
                OpportunityAssessment.rubric_content_hash,
            )
        )
    ).all()
    by_run: dict[uuid.UUID, list[tuple[int, str]]] = defaultdict(list)
    for run_id, version, content_hash, n in rows:
        label = f"{version or 'unstamped'} ({content_hash or '—'}) ×{n}"
        by_run[run_id].append((int(n), label))
    return {
        run_id: [label for _, label in sorted(entries, reverse=True)]
        for run_id, entries in by_run.items()
    }


async def _run_selector_labels(
    db: AsyncSession, runs: list[SimulationRun],
) -> dict[uuid.UUID, list[str]]:
    """Rubric stamp label per run for the `?run=` selector.

    Primary source is `SimulationRun.config["rubric_version"]` /
    `["rubric_content_hash"]` — `_stamp_run_config` (src/agent/main.py) writes
    both at run-open FOR EXACTLY THIS PURPOSE (its docstring: "what lets the
    admin run dropdown label a run by rubric after the log is gone"), it is
    already on every row `_resolve_selected_run` fetched (zero extra queries),
    and it is correct even for a run with zero assessments — a run is stamped
    by which rubric OPENED it, not by what it happened to score. Only a run
    whose config predates that stamp (opened before this feature existed)
    falls back to the assessment-derived scan, and only for those runs."""
    unstamped_run_ids = [
        r.id for r in runs if not (r.config or {}).get("rubric_version")
    ]
    fallback = await _rubric_stamps_by_run(db, unstamped_run_ids)

    labels: dict[uuid.UUID, list[str]] = {}
    for r in runs:
        cfg = r.config or {}
        version = cfg.get("rubric_version")
        if version:
            content_hash = cfg.get("rubric_content_hash")
            labels[r.id] = [f"{version} ({content_hash or '—'})"]
        else:
            labels[r.id] = fallback.get(r.id, [])
    return labels


async def _resolve_selected_run(
    db: AsyncSession, request: Request,
) -> tuple[SimulationRun | None, list[SimulationRun]]:
    """`?run=<uuid>`, defaulting to the latest run by `started_at`. An absent,
    malformed, or unknown `run` param all fall back to the default rather than
    erroring — the selector is a convenience, not a hard filter that can 404."""
    runs = await runs_ordered(db)
    selected = None
    run_param = request.query_params.get("run")
    if run_param:
        try:
            run_uuid = uuid.UUID(run_param)
        except ValueError:
            run_uuid = None
        if run_uuid is not None:
            selected = next((r for r in runs if r.id == run_uuid), None)
    if selected is None:
        selected = runs[0] if runs else None
    return selected, runs


def _agents_detail_map(
    status_row: SimulationProcessStatus | None, now: datetime,
) -> dict[str, Any] | None:
    """The engine's own per-agent heartbeat snapshot
    (`SimulationProcessStatus.detail["agents"]`, written by
    `SimulationEngine._poll_control_plane` every ~30s: `active_threads`,
    `calls_in_window`, `api_calls`, `messages` per agent_id), or None when the
    row itself is absent or stale (same `HEARTBEAT_STALE_SECONDS` threshold
    `derive_panel_state` uses for the engine-wide badge) — a stale snapshot is
    not "the agent is idle", it is "we do not know", so every agent's columns
    read "—" together rather than showing a frozen last-seen number."""
    if status_row is None:
        return None
    age_seconds = (now - status_row.updated_at).total_seconds()
    if age_seconds > HEARTBEAT_STALE_SECONDS:
        return None
    detail = status_row.detail or {}
    return detail.get("agents")


def _agent_live_columns(
    agent_id: str, agents_detail: dict[str, Any] | None,
) -> tuple[str, str]:
    """(active_threads_display, calls_in_window_display) — "—" for both when
    the heartbeat snapshot itself is unavailable (`agents_detail is None`, see
    `_agents_detail_map`) or this particular agent has no entry in it (never
    ticked yet, or dropped from the roster since)."""
    row = (agents_detail or {}).get(agent_id) if agents_detail is not None else None
    if row is None:
        return "—", "—"
    active_threads = row.get("active_threads")
    calls_in_window = row.get("calls_in_window")
    return (
        str(active_threads) if active_threads is not None else "—",
        str(calls_in_window) if calls_in_window is not None else "—",
    )


def _run_options(
    selected_run: SimulationRun | None,
    all_runs: list[SimulationRun],
    rubric_stamps: dict[uuid.UUID, list[str]],
) -> list[dict]:
    return [
        {
            "id": r.id,
            "status": r.status,
            "started_at": fmt.timestamp(r.started_at),
            "rubric_stamps": rubric_stamps.get(r.id, []),
            "is_selected": selected_run is not None and r.id == selected_run.id,
        }
        for r in all_runs
    ]


def _kpi_tiles(overview, cost, fun) -> dict[str, str]:
    # --- KPI row ------------------------------------------------------
    cost_hero_html = stat_tile(
        "Total cost", fmt.money(cost.total, floor=cost.is_floor),
        ("Unpriced model(s) excluded from this total: " + ", ".join(cost.unpriced_models)) if cost.unpriced_models
        else ("Floor: some rows predate cache-token logging." if cost.is_floor else None),
        warn=bool(cost.unpriced_models),
    )
    elapsed_hours = overview.elapsed_seconds / 3600
    burn_html = stat_tile(
        "Average burn rate",
        f"{fmt.money(float(cost.total) / elapsed_hours, floor=cost.is_floor)}/h" if elapsed_hours > 0 else "—",
        f"Total cost ÷ run lifetime ({fmt.duration(overview.elapsed_seconds)}); not a current rate.",
    )
    cache_denominator = cost.total_input_tokens + cost.total_cache_read_tokens
    cache_fraction = cost.total_cache_read_tokens / cache_denominator if cache_denominator > 0 else 0.0
    cache_meter_html = meter("Cache hit rate", cache_fraction,
                             f"{cost.total_cache_read_tokens:,} of {cache_denominator:,} input tokens served from cache")
    if overview.planned_seconds:
        frac = overview.elapsed_seconds / overview.planned_seconds
        progress_html = meter(
            "Progress", frac,
            f"{fmt.duration(overview.elapsed_seconds)} of {fmt.duration(overview.planned_seconds)} planned"
            + (" — overran the limit" if frac > 1 else ""),
            value_text=fmt.percent(frac), warn=frac > 1,
        )
    else:
        progress_html = stat_tile(
            "Progress", "No time limit",
            (f"Running {fmt.duration(overview.elapsed_seconds)} so far" if overview.ended_at is None
             else f"Ran {fmt.duration(overview.elapsed_seconds)}"),
        )
    headlines_owed_html = stat_tile("Headlines owed", str(fun.headlines_owed),
                                    "Terminal verdicts with no #assessments-summary post yet; 0 on a healthy run.",
                                    warn=fun.headlines_owed > 0)
    interviews_concluded_html = stat_tile("Interviews concluded", str(fun.terminal),
                                          f"of {fun.interviews_opened} opened; {fun.provisional} still provisional")
    return {
        "cost_hero_html": cost_hero_html,
        "burn_html": burn_html,
        "cache_meter_html": cache_meter_html,
        "progress_html": progress_html,
        "headlines_owed_html": headlines_owed_html,
        "interviews_concluded_html": interviews_concluded_html,
    }


def _cost_over_time(hours) -> dict[str, Any]:
    # --- cost over time -------------------------------------------------
    running = Decimal(0)
    cum_points: list[tuple[str, float | None]] = []
    for h in hours:
        running += h.cost
        cum_points.append((fmt.hour_label(h.hour), float(running)))
    cumulative_cost_html = line_chart(cum_points, unit="US$", value_fmt=fmt.money,
                                      key="cumulative-cost") if cum_points else None

    token_classes = [("input", CATEGORICAL_COLORS[0]), ("output", CATEGORICAL_COLORS[1]),
                     ("cache read", CATEGORICAL_COLORS[2]), ("cache write", CATEGORICAL_COLORS[3])]
    hourly_token_legend_html = legend(token_classes)
    hour_max = max((h.input_tokens + h.output_tokens + h.cache_read_tokens + h.cache_creation_tokens for h in hours), default=0)
    hourly_token_bars, hourly_twin_rows = [], []
    for h in hours:
        vals = [h.input_tokens, h.output_tokens, h.cache_read_tokens, h.cache_creation_tokens]
        hourly_token_bars.append({
            "hour_label": fmt.hour_label(h.hour),
            "html": stacked_hbar(fmt.hour_label(h.hour), [(n, float(v), c) for (n, c), v in zip(token_classes, vals, strict=True)],
                                 scale_max=float(hour_max), table=False),
        })
        hourly_twin_rows.append([fmt.hour_label(h.hour), *[fmt.count(v) for v in vals], fmt.count(sum(vals))])
    hourly_token_twin_html = table_twin(["Hour (UTC)", "input", "output", "cache read", "cache write", "total"],
                                        hourly_twin_rows, caption="Tokens per hour", key="tokens-per-hour") if hours else None
    return {
        "cumulative_cost_html": cumulative_cost_html,
        "hourly_token_legend_html": hourly_token_legend_html,
        "hourly_token_bars": hourly_token_bars,
        "hourly_token_twin_html": hourly_token_twin_html,
    }


def _cost_breakdowns(cost, stage_costs, specialist_costs, call_kind_costs) -> dict[str, Any]:
    # --- cost by … (descending by cost for display) --------------------------
    def _cost_rows(items, caption, label):
        return hbar_list(
            [(label(r), float(r.cost), f"{fmt.money(r.cost)} ({fmt.plural(r.call_count, 'call')})")
             for r in sorted(items, key=lambda r: float(r.cost), reverse=True)],
            unit="US$", axis_fmt=fmt.money, caption=caption,
        )

    cost_by_agent_html = _cost_rows(cost.by_agent, "Cost by agent", lambda a: a.agent_id) if cost.by_agent else None
    cost_by_model_html = hbar_list(
        [(m.model, float(m.cost) if m.cost is not None else 0.0,
          fmt.money(m.cost) if m.cost is not None else "unpriced — not in the price table")
         for m in sorted(cost.by_model, key=lambda m: float(m.cost or 0), reverse=True)],
        unit="US$", axis_fmt=fmt.money, caption="Cost by model") if cost.by_model else None
    cost_by_phase_html = _cost_rows(cost.by_phase, "Cost by phase", lambda p: p.phase) if cost.by_phase else None
    cost_by_stage_html = _cost_rows(stage_costs, "Cost by interview stage", lambda r: f"{r.role} · {r.thread_phase}") if stage_costs else None
    cost_by_specialist_html = _cost_rows(specialist_costs, "Cost by specialist consult",
                                         lambda r: f"{r.domain} · {r.verdict_signal}") if specialist_costs else None
    # `is_floor` is a property of the RUN (some row has NULL call_stats), so
    # every row carries the same value — read it off the first.
    call_kind_is_floor = bool(call_kind_costs) and call_kind_costs[0].is_floor
    cost_by_call_kind_html = hbar_list(
        [(r.kind, float(r.cost), f"{fmt.money(r.cost, floor=r.is_floor)} ({fmt.plural(r.call_count, 'call')})")
         for r in sorted(call_kind_costs, key=lambda r: float(r.cost), reverse=True)],
        unit="US$", axis_fmt=fmt.money, caption="Cost by call kind") if call_kind_costs else None
    return {
        "cost_by_agent_html": cost_by_agent_html,
        "cost_by_model_html": cost_by_model_html,
        "cost_by_phase_html": cost_by_phase_html,
        "cost_by_stage_html": cost_by_stage_html,
        "cost_by_specialist_html": cost_by_specialist_html,
        "cost_by_call_kind_html": cost_by_call_kind_html,
        "call_kind_is_floor": call_kind_is_floor,
    }


def _funnel_panel_and_latency(fun, domains, fanout, taxonomy, latency) -> dict[str, Any]:
    # --- funnel (pipeline order) + drops ---------------------------------------
    funnel_counts = [("Interviews opened", fun.interviews_opened), ("Verdicts stored", fun.verdicts_stored),
                     ("Terminal (thread closed)", fun.terminal), ("Provisional (still open)", fun.provisional),
                     ("Headline announced", fun.announced), ("Headlines owed", fun.headlines_owed)]
    funnel_html = hbar_list([(lbl, float(v), str(v)) for lbl, v in funnel_counts], unit="interviews",
                            axis_fmt=fmt.whole_mid, caption="Funnel") if any(v for _, v in funnel_counts) else None
    drops_rows = sorted(fun.drops_by_reason.items(), key=lambda kv: kv[1], reverse=True)

    # --- specialist mix + fan-out ----------------------------------------------
    specialist_legend_html = legend([("blocking", DIVERGING_NEG), ("gap", DIVERGING_MID), ("adequate", DIVERGING_POS)])
    mix_max = max((d.blocking + d.gap + d.adequate for d in domains), default=0)
    specialist_rows = [{"domain": d.domain, "historical": d.historical,
                        "html": diverging_hbar(d.domain, d.blocking, d.gap, d.adequate, ("blocking", "gap", "adequate"),
                                               scale_max=float(mix_max), table=False)} for d in domains]
    specialist_twin_html = table_twin(
        ["Domain", "blocking", "gap", "adequate", "historical"],
        [[d.domain, str(d.blocking), str(d.gap), str(d.adequate), str(d.historical)] for d in domains],
        caption="Specialist mix", key="specialist-mix") if domains else None
    fanout_html = hbar_list(
        [(f"{fmt.plural(b.consult_count, 'consult')} per interview", float(b.interview_count), fmt.plural(b.interview_count, "interview"))
         for b in fanout], unit="interviews", axis_fmt=fmt.whole_mid, caption="Panel fan-out") if fanout else None

    # --- stop reasons + latency ----------------------------------------------------
    taxonomy_html = hbar_list(
        [(lbl, float(n), fmt.count(n)) for lbl, n in sorted(taxonomy.items(), key=lambda kv: kv[1], reverse=True)],
        unit="API calls", axis_fmt=fmt.whole_mid, caption="Stop-reason taxonomy") if taxonomy else None
    latency_rows = [{"phase": phase, "n": fmt.count(p.n),
                     "p50": fmt.count(round(p.p50)) if p.p50 is not None else "—",
                     "p95": fmt.count(round(p.p95)) if p.p95 is not None else "—",
                     "p99": fmt.count(round(p.p99)) if p.p99 is not None else "—"}
                    for phase, p in sorted(latency.items(), key=lambda kv: (kv[0] != "overall", kv[0]))]
    return {
        "funnel_html": funnel_html,
        "drops_rows": drops_rows,
        "specialist_legend_html": specialist_legend_html,
        "specialist_rows": specialist_rows,
        "specialist_twin_html": specialist_twin_html,
        "fanout_html": fanout_html,
        "taxonomy_html": taxonomy_html,
        "latency_rows": latency_rows,
    }


def _per_agent_rows(agents, status_row, now) -> list[dict]:
    # --- per-agent table -----------------------------------------------------------
    agents_detail = _agents_detail_map(status_row, now)
    per_agent_rows = []
    for r in agents:
        active_threads, calls_in_window = _agent_live_columns(r.agent_id, agents_detail)
        per_agent_rows.append({"agent_id": r.agent_id, "role": r.role or "—", "registry_status": r.registry_status or "—",
                               "muted": r.muted, "message_count": fmt.count(r.message_count), "call_count": fmt.count(r.call_count),
                               "cost": fmt.money(r.cost), "last_activity": fmt.timestamp(r.last_activity),
                               "active_threads": active_threads, "calls_in_window": calls_in_window})
    return per_agent_rows


def _interview_gantt(timeline, per_interview) -> dict[str, Any]:
    # --- interview gantt (F11: spans start at the first REPLY, not the root post) ----
    # `timeline`'s spans (`InterviewSpan.first_message_at` / `.last_message_at`,
    # from `interview_timeline`) are MIN/MAX over `agent_messages` rows whose
    # `thread_ts` equals the interview's `thread_id` — and the thread's own ROOT
    # post never satisfies that: a root message IS the thing later replies point
    # back to via `thread_ts`, so the root's own `thread_ts` column is NULL, not
    # its own timestamp. Every bar drawn below therefore starts at the first
    # REPLY, and is very slightly narrower than the interview's true wall-clock
    # span. This is a rendering fact, not a bug to fix here — and the card's
    # caption says so to the operator.
    cost_by_thread = {ic.thread_ts: ic for ic in per_interview}
    known = [t for s in timeline for t in (s.first_message_at, s.last_message_at) if t is not None]
    t0, t1 = (min(known), max(known)) if known else (0.0, 0.0)
    gantt_rows, gantt_links = [], []
    for s in sorted(timeline, key=lambda s: s.first_message_at if s.first_message_at is not None else t0):
        start = s.first_message_at if s.first_message_at is not None else t0
        end = s.last_message_at if s.last_message_at is not None else start
        ic = cost_by_thread.get(s.thread_id)
        cost_display = fmt.money(ic.cost, floor=ic.is_floor) if ic is not None else "—"
        label = s.subject_agent_id or s.thread_id
        gantt_rows.append((label, start, end, CATEGORICAL_COLORS[1 if s.announced else 0], f"{s.outcome} — {cost_display}"))
        gantt_links.append({"assessment_id": s.assessment_id, "label": label, "outcome": s.outcome, "cost": cost_display,
                            "announced": s.announced, "span": fmt.duration(end - start)})
    # A run longer than a day makes a bare "18:49" ambiguous between two of its
    # own days — on those, ticks (and the table twin, which shares this
    # formatter) carry the date too.
    tick_fmt = fmt.epoch_label if (t1 - t0) > 86400 else fmt.epoch_hm
    gantt_html = gantt(gantt_rows, t0, t1, tick_fmt=tick_fmt,
                       legend_items=[("headline announced", CATEGORICAL_COLORS[1]), ("not announced", CATEGORICAL_COLORS[0])]) if gantt_rows else None
    unattributed = cost_by_thread.get(None)
    unattributed_note = None
    if gantt_rows and unattributed is not None and unattributed.cost > 0:
        unattributed_note = (f"{fmt.money(unattributed.cost, floor=unattributed.is_floor)} in LLM calls could not be attributed to a "
                             "specific interview thread (no thread_ts recorded) and is excluded from every row above.")
    return {
        "gantt_html": gantt_html,
        "gantt_links": gantt_links,
        "unattributed_note": unattributed_note,
    }


def _burn_line(burn_points) -> str | None:
    # --- hub:lab burn ratio -------------------------------------------------------------
    # A None ratio (BurnPoint.ratio's docstring: hub tokens against zero lab
    # tokens — genuinely unbounded) is the single most alarming state this chart
    # exists to surface. `line_chart` draws it as a HOLLOW marker at the top of
    # the plot, labelled "∞", rather than as a number: plotting it as 0.0 would
    # render that runaway-coordination signal as the CALMEST point on the line.
    burn_line_html = line_chart([(fmt.hour_label(p.hour), p.ratio) for p in burn_points], unit="hub ÷ lab tokens",
                                value_fmt=lambda v: f"{v:.2f}", none_label="∞",
                                none_table_label="∞ — no lab tokens this hour",
                                key="hub-lab-burn") if burn_points else None
    return burn_line_html


async def live_tab_context(
    db: AsyncSession,
    request: Request,
    status_row: SimulationProcessStatus | None,
    now: datetime,
) -> dict[str, Any]:
    """Every value the Live tab's stats sections render, for ONE run — see the
    module comment above. `status_row`/`now` are the same heartbeat row and
    clock `_simulation_context` already read for the engine-status badge —
    passed in rather than re-read, and reused here for the per-agent table's
    real `active_threads`/`calls_in_window` columns (see `_agents_detail_map`).
    Returns a minimal dict (no run selected, no runs exist) when there is
    nothing to show; the template degrades every subsection to an em-dash on
    its own empty data."""
    selected_run, all_runs = await _resolve_selected_run(db, request)
    rubric_stamps = await _run_selector_labels(db, all_runs)
    run_options = _run_options(selected_run, all_runs, rubric_stamps)

    if selected_run is None:
        return {
            "stats_run": None,
            "run_options": run_options,
            "api_call_units_note": API_CALL_UNITS_NOTE,
        }

    run_id = selected_run.id
    overview = await run_overview(db, run_id)
    cost = await cost_summary(db, run_id)
    hours = await hourly_activity(db, run_id)
    fun = await funnel(db, run_id)
    domains = await specialist_mix(db, run_id)
    fanout = await consult_fanout(db, run_id)
    agents = await per_agent(db, run_id)
    taxonomy = await stop_reason_taxonomy(db, run_id)
    latency = await latency_percentiles(db, run_id)
    timeline = await interview_timeline(db, run_id)
    per_interview = await cost_per_interview(db, run_id)
    stage_costs = await cost_by_stage(db, run_id)
    specialist_costs = await cost_by_specialist(db, run_id)
    call_kind_costs = await cost_by_call_kind(db, run_id)

    total_call_rows = (
        await db.execute(
            select(func.count())
            .select_from(LlmCallLog)
            .where(LlmCallLog.simulation_run_id == run_id)
        )
    ).scalar_one()
    latency_capped = total_call_rows > CALL_STATS_ROW_LIMIT

    hub_agent_id = (
        await db.execute(
            select(AgentRegistry.agent_id)
            .where(AgentRegistry.role.in_(hub_role_names()))
            .limit(1)
        )
    ).scalar_one_or_none()
    burn_points = await hub_lab_burn(db, run_id, hub_agent_id) if hub_agent_id else []

    kpi = _kpi_tiles(overview, cost, fun)
    cost_time = _cost_over_time(hours)
    breakdowns = _cost_breakdowns(cost, stage_costs, specialist_costs, call_kind_costs)
    panel = _funnel_panel_and_latency(fun, domains, fanout, taxonomy, latency)
    per_agent_rows = _per_agent_rows(agents, status_row, now)
    gantt_parts = _interview_gantt(timeline, per_interview)
    burn_line_html = _burn_line(burn_points)

    run_facts = {"status": overview.status, "started": fmt.timestamp(overview.started_at), "ended": fmt.timestamp(overview.ended_at),
                 "elapsed": fmt.duration(overview.elapsed_seconds), "total_api_calls": fmt.count(overview.total_api_calls),
                 "total_messages": fmt.count(overview.total_messages), "announcement": overview.run_start_announcement}
    process_facts = {"build": overview.build_info, "prompts": overview.prompt_stamps,
                     "role_labels": {r: ("Hub" if star_role(r) == "hub" else "PI") for r in overview.prompt_stamps},
                     "engine_loaded": overview.engine_loaded, "engine_loaded_label": overview.engine_loaded_label,
                     "rubric_version": overview.rubric_version, "rubric_hash": overview.rubric_content_hash}

    return {
        "stats_run": selected_run, "run_options": run_options, "api_call_units_note": API_CALL_UNITS_NOTE,
        "cost_hero_html": kpi["cost_hero_html"], "burn_html": kpi["burn_html"], "cache_meter_html": kpi["cache_meter_html"],
        "progress_html": kpi["progress_html"], "headlines_owed_html": kpi["headlines_owed_html"],
        "interviews_concluded_html": kpi["interviews_concluded_html"], "cumulative_cost_html": cost_time["cumulative_cost_html"],
        "hourly_token_legend_html": cost_time["hourly_token_legend_html"], "hourly_token_bars": cost_time["hourly_token_bars"],
        "hourly_token_twin_html": cost_time["hourly_token_twin_html"],
        "cost_by_agent_html": breakdowns["cost_by_agent_html"], "cost_by_model_html": breakdowns["cost_by_model_html"],
        "cost_by_phase_html": breakdowns["cost_by_phase_html"], "cost_by_stage_html": breakdowns["cost_by_stage_html"],
        "cost_by_specialist_html": breakdowns["cost_by_specialist_html"], "cost_by_call_kind_html": breakdowns["cost_by_call_kind_html"],
        "call_kind_is_floor": breakdowns["call_kind_is_floor"], "funnel_html": panel["funnel_html"], "drops_rows": panel["drops_rows"],
        "unvetted_panel_count": fun.unvetted_panel_count, "specialist_legend_html": panel["specialist_legend_html"],
        "specialist_rows": panel["specialist_rows"], "specialist_twin_html": panel["specialist_twin_html"], "fanout_html": panel["fanout_html"],
        "taxonomy_html": panel["taxonomy_html"], "latency_rows": panel["latency_rows"], "latency_capped": latency_capped,
        "per_agent_rows": per_agent_rows, "gantt_html": gantt_parts["gantt_html"], "gantt_links": gantt_parts["gantt_links"],
        "unattributed_note": gantt_parts["unattributed_note"], "burn_line_html": burn_line_html,
        "run_facts": run_facts, "process_facts": process_facts, "heartbeat_stale_seconds": HEARTBEAT_STALE_SECONDS,
    }

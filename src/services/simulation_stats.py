"""Read-side aggregates for the simulation control panel's "Live" tab.

Every function here is a plain async query (or a handful of them) scoped to
ONE ``simulation_run_id`` and returning a frozen dataclass defined in this
module — the control panel route (a later task) renders these, this module
never touches ``Request``/Jinja/HTTP. Nothing here is dependency-heavy: the
rubric stamp, the prompt-set stamp and the build info are all read the same
way ``src/agent/main.py``'s startup banner reads them, so the Live tab's
"what is this process running" line and the container log's own banner can
never silently disagree.

Two invariants every function here honours:

* **Strictly run-scoped.** Every query filters on ``simulation_run_id ==
  run_id`` explicitly — never "the latest run" or "all runs" — so a second
  run's rows can never leak into the first's numbers.
* **Tolerates an empty run.** A run with zero messages/calls/assessments
  returns a zeroed structure (empty lists, ``Decimal("0")``, empty dicts) —
  never raises. The one exception is ``run_overview``, which reads the
  ``SimulationRun`` row itself and therefore *does* raise for a run id that
  does not exist at all — there is no sensible zeroed stand-in for "started
  at" or "status" on a row that was never created.

Money is always ``Decimal`` (``src.services.llm_pricing.cost_for_tokens``'s
own type); an unpriced model contributes zero to any total it appears in and
is separately named in ``CostSummary.unpriced_models`` — a silent zero would
misreport spend, so it never happens without being surfaced.

``CostSummary.is_floor`` and each ``InterviewCost.is_floor`` mean the same
thing at different granularities: at least one aggregated row predates
migration 0036 and therefore has NULL cache-token columns, which this module
reads as zero. That is a FLOOR, not a measurement — the true cost is that
number or higher — and the caller is expected to render it with "≥".
"""

from __future__ import annotations

import statistics
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.roles import PromptSetStamp, prompt_set_stamp
from src.agent.specialists import VERDICT_SIGNALS
from src.models import (
    AgentMessage,
    AgentRegistry,
    AssessmentDrop,
    LlmCallLog,
    OpportunityAssessment,
    SimulationRun,
    SpecialistConsult,
    ThreadDecision,
)
from src.services.assessment_detail import unvetted_panel_filter
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION
from src.services.build_info import BuildInfo, get_build_info
from src.services.llm_pricing import cost_for_tokens

#: Cap on how many `llm_call_logs` ROWS (not exploded calls) `stop_reason_taxonomy`
#: and `latency_percentiles` will read, newest first. Exported so a caller (the
#: control-panel route) can compare it against a plain `COUNT(*)` for the run and
#: render the "newest 20k calls" cap note only when the cap actually bound.
CALL_STATS_ROW_LIMIT = 20_000


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunOverview:
    run_id: uuid.UUID
    status: str
    started_at: datetime
    ended_at: datetime | None
    total_messages: int
    total_api_calls: int
    elapsed_seconds: float
    #: None means indefinite (``max_runtime`` 0 or absent from ``config``).
    planned_seconds: int | None
    rubric_version: str
    rubric_content_hash: str
    hub_prompt_stamp: PromptSetStamp
    pi_prompt_stamp: PromptSetStamp
    build_info: BuildInfo
    #: ``SimulationRun.config["run_start_announcement"]`` verbatim (see
    #: ``SimulationEngine._record_run_start_announcement``): ``{"at", "text",
    #: "posted", "failed"}``, or None when this run never announced (a resume,
    #: or announcements disabled).
    run_start_announcement: dict[str, Any] | None


@dataclass(frozen=True)
class ModelCost:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    #: None when the model is unpriced (see ``CostSummary.unpriced_models``).
    cost: Decimal | None


@dataclass(frozen=True)
class AgentCost:
    agent_id: str
    cost: Decimal
    call_count: int


@dataclass(frozen=True)
class PhaseCost:
    phase: str
    cost: Decimal
    call_count: int


@dataclass(frozen=True)
class CostSummary:
    by_model: list[ModelCost]
    total: Decimal
    unpriced_models: list[str]
    #: True when any row in the run predates migration 0036 (NULL cache-token
    #: columns, read as zero) — the total is therefore a floor, not a measurement.
    is_floor: bool
    by_agent: list[AgentCost]
    by_phase: list[PhaseCost]
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cache_creation_tokens: int


@dataclass(frozen=True)
class HourBucket:
    hour: datetime
    calls: int
    cost: Decimal
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


@dataclass(frozen=True)
class Funnel:
    #: Union of distinct ``opportunity_assessments.thread_id`` and distinct
    #: ``thread_decisions.thread_id`` for the run — a thread the panel never
    #: reached a stored verdict on (timeout, a lab's own ⏸️ before the hub's
    #: verdict) still opened and closed, and only ``thread_decisions`` records
    #: that. There is no per-run signal for a thread that opened and is still
    #: live with no verdict yet and no close, so this is a floor on "opened",
    #: not an exact count of every thread the hub ever touched.
    interviews_opened: int
    verdicts_stored: int
    #: A stored verdict is TERMINAL when its thread_id appears in
    #: `thread_decisions` for this run (the thread closed) — the same
    #: derivation the engine itself uses to rebuild `final` after a restart
    #: (`_rehydrate_assessed_threads`). A row with no thread_id (pre-0036) or
    #: whose thread has not closed counts as provisional: we cannot prove it is
    #: done, so it is not counted as done.
    terminal: int
    provisional: int
    announced: int
    #: TERMINAL verdicts with no headline yet — the KPI that should read 0 on a
    #: healthy run. A provisional (still in-flight) verdict is not "owed" a
    #: headline yet; it is not due.
    headlines_owed: int
    drops_by_reason: dict[str, int]
    unvetted_panel_count: int


@dataclass(frozen=True)
class DomainMix:
    domain: str
    blocking: int
    gap: int
    adequate: int
    #: Pre-2026-08-28 labels (``caution``/``clear``) folded in here — counted,
    #: never dropped. See ``src.agent.specialists.HISTORICAL_VERDICT_SIGNALS``.
    historical: int
    total: int


@dataclass(frozen=True)
class ConsultFanoutBucket:
    """One point of the panel fan-out distribution: ``interview_count`` interview
    threads each received exactly ``consult_count`` specialist consults."""

    consult_count: int
    interview_count: int


@dataclass(frozen=True)
class AgentRow:
    agent_id: str
    #: None when the agent_id has no `agents` row at all (never registered, or
    #: the row's own agent_id is stale relative to the roster).
    role: str | None
    registry_status: str | None
    muted: bool
    message_count: int
    call_count: int
    cost: Decimal
    last_activity: datetime | None


@dataclass(frozen=True)
class LatencyPcts:
    n: int
    #: All three None when n < 2 (`statistics.quantiles` raises StatisticsError
    #: below that; this is the guarded empty percentile set for a young run).
    p50: float | None
    p95: float | None
    p99: float | None


@dataclass(frozen=True)
class InterviewSpan:
    assessment_id: uuid.UUID
    thread_id: str
    subject_agent_id: str | None
    #: `agent_messages.posted_at` (writer's clock), or None when no run-scoped
    #: message on this thread_ts was found.
    first_message_at: float | None
    last_message_at: float | None
    #: The verdict's recommendation, or the literal string "in flight" when the
    #: stored verdict carries no recommendation (a sparse/partial capture).
    outcome: str
    announced: bool


@dataclass(frozen=True)
class BurnPoint:
    hour: datetime
    hub_tokens: int
    lab_tokens: int
    #: hub / lab, or None when lab_tokens is 0 (avoids a division by zero and
    #: an uninterpretable "infinite" ratio).
    ratio: float | None


@dataclass(frozen=True)
class InterviewCost:
    #: None marks the UNATTRIBUTED bucket: `llm_call_logs` rows with no
    #: `thread_ts` (written before migration 0042, or from a call site that does
    #: not know its thread). Always present in `cost_per_interview`'s result,
    #: even when zero, so a reader can see the run's per-interview costs do not
    #: sum to the run total without guessing why.
    thread_ts: str | None
    #: The assessment's recommendation for this thread, or None when no
    #: assessment row names this thread_id (including the unattributed bucket).
    outcome: str | None
    cost: Decimal
    is_floor: bool


# ---------------------------------------------------------------------------
# run_overview
# ---------------------------------------------------------------------------


async def run_overview(db: AsyncSession, run_id: uuid.UUID) -> RunOverview:
    """Run row fields, process-level stamps, and the run-start announcement
    record. Raises ValueError for a run id that does not exist — there is no
    zeroed stand-in for a row that was never created."""
    run = (
        await db.execute(select(SimulationRun).where(SimulationRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise ValueError(f"No SimulationRun with id {run_id}")

    config = run.config or {}
    reference = run.ended_at or datetime.now(UTC)
    elapsed_seconds = (reference - run.started_at).total_seconds()
    max_runtime = config.get("max_runtime")
    planned_seconds = int(max_runtime) * 60 if max_runtime else None

    return RunOverview(
        run_id=run.id,
        status=run.status,
        started_at=run.started_at,
        ended_at=run.ended_at,
        total_messages=run.total_messages,
        total_api_calls=run.total_api_calls,
        elapsed_seconds=elapsed_seconds,
        planned_seconds=planned_seconds,
        rubric_version=RUBRIC_VERSION,
        rubric_content_hash=RUBRIC_CONTENT_HASH,
        hub_prompt_stamp=prompt_set_stamp("scout_hub"),
        pi_prompt_stamp=prompt_set_stamp("pi_lab"),
        build_info=get_build_info(),
        run_start_announcement=config.get("run_start_announcement"),
    )


# ---------------------------------------------------------------------------
# cost_summary
# ---------------------------------------------------------------------------


def _token_sums_stmt(run_id: uuid.UUID, *group_cols: Any):
    return (
        select(
            *group_cols,
            LlmCallLog.model,
            func.count(),
            func.coalesce(func.sum(LlmCallLog.input_tokens), 0),
            func.coalesce(func.sum(LlmCallLog.output_tokens), 0),
            func.coalesce(func.sum(LlmCallLog.cache_read_input_tokens), 0),
            func.coalesce(func.sum(LlmCallLog.cache_creation_input_tokens), 0),
        )
        .where(LlmCallLog.simulation_run_id == run_id)
        .group_by(*group_cols, LlmCallLog.model)
    )


async def _grouped_cost(
    db: AsyncSession, run_id: uuid.UUID, key_col: Any,
) -> dict[str, dict[str, Any]]:
    """agent_id/phase -> model -> tokens, reduced to key -> {cost, calls}."""
    rows = (await db.execute(_token_sums_stmt(run_id, key_col))).all()
    agg: dict[str, dict[str, Any]] = {}
    for key, model, n, inp, out, cread, ccreate in rows:
        bucket = agg.setdefault(key, {"cost": Decimal(0), "calls": 0})
        bucket["calls"] += int(n)
        cost = cost_for_tokens(
            model, input_tokens=int(inp), output_tokens=int(out),
            cache_read=int(cread), cache_creation=int(ccreate),
        )
        if cost is not None:
            bucket["cost"] += cost
    return agg


async def cost_summary(db: AsyncSession, run_id: uuid.UUID) -> CostSummary:
    model_rows = (
        await db.execute(
            select(
                LlmCallLog.model,
                func.coalesce(func.sum(LlmCallLog.input_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.output_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.cache_read_input_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.cache_creation_input_tokens), 0),
            )
            .where(LlmCallLog.simulation_run_id == run_id)
            .group_by(LlmCallLog.model)
        )
    ).all()

    by_model: list[ModelCost] = []
    unpriced_models: list[str] = []
    total = Decimal(0)
    total_input = total_output = total_cache_read = total_cache_creation = 0
    for model, inp, out, cread, ccreate in model_rows:
        inp, out, cread, ccreate = int(inp), int(out), int(cread), int(ccreate)
        cost = cost_for_tokens(
            model, input_tokens=inp, output_tokens=out,
            cache_read=cread, cache_creation=ccreate,
        )
        if cost is None:
            unpriced_models.append(model)
        else:
            total += cost
        by_model.append(ModelCost(
            model=model, input_tokens=inp, output_tokens=out,
            cache_read_tokens=cread, cache_creation_tokens=ccreate, cost=cost,
        ))
        total_input += inp
        total_output += out
        total_cache_read += cread
        total_cache_creation += ccreate

    is_floor = (
        await db.execute(
            select(func.count()).select_from(LlmCallLog).where(
                LlmCallLog.simulation_run_id == run_id,
                LlmCallLog.cache_read_input_tokens.is_(None),
            )
        )
    ).scalar_one() > 0

    by_agent_agg = await _grouped_cost(db, run_id, LlmCallLog.agent_id)
    by_phase_agg = await _grouped_cost(db, run_id, LlmCallLog.phase)

    return CostSummary(
        by_model=by_model,
        total=total,
        unpriced_models=unpriced_models,
        is_floor=is_floor,
        by_agent=[
            AgentCost(agent_id=k, cost=v["cost"], call_count=v["calls"])
            for k, v in sorted(by_agent_agg.items())
        ],
        by_phase=[
            PhaseCost(phase=k, cost=v["cost"], call_count=v["calls"])
            for k, v in sorted(by_phase_agg.items())
        ],
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        total_cache_read_tokens=total_cache_read,
        total_cache_creation_tokens=total_cache_creation,
    )


# ---------------------------------------------------------------------------
# hourly_activity
# ---------------------------------------------------------------------------


async def hourly_activity(db: AsyncSession, run_id: uuid.UUID) -> list[HourBucket]:
    hour_expr = func.date_trunc("hour", LlmCallLog.created_at)
    rows = (
        await db.execute(
            select(
                hour_expr.label("hour"),
                LlmCallLog.model,
                func.count(),
                func.coalesce(func.sum(LlmCallLog.input_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.output_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.cache_read_input_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.cache_creation_input_tokens), 0),
            )
            .where(LlmCallLog.simulation_run_id == run_id)
            .group_by(hour_expr, LlmCallLog.model)
            .order_by(hour_expr)
        )
    ).all()

    buckets: dict[datetime, dict[str, Any]] = {}
    for hour, model, n, inp, out, cread, ccreate in rows:
        b = buckets.setdefault(hour, {
            "calls": 0, "cost": Decimal(0), "input": 0, "output": 0,
            "cache_read": 0, "cache_creation": 0,
        })
        b["calls"] += int(n)
        b["input"] += int(inp)
        b["output"] += int(out)
        b["cache_read"] += int(cread)
        b["cache_creation"] += int(ccreate)
        cost = cost_for_tokens(
            model, input_tokens=int(inp), output_tokens=int(out),
            cache_read=int(cread), cache_creation=int(ccreate),
        )
        if cost is not None:
            b["cost"] += cost

    return [
        HourBucket(
            hour=hour, calls=b["calls"], cost=b["cost"],
            input_tokens=b["input"], output_tokens=b["output"],
            cache_read_tokens=b["cache_read"], cache_creation_tokens=b["cache_creation"],
        )
        for hour, b in sorted(buckets.items())
    ]


# ---------------------------------------------------------------------------
# funnel
# ---------------------------------------------------------------------------


async def funnel(db: AsyncSession, run_id: uuid.UUID) -> Funnel:
    assessment_rows = (
        await db.execute(
            select(OpportunityAssessment.thread_id, OpportunityAssessment.summary_posted_at)
            .where(OpportunityAssessment.simulation_run_id == run_id)
        )
    ).all()
    closed_ids = set(
        (
            await db.execute(
                select(ThreadDecision.thread_id).where(
                    ThreadDecision.simulation_run_id == run_id
                )
            )
        ).scalars().all()
    )

    verdicts_stored = len(assessment_rows)
    terminal = sum(
        1 for tid, _ in assessment_rows if tid is not None and tid in closed_ids
    )
    provisional = verdicts_stored - terminal
    announced = sum(1 for _, posted in assessment_rows if posted is not None)
    headlines_owed = sum(
        1
        for tid, posted in assessment_rows
        if tid is not None and tid in closed_ids and posted is None
    )
    opened_ids = {tid for tid, _ in assessment_rows if tid is not None} | closed_ids

    drop_rows = (
        await db.execute(
            select(AssessmentDrop.reason, func.count())
            .where(AssessmentDrop.simulation_run_id == run_id)
            .group_by(AssessmentDrop.reason)
        )
    ).all()

    unvetted_panel_count = (
        await db.execute(
            select(func.count()).select_from(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id,
                unvetted_panel_filter(),
            )
        )
    ).scalar_one()

    return Funnel(
        interviews_opened=len(opened_ids),
        verdicts_stored=verdicts_stored,
        terminal=terminal,
        provisional=provisional,
        announced=announced,
        headlines_owed=headlines_owed,
        drops_by_reason={reason: int(n) for reason, n in drop_rows},
        unvetted_panel_count=unvetted_panel_count,
    )


# ---------------------------------------------------------------------------
# specialist_mix / consult_fanout
# ---------------------------------------------------------------------------


async def specialist_mix(db: AsyncSession, run_id: uuid.UUID) -> list[DomainMix]:
    """`specialist_consults` grouped by domain x signal. The live three
    (`VERDICT_SIGNALS`) get their own column; anything else (the retired
    `caution`/`clear` pair, or any other unexpected value) is folded into
    `historical` — counted, never dropped."""
    rows = (
        await db.execute(
            select(SpecialistConsult.domain, SpecialistConsult.verdict_signal, func.count())
            .where(SpecialistConsult.simulation_run_id == run_id)
            .group_by(SpecialistConsult.domain, SpecialistConsult.verdict_signal)
        )
    ).all()

    mix: dict[str, dict[str, int]] = defaultdict(
        lambda: {"blocking": 0, "gap": 0, "adequate": 0, "historical": 0}
    )
    for domain, signal, count in rows:
        key = signal if signal in VERDICT_SIGNALS else "historical"
        mix[domain][key] += int(count)

    return [
        DomainMix(
            domain=domain, blocking=v["blocking"], gap=v["gap"],
            adequate=v["adequate"], historical=v["historical"], total=sum(v.values()),
        )
        for domain, v in sorted(mix.items())
    ]


async def consult_fanout(db: AsyncSession, run_id: uuid.UUID) -> list[ConsultFanoutBucket]:
    """The panel fan-out distribution (spec §4): how many consults each
    interview thread received, bucketed by that count. `thread_id`-less
    consults (pre-existing rows with no thread attribution) are excluded —
    there is no interview to attribute the fan-out to."""
    rows = (
        await db.execute(
            select(SpecialistConsult.thread_id, func.count())
            .where(
                SpecialistConsult.simulation_run_id == run_id,
                SpecialistConsult.thread_id.is_not(None),
            )
            .group_by(SpecialistConsult.thread_id)
        )
    ).all()
    counts = Counter(int(n) for _, n in rows)
    return [
        ConsultFanoutBucket(consult_count=k, interview_count=v)
        for k, v in sorted(counts.items())
    ]


# ---------------------------------------------------------------------------
# per_agent
# ---------------------------------------------------------------------------


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


async def per_agent(db: AsyncSession, run_id: uuid.UUID) -> list[AgentRow]:
    """DB-only: role/registry status/muted plus messages/calls/cost/last
    activity per agent_id. The route (not this function) merges in the live
    heartbeat detail."""
    call_rows = (
        await db.execute(
            select(
                LlmCallLog.agent_id,
                LlmCallLog.model,
                func.count(),
                func.coalesce(func.sum(LlmCallLog.input_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.output_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.cache_read_input_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.cache_creation_input_tokens), 0),
                func.max(LlmCallLog.created_at),
            )
            .where(LlmCallLog.simulation_run_id == run_id)
            .group_by(LlmCallLog.agent_id, LlmCallLog.model)
        )
    ).all()

    msg_rows = (
        await db.execute(
            select(AgentMessage.agent_id, func.count(), func.max(AgentMessage.created_at))
            .where(
                AgentMessage.simulation_run_id == run_id,
                AgentMessage.agent_id.is_not(None),
            )
            .group_by(AgentMessage.agent_id)
        )
    ).all()

    registry = {
        agent_id: (role, status, muted_at)
        for agent_id, role, status, muted_at in (
            await db.execute(
                select(
                    AgentRegistry.agent_id, AgentRegistry.role,
                    AgentRegistry.status, AgentRegistry.muted_at,
                )
            )
        ).all()
    }

    agg: dict[str, dict[str, Any]] = {}
    for agent_id, model, n, inp, out, cread, ccreate, last_at in call_rows:
        bucket = agg.setdefault(
            agent_id, {"messages": 0, "calls": 0, "cost": Decimal(0), "last": None}
        )
        bucket["calls"] += int(n)
        cost = cost_for_tokens(
            model, input_tokens=int(inp), output_tokens=int(out),
            cache_read=int(cread), cache_creation=int(ccreate),
        )
        if cost is not None:
            bucket["cost"] += cost
        bucket["last"] = _later(bucket["last"], last_at)

    for agent_id, n, last_at in msg_rows:
        bucket = agg.setdefault(
            agent_id, {"messages": 0, "calls": 0, "cost": Decimal(0), "last": None}
        )
        bucket["messages"] += int(n)
        bucket["last"] = _later(bucket["last"], last_at)

    rows = []
    for agent_id, bucket in sorted(agg.items()):
        role, status, muted_at = registry.get(agent_id, (None, None, None))
        rows.append(AgentRow(
            agent_id=agent_id, role=role, registry_status=status,
            muted=muted_at is not None,
            message_count=bucket["messages"], call_count=bucket["calls"],
            cost=bucket["cost"], last_activity=bucket["last"],
        ))
    return rows


# ---------------------------------------------------------------------------
# stop_reason_taxonomy / latency_percentiles
# ---------------------------------------------------------------------------


async def _fetch_call_stat_rows(
    db: AsyncSession, run_id: uuid.UUID,
) -> list[tuple[str, list | None]]:
    stmt = (
        select(LlmCallLog.phase, LlmCallLog.call_stats)
        .where(LlmCallLog.simulation_run_id == run_id)
        .order_by(LlmCallLog.created_at.desc())
        .limit(CALL_STATS_ROW_LIMIT)
    )
    return list((await db.execute(stmt)).all())


def _taxonomy_bucket(stop_reason: str | None) -> str:
    if stop_reason in ("end_turn", "tool_use"):
        return "normal"
    if stop_reason == "max_tokens":
        return "truncated"
    if stop_reason == "refusal":
        return "refused"
    if stop_reason is None:
        return "unknown"
    return stop_reason


async def stop_reason_taxonomy(db: AsyncSession, run_id: uuid.UUID) -> dict[str, int]:
    rows = await _fetch_call_stat_rows(db, run_id)
    counts: Counter[str] = Counter()
    for _phase, call_stats in rows:
        if not call_stats:
            continue
        for entry in call_stats:
            counts[_taxonomy_bucket(entry.get("stop_reason"))] += 1
    return dict(counts)


def _percentiles(values: list[float]) -> LatencyPcts:
    n = len(values)
    if n < 2:
        return LatencyPcts(n=n, p50=None, p95=None, p99=None)
    cuts = statistics.quantiles(values, n=100)
    return LatencyPcts(n=n, p50=cuts[49], p95=cuts[94], p99=cuts[98])


async def latency_percentiles(
    db: AsyncSession, run_id: uuid.UUID,
) -> dict[str, LatencyPcts]:
    """P50/P95/P99 per phase from `call_stats`, plus an "overall" key across
    every phase. `statistics.quantiles(data, n=100)` returns 99 cut points
    (P50/P95/P99 = indices 49/94/98) and raises StatisticsError below 2
    samples — `_percentiles` guards that, returning an all-None percentile
    set instead."""
    rows = await _fetch_call_stat_rows(db, run_id)
    by_phase: dict[str, list[float]] = defaultdict(list)
    overall: list[float] = []
    for phase, call_stats in rows:
        if not call_stats:
            continue
        for entry in call_stats:
            latency = entry.get("latency_ms")
            if latency is None:
                continue
            lat = float(latency)
            by_phase[phase].append(lat)
            overall.append(lat)

    result = {phase: _percentiles(values) for phase, values in by_phase.items()}
    result["overall"] = _percentiles(overall)
    return result


# ---------------------------------------------------------------------------
# interview_timeline
# ---------------------------------------------------------------------------


async def interview_timeline(db: AsyncSession, run_id: uuid.UUID) -> list[InterviewSpan]:
    rows = (
        await db.execute(
            select(
                OpportunityAssessment.id, OpportunityAssessment.thread_id,
                OpportunityAssessment.subject_agent_id, OpportunityAssessment.recommendation,
                OpportunityAssessment.summary_posted_at,
            )
            .where(
                OpportunityAssessment.simulation_run_id == run_id,
                OpportunityAssessment.thread_id.is_not(None),
            )
            .order_by(OpportunityAssessment.thread_id)
        )
    ).all()
    if not rows:
        return []

    thread_ids = [thread_id for _, thread_id, _, _, _ in rows]
    span_rows = (
        await db.execute(
            select(
                AgentMessage.thread_ts,
                func.min(AgentMessage.posted_at),
                func.max(AgentMessage.posted_at),
            )
            .where(
                AgentMessage.simulation_run_id == run_id,
                AgentMessage.thread_ts.in_(thread_ids),
            )
            .group_by(AgentMessage.thread_ts)
        )
    ).all()
    spans_by_thread = {thread_ts: (first, last) for thread_ts, first, last in span_rows}

    result = []
    for assessment_id, thread_id, subject, recommendation, posted_at in rows:
        first_at, last_at = spans_by_thread.get(thread_id, (None, None))
        result.append(InterviewSpan(
            assessment_id=assessment_id, thread_id=thread_id, subject_agent_id=subject,
            first_message_at=first_at, last_message_at=last_at,
            outcome=recommendation or "in flight", announced=posted_at is not None,
        ))
    return result


# ---------------------------------------------------------------------------
# hub_lab_burn
# ---------------------------------------------------------------------------


async def hub_lab_burn(
    db: AsyncSession, run_id: uuid.UUID, hub_agent_id: str,
) -> list[BurnPoint]:
    """Hourly hub-vs-lab token ratio. `hub_agent_id` is the roster's
    `scout_hub` agent_id, passed in by the caller (this module has no roster
    access of its own)."""
    hour_expr = func.date_trunc("hour", LlmCallLog.created_at)
    total_tokens = (
        LlmCallLog.input_tokens + LlmCallLog.output_tokens
        + func.coalesce(LlmCallLog.cache_read_input_tokens, 0)
        + func.coalesce(LlmCallLog.cache_creation_input_tokens, 0)
    )
    rows = (
        await db.execute(
            select(
                hour_expr.label("hour"),
                func.coalesce(
                    func.sum(case(
                        (LlmCallLog.agent_id == hub_agent_id, total_tokens), else_=0,
                    )), 0,
                ),
                func.coalesce(
                    func.sum(case(
                        (LlmCallLog.agent_id != hub_agent_id, total_tokens), else_=0,
                    )), 0,
                ),
            )
            .where(LlmCallLog.simulation_run_id == run_id)
            .group_by(hour_expr)
            .order_by(hour_expr)
        )
    ).all()
    return [
        BurnPoint(
            hour=hour, hub_tokens=int(hub_tok), lab_tokens=int(lab_tok),
            ratio=(hub_tok / lab_tok) if lab_tok else None,
        )
        for hour, hub_tok, lab_tok in rows
    ]


# ---------------------------------------------------------------------------
# cost_per_interview
# ---------------------------------------------------------------------------


async def cost_per_interview(db: AsyncSession, run_id: uuid.UUID) -> list[InterviewCost]:
    """`llm_call_logs` grouped by `thread_ts`, joined to the assessment's
    recommendation for an outcome label. Always includes the `thread_ts=None`
    unattributed bucket (rows with no thread_ts — written before migration
    0042, or from a call site that never learned its thread) even when it is
    zero, so a reader can see the per-interview figures do not sum to the run
    total without having to guess why."""
    rows = (
        await db.execute(
            select(
                LlmCallLog.thread_ts,
                LlmCallLog.model,
                func.coalesce(func.sum(LlmCallLog.input_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.output_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.cache_read_input_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.cache_creation_input_tokens), 0),
                func.sum(case(
                    (LlmCallLog.cache_read_input_tokens.is_(None), 1), else_=0,
                )),
            )
            .where(LlmCallLog.simulation_run_id == run_id)
            .group_by(LlmCallLog.thread_ts, LlmCallLog.model)
        )
    ).all()

    outcomes = dict(
        (
            await db.execute(
                select(OpportunityAssessment.thread_id, OpportunityAssessment.recommendation)
                .where(
                    OpportunityAssessment.simulation_run_id == run_id,
                    OpportunityAssessment.thread_id.is_not(None),
                )
            )
        ).all()
    )

    per_thread: dict[str | None, dict[str, Any]] = {}
    for thread_ts, model, inp, out, cread, ccreate, null_cache_n in rows:
        bucket = per_thread.setdefault(thread_ts, {"cost": Decimal(0), "floor": False})
        cost = cost_for_tokens(
            model, input_tokens=int(inp), output_tokens=int(out),
            cache_read=int(cread), cache_creation=int(ccreate),
        )
        if cost is not None:
            bucket["cost"] += cost
        if (null_cache_n or 0) > 0:
            bucket["floor"] = True

    attributed = sorted(
        ((tid, b) for tid, b in per_thread.items() if tid is not None),
        key=lambda kv: kv[0],
    )
    results = [
        InterviewCost(thread_ts=tid, outcome=outcomes.get(tid), cost=b["cost"], is_floor=b["floor"])
        for tid, b in attributed
    ]
    unattributed = per_thread.get(None, {"cost": Decimal(0), "floor": False})
    results.append(InterviewCost(
        thread_ts=None, outcome=None,
        cost=unattributed["cost"], is_floor=unattributed["floor"],
    ))
    return results

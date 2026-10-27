"""The projects prototype's measures for one run, and one project's refinement cost.

The measures are those of the projects design's §2 ("reported, no targets"): projects
requested per lab, the outcomes and the reasons a project did not finish, research and
refinement cost, the most common gaps and disagreements, which profile sections anchor
ideas, sources by kind, project reviews beside the run's assessment reviews, and the run
time from request to finish.
``scripts/projects_report.py`` renders ``build_report``; the admin's project details page
shows ``refinement_cost``.

Read-only. Money is ``Decimal``; an unpriced model counts zero and is named in
``cost.unpriced_models``, never silently.
"""

from __future__ import annotations

import statistics
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    AssessmentReview,
    HubProject,
    HubProjectCall,
    HubProjectClock,
    HubProjectReview,
    LlmCallLog,
    OpportunityAssessment,
    SimulationRun,
)
from src.models.hub_project import STATUS_NOT_FINISHED
from src.services.llm_pricing import cost_for_tokens
from src.services.project_research import ledger_row_cost
from src.services.projects_view import REASON_LABELS, display_outcome
from src.services.runs import live_run_id

#: The phase the lab's reply step logs. That call is made before the project thread
#: exists, so it carries no thread id and is attributed by run, lab, phase and time.
REPLY_STEP_PHASE = "project_reply"

#: The reason code of an open project in a finalized run (``REASON_LABELS`` has it).
REASON_RUN_FINALIZED = "run_finalized"

#: Outcome kinds that are an outcome; the shares are over these alone, since a paused or
#: in-progress project has none yet.
_FINISHED_KINDS = ("ready", "not_agreed", "not_finished")

_TOP_TEXTS = 10


async def _refinement(db: AsyncSession, project: HubProject) -> tuple[Decimal, set[str]]:
    """(dollars, unpriced models) of the project's refinement: the run's calls on its
    thread (``thread_ts = reply_ts``), plus the lab's reply-step calls (same run and lab,
    phase ``project_reply``) made between the posting and the finish, or now while open.
    A lab has at most one open project, so that window holds only this project's step."""
    conds = []
    if project.reply_ts:
        conds.append(LlmCallLog.thread_ts == project.reply_ts)
    if project.posted_at is not None:
        end = project.finished_at or datetime.now(UTC)
        conds.append(
            and_(
                LlmCallLog.agent_id == project.agent_id,
                LlmCallLog.phase == REPLY_STEP_PHASE,
                LlmCallLog.created_at >= project.posted_at,
                LlmCallLog.created_at <= end,
            )
        )
    if not conds:
        return Decimal(0), set()
    rows = (
        await db.execute(
            select(
                LlmCallLog.model,
                func.coalesce(func.sum(LlmCallLog.input_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.output_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.cache_read_input_tokens), 0),
                func.coalesce(func.sum(LlmCallLog.cache_creation_input_tokens), 0),
            )
            .where(LlmCallLog.simulation_run_id == project.simulation_run_id, or_(*conds))
            .group_by(LlmCallLog.model)
        )
    ).all()
    total = Decimal(0)
    unpriced: set[str] = set()
    for model, inp, out, cread, ccreate in rows:
        cost = cost_for_tokens(
            model, input_tokens=int(inp), output_tokens=int(out),
            cache_read=int(cread), cache_creation=int(ccreate),
        )
        if cost is None:
            unpriced.add(model)
        else:
            total += cost
    return total, unpriced


async def refinement_cost(db: AsyncSession, project: HubProject) -> Decimal:
    """Dollars of the project's refinement (see ``_refinement``); an unpriced model
    counts zero."""
    return (await _refinement(db, project))[0]


def _texts(final: object, key: str) -> list[str]:
    values = final.get(key) if isinstance(final, dict) else None
    return [v for v in values if isinstance(v, str) and v] if isinstance(values, list) else []


def _choice_items(choice: object, key: str) -> list[dict[str, Any]]:
    values = choice.get(key) if isinstance(choice, dict) else None
    return [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


async def _research_costs(db: AsyncSession, run_id: uuid.UUID) -> dict[uuid.UUID, Decimal]:
    """Each project's research dollars: its ledger rows priced with ``ledger_row_cost``
    (a call that never finished counts its reserve)."""
    rows = (
        await db.execute(
            select(
                HubProjectCall.project_id,
                HubProjectCall.usage_by_model,
                HubProjectCall.model,
                HubProjectCall.step,
            )
            .join(HubProject, HubProject.id == HubProjectCall.project_id)
            .where(HubProject.simulation_run_id == run_id)
        )
    ).all()
    costs: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    for project_id, usage, model, step in rows:
        costs[project_id] += ledger_row_cost(usage, model, step)
    return costs


async def _review_figures(db: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    """Project reviews of the run beside its assessment reviews (labs' own pitches)."""
    project_reviews = (
        await db.execute(
            select(HubProjectReview.rating, HubProjectReview.fund)
            .join(HubProject, HubProject.id == HubProjectReview.project_id)
            .where(HubProject.simulation_run_id == run_id)
        )
    ).all()
    assessment_scores = (
        await db.execute(
            select(AssessmentReview.score)
            .join(OpportunityAssessment, OpportunityAssessment.id == AssessmentReview.assessment_id)
            .where(OpportunityAssessment.simulation_run_id == run_id)
        )
    ).scalars().all()
    n = len(project_reviews)
    return {
        "project_review_count": n,
        "project_mean_rating": _mean([float(r) for r, _ in project_reviews]),
        "project_fund_share": (sum(1 for _, f in project_reviews if f) / n) if n else None,
        "assessment_review_count": len(assessment_scores),
        "assessment_mean_score": _mean([float(s) for s in assessment_scores]),
    }


def _lab_rows(projects: list[HubProject], clock: float) -> list[dict[str, Any]]:
    """Per lab with at least one project in the run: its requests, in all and per day of
    run time (``clock`` seconds)."""
    by_lab: Counter[str] = Counter(p.agent_id for p in projects)
    run_days = clock / 86400
    return [
        {
            "agent_id": agent_id,
            "requested": by_lab[agent_id],
            "per_run_day": (by_lab[agent_id] / run_days) if run_days else None,
        }
        for agent_id in sorted(by_lab)
    ]


async def build_report(db: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    """The run's project measures as plain values (``Decimal`` dollars, float hours).

    Each project is classified with ``display_outcome``, so an open project of a
    finalized run counts as not finished with reason ``run_finalized`` (derived from
    ``finalized_at``, nothing stored). Outcome shares are over finished outcomes only.
    Labs are those with at least one project in the run; a lab never requested in it
    is not listed. Raises ``LookupError`` for an unknown run."""
    run = await db.get(SimulationRun, run_id)
    if run is None:
        raise LookupError(f"no simulation run {run_id}")
    run_finalized = run.finalized_at is not None
    run_live = (await live_run_id(db)) == run_id
    clock = (
        await db.execute(
            select(HubProjectClock.run_seconds).where(HubProjectClock.simulation_run_id == run_id)
        )
    ).scalar_one_or_none() or 0.0
    projects = list(
        (
            await db.execute(
                select(HubProject)
                .where(HubProject.simulation_run_id == run_id)
                .order_by(HubProject.agent_id, HubProject.slot)
            )
        ).scalars().all()
    )
    labs = _lab_rows(projects, clock)
    research = await _research_costs(db, run_id)

    outcome_counts: Counter[str] = Counter()
    reason_counts: Counter[str | None] = Counter()
    gaps: Counter[str] = Counter()
    disagreements: Counter[str] = Counter()
    anchor_sections: Counter[str] = Counter()
    sources_by_kind: Counter[str] = Counter()
    with_choice = 0
    unpriced: set[str] = set()
    hours: list[float] = []
    rows = []
    for p in projects:
        kind, label, reason_label = display_outcome(
            p.status, p.reason_code, run_finalized=run_finalized, run_live=run_live
        )
        reason_code = None
        if kind == "not_finished":
            reason_code = p.reason_code if p.status == STATUS_NOT_FINISHED else REASON_RUN_FINALIZED
            reason_counts[reason_code] += 1
        outcome_counts[kind] += 1
        gaps.update(_texts(p.final, "gaps"))
        disagreements.update(_texts(p.final, "disagreements"))
        if isinstance(p.choice, dict):
            with_choice += 1
            anchor_sections.update(str(a.get("section")) for a in _choice_items(p.choice, "anchors"))
            sources_by_kind.update(str(s.get("kind")) for s in _choice_items(p.choice, "sources"))
        refinement, missing = await _refinement(db, p)
        unpriced |= missing
        to_finish = None
        if p.finished_run_seconds is not None:
            to_finish = (p.finished_run_seconds - p.requested_run_seconds) / 3600
            hours.append(to_finish)
        rows.append({
            "id": str(p.id),
            "agent_id": p.agent_id,
            "slot": p.slot,
            "title": p.title,
            "outcome_kind": kind,
            "outcome_label": label,
            "reason_code": reason_code,
            "reason_label": reason_label,
            "research_usd": research.get(p.id, Decimal(0)),
            "refinement_usd": refinement,
            "hours_to_finish": to_finish,
        })

    finished = sum(outcome_counts[k] for k in _FINISHED_KINDS)
    research_total = sum((r["research_usd"] for r in rows), Decimal(0))
    refinement_total = sum((r["refinement_usd"] for r in rows), Decimal(0))
    return {
        "run_id": str(run_id),
        "run_finalized": run_finalized,
        "run_live": run_live,
        "run_hours": clock / 3600,
        "project_count": len(projects),
        "labs": labs,
        "outcome_counts": dict(outcome_counts),
        "outcome_shares": {
            k: (outcome_counts[k] / finished) if finished else None for k in _FINISHED_KINDS
        },
        "not_finished_reasons": [
            {"code": code, "label": REASON_LABELS.get(code or "", "Not recorded"), "count": n}
            for code, n in reason_counts.most_common()
        ],
        "projects": rows,
        "cost": {
            "research_usd": research_total,
            "refinement_usd": refinement_total,
            "total_usd": research_total + refinement_total,
            "unpriced_models": sorted(unpriced),
        },
        "top_gaps": [{"text": t, "count": n} for t, n in gaps.most_common(_TOP_TEXTS)],
        "top_disagreements": [
            {"text": t, "count": n} for t, n in disagreements.most_common(_TOP_TEXTS)
        ],
        "anchor_sections": dict(anchor_sections.most_common()),
        "sources_by_kind": dict(sources_by_kind.most_common()),
        "sources_per_project": (
            sum(sources_by_kind.values()) / with_choice if with_choice else None
        ),
        "reviews": await _review_figures(db, run_id),
        "hours_to_finish": {
            "count": len(hours),
            "mean": _mean(hours),
            "median": statistics.median(hours) if hours else None,
        },
        "notes": [
            "Only labs with at least one project in this run are listed.",
        ],
    }

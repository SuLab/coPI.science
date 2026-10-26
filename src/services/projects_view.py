"""The Projects pages' read models: the list of a run's projects and one project's details.

Both return plain dicts for ``src.web.presentation.project_workspace``; every key is in its
``VIEW_FIELDS``, and the details' cost figures and calls link are in ``ADMIN_KEYS``. A
project's specialist consults are read without their signal, confidence or raw opinion
(D55 of the idea-pitching prototype design): the page shows what each specialist found,
never its label.

``display_outcome``, ``OUTCOME_LABELS``, ``REASON_LABELS`` and ``project_thread_roots`` are
shared with the measures report (``src.services.projects_report``) and the interview
statistics, so a project reads the same on every surface.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.persona_sections import section_items
from src.models import AgentMessage, SimulationRun, SpecialistConsult, User
from src.models.hub_project import (
    FINISHED_STATUSES,
    HubProject,
    HubProjectCall,
    HubProjectReview,
)
from src.services.interview_posts import merge_chunks
from src.services.profile_publish import persona_file_text
from src.services.project_plan import TEMPLATE_FIELDS, display_id, tool_marks
from src.services.project_research import ledger_row_cost
from src.services.publication_links import pubmed_url
from src.services.runs import live_run_id, run_bar_entries, runs_ordered

#: ``agent_messages.phase`` of BlackbirdBot's posting and of the lab's reply that roots the
#: project thread (``src/agent/message_log.py`` names them for the engine).
_PHASE_HUB_PROJECT = "hub_project"
_PHASE_PROJECT_REPLY = "project_reply"
_PHASE_THREAD_REPLY = "thread_reply"

OUTCOME_LABELS = {
    "ready": "Ready",
    "not_agreed": "Not agreed",
    "not_finished": "Not finished",
    "paused": "Paused",
    "in_progress": "In progress",
}

#: Why a project did not finish, in words. ``run_finalized`` is never stored: it is derived
#: for an open project of a finalized run (``display_outcome``).
REASON_LABELS = {
    "no_idea": "No idea found",
    "research_failed": "Research failed",
    "choice_failed": "The choice did not pass its checks",
    "run_ended_before_posting": "The run ended before posting",
    "lab_not_live": "The lab was not live",
    "reply_failed": "The lab's reply could not be posted",
    "thread_timeout": "The thread ended without a close",
    "thread_abandoned": "The thread was abandoned",
    "close_unreadable": "BlackbirdBot's close could not be recorded",
    "run_finalized": "The run was finalized first",
}

#: The list's outcome filter: ``""`` (the default) is ready and not agreed; ``paused``
#: also takes in-progress projects.
_OUTCOME_FILTERS = {
    "": frozenset({"ready", "not_agreed"}),
    "ready": frozenset({"ready"}),
    "not_agreed": frozenset({"not_agreed"}),
    "not_finished": frozenset({"not_finished"}),
    "paused": frozenset({"paused", "in_progress"}),
}

_POSITION_LABELS = {"agree": "Agreed", "disagree": "Not agreed"}
_NCT_RE = re.compile(r"NCT\d{8}")
_TRIAL_URL = "https://clinicaltrials.gov/study/{}"


def display_outcome(
    status: str, reason_code: str | None, *, run_finalized: bool, run_live: bool
) -> tuple[str, str, str | None]:
    """``(kind, label, reason_label)`` of a project as the pages and the report show it.

    A finished status is its own kind, with its stored reason in words. An open status
    depends on its run: ``not_finished`` with the reason "the run was finalized first" when
    the run is finalized (it can never resume), ``in_progress`` while the run is live, else
    ``paused``.
    """
    if status in FINISHED_STATUSES:
        reason = REASON_LABELS.get(reason_code, reason_code) if reason_code else None
        return status, OUTCOME_LABELS[status], reason
    if run_finalized:
        return "not_finished", OUTCOME_LABELS["not_finished"], REASON_LABELS["run_finalized"]
    if run_live:
        return "in_progress", OUTCOME_LABELS["in_progress"], None
    return "paused", OUTCOME_LABELS["paused"], None


def project_thread_roots(run_id: uuid.UUID | None = None) -> Select:
    """The ids of project threads: the ``message_ts`` of each lab reply that roots one.

    Of run ``run_id``, or of every run when it is None. ``message_ts`` is nullable, and a
    NULL inside ``NOT IN`` would exclude every row, so NULLs are left out here; a caller
    excluding these ids from a nullable column still needs ``col IS NULL OR col NOT IN``.
    """
    stmt = select(AgentMessage.message_ts).where(
        AgentMessage.phase == _PHASE_PROJECT_REPLY,
        AgentMessage.thread_ts.is_(None),
        AgentMessage.message_ts.is_not(None),
    )
    if run_id is not None:
        stmt = stmt.where(AgentMessage.simulation_run_id == run_id)
    return stmt


def _select_run(
    runs: list[SimulationRun], run_id: str | None, counts: Mapping[uuid.UUID, int]
) -> SimulationRun | None:
    """The requested run when it names one in ``runs``; else the newest run with a project;
    else the newest run."""
    if run_id:
        try:
            wanted = uuid.UUID(run_id)
        except ValueError:
            wanted = None
        for run in runs:
            if run.id == wanted:
                return run
    for run in runs:
        if counts.get(run.id, 0) > 0:
            return run
    return runs[0] if runs else None


def _mean_rating(ratings: Sequence[int]) -> float | None:
    return round(sum(ratings) / len(ratings), 1) if ratings else None


def _sort_key(row: HubProject) -> tuple:
    """Finished projects first, newest finish first; then open ones, newest request first."""
    finished = row.status in FINISHED_STATUSES
    moment = (row.finished_at if finished else None) or row.requested_at
    return (0 if finished else 1, -moment.timestamp() if moment else 0.0)


async def build_projects_list(
    db: AsyncSession,
    *,
    viewer: User,
    impersonating: bool,
    run_id: str | None,
    lab: str | None,
    outcome: str | None,
    mine: bool,
) -> dict:
    """The Projects list for one run.

    Keys: ``run_bar`` (``run_bar_entries`` with projects per run), ``selected_run_id`` (the
    requested run, else the newest with a project, else the newest; None with no runs; no
    "All runs"), ``projects`` (after the filters), ``project_total``, ``lab_options``,
    ``lab_filter``, ``outcome_filter`` (a key of ``_OUTCOME_FILTERS``), ``mine`` (only
    projects the viewer has not reviewed), ``summary`` (the selected run's outcome
    ``counts``, ``review_count``, ``mean_rating`` and ``fund_share``, unfiltered) and
    ``can_review_projects`` (False under impersonation). An unknown run, lab or outcome
    falls back to its default.
    """
    runs = await runs_ordered(db)
    live_id = await live_run_id(db)
    counts = {
        rid: n
        for rid, n in (
            await db.execute(
                select(HubProject.simulation_run_id, func.count()).group_by(
                    HubProject.simulation_run_id
                )
            )
        ).all()
    }
    run = _select_run(runs, run_id, counts)
    outcome_filter = outcome if outcome in _OUTCOME_FILTERS else ""
    view: dict[str, Any] = {
        "run_bar": run_bar_entries(runs, counts, live_id=live_id),
        "selected_run_id": run.id if run is not None else None,
        "projects": [],
        "project_total": 0,
        "lab_options": [],
        "lab_filter": None,
        "outcome_filter": outcome_filter,
        "mine": mine,
        "summary": {"counts": {}, "review_count": 0, "mean_rating": None, "fund_share": None},
        "can_review_projects": not impersonating,
    }
    if run is None:
        return view

    rows = list(
        (await db.execute(select(HubProject).where(HubProject.simulation_run_id == run.id)))
        .scalars()
        .all()
    )
    reviews_by_project: dict[uuid.UUID, list[HubProjectReview]] = defaultdict(list)
    if rows:
        for review in (
            await db.execute(
                select(HubProjectReview).where(
                    HubProjectReview.project_id.in_([r.id for r in rows])
                )
            )
        ).scalars():
            reviews_by_project[review.project_id].append(review)

    lab_options = sorted({r.agent_id for r in rows})
    lab_filter = lab if lab in lab_options else None
    wanted_kinds = _OUTCOME_FILTERS[outcome_filter]
    outcome_counts: dict[str, int] = defaultdict(int)
    projects = []
    for row in sorted(rows, key=_sort_key):
        kind, label, reason_label = display_outcome(
            row.status,
            row.reason_code,
            run_finalized=run.finalized_at is not None,
            run_live=run.id == live_id,
        )
        outcome_counts[kind] += 1
        reviews = reviews_by_project.get(row.id, [])
        reviewed_by_me = any(r.reviewer_user_id == viewer.id for r in reviews)
        if kind not in wanted_kinds or (lab_filter and row.agent_id != lab_filter):
            continue
        if mine and reviewed_by_me:
            continue
        projects.append(
            {
                "id": row.id,
                "lab": row.agent_id,
                "title": row.title,
                "outcome_kind": kind,
                "outcome_label": label,
                "reason_label": reason_label,
                "finished_at": row.finished_at,
                "review_count": len(reviews),
                "mean_rating": _mean_rating([r.rating for r in reviews]),
                "fund_count": sum(1 for r in reviews if r.fund),
                "reviewed_by_me": reviewed_by_me,
            }
        )

    all_reviews = [r for group in reviews_by_project.values() for r in group]
    view.update(
        projects=projects,
        project_total=len(projects),
        lab_options=lab_options,
        lab_filter=lab_filter,
        summary={
            "counts": dict(outcome_counts),
            "review_count": len(all_reviews),
            "mean_rating": _mean_rating([r.rating for r in all_reviews]),
            "fund_share": (
                sum(1 for r in all_reviews if r.fund) / len(all_reviews) if all_reviews else None
            ),
        },
    )
    return view


def _strings(value: object) -> list[str]:
    """The string items of a stored list; anything else is ``[]``."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _plan_rows(plan: object) -> list[dict]:
    """One ``{label, value}`` per template field, in template order; a list joins with
    ``"; "`` and a missing or non-string value is ``""``."""
    plan = plan if isinstance(plan, Mapping) else {}
    rows = []
    for key, label in TEMPLATE_FIELDS:
        value = plan.get(key)
        if isinstance(value, list):
            text = "; ".join(_strings(value))
        else:
            text = value.strip() if isinstance(value, str) else ""
        rows.append({"label": label, "value": text})
    return rows


def _tool_rows(final: Mapping, plan: Mapping, agent_id: str) -> list[dict]:
    """The close's own tool marks; before a close, the plan's tools checked against the
    lab's current public profile."""
    marks = final.get("tool_marks")
    if not isinstance(marks, list):
        items = section_items(persona_file_text(agent_id))
        marks = tool_marks(_strings(plan.get("lab_tools")), items)
    return [
        {"item": m["item"], "in_profile": bool(m.get("in_profile"))}
        for m in marks
        if isinstance(m, Mapping) and isinstance(m.get("item"), str)
    ]


def _position_label(value: object) -> str | None:
    """``"Agreed"`` or ``"Not agreed"`` for a stored close position; None without a close."""
    return _POSITION_LABELS.get(value) if isinstance(value, str) else None


def _source_url(key: str) -> str | None:
    """PubMed for a paper (through ``pubmed_url``, the one PubMed link builder), the
    registry's study page for a trial, and no link for a patent."""
    if key.startswith("pmid:"):
        return pubmed_url(key[len("pmid:") :])
    if key.startswith("nct:"):
        nct = key[len("nct:") :]
        return _TRIAL_URL.format(nct) if _NCT_RE.fullmatch(nct) else None
    return None


def _choice_parts(choice: Mapping) -> dict:
    """Anchors, sources and the other candidates from the stored choice."""
    anchors = [
        {"section": a.get("section"), "item": a.get("item")}
        for a in choice.get("anchors") or []
        if isinstance(a, Mapping)
    ]
    sources = []
    for s in choice.get("sources") or []:
        if not isinstance(s, Mapping) or not isinstance(s.get("key"), str):
            continue
        sources.append(
            {
                "kind": s.get("kind"),
                "ident": display_id(s["key"]),
                "title": s.get("title"),
                "url": _source_url(s["key"]),
                "specialist": s.get("domain"),
            }
        )
    others = [
        {"use": o.get("use"), "note": o.get("note")}
        for o in choice.get("others") or []
        if isinstance(o, Mapping)
    ]
    return {"anchors": anchors, "sources": sources, "others": others}


async def _timeline(db: AsyncSession, project: HubProject) -> list[dict]:
    """The posting, then the project thread's posts with their chunks merged."""
    run_id = project.simulation_run_id
    columns = (
        AgentMessage.id,
        AgentMessage.agent_id,
        AgentMessage.sender_name,
        AgentMessage.phase,
        AgentMessage.posted_at,
        AgentMessage.content,
    )
    timeline = []
    if project.posting_ts:
        posting = (
            await db.execute(
                select(*columns)
                .where(
                    AgentMessage.simulation_run_id == run_id,
                    AgentMessage.phase == _PHASE_HUB_PROJECT,
                    AgentMessage.message_ts == project.posting_ts,
                )
                .order_by(AgentMessage.posted_at, AgentMessage.created_at)
                .limit(1)
            )
        ).first()
        if posting is not None:
            timeline.append(
                {
                    "speaker": posting.sender_name,
                    "kind": "posting",
                    "text": posting.content,
                    "at": datetime.fromtimestamp(posting.posted_at, UTC),
                }
            )
    if project.reply_ts:
        rows = (
            await db.execute(
                select(*columns)
                .where(
                    AgentMessage.simulation_run_id == run_id,
                    or_(
                        and_(
                            AgentMessage.phase == _PHASE_PROJECT_REPLY,
                            AgentMessage.message_ts == project.reply_ts,
                            AgentMessage.thread_ts.is_(None),
                        ),
                        and_(
                            AgentMessage.phase == _PHASE_THREAD_REPLY,
                            AgentMessage.thread_ts == project.reply_ts,
                        ),
                    ),
                )
                .order_by(AgentMessage.posted_at, AgentMessage.created_at)
            )
        ).all()
        for post in merge_chunks(rows):
            timeline.append(
                {
                    "speaker": post.speaker,
                    "kind": "reply",
                    "text": post.text,
                    "at": datetime.fromtimestamp(post.posted_at, UTC),
                }
            )
    return timeline


async def _consults(db: AsyncSession, project: HubProject) -> list[dict]:
    """What each specialist consulted in the project thread found. Only the columns shown
    are selected: the signal, confidence and raw opinion never reach the page (D55)."""
    if not project.reply_ts:
        return []
    rows = (
        await db.execute(
            select(
                SpecialistConsult.domain,
                SpecialistConsult.question,
                SpecialistConsult.established,
                SpecialistConsult.concerns,
                SpecialistConsult.questions_to_ask,
            )
            .where(
                SpecialistConsult.simulation_run_id == project.simulation_run_id,
                SpecialistConsult.thread_id == project.reply_ts,
            )
            .order_by(SpecialistConsult.created_at)
        )
    ).all()
    return [
        {
            "domain": r.domain,
            "question": r.question,
            "established": _strings(r.established),
            "concerns": _strings(r.concerns),
            "questions_to_ask": _strings(r.questions_to_ask),
        }
        for r in rows
    ]


async def _research_cost(db: AsyncSession, project_id: uuid.UUID) -> Decimal:
    rows = (
        await db.execute(
            select(HubProjectCall.usage_by_model, HubProjectCall.model, HubProjectCall.step).where(
                HubProjectCall.project_id == project_id
            )
        )
    ).all()
    return sum((ledger_row_cost(r.usage_by_model, r.model, r.step) for r in rows), Decimal(0))


async def build_project_detail(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    viewer: User,
    is_admin: bool,
    impersonating: bool,
) -> dict | None:
    """One project's details as ``{"project": {...}}``, or None when it does not exist.

    The plan shown is the close's final plan, else the opening plan; ``opening_rows`` is
    always the opening plan as posted. Positions read ``"Agreed"``, ``"Not agreed"`` or
    None (no close). ``can_edit`` on a review and ``can_review_projects`` are False under
    impersonation. Only when ``is_admin``: ``research_cost_usd`` (the ledger),
    ``refinement_cost_usd`` (the thread's and the lab reply step's model calls) and
    ``calls_url`` (the admin calls page filtered to the thread; None before a reply).
    """
    project = await db.get(HubProject, project_id)
    if project is None:
        return None
    run = await db.get(SimulationRun, project.simulation_run_id)
    live_id = await live_run_id(db)
    kind, label, reason_label = display_outcome(
        project.status,
        project.reason_code,
        run_finalized=run is not None and run.finalized_at is not None,
        run_live=project.simulation_run_id == live_id,
    )
    choice = project.choice if isinstance(project.choice, Mapping) else {}
    final = project.final if isinstance(project.final, Mapping) else {}
    opening_plan = choice.get("plan") if isinstance(choice.get("plan"), Mapping) else {}
    plan = final.get("plan") if isinstance(final.get("plan"), Mapping) else opening_plan

    reviews = []
    my_review = None
    for r in (
        await db.execute(
            select(HubProjectReview)
            .where(HubProjectReview.project_id == project.id)
            .order_by(HubProjectReview.created_at, HubProjectReview.id)
        )
    ).scalars():
        entry = {
            "id": r.id,
            "reviewer_name": r.reviewer_name,
            "rating": r.rating,
            "comment": r.comment,
            "fund": r.fund,
            "created_at": r.created_at,
            "can_edit": not impersonating and r.reviewer_user_id == viewer.id,
        }
        reviews.append(entry)
        if r.reviewer_user_id == viewer.id:
            my_review = entry

    detail: dict[str, Any] = {
        "id": project.id,
        "lab": project.agent_id,
        "title": project.title or choice.get("title"),
        "summary": choice.get("summary") if isinstance(choice.get("summary"), str) else "",
        "outcome_kind": kind,
        "outcome_label": label,
        "reason_label": reason_label,
        "run_id": project.simulation_run_id,
        "requested_at": project.requested_at,
        "posted_at": project.posted_at,
        "finished_at": project.finished_at,
        "plan_rows": _plan_rows(plan),
        "opening_rows": _plan_rows(opening_plan),
        "tool_rows": _tool_rows(final, plan, project.agent_id),
        "hub_position": _position_label(final.get("hub_position")),
        "lab_position": _position_label(final.get("lab_position")),
        "gaps": _strings(final.get("gaps")),
        "disagreements": _strings(final.get("disagreements")),
        "why_chosen": choice.get("why_chosen") if isinstance(choice.get("why_chosen"), str) else "",
        **_choice_parts(choice),
        "timeline": await _timeline(db, project),
        "consults": await _consults(db, project),
        "reviews": reviews,
        "my_review": my_review,
        "can_review_projects": not impersonating,
    }
    if is_admin:
        # Imported here: projects_report imports this module's labels at its top.
        from src.services.projects_report import refinement_cost

        detail["research_cost_usd"] = await _research_cost(db, project.id)
        detail["refinement_cost_usd"] = await refinement_cost(db, project)
        detail["calls_url"] = (
            f"/admin/activity/{project.simulation_run_id}/llm-calls?"
            + urlencode({"thread": project.reply_ts})
            if project.reply_ts
            else None
        )
    return {"project": detail}

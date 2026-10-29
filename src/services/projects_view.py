"""The Projects pages' read models: the list of a run's projects and one project's details.

Both return plain dicts for ``src.web.presentation.project_workspace``; every key is
registered there, and the details' cost figures, calls link and raw choice and close are
admin-only. A project's specialist consults are read without their signal, confidence or
raw opinion (D55 of the idea-pitching prototype design): the page shows what each
specialist found, never its label, and nothing is ordered or coloured by one.

The list's state (run, lab, outcome, review tab, sort, "Not reviewed by me" and the
assignment filter) travels in URLs as ``project_list_state`` keeps it and
``project_list_query`` writes it; the list, the details page and the review routes all
use these two, so a junk value is dropped everywhere and never echoed.

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
from sqlalchemy.orm import defer

from src.agent.persona_sections import section_items
from src.agent.slack_client import split_for_wire
from src.models import AgentMessage, AgentRegistry, SimulationRun, SpecialistConsult, User
from src.models.hub_project import (
    FINISHED_STATUSES,
    HubProject,
    HubProjectCall,
    HubProjectReview,
    HubProjectReviewAssignment,
)
from src.services.interview_posts import merge_chunks
from src.services.profile_publish import persona_file_text
from src.services.project_plan import (
    TEMPLATE_FIELDS,
    display_id,
    tool_marks,
)
from src.services.project_research import ledger_row_cost
from src.services.project_reviews import project_assignments
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
    "close_unreadable": "BlackbirdBot's close could not be read in full",
    "run_finalized": "The run was finalized first",
    "stopped": "Projects were stopped",
}
#: The pages' reason for a Not finished project whose ``reason_code`` is NULL.
_NO_REASON_LABEL = "No reason recorded"

#: The list's outcome filter: ``""`` (the default) is ready and not agreed; ``paused``
#: also takes in-progress projects.
_OUTCOME_FILTERS = {
    "": frozenset({"ready", "not_agreed"}),
    "ready": frozenset({"ready"}),
    "not_agreed": frozenset({"not_agreed"}),
    "not_finished": frozenset({"not_finished"}),
    "paused": frozenset({"paused", "in_progress"}),
}

LIST_REVIEW_TABS = ("unreviewed", "reviewed", "all")
LIST_REVIEW_DEFAULT = "unreviewed"
LIST_SORTS = ("finished", "lab", "outcome")
LIST_SORT_DEFAULT = "finished"
#: ``hub_projects.agent_id`` is String(50); a longer lab names none.
_LAB_MAX_CHARS = 50

_FINISHED_KINDS = frozenset({"ready", "not_agreed", "not_finished"})
#: The ``outcome`` sort: Ready, Not agreed, Not finished, then In progress and Paused.
_OUTCOME_ORDER = {"ready": 0, "not_agreed": 1, "not_finished": 2, "in_progress": 3, "paused": 3}
_TILES = (
    ("total", "Total"),
    ("ready", "Ready"),
    ("not_agreed", "Not agreed"),
    ("not_finished", "Not finished"),
    ("in_progress", "In progress"),
)
#: An open project's stage: its label, and which time the card shows with which words.
_STAGE_LABELS = {
    "requested": "Waiting for research",
    "researching": "Researching",
    "ready_to_post": "Ready to post",
    "posted": "Posted",
    "refining": "Refining",
}
_STAGE_TIME_LABELS = {
    "requested": "requested",
    "researching": "last activity",
    "ready_to_post": "last activity",
    "posted": "posted",
    "refining": "lab replied",
}
#: The card's key points, from the final plan, else the opening plan.
_KEY_POINT_FIELDS = ("indication", "target", "time_and_budget")
_FIELD_LABELS = dict(TEMPLATE_FIELDS)

_POSITION_LABELS = {"agree": "Agreed", "disagree": "Not agreed"}
_NO_POSITION = "No position stated"
_NO_CLOSE = "No close recorded"

#: The Research card's groups, in order: ``(kind, label, stored source kinds, key prefix)``.
_SOURCE_GROUPS = (
    ("papers", "Papers", ("pubmed",), "pmid:"),
    ("trials", "Clinical trials", ("trial",), "nct:"),
    ("patents", "Patents", ("patent",), "patent:"),
)
_OTHER_SOURCES = ("other", "Other sources")

_NCT_RE = re.compile(r"NCT\d{8}")
_TRIAL_URL = "https://clinicaltrials.gov/study/{}"
#: The posting's speaker when none of its chunks was logged.
_HUB_SPEAKER = "BlackbirdBot"


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


def _page_outcome(
    status: str, reason_code: str | None, *, run_finalized: bool, run_live: bool
) -> tuple[str, str, str | None]:
    """``display_outcome``, with a Not finished project's missing reason in words."""
    kind, label, reason = display_outcome(
        status, reason_code, run_finalized=run_finalized, run_live=run_live
    )
    if kind == "not_finished" and reason is None:
        reason = _NO_REASON_LABEL
    return kind, label, reason


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


def _parse_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def project_list_state(
    *,
    run_id: object,
    lab: object,
    outcome: object,
    review: object,
    sort: object,
    mine: object,
    assignment: object = None,
) -> dict[str, str]:
    """The list's state as it travels in URLs: only valid, non-default values, as strings.

    Keys, in this order, each present only when valid: ``run_id`` (a UUID, canonical form),
    ``lab`` (stripped; non-empty, at most 50 characters, no control character),
    ``outcome`` (a non-default outcome filter), ``review`` (a review tab other than
    Unreviewed), ``sort`` (a sort other than Recently finished), ``mine`` (``"1"``) and
    ``assignment`` (``"mine"``). Anything else is dropped, never echoed.
    """
    state: dict[str, str] = {}
    parsed = _parse_uuid(run_id)
    if parsed is not None:
        state["run_id"] = str(parsed)
    if isinstance(lab, str):
        stripped = lab.strip()
        if (
            stripped
            and len(stripped) <= _LAB_MAX_CHARS
            and not any(ord(c) < 32 or ord(c) == 127 for c in stripped)
        ):
            state["lab"] = stripped
    if isinstance(outcome, str) and outcome and outcome in _OUTCOME_FILTERS:
        state["outcome"] = outcome
    if isinstance(review, str) and review in LIST_REVIEW_TABS and review != LIST_REVIEW_DEFAULT:
        state["review"] = review
    if isinstance(sort, str) and sort in LIST_SORTS and sort != LIST_SORT_DEFAULT:
        state["sort"] = sort
    if mine == "1":
        state["mine"] = "1"
    if assignment == "mine":
        state["assignment"] = "mine"
    return state


def project_list_query(state: Mapping[str, str]) -> str:
    """``"?"`` and the state ``urlencode``d, or ``""`` for an empty state."""
    return "?" + urlencode(dict(state)) if state else ""


def _select_run(
    runs: list[SimulationRun], run_id: str | None, counts: Mapping[uuid.UUID, int]
) -> SimulationRun | None:
    """The requested run when it names one in ``runs``; else the newest run with a project;
    else the newest run."""
    wanted = _parse_uuid(run_id) if run_id else None
    if wanted is not None:
        for run in runs:
            if run.id == wanted:
                return run
    for run in runs:
        if counts.get(run.id, 0) > 0:
            return run
    return runs[0] if runs else None


def _mean_rating(ratings: Sequence[int]) -> float | None:
    return round(sum(ratings) / len(ratings), 1) if ratings else None


def _sort_key(row: HubProject, kind: str) -> tuple:
    """Finished projects first, newest finish first; then open ones, newest request first.
    Finished is the displayed ``kind``, so an open project of a finalized run (shown as Not
    finished) sorts with the finished ones."""
    finished = kind in _FINISHED_KINDS
    moment = (row.finished_at if finished else None) or row.requested_at
    return (0 if finished else 1, -moment.timestamp() if moment else 0.0)


def _strings(value: object) -> list[str]:
    """The string items of a stored list; anything else is ``[]``."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _field_text(plan: Mapping, key: str) -> str:
    """A plan field as text: a list joins with ``"; "``; a missing or non-string value is
    ``""``."""
    value = plan.get(key)
    if isinstance(value, list):
        return "; ".join(_strings(value))
    return value.strip() if isinstance(value, str) else ""


def _plans(project: HubProject) -> tuple[Mapping, Mapping, Mapping | None]:
    """``(choice, opening plan, final plan)``; the final plan is None without a close or
    when the close holds no plan."""
    choice = _mapping(project.choice)
    opening = _mapping(choice.get("plan"))
    final_plan = None
    if isinstance(project.final, Mapping) and isinstance(project.final.get("plan"), Mapping):
        final_plan = project.final["plan"]
    return choice, opening, final_plan


def _position_label(final: object, key: str) -> str:
    """C4: with a close ``"Agreed"``, ``"Not agreed"`` or ``"No position stated"``; without
    one ``"No close recorded"``."""
    if not isinstance(final, Mapping):
        return _NO_CLOSE
    value = final.get(key)
    return _POSITION_LABELS.get(value, _NO_POSITION) if isinstance(value, str) else _NO_POSITION


def _title(project: HubProject, choice: Mapping) -> str | None:
    if project.title:
        return project.title
    title = choice.get("title")
    return title if isinstance(title, str) and title else None


async def _pi_user_ids(db: AsyncSession, agent_ids: set[str]) -> dict[str, str]:
    """``{agent_id: str(user_id)}`` for the labs with a registry row and a linked user."""
    if not agent_ids:
        return {}
    rows = (
        await db.execute(
            select(AgentRegistry.agent_id, AgentRegistry.user_id).where(
                AgentRegistry.agent_id.in_(agent_ids), AgentRegistry.user_id.is_not(None)
            )
        )
    ).all()
    return {agent_id: str(user_id) for agent_id, user_id in rows}


async def _run_reviews_and_replies(
    db: AsyncSession, run_id: uuid.UUID, rows: Sequence[HubProject]
) -> tuple[
    dict[uuid.UUID, list[HubProjectReview]],
    dict[uuid.UUID, list[HubProjectReviewAssignment]],
    dict[str, float],
]:
    """One run's reviews and review assignments per project id, oldest first, and
    ``{reply_ts: first posted_at}`` for its refining projects' replies. All empty without
    ``rows``."""
    reviews_by_project: dict[uuid.UUID, list[HubProjectReview]] = defaultdict(list)
    assignments_by_project: dict[uuid.UUID, list[HubProjectReviewAssignment]] = defaultdict(list)
    reply_times: dict[str, float] = {}
    if not rows:
        return reviews_by_project, assignments_by_project, reply_times
    for r in (
        await db.execute(
            select(HubProjectReview)
            .join(HubProject, HubProject.id == HubProjectReview.project_id)
            .where(HubProject.simulation_run_id == run_id)
            .order_by(HubProjectReview.created_at, HubProjectReview.id)
        )
    ).scalars():
        reviews_by_project[r.project_id].append(r)
    for a in (
        await db.execute(
            select(HubProjectReviewAssignment)
            .join(HubProject, HubProject.id == HubProjectReviewAssignment.project_id)
            .where(HubProject.simulation_run_id == run_id)
            .order_by(HubProjectReviewAssignment.created_at, HubProjectReviewAssignment.id)
        )
    ).scalars():
        assignments_by_project[a.project_id].append(a)
    refining_replies = {r.reply_ts for r in rows if r.status == "refining" and r.reply_ts}
    if refining_replies:
        reply_times = {
            ts: posted_at
            for ts, posted_at in (
                await db.execute(
                    select(AgentMessage.message_ts, func.min(AgentMessage.posted_at))
                    .where(
                        AgentMessage.simulation_run_id == run_id,
                        AgentMessage.phase == _PHASE_PROJECT_REPLY,
                        AgentMessage.thread_ts.is_(None),
                        AgentMessage.message_ts.in_(refining_replies),
                    )
                    .group_by(AgentMessage.message_ts)
                )
            ).all()
        }
    return reviews_by_project, assignments_by_project, reply_times


async def build_projects_list(
    db: AsyncSession,
    *,
    viewer: User,
    impersonating: bool,
    run_id: str | None,
    lab: str | None,
    outcome: str | None,
    review: str | None = None,
    sort: str | None = None,
    mine: bool = False,
    assignment: str | None = None,
) -> dict:
    """The Projects list for one run.

    ``selected_run_id`` is the requested run, else the newest with a project, else the
    newest (None with no runs; no "All runs"); an unknown lab, outcome, tab, sort or
    assignment value falls back to its default. Filters: lab, outcome, the review tab (a
    project is reviewed once anyone reviewed it), ``mine`` (only projects the viewer has
    not reviewed) and ``assignment == "mine"`` (only projects assigned to the viewer).

    Keys: ``run_bar`` (``rubric_version`` None: projects carry no rubric),
    ``selected_run_id``, ``lab_options``, ``lab_filter``, ``outcome_filter``,
    ``review_filter``, ``sort``, ``mine``, ``assignment_filter`` (``"all"`` or ``"mine"``),
    ``list_state`` (``project_list_state`` of the effective state), ``review_counts``
    (per tab, every filter but the tab), ``tiles`` (``{kind, label, count}``, every filter
    but the outcome; In progress counts paused projects), ``not_finished_reasons``
    (``{label, count}`` over the whole run, count descending then label), ``summary`` (the
    run's outcome ``counts``, ``review_count``, ``mean_rating`` and ``fund_share``,
    unfiltered), ``projects`` (cards, every filter, in ``sort`` order, each with the next
    card's id as ``next_id``, ``""`` on the last), ``project_total``, ``pi_user_ids`` and
    ``can_review_projects`` (False under impersonation).
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
    outcome_filter = outcome if isinstance(outcome, str) and outcome in _OUTCOME_FILTERS else ""
    review_filter = review if review in LIST_REVIEW_TABS else LIST_REVIEW_DEFAULT
    sort_by = sort if sort in LIST_SORTS else LIST_SORT_DEFAULT
    assignment_filter = "mine" if assignment == "mine" else "all"
    run_bar = [
        {**entry, "rubric_version": None}
        for entry in run_bar_entries(runs, counts, live_id=live_id)
    ]

    def list_state(lab_filter: str | None) -> dict[str, str]:
        requested = _parse_uuid(run_id) if run_id else None
        return project_list_state(
            run_id=run.id if run is not None and requested == run.id else None,
            lab=lab_filter,
            outcome=outcome_filter,
            review=review_filter,
            sort=sort_by,
            mine="1" if mine else None,
            assignment=assignment_filter,
        )

    view: dict[str, Any] = {
        "run_bar": run_bar,
        "selected_run_id": run.id if run is not None else None,
        "lab_options": [],
        "lab_filter": None,
        "outcome_filter": outcome_filter,
        "review_filter": review_filter,
        "sort": sort_by,
        "mine": mine,
        "assignment_filter": assignment_filter,
        "list_state": list_state(None),
        "review_counts": {tab: 0 for tab in LIST_REVIEW_TABS},
        "tiles": [{"kind": k, "label": label, "count": 0} for k, label in _TILES],
        "not_finished_reasons": [],
        "summary": {"counts": {}, "review_count": 0, "mean_rating": None, "fund_share": None},
        "projects": [],
        "project_total": 0,
        "pi_user_ids": {},
        "can_review_projects": not impersonating,
    }
    if run is None:
        return view

    rows = list(
        (
            await db.execute(
                select(HubProject)
                .options(defer(HubProject.research))
                .where(HubProject.simulation_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    reviews_by_project, assignments_by_project, reply_times = await _run_reviews_and_replies(
        db, run.id, rows
    )

    lab_options = sorted({r.agent_id for r in rows})
    lab_filter = lab if lab in lab_options else None
    wanted_kinds = _OUTCOME_FILTERS[outcome_filter]
    outcome_counts: dict[str, int] = defaultdict(int)
    reason_counts: dict[str, int] = defaultdict(int)
    review_counts = {tab: 0 for tab in LIST_REVIEW_TABS}
    tile_counts: dict[str, int] = defaultdict(int)
    kept: list[tuple[HubProject, str, str, str | None]] = []
    for row in rows:
        kind, label, reason_label = _page_outcome(
            row.status,
            row.reason_code,
            run_finalized=run.finalized_at is not None,
            run_live=run.id == live_id,
        )
        outcome_counts[kind] += 1
        if kind == "not_finished":
            reason_counts[reason_label or _NO_REASON_LABEL] += 1
        reviews = reviews_by_project.get(row.id, [])
        if lab_filter and row.agent_id != lab_filter:
            continue
        if mine and any(r.reviewer_user_id == viewer.id for r in reviews):
            continue
        if assignment_filter == "mine" and not any(
            a.assignee_user_id == viewer.id for a in assignments_by_project.get(row.id, [])
        ):
            continue
        tab = "reviewed" if reviews else "unreviewed"
        if kind in wanted_kinds:
            review_counts[tab] += 1
            review_counts["all"] += 1
        if review_filter != "all" and tab != review_filter:
            continue
        tile_counts["total"] += 1
        tile_counts["in_progress" if kind == "paused" else kind] += 1
        if kind in wanted_kinds:
            kept.append((row, kind, label, reason_label))

    if sort_by == "lab":
        kept.sort(key=lambda item: (item[0].agent_id, _sort_key(item[0], item[1])))
    elif sort_by == "outcome":
        kept.sort(key=lambda item: (_OUTCOME_ORDER[item[1]], _sort_key(item[0], item[1])))
    else:
        kept.sort(key=lambda item: _sort_key(item[0], item[1]))

    projects = [
        _card(
            row,
            kind,
            label,
            reason_label,
            viewer=viewer,
            reviews=reviews_by_project.get(row.id, []),
            assignments=assignments_by_project.get(row.id, []),
            reply_times=reply_times,
        )
        for row, kind, label, reason_label in kept
    ]
    for card, following in zip(projects, [*projects[1:], None], strict=False):
        card["next_id"] = str(following["id"]) if following is not None else ""

    all_reviews = [r for group in reviews_by_project.values() for r in group]
    view.update(
        lab_options=lab_options,
        lab_filter=lab_filter,
        list_state=list_state(lab_filter),
        review_counts=review_counts,
        tiles=[{"kind": k, "label": label, "count": tile_counts[k]} for k, label in _TILES],
        not_finished_reasons=[
            {"label": label, "count": n}
            for label, n in sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        summary={
            "counts": dict(outcome_counts),
            "review_count": len(all_reviews),
            "mean_rating": _mean_rating([r.rating for r in all_reviews]),
            "fund_share": (
                sum(1 for r in all_reviews if r.fund) / len(all_reviews) if all_reviews else None
            ),
        },
        projects=projects,
        project_total=len(projects),
        pi_user_ids=await _pi_user_ids(db, set(lab_options)),
    )
    return view


def _card(
    row: HubProject,
    kind: str,
    label: str,
    reason_label: str | None,
    *,
    viewer: User,
    reviews: Sequence[HubProjectReview],
    assignments: Sequence[HubProjectReviewAssignment],
    reply_times: Mapping[str, float],
) -> dict:
    """One list card; ``next_id`` is filled in once the order is known."""
    choice, opening, final_plan = _plans(row)
    final = _mapping(row.final)
    plan = final_plan if final_plan is not None else opening
    key_points = []
    for key in _KEY_POINT_FIELDS:
        value = _field_text(plan, key)
        if value:
            key_points.append({"label": _FIELD_LABELS[key], "value": value})
    mine = next((r for r in reviews if r.reviewer_user_id == viewer.id), None)
    is_finished = kind in _FINISHED_KINDS
    stage = None if is_finished or row.status not in _STAGE_LABELS else row.status
    stage_time = None
    if stage == "requested":
        stage_time = row.requested_at
    elif stage in ("researching", "ready_to_post"):
        stage_time = row.updated_at
    elif stage == "posted":
        stage_time = row.posted_at
    elif stage == "refining" and row.reply_ts in reply_times:
        stage_time = datetime.fromtimestamp(reply_times[row.reply_ts], UTC)
    summary = choice.get("summary")
    return {
        "id": row.id,
        "anchor": f"p-{row.id}",
        "lab": row.agent_id,
        "title": _title(row, choice),
        "outcome_kind": kind,
        "outcome_label": label,
        "reason_label": reason_label,
        "is_finished": is_finished,
        "requested_at": row.requested_at,
        "posted_at": row.posted_at,
        "finished_at": row.finished_at,
        "summary": summary if isinstance(summary, str) else "",
        "key_points": key_points,
        "hub_position": _position_label(row.final, "hub_position"),
        "lab_position": _position_label(row.final, "lab_position"),
        "gap_count": len(_strings(final.get("gaps"))),
        "disagreement_count": len(_strings(final.get("disagreements"))),
        "reviewers": [
            {"name": r.reviewer_name, "rating": r.rating, "fund": r.fund} for r in reviews
        ],
        "review_count": len(reviews),
        "mean_rating": _mean_rating([r.rating for r in reviews]),
        "fund_count": sum(1 for r in reviews if r.fund),
        "reviewed_by_me": mine is not None,
        "my_review": {"rating": mine.rating, "fund": mine.fund} if mine is not None else None,
        "stage": stage,
        "stage_label": _STAGE_LABELS.get(stage) if stage else None,
        "stage_paused": kind == "paused",
        "stage_time": stage_time,
        "stage_time_label": _STAGE_TIME_LABELS.get(stage) if stage else None,
        "next_id": "",
        "assigned_names": [a.assignee_name for a in assignments],
    }


def display_tool_marks(
    tools: Sequence[str],
    profile_items: Mapping[str, Sequence[str]],
    anchors: Sequence[Mapping],
) -> list[dict]:
    """``{"item", "in_profile", "matched"}`` per string tool, in order.

    A thin delegation to ``project_plan.tool_marks``, which holds the one marking rule the
    close and the page share: candidates are the profile's ``METHOD_MODEL_SECTIONS`` items
    and the project's anchors from those sections, normalised and kept when at least two
    words; a tool matches when its normalised text contains a candidate as whole words, and
    the longest such candidate is ``matched``.
    """
    return tool_marks(tools, profile_items, anchors)


def _stored_tool_marks(final: Mapping) -> list[dict] | None:
    """The close's own ``tool_marks``, when they are the ones the page should show.

    One computation at the close is what every reader sees, so the stored list wins — but
    only when it can be trusted. A pre-change close stored marks without a ``matched`` key,
    and a close written with no profile in memory (``tool_profile_seen`` false) stored an
    all-unmarked list; both are recomputed from the live profile instead, so the page is
    never frozen to marks that say nothing.
    """
    if not final.get("tool_profile_seen"):
        return None
    stored = final.get("tool_marks")
    if not isinstance(stored, list) or not stored:
        return None
    if not all(isinstance(m, Mapping) and "matched" in m for m in stored):
        return None
    return list(stored)


def _source_url(key: str) -> str | None:
    """PubMed for a paper (through ``pubmed_url``, the one PubMed link builder), the
    registry's study page for a trial, and no link for a patent."""
    if key.startswith("pmid:"):
        return pubmed_url(key[len("pmid:") :])
    if key.startswith("nct:"):
        nct = key[len("nct:") :]
        return _TRIAL_URL.format(nct) if _NCT_RE.fullmatch(nct) else None
    return None


def _sources_by_kind(choice: Mapping) -> tuple[list[dict], int]:
    """The stored sources grouped Papers, Clinical trials, Patents, Other sources (empty
    groups left out), each by its stored kind, else its key's prefix; and their count."""
    groups: dict[str, list[dict]] = defaultdict(list)
    total = 0
    raw = choice.get("sources")
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, Mapping) or not isinstance(s.get("key"), str):
            continue
        key = s["key"]
        group = next(
            (
                kind
                for kind, _, stored, prefix in _SOURCE_GROUPS
                if s.get("kind") in stored or key.startswith(prefix)
            ),
            _OTHER_SOURCES[0],
        )
        groups[group].append(
            {
                "ident": display_id(key),
                "title": s.get("title"),
                "url": _source_url(key),
                "specialist": s.get("domain"),
            }
        )
        total += 1
    ordered = [(kind, label) for kind, label, _, _ in _SOURCE_GROUPS] + [_OTHER_SOURCES]
    return [
        {"kind": kind, "label": label, "items": groups[kind]}
        for kind, label in ordered
        if groups.get(kind)
    ], total


def _choice_lists(choice: Mapping) -> tuple[list[dict], list[dict]]:
    """``(anchors, others)`` from the stored choice."""
    anchors_raw = choice.get("anchors")
    others_raw = choice.get("others")
    anchors = [
        {"section": a.get("section"), "item": a.get("item")}
        for a in (anchors_raw if isinstance(anchors_raw, list) else [])
        if isinstance(a, Mapping)
    ]
    others = [
        {"use": o.get("use"), "note": o.get("note")}
        for o in (others_raw if isinstance(others_raw, list) else [])
        if isinstance(o, Mapping)
    ]
    return anchors, others


def _plan_rows(opening: Mapping, final_plan: Mapping | None) -> list[dict]:
    """One row per template field but ``lab_tools``, in template order: the final plan's
    value with its change mark when there is one, else the opening plan's, never changed."""
    rows = []
    for key, label in TEMPLATE_FIELDS:
        if key == "lab_tools":
            continue
        opening_value = _field_text(opening, key)
        if final_plan is None:
            rows.append(
                {"label": label, "value": opening_value, "changed": False, "opening_value": None}
            )
            continue
        value = _field_text(final_plan, key)
        changed = " ".join(value.split()) != " ".join(opening_value.split())
        rows.append(
            {
                "label": label,
                "value": value,
                "changed": changed,
                "opening_value": opening_value if changed else None,
            }
        )
    return rows


_MESSAGE_COLUMNS = (
    AgentMessage.id,
    AgentMessage.agent_id,
    AgentMessage.sender_name,
    AgentMessage.phase,
    AgentMessage.posted_at,
    AgentMessage.content,
)
#: Sorts an entry with no time before every timed one.
_EPOCH = datetime.fromtimestamp(0, UTC)


async def _posting_entry(db: AsyncSession, project: HubProject) -> dict | None:
    """The posting (C5): the stored text, else the first logged chunk; None with neither.
    ``partial`` when fewer chunks were logged than the stored text splits into, unless one
    logged row holds the whole text (with Slack off the engine logs a post as one row)."""
    if not project.posting_ts:
        return None
    chunks = (
        await db.execute(
            select(*_MESSAGE_COLUMNS, AgentMessage.message_ts)
            .where(
                AgentMessage.simulation_run_id == project.simulation_run_id,
                AgentMessage.phase == _PHASE_HUB_PROJECT,
                or_(
                    AgentMessage.message_ts == project.posting_ts,
                    AgentMessage.thread_ts == project.posting_ts,
                ),
            )
            .order_by(AgentMessage.posted_at, AgentMessage.created_at)
        )
    ).all()
    first = next((c for c in chunks if c.message_ts == project.posting_ts), None)
    text = project.posting_text or (first.content if first is not None else None)
    if not text:
        return None
    partial = (
        bool(project.posting_text)
        and len(chunks) < len(split_for_wire(project.posting_text))
        and not any(c.content == project.posting_text for c in chunks)
    )
    at = datetime.fromtimestamp(first.posted_at, UTC) if first is not None else project.posted_at
    return {
        "kind": "posting",
        "anchor": None,
        "speaker": first.sender_name if first is not None else _HUB_SPEAKER,
        "at": at,
        "text": text,
        "partial": partial,
    }


async def _thread_posts(db: AsyncSession, project: HubProject) -> tuple[list[dict], bool]:
    """The lab's reply and the thread's posts, chunks merged; and whether the thread is
    unavailable (``reply_ts`` set, no row logged). With a close, the last post not by the
    lab (BlackbirdBot's) is ``closed_thread``."""
    if not project.reply_ts:
        return [], False
    rows = (
        await db.execute(
            select(*_MESSAGE_COLUMNS)
            .where(
                AgentMessage.simulation_run_id == project.simulation_run_id,
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
    posts = merge_chunks(rows)
    closer = None
    if isinstance(project.final, Mapping):
        closer = next((p for p in reversed(posts) if p.agent_id != project.agent_id), None)
    entries = [
        {
            "kind": "post",
            "anchor": None,
            "speaker": post.speaker,
            "at": datetime.fromtimestamp(post.posted_at, UTC),
            "text": post.text,
            "closed_thread": post is closer,
        }
        for post in posts
    ]
    return entries, not rows


async def _consults(db: AsyncSession, project: HubProject) -> list[dict]:
    """The thread's specialist consults in time order (C5). Only the columns shown are
    selected: the signal, confidence and raw opinion never reach the page (D55)."""
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
                SpecialistConsult.truncated,
                SpecialistConsult.created_at,
            )
            .where(
                SpecialistConsult.simulation_run_id == project.simulation_run_id,
                SpecialistConsult.thread_id == project.reply_ts,
            )
            .order_by(SpecialistConsult.created_at, SpecialistConsult.id)
        )
    ).all()
    consults = []
    for r in rows:
        concerns = _strings(r.concerns)
        consults.append(
            {
                "kind": "consult",
                "anchor": None,
                "domain": r.domain,
                "question": r.question,
                "established": _strings(r.established),
                "concerns": concerns,
                "questions_to_ask": _strings(r.questions_to_ask),
                "concern_count": len(concerns),
                "truncated": bool(r.truncated),
                "at": r.created_at,
            }
        )
    return consults


def _evidence(consults: Sequence[dict]) -> list[dict]:
    """One entry per consulted domain, in order of its first consult, from its latest."""
    by_domain: dict[str, list[dict]] = {}
    for consult in consults:
        by_domain.setdefault(consult["domain"], []).append(consult)
    entries = []
    for domain, group in by_domain.items():
        latest = group[-1]
        entries.append(
            {
                "domain": domain,
                "anchor": latest["anchor"],
                "established": latest["established"],
                "concerns": latest["concerns"],
                "questions_to_ask": latest["questions_to_ask"],
                "truncated": latest["truncated"],
                "earlier_count": len(group) - 1,
            }
        )
    return entries


async def _research_cost(db: AsyncSession, project_id: uuid.UUID) -> Decimal:
    rows = (
        await db.execute(
            select(HubProjectCall.usage_by_model, HubProjectCall.model, HubProjectCall.step).where(
                HubProjectCall.project_id == project_id
            )
        )
    ).all()
    return sum((ledger_row_cost(r.usage_by_model, r.model, r.step) for r in rows), Decimal(0))


async def project_display_kind(db: AsyncSession, project_id: uuid.UUID) -> str | None:
    """The project's outcome kind as the pages show it (``display_outcome``), or None when
    it does not exist."""
    row = (
        await db.execute(
            select(HubProject.status, HubProject.reason_code, HubProject.simulation_run_id)
            .where(HubProject.id == project_id)
        )
    ).first()
    if row is None:
        return None
    finalized_at = (
        await db.execute(
            select(SimulationRun.finalized_at).where(SimulationRun.id == row.simulation_run_id)
        )
    ).scalar_one_or_none()
    live_id = await live_run_id(db)
    kind, _, _ = display_outcome(
        row.status,
        row.reason_code,
        run_finalized=finalized_at is not None,
        run_live=row.simulation_run_id == live_id,
    )
    return kind


async def _review_capable_users(db: AsyncSession) -> list[dict]:
    """Every staff or reviewer account with allowed access, by name then id. Callers gate
    this on a staff viewer: it is the assign form's roster."""
    rows = (
        await db.execute(
            select(User.id, User.name)
            .where(or_(User.is_staff, User.is_reviewer), User.access_status == "allowed")
            .order_by(User.name, User.id)
        )
    ).all()
    return [{"id": str(user_id), "name": name} for user_id, name in rows]


async def build_project_detail(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    viewer: User | None,
    is_admin: bool,
    impersonating: bool,
) -> dict | None:
    """One project's details as ``{"project": {...}}``, or None when it does not exist.

    With a close the plan is the final plan, each field the close changed marked with its
    opening value; without one the opening plan, titled "Plan (as posted)". Positions read
    as ``_position_label`` gives them. The timeline is time-ordered: the posting first, then
    the thread's posts and the consults; posts are anchored ``m-1…`` and consults
    ``consult-1…`` in that order. ``viewer=None`` (the chat record) has no review of its own
    and may edit and write none; ``can_edit``, ``can_review_projects`` and the
    ``review_capable_users`` roster (staff only) are also off under impersonation. Only when
    ``is_admin``: ``research_cost_usd`` (the ledger), ``refinement_cost_usd`` (the thread's
    and the lab reply step's model calls), ``calls_url`` (the admin calls page filtered to
    the thread; None before a reply), ``raw_choice`` and ``raw_final``.
    """
    project = (
        await db.execute(
            select(HubProject)
            .options(defer(HubProject.research))
            .where(HubProject.id == project_id)
        )
    ).scalar_one_or_none()
    if project is None:
        return None
    run = await db.get(SimulationRun, project.simulation_run_id)
    live_id = await live_run_id(db)
    kind, label, reason_label = _page_outcome(
        project.status,
        project.reason_code,
        run_finalized=run is not None and run.finalized_at is not None,
        run_live=project.simulation_run_id == live_id,
    )
    choice, opening, final_plan = _plans(project)
    has_close = isinstance(project.final, Mapping)
    final = _mapping(project.final)
    anchors, others = _choice_lists(choice)
    sources_by_kind, source_count = _sources_by_kind(choice)

    tools_plan = final_plan if final_plan is not None else opening
    tools = _strings(tools_plan.get("lab_tools"))
    tool_rows = _stored_tool_marks(final)
    if tool_rows is None:
        profile_items = section_items(persona_file_text(project.agent_id)) if tools else {}
        tool_rows = display_tool_marks(tools, profile_items, anchors)

    posting = await _posting_entry(db, project)
    posts, thread_unavailable = await _thread_posts(db, project)
    consults = await _consults(db, project)
    # Posts before consults at the same moment; otherwise each keeps its own order.
    rest = sorted(
        [(e["at"] or _EPOCH, 0, i, e) for i, e in enumerate(posts)]
        + [(e["at"] or _EPOCH, 1, i, e) for i, e in enumerate(consults)],
        key=lambda item: item[:3],
    )
    timeline = ([posting] if posting is not None else []) + [item[3] for item in rest]
    post_number = consult_number = 0
    for entry in timeline:
        if entry["kind"] == "consult":
            consult_number += 1
            entry["anchor"] = f"consult-{consult_number}"
        else:
            post_number += 1
            entry["anchor"] = f"m-{post_number}"

    reviews = []
    my_review = None
    for r in (
        await db.execute(
            select(HubProjectReview)
            .where(HubProjectReview.project_id == project.id)
            .order_by(HubProjectReview.created_at, HubProjectReview.id)
        )
    ).scalars():
        own = viewer is not None and r.reviewer_user_id == viewer.id
        entry = {
            "id": r.id,
            "reviewer_name": r.reviewer_name,
            "rating": r.rating,
            "comment": r.comment,
            "fund": r.fund,
            "created_at": r.created_at,
            "edited": (r.updated_at - r.created_at).total_seconds() > 1,
            "can_edit": own and not impersonating,
        }
        reviews.append(entry)
        if own:
            my_review = entry

    assignments = [
        {"assignee_user_id": str(a.assignee_user_id), "assignee_name": a.assignee_name}
        for a in (await project_assignments(db, [project.id])).get(project.id, [])
    ]
    staff_viewer = viewer is not None and viewer.is_staff and not impersonating
    why_chosen = choice.get("why_chosen")
    summary = choice.get("summary")

    detail: dict[str, Any] = {
        "id": project.id,
        "lab": project.agent_id,
        "pi_user_id": (await _pi_user_ids(db, {project.agent_id})).get(project.agent_id),
        "title": _title(project, choice),
        "summary": summary if isinstance(summary, str) else "",
        "outcome_kind": kind,
        "outcome_label": label,
        "reason_label": reason_label,
        "is_finished": kind in _FINISHED_KINDS,
        "run_id": project.simulation_run_id,
        "run_started_at": run.started_at if run is not None else None,
        "requested_at": project.requested_at,
        "posted_at": project.posted_at,
        "finished_at": project.finished_at,
        "has_close": has_close,
        "why_chosen": why_chosen if isinstance(why_chosen, str) else "",
        "others": others,
        "anchors": anchors,
        "plan_title": "Plan" if final_plan is not None else "Plan (as posted)",
        "plan_rows": _plan_rows(opening, final_plan),
        "tool_rows": tool_rows,
        "hub_position": _position_label(project.final, "hub_position"),
        "lab_position": _position_label(project.final, "lab_position"),
        "gaps": _strings(final.get("gaps")),
        "disagreements": _strings(final.get("disagreements")),
        # The close's own sentence on the disagreement question (D75). On a ready project it
        # is the only record of why none remains; None for every close written before D75.
        "disagreement_note": (
            note.strip()
            if isinstance(note := final.get("disagreement_note"), str) and note.strip()
            else None
        ),
        "sources_by_kind": sources_by_kind,
        "evidence": _evidence(consults),
        "timeline": timeline,
        "thread_unavailable": thread_unavailable,
        "counts": {
            "sources": source_count,
            "consults": len(consults),
            "posts": len(posts),
            "others": len(others),
        },
        "reviews": reviews,
        "my_review": my_review,
        "can_review_projects": viewer is not None and not impersonating,
        "assignments": assignments,
        "review_capable_users": await _review_capable_users(db) if staff_viewer else [],
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
        detail["raw_choice"] = project.choice
        detail["raw_final"] = project.final
    return {"project": detail}

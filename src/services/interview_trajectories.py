"""The Interviews page's view: one line per post of each interview, with its outcome.

Reads ``src.services.interview_posts`` for the posts and ``InterviewTrajectory`` for the
model-written lines; a post with no usable stored line shows its first sentence. The page
never renders a post's full text.
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.role_capabilities import hub_role_names, spoke_role_names
from src.models import (
    AgentMessage,
    AssessmentDrop,
    HubDecline,
    InterviewTrajectory,
    OpportunityAssessment,
    SimulationRun,
    SpecialistConsult,
    ThreadDecision,
)
from src.models.interview_trajectory import TRAJECTORY_STATUS_READY
from src.services.interview_posts import (
    CLIP_CHARS,
    Post,
    interview_and_root_counts,
    merge_chunks,
    posts_hash,
    run_interview_roots,
)
from src.services.runs import live_run_id, run_bar_entries, runs_ordered

LINE_LIMIT = 160

_SENTENCE_END = re.compile(r"[.!?](?=\s)")
_PANEL_NOTE = re.compile(r"🧪\s*Panel\s*·\s*([^—\n]+?)\s*—\s*([^—\n]+?)\s*—")


def _note_signal(label: str) -> str:
    """The signal key behind a panel note's rendered label: ``format_panel_note`` writes an
    emoji plus wording (``"⚠️ gap"``, ``"☑️ adequate for stage"``), and the page's glyph and
    colour maps are keyed by the bare signal (``gap``, ``adequate``, the historical
    ``caution``/``clear``), which is the wording's first word."""
    word = re.search(r"[A-Za-z]+", label)
    return word.group(0).lower() if word else label.strip()


def first_sentence(text: str, limit: int = LINE_LIMIT) -> str:
    """``text`` without ``**``, whitespace collapsed, cut after the first ``.``, ``!`` or
    ``?`` that is followed by whitespace when that fits in ``limit``; otherwise clipped at
    the last space before ``limit`` with ``…`` appended."""
    flat = " ".join(text.replace("**", "").split())
    match = _SENTENCE_END.search(flat)
    if match and match.end() <= limit:
        return flat[: match.end()]
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut).rstrip() + "…"


def _outcome(
    thread_id: str,
    assessment: Any,
    declined: set[str],
    decision: Any,
    dropped: set[str],
    is_live: bool,
) -> tuple[str, str]:
    """``(label, kind)`` by the first matching rule; ``kind`` selects the badge colour."""
    if assessment is not None:
        return f"Verdict: {assessment.recommendation or 'recorded'}", "verdict"
    if thread_id in declined:
        return "Hub decline", "decline"
    if decision is not None:
        if decision.outcome == "timeout":
            return "Timeout", "timeout"
        if decision.outcome == "abandoned":
            return "Abandoned", "abandoned"
        if decision.outcome == "no_proposal" and decision.closed_by_role in spoke_role_names():
            return "Lab ⏸️", "lab_pause"
        if decision.outcome == "no_proposal" and decision.closed_by_role in hub_role_names():
            return "Hub decline", "decline"
        if thread_id not in dropped:
            return "Closed", "closed"
    if thread_id in dropped:
        return "Verdict dropped", "dropped"
    if is_live:
        return "Open", "open"
    return "Run ended before a close", "ended"


async def _assessments_by_thread(db: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    """The run's newest assessment per interview thread; a pre-0036 row with no
    ``thread_id`` resolves through its ``slack_ts`` to the root of the post it names."""
    rows = (
        (
            await db.execute(
                select(OpportunityAssessment)
                .where(OpportunityAssessment.simulation_run_id == run_id)
                .order_by(OpportunityAssessment.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    legacy = [r for r in rows if not r.thread_id and r.slack_ts]
    anchors: dict[tuple[str, str], str] = {}
    if legacy:
        stamps = {r.slack_ts for r in legacy}
        msgs = (
            await db.execute(
                select(
                    AgentMessage.channel_name,
                    AgentMessage.slack_ts,
                    AgentMessage.message_ts,
                    AgentMessage.thread_ts,
                )
                .where(
                    AgentMessage.simulation_run_id == run_id,
                    or_(AgentMessage.slack_ts.in_(stamps), AgentMessage.message_ts.in_(stamps)),
                )
                .order_by(AgentMessage.posted_at, AgentMessage.created_at)
            )
        ).all()
        for channel, slack_ts, message_ts, thread_ts in reversed(msgs):
            root = thread_ts or message_ts
            if root:
                for stamp in (slack_ts, message_ts):
                    if stamp:
                        anchors[(channel, stamp)] = root
    out: dict[str, Any] = {}
    for r in rows:
        thread = r.thread_id or anchors.get((r.channel_name, r.slack_ts or ""))
        if thread and thread not in out:
            out[thread] = r
    return out


async def _panel_entries(
    db: AsyncSession, run_id: uuid.UUID
) -> tuple[dict[str, list[tuple[float, dict]]], dict[str, list[tuple[float, dict]]]]:
    """``(consults, notes)``: per thread id, ``(epoch seconds, entry)`` pairs in time order.

    ``notes`` are the ``panel_note`` rows parsed into the same entry shape, the fallback
    for a thread with no consult rows."""
    consults: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    for row in (
        await db.execute(
            select(
                SpecialistConsult.thread_id,
                SpecialistConsult.created_at,
                SpecialistConsult.domain,
                SpecialistConsult.verdict_signal,
                SpecialistConsult.truncated,
            )
            .where(
                SpecialistConsult.simulation_run_id == run_id,
                SpecialistConsult.thread_id.is_not(None),
            )
            .order_by(SpecialistConsult.created_at)
        )
    ).all():
        consults[row.thread_id].append(
            (
                row.created_at.timestamp(),
                {
                    "domain": row.domain,
                    "verdict_signal": row.verdict_signal,
                    "reply_truncated": bool(row.truncated),
                },
            )
        )
    notes: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    for row in (
        await db.execute(
            select(AgentMessage.thread_ts, AgentMessage.posted_at, AgentMessage.content)
            .where(
                AgentMessage.simulation_run_id == run_id,
                AgentMessage.phase == "panel_note",
                AgentMessage.thread_ts.is_not(None),
            )
            .order_by(AgentMessage.posted_at, AgentMessage.created_at)
        )
    ).all():
        match = _PANEL_NOTE.search(row.content or "")
        if match:
            notes[row.thread_ts].append(
                (
                    row.posted_at,
                    {
                        "domain": match.group(1).strip(),
                        "verdict_signal": _note_signal(match.group(2)),
                        "reply_truncated": False,
                    },
                )
            )
    return consults, notes


def _lines(
    posts: list[Post],
    trajectory: Any,
    placed: dict[uuid.UUID, list[dict]],
) -> list[dict]:
    stored: dict = {}
    if (
        trajectory is not None
        and trajectory.status == TRAJECTORY_STATUS_READY
        and trajectory.lines
        and trajectory.posts_sha256_12 == posts_hash(posts)
    ):
        stored = trajectory.lines
    out: list[dict] = []
    for n, post in enumerate(posts, start=1):
        if placed.get(post.first_row_id):
            out.append(
                {
                    "n": None,
                    "speaker": "",
                    "text": "",
                    "source": "panel",
                    "panel": placed[post.first_row_id],
                }
            )
        model_line = stored.get(str(post.first_row_id))
        out.append(
            {
                "n": n,
                "speaker": post.speaker,
                "text": model_line if model_line else first_sentence(post.text),
                "source": "model" if model_line else "first_sentence",
                "panel": [],
            }
        )
    return out


def _select_run(runs: list[SimulationRun], run_id: str | None) -> SimulationRun | None:
    """The requested run when it names one in ``runs``, else the newest."""
    if run_id:
        try:
            wanted = uuid.UUID(run_id)
        except ValueError:
            wanted = None
        for run in runs:
            if run.id == wanted:
                return run
    return runs[0] if runs else None


async def build_interviews_view(db: AsyncSession, run_id: str | None) -> dict:
    """The Interviews page's data for one run (the requested one, else the newest).

    Keys: ``run_bar`` (``run_bar_entries`` with interview counts), ``selected_run_id``
    (None when there are no runs), ``interviews`` (newest first), ``interview_total`` and
    ``unanswered_count`` (root posts nobody else replied to, counted and not listed). Each
    interview carries ``thread_id, lab, started_at, outcome_label, outcome_kind,
    assessment_id, band, lines``; each line ``n`` (None for a panel line), ``speaker``,
    ``text``, ``source`` (``model`` | ``first_sentence`` | ``panel``) and ``panel``.
    """
    runs = await runs_ordered(db)
    live_id = await live_run_id(db)
    interview_counts, root_counts = await interview_and_root_counts(db)
    run_bar = run_bar_entries(runs, interview_counts, live_id=live_id)
    run = _select_run(runs, run_id)
    if run is None:
        return {
            "run_bar": run_bar,
            "selected_run_id": None,
            "interviews": [],
            "interview_total": 0,
            "unanswered_count": 0,
        }

    roots = await run_interview_roots(db, run.id)
    threads = {str(r.thread_id) for r in roots}
    assessments = await _assessments_by_thread(db, run.id)
    declined = set(
        (
            await db.execute(
                select(HubDecline.thread_id).where(HubDecline.simulation_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    dropped = {
        t
        for t in (
            await db.execute(
                select(AssessmentDrop.thread_id).where(AssessmentDrop.simulation_run_id == run.id)
            )
        )
        .scalars()
        .all()
        if t
    }
    last_decision: dict[str, Any] = {}
    for d in (
        (
            await db.execute(
                select(ThreadDecision)
                .where(ThreadDecision.simulation_run_id == run.id)
                .order_by(ThreadDecision.decided_at)
            )
        )
        .scalars()
        .all()
    ):
        last_decision[d.thread_id] = d
    trajectories = {
        t.thread_id: t
        for t in (
            await db.execute(
                select(InterviewTrajectory).where(InterviewTrajectory.simulation_run_id == run.id)
            )
        )
        .scalars()
        .all()
    }
    consults, notes = await _panel_entries(db, run.id)

    # Every post of the run's interviews in one query, grouped per thread.
    rows_by_thread: dict[str, list] = defaultdict(list)
    if threads:
        for row in (
            await db.execute(
                select(
                    AgentMessage.id,
                    AgentMessage.agent_id,
                    AgentMessage.sender_name,
                    AgentMessage.phase,
                    AgentMessage.posted_at,
                    AgentMessage.thread_ts,
                    AgentMessage.message_ts,
                    # The page shows a first sentence at most; never ship whole posts.
                    func.left(AgentMessage.content, CLIP_CHARS).label("content"),
                )
                .where(
                    AgentMessage.simulation_run_id == run.id,
                    AgentMessage.phase.in_(("new_post", "thread_reply")),
                    or_(
                        AgentMessage.thread_ts.in_(threads),
                        AgentMessage.message_ts.in_(threads),
                    ),
                )
                .order_by(AgentMessage.posted_at, AgentMessage.created_at, AgentMessage.id)
            )
        ).all():
            thread = row.thread_ts or row.message_ts
            if thread in threads:
                rows_by_thread[thread].append(row)

    interviews = []
    for root in roots:
        thread_id = str(root.thread_id)
        posts = merge_chunks(rows_by_thread.get(thread_id, []))
        hub_ids = {p.agent_id for p in posts if p.agent_id != root.agent_id}
        entries = consults.get(thread_id) or notes.get(thread_id) or []
        placed = _fold_panel(posts, entries, hub_ids)
        assessment = assessments.get(thread_id)
        label, kind = _outcome(
            thread_id,
            assessment,
            declined,
            last_decision.get(thread_id),
            dropped,
            run.id == live_id,
        )
        interviews.append(
            {
                "thread_id": thread_id,
                "lab": root.agent_id,
                "started_at": root.created_at,
                "outcome_label": label,
                "outcome_kind": kind,
                "assessment_id": assessment.id if assessment is not None else None,
                "band": assessment.band if assessment is not None else None,
                # A recorded hub decline is listed on the Assessments page (since 0071).
                "decline_listed": assessment is None and thread_id in declined,
                "lines": _lines(posts, trajectories.get(thread_id), placed),
            }
        )
    total = len(interviews)
    return {
        "run_bar": run_bar,
        "selected_run_id": run.id,
        "interviews": interviews,
        "interview_total": total,
        "unanswered_count": max(root_counts.get(run.id, 0) - total, 0),
    }


def _fold_panel(
    posts: list[Post], entries: list[tuple[float, dict]], hub_ids: set[str | None]
) -> dict[uuid.UUID, list[dict]]:
    """Each entry goes to the first hub post whose ``posted_at`` is at or after it; the hub
    is the thread's non-root author, and an entry later than every hub post goes nowhere."""
    hub_posts = [p for p in posts if p.agent_id in hub_ids and p.kind == "reply"]
    placed: dict[uuid.UUID, list[dict]] = defaultdict(list)
    for stamp, entry in entries:
        for post in hub_posts:
            if post.posted_at >= stamp:
                placed[post.first_row_id].append(entry)
                break
    return placed

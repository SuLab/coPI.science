"""BlackbirdBot projects in the engine: the run clock, requests, postings, the lab's reply and
the close (the projects prototype design, §5.2, §5.6, §5.7, §5.9).

``Projects`` keeps the per-run clock the once-per-interval rule counts, writes a ``requested``
row for each due live lab (the worker researches it; the engine never calls a model for
research), posts a researched project as the hub, offers the lab its reply inside the lab's
Phase-5 turn, and records the outcome when the project thread closes. Every status change is
a compare-and-set (``UPDATE … WHERE status = <expected>``), so the engine and the worker never
overwrite each other.

The switch ``hub_projects_enabled`` is read once, by ``_rehydrate_projects`` at engine start.
It gates requests, postings, lab replies and the drain's wait for replies; it never gates how
a project thread is recognised or ended (claims, the stale-project rules and the close).
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.agent.agent import Agent
from src.agent.claim_check import PROJECT_REPLY_THREAD_KEY, feedback_text
from src.agent.engine import deps
from src.agent.engine.context import EngineContext, via
from src.agent.engine.helpers import _was_truncated
from src.agent.engine.sidecar import _extract_project_json, _extract_slack_message
from src.agent.message_log import PHASE_HUB_PROJECT, PHASE_PROJECT_REPLY
from src.agent.persona_sections import section_items, sections
from src.agent.role_capabilities import capabilities_for, spoke_role_names
from src.agent.state import ThreadState
from src.models.agent_activity import AgentMessage, ThreadDecision
from src.models.hub_project import (
    OPEN_STATUSES,
    REASON_CLOSE_UNREADABLE,
    REASON_LAB_NOT_LIVE,
    REASON_REPLY_FAILED,
    REASON_RUN_ENDED,
    REASON_THREAD_ABANDONED,
    REASON_THREAD_TIMEOUT,
    STATUS_NOT_FINISHED,
    STATUS_POSTED,
    STATUS_READY_TO_POST,
    STATUS_REFINING,
    STATUS_REQUESTED,
    STATUS_RESEARCHING,
    THREAD_OUTCOME_PROJECT_CLOSED,
    HubProject,
    HubProjectClock,
)
from src.services.project_plan import (
    REPLY_HEADER,
    fits_one_message,
    position_marker,
    project_outcome,
    tool_marks,
)

if TYPE_CHECKING:
    from src.agent.engine.scheduler import Scheduler
    from src.agent.engine.slack_io import SlackIO

logger = logging.getLogger("src.agent.simulation")

#: The tick's cadence: the clock advances, claims resolve and due labs are requested at
#: most once per this many wall seconds.
PROJECT_TICK_SECONDS = 60.0
#: The most one tick adds to the run clock, so a stall or a suspended host adds little. A stop
#: adds nothing: a new engine starts with no previous tick.
CLOCK_STEP_MAX_SECONDS = 900.0
#: How old an unheld, undecided project thread's reply claim must be before the project ends
#: ``thread_abandoned``: the time BlackbirdBot's next Phase-3 scan needs to take the new
#: thread up (spec §5.9).
ORPHAN_GRACE_SECONDS = 600.0
#: The counted reply failure that ends a project ``not_finished`` / ``reply_failed``.
REPLY_FAILURES_MAX = 2
#: Postings and lab replies go to this channel.
PROJECT_CHANNEL = "general"
#: The statuses before a posting; ended ``run_ended_before_posting`` while the run drains.
_BEFORE_POSTING = (STATUS_REQUESTED, STATUS_RESEARCHING, STATUS_READY_TO_POST)


class Projects:
    """The run clock, project requests and postings, the lab's reply and the close.

    A lab is **live** when it is on the roster, its role is a spoke role and its public
    profile has a non-empty Research Summary. A project thread is **held** when its id is in
    some agent's ``state.active_threads``. ``_pending_links`` maps a project id to
    (``"posting"`` or ``"reply"``, the posted root's id) for a post that went out but whose
    database link failed; the next tick writes it again, so the post is never repeated.
    """

    agents = via("ctx")
    message_log = via("ctx")
    session_factory = via("ctx")
    simulation_run_id = via("ctx")

    OWNED_STATE: tuple[str, ...] = (
        "_enabled", "_clock_seconds", "_last_tick", "_awaiting_reply", "_pending_links",
    )

    def __init__(self, ctx: EngineContext, *, scheduler: Scheduler, slack_io: SlackIO) -> None:
        # Reads no setting: tests stub deps.get_settings before building an engine.
        self.ctx = ctx
        self._scheduler = scheduler
        self._slack_io = slack_io
        self._enabled = False
        # The run clock as last read or advanced (hub_project_clocks.run_seconds).
        self._clock_seconds = 0.0
        self._last_tick: float | None = None
        # Posted projects of live labs still waiting for the lab's reply; the drain waits on it.
        self._awaiting_reply = 0
        self._pending_links: dict[uuid.UUID, tuple[str, str]] = {}

    # ------------------------------------------------------------------
    # Start and the tick
    # ------------------------------------------------------------------

    async def _rehydrate_projects(self) -> None:
        """Snapshot the switch, then (with a database and a run) create this run's clock row
        if missing, read the clock and resolve claims a previous process left. Never raises."""
        self._enabled = bool(getattr(deps.get_settings(), "hub_projects_enabled", False))
        if not self.session_factory or not self.simulation_run_id:
            return
        try:
            async with self.session_factory() as db:
                await db.execute(
                    pg_insert(HubProjectClock)
                    .values(simulation_run_id=self.simulation_run_id, run_seconds=0.0)
                    .on_conflict_do_nothing(index_elements=["simulation_run_id"])
                )
                seconds = await db.scalar(
                    select(HubProjectClock.run_seconds)
                    .where(HubProjectClock.simulation_run_id == self.simulation_run_id)
                )
                await db.commit()
                self._clock_seconds = float(seconds or 0.0)
                await self._resolve_project_claims(db)
        except Exception:
            logger.warning("Projects: restoring the run clock and claims failed", exc_info=True)

    async def _projects_tick(self, *, draining: bool) -> None:
        """Once per ``PROJECT_TICK_SECONDS``: advance the clock, resolve claims, end stale
        projects, request due labs and post ready projects (the last two only while the switch
        is on and the run is not draining), then recount the replies the drain waits for.

        ``draining`` is the post lane's ``max_proposals > 0 and _proposals_posted >=
        max_proposals``. Each step that fails logs one warning and the next step still runs;
        this never raises."""
        now = deps.time.time()
        if self._last_tick is not None and now - self._last_tick < PROJECT_TICK_SECONDS:
            return
        if not self.session_factory or not self.simulation_run_id:
            return
        elapsed = 0.0 if self._last_tick is None else max(
            0.0, min(now - self._last_tick, CLOCK_STEP_MAX_SECONDS),
        )
        self._last_tick = now
        try:
            async with self.session_factory() as db:
                await self._run_project_step(db, "advancing the clock", self._advance_clock, elapsed)
                await self._run_project_step(db, "resolving claims", self._resolve_project_claims)
                await self._run_project_step(
                    db, "ending stale projects", self._end_stale_projects, draining,
                )
                if self._enabled and not draining:
                    await self._run_project_step(db, "requesting projects", self._request_due_labs)
                    await self._run_project_step(db, "posting projects", self._post_ready_projects)
                await self._run_project_step(
                    db, "counting replies owed", self._count_awaiting_replies,
                )
        except Exception:
            logger.warning("Projects: the tick could not open a session", exc_info=True)

    async def _run_project_step(self, db, label: str, step, *args: Any) -> None:
        """Run one tick step; a failure rolls the session back and logs one warning."""
        try:
            await step(db, *args)
        except Exception:
            logger.warning("Projects: %s failed", label, exc_info=True)
            try:
                await db.rollback()
            except Exception:
                logger.debug("Projects: rollback after %s failed", label, exc_info=True)

    async def _advance_clock(self, db, elapsed: float) -> None:
        """Add ``elapsed`` seconds to this run's clock by a SQL increment (never a write of the
        cached value, so two writers cannot lose time) and cache the result."""
        stmt = (
            update(HubProjectClock)
            .where(HubProjectClock.simulation_run_id == self.simulation_run_id)
            .values(run_seconds=HubProjectClock.run_seconds + elapsed, updated_at=func.now())
            .returning(HubProjectClock.run_seconds)
            .execution_options(synchronize_session=False)
        )
        seconds = (await db.execute(stmt)).scalar_one_or_none()
        if seconds is None:
            # The start could not create the row; create it now and count from here.
            await db.execute(
                pg_insert(HubProjectClock)
                .values(simulation_run_id=self.simulation_run_id, run_seconds=0.0)
                .on_conflict_do_nothing(index_elements=["simulation_run_id"])
            )
            seconds = await db.scalar(
                select(HubProjectClock.run_seconds)
                .where(HubProjectClock.simulation_run_id == self.simulation_run_id)
            )
        await db.commit()
        self._clock_seconds = float(seconds or 0.0)

    # ------------------------------------------------------------------
    # Claims and links
    # ------------------------------------------------------------------

    async def _resolve_project_claims(self, db) -> None:
        """Write the links ``_pending_links`` holds, then settle every other open claim of
        this run from the log: a claimed posting or reply whose row is logged is linked, and
        one with no row is cleared, so the next tick or turn posts it again.

        Clearing is safe because the tick, the reply lane and the post turn run one after
        another on the main loop: no post is in flight while this runs, and a post whose link
        failed is in ``_pending_links`` (its row may still be queued for the database), not
        left to this lookup."""
        for project_id, (kind, ts) in list(self._pending_links.items()):
            try:
                await self._link_project_post(db, project_id, kind, ts)
            except Exception:
                logger.warning(
                    "Projects: linking %s %s of project %s failed again",
                    kind, ts, project_id, exc_info=True,
                )
                await db.rollback()
                continue
            self._pending_links.pop(project_id, None)
        rows = (await db.execute(
            select(
                HubProject.id, HubProject.agent_id, HubProject.status,
                HubProject.posting_text, HubProject.posting_claimed_at, HubProject.posting_ts,
                HubProject.reply_claimed_at, HubProject.reply_ts,
            ).where(
                HubProject.simulation_run_id == self.simulation_run_id,
                or_(
                    (HubProject.status == STATUS_READY_TO_POST)
                    & HubProject.posting_claimed_at.isnot(None)
                    & HubProject.posting_ts.is_(None),
                    HubProject.reply_claimed_at.isnot(None) & HubProject.reply_ts.is_(None),
                ),
            )
        )).all()
        for row in rows:
            if row.id in self._pending_links:
                continue
            if (row.status == STATUS_READY_TO_POST and row.posting_claimed_at is not None
                    and row.posting_ts is None):
                await self._resolve_logged_claim(db, row, "posting")
            if row.reply_claimed_at is not None and row.reply_ts is None:
                await self._resolve_logged_claim(db, row, "reply")

    async def _resolve_logged_claim(self, db, row, kind: str) -> None:
        """Link one claimed post to its logged root row, or clear the claim when there is none.

        A posting is the poster's first ``hub_project`` root at or after the claim whose text is
        the stored ``posting_text`` (a posting always fits one message, and ``_post_message`` logs
        the text it was given; the header alone names only a surname, which two labs can share).
        A reply is the lab's first ``project_reply`` root at or after the claim. Continuation
        chunks hang off their root, so ``thread_ts IS NULL`` picks it."""
        stmt = select(AgentMessage.message_ts).where(
            AgentMessage.simulation_run_id == self.simulation_run_id,
            AgentMessage.thread_ts.is_(None),
        )
        if kind == "posting":
            poster = self._project_poster()
            stmt = stmt.where(
                AgentMessage.phase == PHASE_HUB_PROJECT,
                AgentMessage.created_at >= row.posting_claimed_at,
                AgentMessage.content == (row.posting_text or ""),
            )
            if poster is not None:
                stmt = stmt.where(AgentMessage.agent_id == poster.agent_id)
        else:
            stmt = stmt.where(
                AgentMessage.phase == PHASE_PROJECT_REPLY,
                AgentMessage.agent_id == row.agent_id,
                AgentMessage.created_at >= row.reply_claimed_at,
            )
        stmt = stmt.order_by(AgentMessage.created_at, AgentMessage.posted_at).limit(1)
        ts = await db.scalar(stmt)
        if ts:
            await self._link_project_post(db, row.id, kind, ts)
            logger.info("Projects: linked the logged %s %s to project %s", kind, ts, row.id)
        else:
            await self._clear_project_claim(db, row.id, kind)
            logger.info("Projects: cleared the %s claim of project %s (nothing logged)", kind, row.id)

    async def _link_project_post(self, db, project_id: uuid.UUID, kind: str, ts: str) -> None:
        """Link a posted root to its project and commit.

        A posting: the compare-and-set ``ready_to_post`` -> ``posted`` with ``posting_ts`` and
        ``posted_at``. A reply: ``reply_ts`` is written whatever the status (so a project
        thread always finds its project, even one the tick ended meanwhile), and only a
        ``posted`` project becomes ``refining``."""
        if kind == "posting":
            await db.execute(
                update(HubProject)
                .where(HubProject.id == project_id, HubProject.status == STATUS_READY_TO_POST)
                .values(status=STATUS_POSTED, posting_ts=ts, posted_at=func.now(),
                        updated_at=func.now())
                .execution_options(synchronize_session=False)
            )
        else:
            await db.execute(
                update(HubProject)
                .where(HubProject.id == project_id, HubProject.reply_ts.is_(None))
                .values(reply_ts=ts, updated_at=func.now())
                .execution_options(synchronize_session=False)
            )
            await db.execute(
                update(HubProject)
                .where(HubProject.id == project_id, HubProject.status == STATUS_POSTED)
                .values(status=STATUS_REFINING, updated_at=func.now())
                .execution_options(synchronize_session=False)
            )
        await db.commit()

    async def _clear_project_claim(self, db, project_id: uuid.UUID, kind: str) -> None:
        """Clear an unlinked posting or reply claim and commit."""
        if kind == "posting":
            stmt = (
                update(HubProject)
                .where(HubProject.id == project_id, HubProject.posting_ts.is_(None))
                .values(posting_claimed_at=None, updated_at=func.now())
            )
        else:
            stmt = (
                update(HubProject)
                .where(HubProject.id == project_id, HubProject.reply_ts.is_(None))
                .values(reply_claimed_at=None, updated_at=func.now())
            )
        await db.execute(stmt.execution_options(synchronize_session=False))
        await db.commit()

    async def _finish_project(
        self, db, project_id: uuid.UUID, *, expected: str, reason: str, where: tuple = (),
    ) -> bool:
        """The compare-and-set ``expected`` -> ``not_finished`` with ``reason``, the finish time
        and the run clock; commits. False when the row had moved on."""
        result = await db.execute(
            update(HubProject)
            .where(HubProject.id == project_id, HubProject.status == expected, *where)
            .values(
                status=STATUS_NOT_FINISHED, reason_code=reason, finished_at=func.now(),
                finished_run_seconds=self._clock_seconds, updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if result.rowcount:
            logger.info("Projects: project %s ended not finished (%s)", project_id, reason)
        return bool(result.rowcount)

    # ------------------------------------------------------------------
    # Tick steps
    # ------------------------------------------------------------------

    async def _end_stale_projects(self, db, draining: bool) -> None:
        """End what can no longer finish: while draining, every project not yet posted and
        not being posted (``run_ended_before_posting``); a posted project with no reply and no
        reply claim whose lab is not live (``lab_not_live``); and the refining projects
        ``_end_refining_projects`` covers."""
        if draining:
            result = await db.execute(
                update(HubProject)
                .where(
                    HubProject.simulation_run_id == self.simulation_run_id,
                    HubProject.status.in_(_BEFORE_POSTING),
                    HubProject.posting_claimed_at.is_(None),
                )
                .values(
                    status=STATUS_NOT_FINISHED, reason_code=REASON_RUN_ENDED,
                    finished_at=func.now(), finished_run_seconds=self._clock_seconds,
                    updated_at=func.now(),
                )
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            if result.rowcount:
                logger.info(
                    "Projects: %d unposted project(s) ended: the run is draining", result.rowcount,
                )
        posted = (await db.execute(
            select(HubProject.id, HubProject.agent_id).where(
                HubProject.simulation_run_id == self.simulation_run_id,
                HubProject.status == STATUS_POSTED,
                HubProject.reply_ts.is_(None),
                HubProject.reply_claimed_at.is_(None),
            )
        )).all()
        for row in posted:
            if not self._project_lab_is_live(row.agent_id):
                await self._finish_project(
                    db, row.id, expected=STATUS_POSTED, reason=REASON_LAB_NOT_LIVE,
                    where=(HubProject.reply_ts.is_(None), HubProject.reply_claimed_at.is_(None)),
                )
        await self._end_refining_projects(db)

    async def _end_refining_projects(self, db) -> None:
        """End a refining project whose thread closed without a recorded close: a
        ``ThreadDecision`` of this run on its thread ends it ``thread_timeout`` (outcome
        ``timeout``), ``close_unreadable`` (``project_closed``: the close was posted but its
        result was not written) or ``thread_abandoned`` (any other outcome). With no decision,
        a thread no agent holds whose reply claim is older than ``ORPHAN_GRACE_SECONDS`` ends
        ``thread_abandoned``: it was evicted without a decision (its root deleted on Slack, or
        missing from this run's log) and would otherwise keep the lab from every later project.

        Decisions are read from the database only; one still queued for writing can let the
        grace rule end the project ``thread_abandoned`` first (all three are not finished)."""
        refining = (await db.execute(
            select(HubProject.id, HubProject.reply_ts).where(
                HubProject.simulation_run_id == self.simulation_run_id,
                HubProject.status == STATUS_REFINING,
                HubProject.reply_ts.isnot(None),
            )
        )).all()
        if not refining:
            return
        outcomes = dict((await db.execute(
            select(ThreadDecision.thread_id, ThreadDecision.outcome).where(
                ThreadDecision.simulation_run_id == self.simulation_run_id,
                ThreadDecision.thread_id.in_([r.reply_ts for r in refining]),
            )
        )).all())
        held = {tid for a in self.agents.values() for tid in a.state.active_threads}
        for row in refining:
            outcome = outcomes.get(row.reply_ts)
            if outcome is not None:
                reason = {
                    "timeout": REASON_THREAD_TIMEOUT,
                    THREAD_OUTCOME_PROJECT_CLOSED: REASON_CLOSE_UNREADABLE,
                }.get(str(outcome), REASON_THREAD_ABANDONED)
                await self._finish_project(db, row.id, expected=STATUS_REFINING, reason=reason)
            elif row.reply_ts not in held:
                await self._finish_project(
                    db, row.id, expected=STATUS_REFINING, reason=REASON_THREAD_ABANDONED,
                    where=(HubProject.reply_claimed_at
                           < func.now() - timedelta(seconds=ORPHAN_GRACE_SECONDS),),
                )

    async def _request_due_labs(self, db) -> None:
        """Write a ``requested`` row for each due live lab (spec §5.2 "When"): no open project
        in this run, and either no project in this run or ``project_interval_hours`` of run
        time since its last one was requested. The slot is one past the lab's highest in
        this run, so a repeated request conflicts and writes nothing."""
        interval = float(deps.get_settings().project_interval_hours) * 3600.0
        history = {
            row.agent_id: row for row in (await db.execute(
                select(
                    HubProject.agent_id,
                    func.max(HubProject.slot).label("slot"),
                    func.max(HubProject.requested_run_seconds).label("last_requested"),
                    func.count().filter(HubProject.status.in_(OPEN_STATUSES)).label("open"),
                )
                .where(HubProject.simulation_run_id == self.simulation_run_id)
                .group_by(HubProject.agent_id)
            )).all()
        }
        clock = self._clock_seconds
        for agent_id in sorted(self.agents):
            if not self._project_lab_is_live(agent_id):
                continue
            past = history.get(agent_id)
            if past is not None and (past.open or clock - past.last_requested < interval):
                continue
            slot = 1 if past is None else int(past.slot) + 1
            await db.execute(
                pg_insert(HubProject)
                .values(
                    simulation_run_id=self.simulation_run_id, agent_id=agent_id, slot=slot,
                    status=STATUS_REQUESTED, requested_run_seconds=clock,
                )
                .on_conflict_do_nothing(constraint="uq_hub_projects_run_agent_slot")
            )
            await db.commit()
            logger.info(
                "[%s] Projects: requested project slot %d at run time %.0f s",
                agent_id, slot, clock,
            )

    async def _post_ready_projects(self, db) -> None:
        """Post each unclaimed ``ready_to_post`` project of this run as the poster.

        Lab not live: the project ends ``lab_not_live``. No poster on the roster: it waits.
        Otherwise the claim is committed before ``_post_message``; an exception (raised only
        before anything is logged) or no id clears it, and the next tick posts again. An id is
        linked; a link that raises is kept in ``_pending_links``. No ``landed_check``: within
        its clock skew of a fresh run's start it could take the run-start marker, which the
        log does not hold, for the posting."""
        ready = (await db.execute(
            select(HubProject.id, HubProject.agent_id, HubProject.posting_text).where(
                HubProject.simulation_run_id == self.simulation_run_id,
                HubProject.status == STATUS_READY_TO_POST,
                HubProject.posting_claimed_at.is_(None),
            ).order_by(HubProject.requested_at)
        )).all()
        for row in ready:
            if not self._project_lab_is_live(row.agent_id):
                await self._finish_project(
                    db, row.id, expected=STATUS_READY_TO_POST, reason=REASON_LAB_NOT_LIVE,
                    where=(HubProject.posting_claimed_at.is_(None),),
                )
                continue
            poster = self._project_poster()
            if poster is None or not row.posting_text:
                continue
            claimed = await db.execute(
                update(HubProject)
                .where(
                    HubProject.id == row.id, HubProject.status == STATUS_READY_TO_POST,
                    HubProject.posting_claimed_at.is_(None),
                )
                .values(posting_claimed_at=func.now(), updated_at=func.now())
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            if not claimed.rowcount:
                continue
            try:
                ts = await self._slack_io._post_message(
                    poster.agent_id, PROJECT_CHANNEL, row.posting_text, phase=PHASE_HUB_PROJECT,
                )
            except Exception:
                logger.warning(
                    "[%s] Projects: posting project %s failed; the next tick posts it again",
                    poster.agent_id, row.id, exc_info=True,
                )
                ts = None
            if not ts:
                await self._clear_project_claim(db, row.id, "posting")
                continue
            await self._link_or_keep(db, row.id, "posting", ts)

    async def _link_or_keep(self, db, project_id: uuid.UUID, kind: str, ts: str) -> None:
        """Link a just-posted root; when the write raises, keep it for the next tick."""
        try:
            await self._link_project_post(db, project_id, kind, ts)
        except Exception:
            logger.warning(
                "Projects: linking %s %s of project %s failed; retried at the next tick",
                kind, ts, project_id, exc_info=True,
            )
            try:
                await db.rollback()
            except Exception:
                logger.debug("Projects: rollback after a failed link failed", exc_info=True)
            self._pending_links[project_id] = (kind, ts)

    async def _count_awaiting_replies(self, db) -> None:
        """Recount the posted projects of live labs with no reply. 0 while the switch is off:
        a switched-off run must not hold its drain for replies that will never be offered."""
        if not self._enabled:
            self._awaiting_reply = 0
            return
        labs = (await db.execute(
            select(HubProject.agent_id).where(
                HubProject.simulation_run_id == self.simulation_run_id,
                HubProject.status == STATUS_POSTED,
                HubProject.reply_ts.is_(None),
            )
        )).scalars().all()
        self._awaiting_reply = sum(1 for agent_id in labs if self._project_lab_is_live(agent_id))

    def _awaiting_project_reply_count(self) -> int:
        """Posted projects of live labs still owed the lab's reply, as of the last tick."""
        return self._awaiting_reply

    # ------------------------------------------------------------------
    # Roster predicates
    # ------------------------------------------------------------------

    def _project_lab_is_live(self, agent_id: str) -> bool:
        """On the roster, a spoke role, and a public profile with a Research Summary."""
        agent = self.agents.get(agent_id)
        if agent is None or agent.role not in spoke_role_names():
            return False
        return bool(sections(agent.public_profile).get("Research Summary", "").strip())

    def _project_poster(self) -> Agent | None:
        """The first agent on the roster whose role may post stored project ideas."""
        for agent in self.agents.values():
            caps = capabilities_for(agent.role)
            if caps is not None and caps.posts_project_ideas:
                return agent
        return None

    # ------------------------------------------------------------------
    # The lab's reply (Phase 5)
    # ------------------------------------------------------------------

    async def _offer_project_reply(self, agent: Agent, *, draining: bool) -> bool:
        """Offer ``agent`` its oldest unanswered posted project of this run, inside its Phase-5
        turn (spec §5.7). True: the turn was used for the reply, end it. False: carry on to the
        lab's own pitch.

        False with no model call when the switch is off, there is no database, the lab has no
        posted project without a claim or reply, or its open partnered threads reach
        ``active_thread_threshold``. A refused rate reservation is False and counts nothing. A
        model exception, a truncated or refused answer or an empty one is False and uncounted
        (tried again at the lab's next turn, while its own pitch still runs) unless
        ``draining``, when each counts and returns True: the drain waits for this reply, so a
        reply that cannot succeed must not hold the run open. A post too long for one message,
        a suppressed or blocked post, or one that raised counts and returns True. The second
        counted failure ends the project ``not_finished`` / ``reply_failed``. Never changes
        ``consecutive_phase5_skips`` or ``last_phase5_action_time``."""
        if not self._enabled or not self.session_factory or not self.simulation_run_id:
            return False
        settings = deps.get_settings()
        if self._open_partnered_thread_count(agent) >= settings.active_thread_threshold:
            return False
        project = await self._unanswered_project(agent.agent_id)
        if project is None:
            return False
        if not agent.try_reserve(
            self._scheduler._allowance_for(agent), settings.llm_rate_window_seconds
        ):
            logger.warning("[%s] rate-limited; deferring the project reply", agent.agent_id)
            return False
        agent.record_api_call(already_reserved=True)
        title = project.title or ""
        text = await self._compose_project_reply(agent, project, settings)
        if text is None:
            if not draining:
                return False
            await self._count_project_reply_failure(project.id)
            return True
        post_text = REPLY_HEADER.format(title=title) + "\n" + text
        if not fits_one_message(post_text):
            logger.warning(
                "[%s] Projects: the reply to project %s would not fit one Slack message",
                agent.agent_id, project.id,
            )
            await self._count_project_reply_failure(project.id)
            return True
        await self._post_project_reply(agent, project.id, post_text)
        return True

    def _open_partnered_thread_count(self, agent: Agent) -> int:
        """The agent's active threads whose partner is on the roster (the predicate
        ``_open_interview_ids`` uses), so threads orphaned by a partner leaving the roster,
        which nothing will close, cannot block the reply forever."""
        return sum(
            1 for t in agent.state.active_threads.values()
            if t.status == "active" and t.other_agent_id in self.agents
        )

    async def _unanswered_project(self, agent_id: str):
        """The lab's oldest ``posted`` project of this run with no reply claim and no reply
        (``id``, ``title``, ``posting_text``), or None; None too when the read fails."""
        try:
            async with self.session_factory() as db:
                return (await db.execute(
                    select(HubProject.id, HubProject.title, HubProject.posting_text).where(
                        HubProject.simulation_run_id == self.simulation_run_id,
                        HubProject.agent_id == agent_id,
                        HubProject.status == STATUS_POSTED,
                        HubProject.reply_claimed_at.is_(None),
                        HubProject.reply_ts.is_(None),
                    ).order_by(HubProject.posted_at, HubProject.requested_at).limit(1)
                )).first()
        except Exception:
            logger.warning("[%s] Projects: reading the lab's posted project failed",
                           agent_id, exc_info=True)
            return None

    async def _compose_project_reply(self, agent: Agent, project, settings) -> str | None:
        """One model call for the lab's reply; the reply text, or None after an exception, a
        truncated or refused answer, or an empty one."""
        claim_reason = self.ctx.claim_feedback.get(agent.agent_id, PROJECT_REPLY_THREAD_KEY)
        stop_reasons: list[str] = []
        try:
            system_prompt, messages = agent.build_project_reply_post_prompt(
                posting_text=project.posting_text or "",
                title=project.title or "",
                authorship_feedback=feedback_text(claim_reason) if claim_reason else None,
            )
            response = await deps.generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                model=settings.llm_agent_model_opus,
                max_tokens=3300,
                # thread_phase is what cost_by_stage groups by.
                log_meta={
                    "agent_id": agent.agent_id,
                    "phase": "project_reply",
                    "thread_phase": "project_reply",
                    "call_id": uuid.uuid4().hex,
                },
                on_retry=agent.record_api_call,
                on_stop_reason=stop_reasons.append,
            )
        except Exception as exc:
            logger.warning("[%s] Projects: the reply call failed: %s", agent.agent_id, exc)
            return None
        if _was_truncated(stop_reasons):
            logger.warning(
                "[%s] Projects: the reply was TRUNCATED (%s); not posted",
                agent.agent_id, ", ".join(stop_reasons) or "?",
            )
            return None
        text = _extract_slack_message(response or "").strip()
        if not text:
            logger.warning("[%s] Projects: the reply was empty; not posted", agent.agent_id)
            return None
        return text

    async def _post_project_reply(self, agent: Agent, project_id: uuid.UUID, post_text: str) -> None:
        """Claim, post and link the lab's reply. A post that raises (nothing was logged: the
        client raises only when the first chunk failed twice) or returns no id clears the
        claim and counts a failure. A link that raises is kept in ``_pending_links``."""
        try:
            async with self.session_factory() as db:
                claimed = await db.execute(
                    update(HubProject)
                    .where(
                        HubProject.id == project_id, HubProject.status == STATUS_POSTED,
                        HubProject.reply_claimed_at.is_(None), HubProject.reply_ts.is_(None),
                    )
                    .values(reply_claimed_at=func.now(), updated_at=func.now())
                    .execution_options(synchronize_session=False)
                )
                await db.commit()
        except Exception:
            logger.warning("[%s] Projects: claiming the reply to project %s failed",
                           agent.agent_id, project_id, exc_info=True)
            return
        if not claimed.rowcount:
            logger.warning("[%s] Projects: project %s changed before its reply was posted",
                           agent.agent_id, project_id)
            return
        blocked: list[str] = []
        try:
            ts = await self._slack_io._post_message(
                agent.agent_id, PROJECT_CHANNEL, post_text, phase=PHASE_PROJECT_REPLY,
                landed_check=True, on_claim_block=blocked.append,
            )
        except Exception:
            logger.warning("[%s] Projects: posting the reply to project %s failed",
                           agent.agent_id, project_id, exc_info=True)
            ts = None
        if not ts:
            logger.warning(
                "[%s] Projects: the reply to project %s was not posted%s", agent.agent_id,
                project_id, f" (blocked: {blocked[0]})" if blocked else "",
            )
            try:
                async with self.session_factory() as db:
                    await self._clear_project_claim(db, project_id, "reply")
            except Exception:
                logger.warning("[%s] Projects: clearing the reply claim of project %s failed",
                               agent.agent_id, project_id, exc_info=True)
            await self._count_project_reply_failure(project_id)
            return
        agent.message_count += 1
        try:
            async with self.session_factory() as db:
                await self._link_or_keep(db, project_id, "reply", ts)
        except Exception:
            logger.warning("Projects: linking reply %s of project %s failed; retried at the "
                           "next tick", ts, project_id, exc_info=True)
            self._pending_links[project_id] = ("reply", ts)

    async def _count_project_reply_failure(self, project_id: uuid.UUID) -> None:
        """Count one reply failure on a ``posted`` project; the ``REPLY_FAILURES_MAX``-th ends
        it ``not_finished`` / ``reply_failed``. Never raises."""
        try:
            async with self.session_factory() as db:
                failures = (await db.execute(
                    update(HubProject)
                    .where(HubProject.id == project_id, HubProject.status == STATUS_POSTED)
                    .values(reply_failures=HubProject.reply_failures + 1, updated_at=func.now())
                    .returning(HubProject.reply_failures)
                    .execution_options(synchronize_session=False)
                )).scalar_one_or_none()
                await db.commit()
                if failures is not None and failures >= REPLY_FAILURES_MAX:
                    await self._finish_project(
                        db, project_id, expected=STATUS_POSTED, reason=REASON_REPLY_FAILED,
                        where=(HubProject.reply_ts.is_(None),),
                    )
        except Exception:
            logger.warning("Projects: counting a reply failure of project %s failed",
                           project_id, exc_info=True)

    # ------------------------------------------------------------------
    # The project thread (read by the reply lane)
    # ------------------------------------------------------------------

    async def _project_thread_context(self, thread_id: str) -> dict | None:
        """The project whose lab reply is ``thread_id`` in this run, as ``{"project_id",
        "status", "title", "summary", "plan", "anchors", "sources", "why_chosen"}`` from its
        stored choice (anchors ``{section, item}``, sources ``{key, kind, title, domain,
        shows}``). ``{"project_id": None, "status": None}`` when no project of this run has that
        reply. None when the read fails, or the reply's link is still in ``_pending_links`` or
        still an unresolved claim of the root's author (``_resolve_project_claims`` links it at
        a tick): the caller skips the turn then, and closes the thread in the other two cases
        unless the status is ``refining``."""
        if not self.session_factory or not self.simulation_run_id:
            return None
        try:
            async with self.session_factory() as db:
                row = (await db.execute(
                    select(
                        HubProject.id, HubProject.status, HubProject.title, HubProject.choice,
                    ).where(
                        HubProject.simulation_run_id == self.simulation_run_id,
                        HubProject.reply_ts == thread_id,
                    ).order_by(HubProject.requested_at.desc()).limit(1)
                )).first()
        except Exception:
            logger.warning("Projects: reading the project of thread %s failed",
                           thread_id, exc_info=True)
            return None
        if row is None:
            if ("reply", thread_id) in self._pending_links.values():
                return None
            if await self._unresolved_reply_claim(thread_id):
                return None
            return {"project_id": None, "status": None}
        choice = row.choice if isinstance(row.choice, dict) else {}
        return {
            "project_id": row.id,
            "status": row.status,
            "title": choice.get("title") or row.title or "",
            "summary": choice.get("summary") or "",
            "plan": choice.get("plan") if isinstance(choice.get("plan"), dict) else {},
            "anchors": list(choice.get("anchors") or []),
            "sources": list(choice.get("sources") or []),
            "why_chosen": choice.get("why_chosen") or "",
        }

    async def _unresolved_reply_claim(self, thread_id: str) -> bool:
        """True when the author of ``thread_id``'s root has a project in this run with a reply
        claim and no reply linked yet: a crash between the post and its link, not yet resolved
        because the resolving reads failed. True too when the read fails, so a thread is never
        closed on a guess."""
        root = self.message_log.get_entry(thread_id)
        if root is None or not root.sender_agent_id:
            return False
        try:
            async with self.session_factory() as db:
                found = await db.scalar(
                    select(HubProject.id).where(
                        HubProject.simulation_run_id == self.simulation_run_id,
                        HubProject.agent_id == root.sender_agent_id,
                        HubProject.reply_claimed_at.is_not(None),
                        HubProject.reply_ts.is_(None),
                    ).limit(1)
                )
        except Exception:
            logger.warning("Projects: reading the reply claims for thread %s failed",
                           thread_id, exc_info=True)
            return True
        return found is not None

    def _latest_lab_position(self, thread_id: str, lab_agent_id: str) -> str | None:
        """The lab's position in a project thread: consecutive entries by one sender form one
        post (a reply over Slack's limit arrives as several chunks, its marker on the first);
        the last post by the lab that is not the root gives ``position_marker`` of its first
        entry. None when the lab has no such post or it opens with no marker."""
        posts: list[list[Any]] = []
        for entry in self.message_log.get_thread_history(thread_id):
            if posts and posts[-1][0].sender_agent_id == entry.sender_agent_id:
                posts[-1].append(entry)
            else:
                posts.append([entry])
        for post in reversed(posts):
            if post[0].sender_agent_id != lab_agent_id:
                continue
            if any(entry.ts == thread_id for entry in post):
                return None
            return position_marker(post[0].content)
        return None

    async def _record_project_close(
        self, agent: Agent, thread: ThreadState, raw_response: str, posted_ts: str | None,
    ) -> str | None:
        """Record BlackbirdBot's close of a project thread (spec §5.9), from ``refining``.

        No readable ``<project_json>`` block: ``not_finished`` / ``close_unreadable``. Else
        ``project_outcome`` decides ``ready`` or ``not_agreed`` from the lab's latest position,
        the marker the close opens with as logged, and the block; ``final`` holds the plan,
        the plan's lab tools marked against the lab's profile, both positions, the gaps and
        the disagreements. Returns the status written, None when nothing was written."""
        if not self.session_factory or not self.simulation_run_id:
            return None
        close = _extract_project_json(raw_response)
        values: dict[str, Any]
        if close is None:
            values = {"status": STATUS_NOT_FINISHED, "reason_code": REASON_CLOSE_UNREADABLE}
        else:
            values = self._close_values(thread, close, posted_ts)
        try:
            async with self.session_factory() as db:
                result = await db.execute(
                    update(HubProject)
                    .where(
                        HubProject.simulation_run_id == self.simulation_run_id,
                        HubProject.reply_ts == thread.thread_id,
                        HubProject.status == STATUS_REFINING,
                    )
                    .values(
                        **values, finished_at=func.now(),
                        finished_run_seconds=self._clock_seconds, updated_at=func.now(),
                    )
                    .execution_options(synchronize_session=False)
                )
                await db.commit()
        except Exception:
            logger.warning("[%s] Projects: recording the close of thread %s failed",
                           agent.agent_id, thread.thread_id, exc_info=True)
            return None
        if not result.rowcount:
            logger.warning("[%s] Projects: no refining project for thread %s; close not recorded",
                           agent.agent_id, thread.thread_id)
            return None
        logger.info("[%s] Projects: thread %s closed as %s",
                    agent.agent_id, thread.thread_id, values["status"])
        return values["status"]

    def _close_values(self, thread: ThreadState, close: dict, posted_ts: str | None) -> dict:
        """The columns a readable close writes: the status and ``final``."""
        lab_position = self._latest_lab_position(thread.thread_id, thread.other_agent_id)
        logged = self.message_log.get_entry(posted_ts) if posted_ts else None
        hub_marker = position_marker(logged.content if logged is not None else None)
        status, gaps = project_outcome(
            lab_position=lab_position, hub_marker=hub_marker, close=close,
        )
        plan = close.get("plan") if isinstance(close.get("plan"), dict) else {}
        lab_tools = plan.get("lab_tools")
        lab_tools = [t for t in lab_tools if isinstance(t, str)] if isinstance(lab_tools, list) else []
        lab = self.agents.get(thread.other_agent_id)
        disagreements = close.get("disagreements")
        final = {
            "plan": plan,
            "tool_marks": tool_marks(lab_tools, section_items(lab.public_profile if lab else None)),
            "hub_position": (
                "agree" if hub_marker == "agree" and close.get("position") == "agree"
                else "disagree"
            ),
            "lab_position": lab_position,
            "gaps": gaps,
            "disagreements": (
                [d for d in disagreements if isinstance(d, str)]
                if isinstance(disagreements, list) else []
            ),
        }
        return {"status": status, "reason_code": None, "final": final}

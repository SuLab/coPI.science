"""Startup reconstruction from the database: the message log, per-agent threads, call ledgers and cursors, and the Slack cursor seed on resume (spec §7.1). Resume reads the DB only (B3)."""

from __future__ import annotations

import logging
from datetime import UTC, timedelta
from typing import TYPE_CHECKING

from src.agent.engine import deps
from src.agent.engine.constants import _CALLS_PER_LOG_ROW, REBUILD_WINDOW_S
from src.agent.engine.context import EngineContext, via
from src.agent.engine.helpers import _restored_slack_ts
from src.agent.message_log import LogEntry, is_panel_note
from src.models import (
    AgentMessage,
    LlmCallLog,
    ThreadDecision,
)
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE

if TYPE_CHECKING:
    from src.agent.engine.channel_directory import ChannelDirectory
    from src.agent.engine.panel import Panel
    from src.agent.engine.slack_io import SlackIO
    from src.agent.engine.threads import Threads

logger = logging.getLogger("src.agent.simulation")


class Rebuild:
    """Startup reconstruction: the message log from the DB, then per-agent state from the log."""

    agents = via("ctx")
    message_log = via("ctx")
    session_factory = via("ctx")
    simulation_run_id = via("ctx")
    _channel_visibility = via("_channel_directory")
    _specialist_consults = via("_panel")
    _seed_slack_cursors_without_ingest = via("_slack_io")
    _ts_minter = via("_slack_io")
    _closed_thread_ids = via("_threads")

    OWNED_STATE: tuple[str, ...] = ("_fresh_start", "_reset_cursors")

    def __init__(
        self,
        ctx: EngineContext,
        *,
        threads: Threads,
        slack_io: SlackIO,
        panel: Panel,
        channel_directory: ChannelDirectory,
        reset_cursors: bool,
        fresh_start: bool,
    ) -> None:
        self.ctx = ctx
        self._threads = threads
        self._slack_io = slack_io
        self._panel = panel
        self._channel_directory = channel_directory
        self._reset_cursors = reset_cursors
        # True only for `--fresh`, which has just minted a new run with no
        # agent_messages/agent_channels rows of its own (it deletes nothing —
        # see main._open_fresh_run). Read by `start()`: only a resume runs the
        # hub's reply-less-pitch recovery, and only a fresh run announces
        # itself. Both kinds of start seed the Slack poll cursors past the
        # history already on the transport (see _restore_slack_state).
        self._fresh_start = fresh_start

    async def _rebuild_state_from_db(self) -> None:
        """Hydrate the MessageLog from agent_messages — the primary store.

        Loads message bodies (available since migration 0019) via the
        callback-bypassing path so restored rows aren't re-persisted. Seeds the
        mint_ts high-water mark and, for rows that were mirrored to Slack, the
        Slack poll cursors.
        See specs/local-db-conversations.md.
        """
        if not self.session_factory or not self.simulation_run_id:
            logger.info("No DB session — skipping DB rebuild")
            return
        from sqlalchemy import func as sa_func
        from sqlalchemy import or_
        from sqlalchemy import select as sa_select
        # Bound the load (B2): recent messages, plus the full history of any
        # thread that has no ThreadDecision (still undecided/active). Active-thread
        # reconstruction only needs undecided threads; old closed-thread bodies
        # would just bloat RAM and startup. Nothing in the engine reopens an old
        # closed thread since 23da58d.
        recent_floor = deps.time.time() - REBUILD_WINDOW_S
        closed_thread_ids_subq = sa_select(ThreadDecision.thread_id).where(
            ThreadDecision.simulation_run_id == self.simulation_run_id
        )
        try:
            async with self.session_factory() as db:
                result = await db.execute(
                    sa_select(AgentMessage)
                    .where(
                        AgentMessage.simulation_run_id == self.simulation_run_id,
                        or_(
                            AgentMessage.posted_at > recent_floor,
                            sa_func.coalesce(
                                AgentMessage.thread_ts, AgentMessage.message_ts
                            ).notin_(closed_thread_ids_subq),
                        ),
                    )
                    .order_by(AgentMessage.posted_at.asc(), AgentMessage.created_at.asc())
                )
                rows = result.scalars().all()
        except Exception as exc:
            logger.warning("DB rebuild failed: %s", exc)
            return

        loaded = 0
        max_posted = 0.0
        for r in rows:
            # Pre-0019 rows carry only metadata (empty body): skip them. They
            # hold no conversational signal, and if Slack is on the reconcile
            # pass re-adds them with content. Stage 7 backfills legacy content.
            if not r.content or not r.message_ts:
                continue
            entry = LogEntry(
                ts=r.message_ts,
                channel=r.channel_name,
                sender_agent_id=r.agent_id,
                sender_name=r.sender_name or "",
                content=r.content,
                thread_ts=r.thread_ts,
                posted_at=r.posted_at or 0.0,
                is_bot=r.is_bot,
                visibility=r.visibility,
                # Restore the Slack mirror mapping, not just the content: a reply
                # posted after this restart needs the root's Slack ts to thread
                # on, and its absence is how a DB-origin thread is recognised.
                slack_ts=_restored_slack_ts(r),
                slack_channel_id=r.slack_channel_id,
                slack_thread_ts=r.slack_thread_ts,
                # Restore the KIND too, or a panel note comes back from the DB
                # as an ordinary reply and re-enters every agent-facing read
                # the moment the process restarts — thread histories, message
                # counts, the other party's reply trigger. The exclusions are
                # only as durable as this line.
                phase=r.phase,
            )
            self.message_log.load_entry(entry)
            loaded += 1
            if entry.posted_at > max_posted:
                max_posted = entry.posted_at
            # Advance the Slack poll cursor past every stored mirrored message.
            if r.slack_ts and r.slack_channel_id:
                self._slack_io.seed_cursor(r.slack_channel_id, r.slack_ts)
        self._ts_minter.seed_floor(max_posted)
        logger.info("Rebuilt MessageLog from DB: %d messages", loaded)

    async def _restore_slack_state(self) -> None:
        """Park the Slack poll cursors past the history already on the transport.

        Every start does this — fresh and resumed alike. A resume used to
        reconcile Slack history into the log instead; that pass re-imported
        other runs' conversations and every other bot's messages into this run,
        so a restart now restores from the database only, and the engine's own
        posts reach it write-through (`_post_message`). What a restart can no
        longer recover — Slack-native messages posted while the engine was down
        — is accepted.

        The cursor seed is still needed on a resume: `_rebuild_state_from_db`
        only advances cursors for channels with stored mirrored rows, and the
        live poller defaults every other channel to "0".
        """
        await self._seed_slack_cursors_without_ingest()

    async def _rebuild_agent_state(self) -> None:
        """Reconstruct per-agent state from the message log + DB.

        Runs after the DB rebuild, so it
        behaves identically with Slack on or off. Reads only self.message_log,
        thread_decisions and llm_call_logs — no Slack calls.
        """
        closed_thread_ids = await self._restore_prior_decisions()
        self._restore_active_threads(closed_thread_ids)
        await self._restore_api_call_counts()
        await self._restore_call_ledgers()
        self._restore_cursors_and_log_summary()

    async def _restore_prior_decisions(self) -> set[str]:
        """Load the run's thread decisions: the closed set and Phase 5's prior summaries."""
        # Rebuild active_threads per agent.
        # Get all closed thread IDs and prior thread summaries from thread_decisions
        closed_thread_ids: set[str] = set()
        if self.session_factory and self.simulation_run_id:
            try:
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    result = await db.execute(
                        sa_select(ThreadDecision).where(
                            ThreadDecision.simulation_run_id == self.simulation_run_id
                        )
                    )
                    all_decisions = result.scalars().all()
                    for td in all_decisions:
                        closed_thread_ids.add(td.thread_id)
                        # _prior_threads is a list per pair, so appending here
                        # unconditionally is not idempotent: a second rebuild —
                        # or a rebuild after _close_thread already recorded this
                        # thread in-process — feeds Phase 5 the same prior
                        # discussion twice, as "you already tried this N times".
                        # _closed_thread_ids is the shared already-accounted-for
                        # marker (_close_thread sets it before its own append),
                        # and it is only updated after this loop, so a thread with
                        # several decision rows from repeated propose/reopen cycles
                        # still contributes each of them on the first pass.
                        if td.thread_id in self._closed_thread_ids:
                            continue
                        pair_key = tuple(sorted([td.agent_a, td.agent_b]))
                        self._threads.restore_prior(pair_key, {
                            "channel": td.channel,
                            "outcome": td.outcome,
                            "summary": (td.summary_text or "")[:400] or None,
                            # Carried for G3 dedup-context visibility filtering.
                            "origin_visibility": td.origin_visibility,
                        })
                    self._threads.mark_closed(*closed_thread_ids)
            except Exception as exc:
                logger.warning("Failed to load thread decisions: %s", exc)
        return closed_thread_ids

    def _restore_active_threads(self, closed_thread_ids: set[str]) -> None:
        """Re-open each agent's unfinished threads from the message log."""
        for agent in self.agents.values():
            aid = agent.agent_id
            # Find threads where this agent participated
            for entry in self.message_log._entries:
                if entry.sender_agent_id != aid:
                    continue
                if is_panel_note(entry):
                    # Posting a note is not participating. Restoring a thread
                    # off one would resurrect, for the hub, an interview it had
                    # not yet said anything in — and would do it from an entry
                    # that `get_thread_history` (used further down for the
                    # participant and pending-reply decisions) cannot see, so
                    # the two halves of this reconstruction would disagree.
                    continue
                thread_id = entry.thread_ts or entry.ts
                # Skip if already closed or already tracked
                if thread_id in closed_thread_ids:
                    continue
                if thread_id in agent.state.active_threads:
                    continue
                # Skip thread reconstruction for collab_private channels —
                # discussion in those channels is flat, not threaded, so
                # there shouldn't be an active_thread at all. Any threaded
                # replies pre-dating this rule are left alone in Slack but
                # not reactivated in-memory.
                if self._channel_visibility.get(entry.channel) == VISIBILITY_COLLAB_PRIVATE:
                    continue
                # Skip a root post that has no replies — nothing to restore.
                # (Root posts that DO have replies must be restored: a reply
                # arriving while we were at the active-thread cap could have
                # been dropped from Phase 3 and would otherwise be ghosted.)
                if entry.thread_ts is None:
                    history = self.message_log.get_thread_history(thread_id)
                    if len(history) <= 1:
                        continue
                # Find the other agent in this thread
                root = self.message_log.get_entry(thread_id)
                if not root:
                    continue
                other_id = root.sender_agent_id if root.sender_agent_id != aid else None
                if not other_id:
                    # Check other replies for the other agent
                    history = self.message_log.get_thread_history(thread_id)
                    for h in history:
                        if h.sender_agent_id and h.sender_agent_id != aid:
                            other_id = h.sender_agent_id
                            break
                if not other_id:
                    continue

                msg_count = self.message_log.get_thread_message_count(thread_id)
                # Check if the last message was from the other agent (pending reply)
                history = self.message_log.get_thread_history(thread_id)
                last_sender = history[-1].sender_agent_id if history else None
                has_pending = last_sender is not None and last_sender != aid
                self._threads.activate_thread(
                    agent, thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=msg_count,
                    has_pending_reply=has_pending,
                    # Initial seed for the monotonic latch — see
                    # ThreadState.floor_armed. This rebuild runs at process
                    # startup, before this fresh process's _specialist_consults
                    # could hold anything, so this always starts False here —
                    # correctly matching the post-restart fail-open case
                    # _specialist_floor_gap's docstring describes. It is not
                    # stuck there: the per-turn latch at the top of
                    # _reply_to_thread re-checks the global map on every later
                    # turn this restored thread takes, and arms once the
                    # restarted process itself records any consult.
                    floor_armed=bool(self._specialist_consults),
                )

    async def _restore_api_call_counts(self) -> None:
        """Rebuild each agent's lifetime api_call_count from llm_call_logs."""
        # 4. Rebuild api_call_count per agent from DB.
        #
        # Per CALL, not per ROW. One `llm_call_logs` row is one TURN and a turn
        # can be several real billed API calls — 78.6% of stored `thread_reply`
        # rows are 2+. Live booking is per-call (`_on_llm_call` books the tool
        # rounds; `record_api_call` books everything else), so a per-row rebuild
        # would silently reset every restarted agent's lifetime count and window
        # to a fraction of its real spend.
        #
        # `COALESCE(jsonb_array_length(call_stats), 1)`, never a bare
        # `jsonb_array_length`: 4,650 of the 5,771 stored rows have `call_stats
        # IS NULL` (the column arrived in migration 0032), and NULL propagates
        # through SUM — collapsing the lifetime rebuild and loosening the
        # throttle in the opposite direction. A row that recorded nothing is
        # worth exactly the one call we know it made.
        if self.session_factory and self.simulation_run_id:
            try:
                from sqlalchemy import func as sa_func
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    result = await db.execute(
                        sa_select(
                            LlmCallLog.agent_id,
                            sa_func.sum(_CALLS_PER_LOG_ROW).label("count"),
                        )
                        .where(LlmCallLog.simulation_run_id == self.simulation_run_id)
                        .group_by(LlmCallLog.agent_id)
                    )
                    for r in result:
                        agent = self.agents.get(r.agent_id)
                        if agent:
                            agent.api_call_count = int(r.count or 0)
            except Exception as exc:
                logger.warning("Failed to rebuild api_call_count: %s", exc)

    async def _restore_call_ledgers(self) -> None:
        """Rebuild each agent's sliding-window call ledger from llm_call_logs."""
        # 4b. Rebuild the sliding-window call ledger from the same table.
        #
        # Deliberately SEPARATE from step 4. Both now sum `_CALLS_PER_LOG_ROW`
        # (this comment said step 4 "stays an all-time COUNT(*)" until the
        # per-call change landed), but they differ in WINDOW and in purpose:
        # step 4 is all-time lifetime accounting (run summary,
        # SimulationRun.total_api_calls) while call_times is the live throttle
        # and reads only back to the rate window's cutoff.
        # Folding these together is the bug — it is what made an over-budget
        # agent over-budget again on every restart, forever. See design §4.2.
        if self.session_factory and self.simulation_run_id:
            try:
                from sqlalchemy import select as sa_select

                # datetime, UTC and timedelta are already module-level imports
                # of this module — do not re-import them here.
                cutoff = deps.datetime.now(UTC) - timedelta(
                    seconds=deps.get_settings().llm_rate_window_seconds
                )
                async with self.session_factory() as db:
                    result = await db.execute(
                        sa_select(
                            LlmCallLog.agent_id,
                            LlmCallLog.created_at,
                            _CALLS_PER_LOG_ROW.label("calls"),
                        )
                        .where(
                            LlmCallLog.simulation_run_id == self.simulation_run_id,
                            LlmCallLog.created_at >= cutoff,
                        )
                        .order_by(LlmCallLog.created_at)
                    )
                    rows = result.all()
                # call_times is a deque that try_reserve appends to
                # AND that record_api_call's default (already_reserved=False)
                # path also appends to (see that method's
                # docstring; the seven call sites that rely on this default are
                # never separately reserved, so record_api_call is the only
                # place they are booked into the window at all), same
                # shape as the _prior_threads rebuild above — so a plain append here is
                # not idempotent either: a second rebuild call would duplicate
                # every in-window entry and could throttle an agent that isn't
                # actually over its allowance. Unlike _prior_threads, this
                # query is a full window snapshot (not one row per agent), so
                # the fix is a clear-then-repopulate rather than a replace-by-key.
                # Clear ALL agents, not just the ones with rows in `rows`: the
                # window query is authoritative for every agent, and an agent
                # with zero in-window calls must end up with an EMPTY ledger,
                # not whatever stale entries it had before this rebuild. The
                # clear is sequenced after the query succeeds (not before) so a
                # DB failure below is caught and logged without first wiping a
                # ledger it then fails to repopulate.
                for agent in self.agents.values():
                    agent.state.call_times.clear()
                for r in rows:
                    agent = self.agents.get(r.agent_id)
                    if agent:
                        # One ENTRY PER CALL, not per row — the same change as
                        # step 4, and it has to move with it. Live booking is
                        # per-call, so a per-row ledger would let a restarted
                        # agent spend the ratio of calls-to-turns more than its
                        # allowance. All of a turn's calls share the row's
                        # timestamp; the window only cares about the boundary,
                        # and the individual calls are seconds apart at most.
                        stamp = r.created_at.timestamp()
                        for _ in range(int(r.calls or 1)):
                            agent.state.call_times.append(stamp)
            except Exception as exc:
                logger.warning("Failed to rebuild call_times: %s", exc)

    def _restore_cursors_and_log_summary(self) -> None:
        """Set each agent's last_seen_cursor and log what was restored."""
        # 5. Set last_seen_cursor per agent to latest message time
        if self._reset_cursors:
            logger.info("--reset-cursors: agents will re-scan all posts")
            for agent in self.agents.values():
                agent.state.last_seen_cursor = 0
        elif self.message_log._entries:
            # Panel notes are deliberately NOT excluded from this max. It is a
            # "don't rescan what is already stored" high-water mark, not a
            # per-entry decision, and a note is a real message on the transport
            # — stopping the cursor short of one would leave every agent
            # rescanning up to it forever, and the entries a note could hide
            # behind it are older than it by construction.
            latest_ts = max(e.posted_at for e in self.message_log._entries)
            for agent in self.agents.values():
                agent.state.last_seen_cursor = latest_ts

        # Log rebuild summary
        for agent in self.agents.values():
            at = len(agent.state.active_threads)
            if at:
                logger.info(
                    "[%s] Restored: %d active threads, %d API calls",
                    agent.agent_id, at, agent.api_call_count,
                )

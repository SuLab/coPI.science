"""Interview threads: close and evict, the closed-thread set, prior-thread summaries for Phase 5, and the one place an activating ThreadState is built (spec §7.1, S1-09)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.agent.agent import Agent
from src.agent.engine.constants import PRIOR_THREADS_KEPT_PER_PAIR
from src.agent.engine.context import EngineContext, via
from src.agent.engine.headlines import should_announce
from src.agent.engine.helpers import _visibility_permits
from src.agent.prompt_safety import delimit
from src.agent.state import ThreadState
from src.models import ThreadDecision
from src.models.agent_activity import VISIBILITY_PUBLIC

if TYPE_CHECKING:
    from src.agent.engine.headlines import Headlines
    from src.agent.engine.memory import Memory
    from src.agent.engine.persistence import Persistence
    from src.agent.engine.verdicts import Verdicts

logger = logging.getLogger("src.agent.simulation")


class Threads:
    """The interview threads: closes, evictions, the closed set and the prior-thread summaries."""

    _agent_locks = via("ctx", "agent_locks")
    agents = via("ctx")
    session_factory = via("ctx")
    simulation_run_id = via("ctx")
    _assessed_threads = via("_verdicts")
    flush_before_dependent = via("_persistence")

    OWNED_STATE: tuple[str, ...] = ("_closed_thread_ids", "_prior_threads", "_pending_decisions")

    def __init__(
        self,
        ctx: EngineContext,
        *,
        headlines: Headlines,
        memory: Memory,
        verdicts: Verdicts,
        persistence: Persistence,
    ) -> None:
        self.ctx = ctx
        self._persistence = persistence
        self._headlines = headlines
        self._memory = memory
        self._verdicts = verdicts
        # Closed thread IDs — prevents Phase 3 from re-activating decided threads
        self._closed_thread_ids: set[str] = set()

        # Prior thread decisions per agent pair — for Phase 5 dedup context.
        # Key: tuple(sorted([agent_a, agent_b])), Value: list of dicts
        self._prior_threads: dict[tuple[str, str], list[dict]] = {}

        # ThreadDecision rows not yet written (S2-06, S1-12): waiting on their
        # thread's message rows, or a failed write. Drained by
        # `flush_pending_decisions`.
        self._pending_decisions: list[dict] = []

    def mark_closed(self, *thread_ids: str) -> None:
        """Record threads as closed (spec §7.2 rule 1). Insert-only: nothing un-closes
        a thread (see the note in ``_evict_dead_thread``)."""
        self._closed_thread_ids.update(thread_ids)

    def restore_prior(self, pair_key: tuple[str, str], record: dict) -> None:
        """Append one closed-thread summary for ``pair_key`` (spec §7.2 rule 1), keeping
        the newest ``PRIOR_THREADS_KEPT_PER_PAIR`` per pair exactly as ``_close_thread``
        does."""
        self._prior_threads.setdefault(pair_key, []).append(record)
        pair_list = self._prior_threads[pair_key]
        if len(pair_list) > PRIOR_THREADS_KEPT_PER_PAIR:
            del pair_list[: len(pair_list) - PRIOR_THREADS_KEPT_PER_PAIR]

    def activate_thread(
        self,
        agent: Agent,
        thread_id: str,
        *,
        channel: str,
        other_agent_id: str,
        message_count: int,
        has_pending_reply: bool,
        floor_armed: bool,
    ) -> ThreadState:
        """Open ``thread_id`` in ``agent``'s active threads — the one place an activating
        ``ThreadState`` is built (S1-09). ``floor_armed`` seeds the monotonic latch
        (``ThreadState.floor_armed``); callers pass ``bool(self._specialist_consults)``
        because this unit may not read the panel (spec §7.2). The synthetic
        ``ThreadState`` in ``Headlines._announce_owed_headline`` is not an activation
        and does not come here."""
        thread = ThreadState(
            thread_id=thread_id,
            channel=channel,
            other_agent_id=other_agent_id,
            message_count=message_count,
            has_pending_reply=has_pending_reply,
            floor_armed=floor_armed,
        )
        agent.state.active_threads[thread_id] = thread
        return thread

    async def _close_thread(
        self,
        agent: Agent,
        thread: ThreadState,
        outcome: str,
        summary_text: str | None = None,
        closed_by_role: str | None = None,
    ) -> None:
        """Close a thread and log the decision.

        ``closed_by_role`` is the role of whoever's reply ended the interview, or
        None for the ``max_thread_messages`` timeout, which no reply triggered.
        Recorded because ⏸️ is an instruction to BOTH roles — the hub's decline
        and a lab withdrawing its own pitch — and ``_check_thread_outcome`` tests
        for it on whichever agent just replied. Seven of run 8b64a0e0's closes
        were a lab bot ending the hub's own screen mid-interview, and in this
        table they looked identical to the single genuine timeout.

        Fires from inside `_reply_to_thread` (system-enforced close) or
        `_check_thread_outcome` (⏸️ decline), both of which run under the
        reply lane's THREAD lock for `thread.thread_id` — see
        `_dispatch_reply_lane`. This method's own mutation of BOTH agents'
        `active_threads` (one of the two motivating cases for `_agent_locks`
        at all, alongside `_evict_dead_thread` below) is additionally guarded
        by the
        AGENT lock, acquired here, nested INSIDE the already-held thread
        lock: thread-lock-outer, agent-lock-inner, per the ordering note on
        `_thread_locks` in __init__. `acquire_all` sorts the two agent ids,
        so two closes racing in opposite directions (this agent/other vs.
        other/this agent — spec §3.2's motivating scenario) converge on the
        same acquisition order and cannot deadlock on each other.
        """
        async with self._agent_locks.acquire_all(agent.agent_id, thread.other_agent_id):
            thread.status = "closed"
            self._closed_thread_ids.add(thread.thread_id)

            # Track for Phase 5 dedup context
            pair_key = tuple(sorted([agent.agent_id, thread.other_agent_id]))
            self._prior_threads.setdefault(pair_key, []).append({
                "channel": thread.channel,
                "outcome": outcome,
                "summary": (summary_text or "")[:400] or None,
            })
            pair_list = self._prior_threads[pair_key]
            if len(pair_list) > PRIOR_THREADS_KEPT_PER_PAIR:
                del pair_list[: len(pair_list) - PRIOR_THREADS_KEPT_PER_PAIR]
            # Remove from active threads
            agent.state.active_threads.pop(thread.thread_id, None)

            # Also close for the other agent if they have this thread active
            other_agent = self.agents.get(thread.other_agent_id)
            if other_agent and thread.thread_id in other_agent.state.active_threads:
                other_agent.state.active_threads[thread.thread_id].status = "closed"
                other_agent.state.active_threads.pop(thread.thread_id, None)

            # Log to DB
            if self.session_factory and self.simulation_run_id:
                await self._record_decision(agent.agent_id, {
                    "simulation_run_id": self.simulation_run_id,
                    "thread_id": thread.thread_id,
                    "channel": thread.channel,
                    "agent_a": agent.agent_id,
                    "agent_b": thread.other_agent_id,
                    "outcome": outcome,
                    "summary_text": summary_text,
                    "closed_by_role": closed_by_role,
                })

            logger.info(
                "[%s] Thread %s closed: %s",
                agent.agent_id, thread.thread_id, outcome,
            )

            # Queue working-memory updates for both agents. NOT awaited here:
            # these are LLM calls, and this block holds the thread lock, both
            # agent locks and a reply-lane semaphore slot — running them here
            # serialized every close on the hub's key and starved the reply
            # lane (audit finding 1). _drain_memory_events (main loop / stop)
            # applies them sequentially, which preserves the same lost-update
            # protection the lock provided. summary_text is derived from a
            # cross-agent conversation, so it is fenced as untrusted before it
            # lands in working memory (SEC-14), same as before.
            event = f"Thread in #{thread.channel} with {thread.other_agent_id} closed: {outcome}"
            if summary_text:
                event += f". Summary: {delimit(summary_text[:200], 'proposal_summary')}"
            self._memory.enqueue(
                (agent.agent_id, event, VISIBILITY_PUBLIC, None)
            )
            if other_agent:
                other_event = f"Thread in #{thread.channel} with {agent.agent_id} closed: {outcome}"
                if summary_text:
                    other_event += f". Summary: {delimit(summary_text[:200], 'proposal_summary')}"
                self._memory.enqueue(
                    (other_agent.agent_id, other_event, VISIBILITY_PUBLIC, None)
                )

            # An interview is over. If it still holds a verdict nobody
            # announced, that verdict has no later turn coming and this is the
            # last moment anything knows the interview ended — before 2026-08-29
            # nothing looked, and production lost two headlines (slusher,
            # rothstein) exactly here. QUEUE only: see `_pending_headlines`.
            if thread.thread_id in self._assessed_threads and should_announce(
                trigger="thread-close",
                already_announced=self._headlines.is_announced(thread.thread_id),
            ):
                self._headlines.enqueue(thread.thread_id)

    async def _write_decision(self, row: dict) -> None:
        async with self.ctx.session_factory() as db:
            db.add(ThreadDecision(**row))
            await db.commit()

    async def _record_decision(self, agent_id: str, row: dict) -> None:
        """Write a ThreadDecision after its thread's message rows are durable
        (S2-06); on a flush that left rows behind, or a failed write, queue it
        for the next flush instead of losing it (S1-12)."""
        if not await self.flush_before_dependent():
            self._pending_decisions.append(row)
            logger.warning(
                "[%s] Thread decision for %s queued: its message rows are not "
                "flushed yet (S2-06)", agent_id, row["thread_id"],
            )
            return
        try:
            await self._write_decision(row)
        except Exception as exc:  # noqa: BLE001 — queued, retried by the next flush
            self._pending_decisions.append(row)
            logger.error(
                "[%s] Failed to log thread decision for %s (outcome=%s): %s — "
                "queued for retry", agent_id, row["thread_id"], row["outcome"], exc,
                exc_info=True,
            )

    async def flush_pending_decisions(self, *, final: bool = False) -> list[str]:
        """Retry queued ThreadDecisions after their messages flushed (S1-12).
        ``final=True`` is the last attempt at shutdown: it writes even behind an
        unflushed message buffer (the rows would otherwise be lost for good) and
        logs any that still fail as LOST with their thread ids. Returns the
        thread ids of the decisions it wrote (``stop()`` sweeps their headlines
        again)."""
        if not self._pending_decisions:
            return []
        if not final and not await self.flush_before_dependent():
            return []
        rows, self._pending_decisions = self._pending_decisions, []
        failed: list[dict] = []
        last_exc: Exception | None = None
        for row in rows:
            try:
                await self._write_decision(row)
            except Exception as exc:  # noqa: BLE001
                failed.append(row)
                last_exc = exc
        if not failed:
            logger.info("Flushed %d queued thread decision(s) to DB", len(rows))
            return [r["thread_id"] for r in rows]
        if final:
            logger.error(
                "LOST %d thread decision(s) at shutdown (threads: %s): %s",
                len(failed), ", ".join(r["thread_id"] for r in failed), last_exc,
            )
        else:
            self._pending_decisions[0:0] = failed
            logger.warning(
                "%d thread decision(s) still failing; re-queued: %s", len(failed), last_exc,
            )
        return [r["thread_id"] for r in rows if not any(r is f for f in failed)]

    async def _evict_dead_thread(self, thread_id: str) -> None:
        """Remove a thread_id from every agent's in-memory state.

        Fires when Slack reports the parent message no longer exists (via
        ThreadNotFound or a silent thread_ts drop
        on chat.postMessage). Without eviction the same dead thread gets
        re-polled and replied-to forever, producing noisy error logs and —
        worse — cascading top-level posts.

        Called from `_post_message`'s ThreadNotFound handling, itself called
        from `_reply_to_thread` — i.e. from inside the reply lane's THREAD
        lock for this exact `thread_id` (see `_dispatch_reply_lane`). This
        loops over EVERY agent's `active_threads`, so it needs the AGENT lock
        for every agent, not just the two `_close_thread` above locks: without it, `_close_thread`
        would be the only mutator of `active_threads` under agent-lock
        protection while this one, mutating the SAME dict, raced unguarded.
        `acquire_all` takes every key sorted, so this composes with
        `_close_thread`'s narrower 2-key acquisition (and any other
        `_evict_dead_thread` racing it) without deadlocking on each other —
        one global sorted order across every multi-key acquisition, agent or
        thread. Nested inside the already-held thread lock: thread-lock-
        outer, agent-lock-inner, per the ordering note on `_thread_locks` in
        __init__.
        """
        async with self._agent_locks.acquire_all(*self.agents.keys()):
            evicted_from = 0
            for ag in self.agents.values():
                if thread_id in ag.state.active_threads:
                    ag.state.active_threads.pop(thread_id, None)
                    evicted_from += 1
            # Eviction removes per-agent state but must NEVER un-close a thread.
            # If another caller is racing a _close_thread add() against this eviction,
            # the discard would remove the closed marker, and Phase 3 would re-activate
            # the finished interview. _closed_thread_ids is insert-only.
            if evicted_from:
                logger.info(
                    "Evicted dead thread %s from %d agent(s)' state",
                    thread_id, evicted_from,
                )

    def _get_prior_threads_for_agent(
        self,
        agent_id: str,
        current_visibility: str = VISIBILITY_PUBLIC,
    ) -> dict[str, list[dict]]:
        """Return {other_agent_id: [thread summaries]} visible at the given visibility level.

        Implements G3 (visibility-filtered dedup context): a thread_decision
        with ``origin_visibility='collab_private'`` never surfaces in a
        ``public``-channel Phase 5 prompt. See
        specs/privacy-and-channel-visibility.md §G3.
        """
        result: dict[str, list[dict]] = {}
        for (a, b), threads in self._prior_threads.items():
            if agent_id not in (a, b):
                continue
            other = b if a == agent_id else a
            visible = [
                t for t in threads
                if _visibility_permits(
                    t.get("origin_visibility", VISIBILITY_PUBLIC),
                    current_visibility,
                )
            ]
            if visible:
                result[other] = visible
        return result

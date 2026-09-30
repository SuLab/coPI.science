"""Buffered write-through of the message log to agent_messages (spec §7.1): the flush, its per-row recovery, and the P0-01 flush lock."""

from __future__ import annotations

import asyncio
import logging

from src.agent.engine import deps
from src.agent.engine.constants import (
    _ROW_LEVEL_DB_ERRORS,
    PER_ROW_RECOVERY_DEADLINE_S,
    PERSIST_UPSERT_CHUNK_ROWS,
    RUN_STATS_UPDATE_INTERVAL,
)
from src.agent.engine.context import EngineContext, via
from src.agent.message_log import LogEntry
from src.models import AgentMessage, SimulationRun

logger = logging.getLogger("src.agent.simulation")


class Persistence:
    """The message-log write-through buffer and its flush."""

    agents = via("ctx")
    session_factory = via("ctx")
    simulation_run_id = via("ctx")

    OWNED_STATE: tuple[str, ...] = (
        "_pending_persist",
        "_last_run_stats_update",
        "_persist_flush_lock",
        "_persist_in_flight",
    )

    def __init__(self, ctx: EngineContext) -> None:
        self.ctx = ctx
        # DB persistence buffer for the message log. MessageLog.append fires a
        # sync callback that enqueues here; _flush_persisted() batch-writes to
        # agent_messages once per main-loop tick. This makes the DB the primary
        # conversation store. See specs/local-db-conversations.md.
        self._pending_persist: list[LogEntry] = []
        # Serializes `_flush_persisted`. A caller that arrives while a flush is
        # in flight waits for it, then flushes whatever is left. The write-through
        # in `_post_message` flushes from inside reply turns while the main loop
        # and `stop()` flush too, and without this two flushes could run per-row
        # recovery over overlapping re-queues at once.
        self._persist_flush_lock = asyncio.Lock()
        # The batch the flush in progress took out of `_pending_persist`, empty
        # between flushes; `flush_before_dependent` reads it (see there).
        self._persist_in_flight: list[LogEntry] = []
        # Wall-clock of the last run-stats refresh (total_messages /
        # total_api_calls), throttled to RUN_STATS_UPDATE_INTERVAL. See
        # _flush_persisted (B1). `total_messages` is a display counter;
        # `total_api_calls` stopped being one on 2026-08-22, when its UNITS
        # changed from turns to real API calls — see the module comment on
        # RUN_STATS_UPDATE_INTERVAL.
        self._last_run_stats_update: float = 0.0

    async def _recover_rows_individually(
        self, rows: list, apply_one, *, what: str,
    ) -> tuple[int, list, list]:
        """Re-attempt a failed batch ONE ROW AT A TIME, isolating the poison row.

        Returns ``(written, lost, unattempted)``:

        - ``written``   — rows now durably in the database;
        - ``lost``      — ``(row, exception)`` pairs that failed on their own,
          with nothing else to blame; retrying these forever would fail the whole
          batch forever, so the caller drops them (loudly) rather than
          re-queueing. The exception rides along so a caller can record WHY —
          `_flush_pending_assessments` writes it into an ``AssessmentDrop``;
        - ``unattempted`` — rows the deadline (or a failure of the recovery pass
          itself) stopped us writing; the caller re-queues these.

        Two things here are not incidental:

        - **A NEW session.** In all three flushers the ``except`` sits OUTSIDE
          ``async with self.session_factory() as db:``, so by the time we get
          here that session is closed and rolled back. Reusing it would raise
          ``PendingRollbackError`` on the first row and lose the remainder — the
          exact loss this is supposed to prevent.
        - **A savepoint per row.** ``begin_nested`` means one row's failure rolls
          back to the savepoint instead of poisoning the whole transaction, so
          the rows after it still commit.

        Callers must gate entry on ``_ROW_LEVEL_DB_ERRORS`` — see that constant.
        """
        lost: list = []
        ok: list = []
        unattempted: list = []
        deadline = deps.time.monotonic() + PER_ROW_RECOVERY_DEADLINE_S
        try:
            async with self.session_factory() as db:
                for i, row in enumerate(rows):
                    if deps.time.monotonic() >= deadline:
                        unattempted = list(rows[i:])
                        logger.error(
                            "Per-row recovery of %s hit its %.0fs deadline; "
                            "re-queueing the %d row(s) not attempted",
                            what, PER_ROW_RECOVERY_DEADLINE_S, len(unattempted),
                        )
                        break
                    try:
                        async with db.begin_nested():
                            await apply_one(db, row)
                        ok.append(row)
                    except Exception as row_exc:  # noqa: BLE001
                        lost.append((row, row_exc))
                        logger.error(
                            "DROPPING one un-writable %s row: %s", what, row_exc,
                        )
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            # The recovery pass itself died, so nothing it "accepted" is durable.
            logger.error(
                "Per-row recovery of %s failed outright (%s); re-queueing "
                "%d row(s)", what, exc, len(ok) + len(unattempted),
            )
            return 0, lost, ok + unattempted
        if ok:
            logger.warning(
                "Per-row recovery of %s salvaged %d of %d row(s)",
                what, len(ok), len(rows),
            )
        return len(ok), lost, unattempted

    def _report_flush_failure(
        self, *, what: str, requeue: list, exc: object, final: bool, log,
        exc_info: bool = False,
    ) -> bool:
        """Say what actually happens to ``requeue`` — and say LOST when it is lost.

        ``stop()`` makes exactly ONE final attempt at each buffer, so "re-queued
        for retry" is a false statement on that path: nothing will ever drain the
        buffer again. Returns True when the caller should re-queue.

        ``log`` is the caller's chosen level for the recoverable case (an
        ``opportunity_assessments`` row is the product of the pipeline and stays
        at ERROR where the other two are WARNING); the LOST case is ERROR for
        everyone, because nothing is recoverable about it.
        """
        if not requeue:
            return False
        if final:
            logger.error(
                "SHUTDOWN FLUSH FAILED: %d %s row(s) LOST — this was the final "
                "attempt and nothing will retry them: %s",
                len(requeue), what, exc, exc_info=exc_info,
            )
            return False
        log(
            "Failed to flush %d %s row(s), re-queued for retry: %s",
            len(requeue), what, exc, exc_info=exc_info,
        )
        return True

    async def flush_before_dependent(self) -> bool:
        """S2-06: flush buffered message rows (under the P0-01 flush lock) before a
        write that depends on them — a ThreadDecision or a verdict. True when
        every row that was buffered when this call started is now in the
        database, so every reply row the dependent write refers to is there;
        False means the flush failed or re-queued one of them, and the caller
        queues its write for the next successful flush to drain. Always True
        without a database.

        Only the rows buffered at entry count. A concurrent reply pair (the
        reply lane gathers several) can append rows during this flush's DB
        await; those are not this caller's, and counting them would queue a
        verdict whose reply row landed, which suppresses its capture-time
        headline. The snapshot includes the batch a flush already in flight has
        taken out of the buffer: that flush either lands those rows or re-queues
        the same entries, so they are checked too."""
        if not self.session_factory or not self.simulation_run_id:
            return True
        owed = [*self._persist_in_flight, *self._pending_persist]
        await self._flush_persisted()
        owed_ids = {id(e) for e in owed}
        return not any(id(e) in owed_ids for e in self._pending_persist)

    async def _flush_persisted(
        self, force_stats: bool = False, *, final: bool = False,
    ) -> None:
        """Batch-upsert buffered message-log entries into agent_messages.

        Uses ON CONFLICT (simulation_run_id, message_ts) so it is safe to run
        alongside legacy rows, transitional double-writes, and repeated restarts.
        Drops the buffer when there is no DB so it can't grow unbounded.

        Serialized by `_persist_flush_lock`: a second caller waits for the flush
        in flight, then flushes what is left. The rows go out in chunks of
        `PERSIST_UPSERT_CHUNK_ROWS`, all in one session and transaction, so the
        commit is still all-or-nothing and per-row recovery is unchanged.
        """
        async with self._persist_flush_lock:
            if not self._pending_persist:
                if force_stats:
                    await self._refresh_run_stats_alone()
                return
            if not self.session_factory or not self.simulation_run_id:
                self._pending_persist.clear()
                return
            entries = self._pending_persist
            self._pending_persist = []
            self._persist_in_flight = entries
            try:
                # Dedup by canonical id within the batch — a single ON CONFLICT statement
                # cannot touch the same row twice.
                by_ts: dict[str, dict] = {}
                # The LogEntry each row came from, so a per-row recovery can re-queue the
                # ENTRIES (which is what `_pending_persist` holds) for the rows it did
                # not manage to write.
                by_entry: dict[str, LogEntry] = {}
                for e in entries:
                    if not e.ts:
                        continue
                    channel_id = self.ctx.channel_id_resolver(e.channel) or f"local:{e.channel}"
                    by_entry[e.ts] = e
                    by_ts[e.ts] = {
                        "simulation_run_id": self.simulation_run_id,
                        "agent_id": e.sender_agent_id,
                        "channel_id": channel_id,
                        "channel_name": e.channel,
                        "message_ts": e.ts,
                        "message_length": len(e.content or ""),
                        "thread_ts": e.thread_ts,
                        # The entry's own phase wins when it has one; otherwise the
                        # shape decides, exactly as it always has. Only a panel note
                        # sets it (PHASE_PANEL_NOTE — see _post_panel_note), and this is
                        # the write that makes the staff pages agree with the engine
                        # for free: src/services/directory.py's discussions listing
                        # already keys its roots on phase == 'new_post' and its reply
                        # counts on phase == 'thread_reply', so a third value is
                        # excluded from both with no query change.
                        "phase": e.phase or ("thread_reply" if e.thread_ts else "new_post"),
                        "visibility": e.visibility,
                        "content": e.content or "",
                        "sender_name": e.sender_name or "",
                        "is_bot": e.is_bot,
                        "posted_at": e.posted_at,
                        "slack_ts": e.slack_ts,
                        "slack_channel_id": e.slack_channel_id,
                        # The root's *Slack* ts, not the canonical thread_ts — they differ
                        # whenever the thread started Slack-off. Only meaningful when this
                        # entry is itself on Slack. See _slack_parent_ts.
                        "slack_thread_ts": e.slack_thread_ts if e.slack_ts else None,
                    }
                rows = list(by_ts.values())
                if not rows:
                    if force_stats:
                        await self._refresh_run_stats_alone()
                    return
                from sqlalchemy import or_
                from sqlalchemy.dialects.postgresql import insert as pg_insert

                def _upsert(batch: list[dict]):
                    stmt = pg_insert(AgentMessage.__table__).values(batch)
                    return stmt.on_conflict_do_update(
                        constraint="uq_agent_messages_run_ts",
                        set_={
                            "content": stmt.excluded.content,
                            "sender_name": stmt.excluded.sender_name,
                            "is_bot": stmt.excluded.is_bot,
                            "posted_at": stmt.excluded.posted_at,
                            "message_length": stmt.excluded.message_length,
                            "visibility": stmt.excluded.visibility,
                            "thread_ts": stmt.excluded.thread_ts,
                            "channel_id": stmt.excluded.channel_id,
                            "channel_name": stmt.excluded.channel_name,
                            "agent_id": stmt.excluded.agent_id,
                            "slack_ts": stmt.excluded.slack_ts,
                            "slack_channel_id": stmt.excluded.slack_channel_id,
                            "slack_thread_ts": stmt.excluded.slack_thread_ts,
                        },
                        # M1a guard: never let a bot message clobber an existing human
                        # (PI) row on a cross-process canonical-id collision. Allow the
                        # update only when the existing row is itself a bot row, or the
                        # incoming row is human (re-flush of an ingested PI message /
                        # slack mirror). A blocked conflict is left untouched, like
                        # DO NOTHING for that row. See PR #19 review M1.
                        where=or_(
                            AgentMessage.__table__.c.is_bot.is_(True),
                            stmt.excluded.is_bot.is_(False),
                        ),
                    )

                try:
                    async with self.session_factory() as db:
                        for start in range(0, len(rows), PERSIST_UPSERT_CHUNK_ROWS):
                            await db.execute(_upsert(rows[start:start + PERSIST_UPSERT_CHUNK_ROWS]))
                        # Refresh the run's counters at most every
                        # RUN_STATS_UPDATE_INTERVAL (a full COUNT every flush is wasteful
                        # at scale — B1). The bulk upsert can't cheaply tell inserts from
                        # updates, so total_messages is a recomputed count; slight
                        # staleness between refreshes is fine for a display counter.
                        # `total_api_calls` is NOT merely cosmetic any more — its
                        # units changed on 2026-08-22; see `_refresh_run_stats`.
                        await self._refresh_run_stats(db, force=force_stats)
                        await db.commit()
                except Exception as exc:
                    # Re-queue the failed batch instead of dropping it. The DB is now the
                    # source of truth for conversations, so a silently-dropped flush is
                    # unrecoverable — a restart rebuilds from the DB and these messages
                    # would be gone for good. New entries may have been enqueued while we
                    # were awaiting the (failed) commit; put the failed batch back in
                    # front to preserve chronological order for the next flush attempt.
                    #
                    # ONE bad row used to take every good row beside it, forever: the
                    # re-queued batch fails identically on the next attempt. If (and only
                    # if) the error names a ROW rather than the pool or the connection,
                    # retry them individually so the poison row is the only casualty.
                    requeue = rows
                    if isinstance(exc, _ROW_LEVEL_DB_ERRORS):
                        async def _one(db, row):
                            await db.execute(_upsert([row]))

                        _written, _lost, requeue = await self._recover_rows_individually(
                            rows, _one, what="message",
                        )
                    if self._report_flush_failure(
                        what="message", requeue=requeue, exc=exc, final=final,
                        log=logger.warning,
                    ):
                        self._pending_persist[0:0] = [
                            by_entry[r["message_ts"]] for r in requeue
                        ]
            finally:
                self._persist_in_flight = []

    async def _refresh_run_stats(self, db, *, force: bool) -> None:
        """Refresh the run row's ``total_messages`` / ``total_api_calls`` inside
        ``db``'s transaction, at most every RUN_STATS_UPDATE_INTERVAL unless
        ``force``. The caller commits."""
        from sqlalchemy import func as sa_func
        from sqlalchemy import select as sa_select

        now = deps.time.time()
        if not force and now - self._last_run_stats_update < RUN_STATS_UPDATE_INTERVAL:
            return
        self._last_run_stats_update = now
        run = (await db.execute(
            sa_select(SimulationRun).where(SimulationRun.id == self.simulation_run_id)
        )).scalar_one_or_none()
        if run:
            total = (await db.execute(
                sa_select(sa_func.count(AgentMessage.id)).where(
                    AgentMessage.simulation_run_id == self.simulation_run_id
                )
            )).scalar_one()
            run.total_messages = total
            # UNITS: real API CALLS, not turns, since 2026-08-22 —
            # `api_call_count` books tool rounds and retries too, so
            # this column is NOT comparable with any earlier run.
            # The old per-turn figure is `COUNT(*)` over
            # `llm_call_logs` for the same run. See `_unbooked_calls`.
            run.total_api_calls = sum(a.api_call_count for a in self.agents.values())

    async def _refresh_run_stats_alone(self) -> None:
        """A forced stats refresh with no rows to write: ``stop()``'s final flush
        usually finds the buffer empty (step 1 drained it), and the totals must
        still count the rows and the calls booked since the last refresh (the
        shutdown memory drain's among them). Its own session; a failure is
        logged, never raised."""
        if not self.session_factory or not self.simulation_run_id:
            return
        try:
            async with self.session_factory() as db:
                await self._refresh_run_stats(db, force=True)
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — a display/accounting refresh, never the stop
            logger.warning("Failed to refresh the run's totals: %s", exc)

    def _enqueue_persist(self, entry: LogEntry) -> None:
        """MessageLog persist callback — buffer a new entry for the next flush."""
        self._pending_persist.append(entry)

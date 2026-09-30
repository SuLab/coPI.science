"""The llm_call_logs buffer: the call-log callback, its background flush tasks and per-row recovery (spec §7.1)."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from src.agent.engine.constants import _ROW_LEVEL_DB_ERRORS
from src.agent.engine.context import EngineContext, via
from src.models import LlmCallLog

if TYPE_CHECKING:
    from src.agent.engine.persistence import Persistence

logger = logging.getLogger("src.agent.simulation")


class LlmLog:
    """The buffer of LLM call-log rows and the tasks that flush it."""

    agents = via("ctx")
    session_factory = via("ctx")
    simulation_run_id = via("ctx")
    _recover_rows_individually = via("_persistence")
    _report_flush_failure = via("_persistence")

    OWNED_STATE: tuple[str, ...] = (
        "_llm_log_buffer",
        "_llm_log_flush_size",
        "_flush_tasks",
    )

    def __init__(self, ctx: EngineContext, *, persistence: Persistence) -> None:
        self.ctx = ctx
        self._persistence = persistence
        # LLM call log buffer
        self._llm_log_buffer: list[dict] = []
        self._llm_log_flush_size = 10
        # Every fire-and-forget flush task `_on_llm_call` has spawned and that
        # has not finished yet. `stop()` gathers these before its own final
        # flush; without that, `asyncio.run` cancelled them at interpreter
        # shutdown, and because `_flush_llm_logs` takes its batch OUT of the
        # buffer before awaiting the commit, a cancelled task loses those rows
        # from the buffer AND the database. Entries are discarded in
        # `_on_flush_done`, so the set does not grow for the run's life.
        self._flush_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _unbooked_calls(call_stats: object) -> int:
        """How many REAL API calls in this turn nothing has booked yet.

        Every caller already books its own terminating call (the two reserved
        sites via ``try_reserve`` + ``record_api_call(already_reserved=True)``,
        consults via ``on_api_call``, the memory update directly) and every
        truncation retry already books itself via ``on_retry``. What no site
        books is the extra TOOL ROUNDS inside ``generate_with_tools``: a turn
        that used three rounds before its final text call made four real billed
        calls and was metered as one.

        So this counts ``kind == "round"`` entries and nothing else. Counting
        ``len(call_stats)`` instead — the obvious fix — double-books every retry
        AND the reservation at the two reserved sites.

        Defensive throughout: this runs inside a logging callback, where raising
        would take a turn down over bookkeeping. A missing or malformed
        ``call_stats`` books nothing extra, which is also exactly right for the
        4,650 of 5,771 stored rows that predate the column.
        """
        if not isinstance(call_stats, list):
            return 0
        return sum(
            1 for c in call_stats
            if isinstance(c, dict) and c.get("kind") == "round"
        )

    def _on_llm_call(self, data: dict) -> None:
        """Callback fired after each LLM API call."""
        # Book the calls this turn made that nothing else booked, BEFORE the
        # buffer append: the flush below can hand control to another coroutine,
        # and the throttle should see the spend as soon as it is known.
        extra = self._unbooked_calls(data.get("call_stats"))
        if extra:
            agent = self.agents.get(data.get("agent_id"))
            if agent is not None:
                for _ in range(extra):
                    agent.record_api_call()
        self._llm_log_buffer.append(data)
        if len(self._llm_log_buffer) >= self._llm_log_flush_size:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._flush_llm_logs())
                # Held in `_flush_tasks` for two reasons: `stop()` has to be able
                # to await it (see there), and a bare `create_task` reference is
                # otherwise only weakly held by the loop, so the task can be
                # garbage-collected mid-flight.
                self._flush_tasks.add(task)
                task.add_done_callback(self._on_flush_done)
            except RuntimeError:
                pass

    def _on_flush_done(self, task: asyncio.Task) -> None:
        """Done-callback for a spawned `_flush_llm_logs`.

        The cancelled-task guard is not defensive tidiness: ``task.exception()``
        RE-RAISES ``CancelledError`` for a cancelled task, so the pre-fix
        callback answered a batch lost to shutdown cancellation with a traceback
        out of the done-callback that said nothing about the rows.
        """
        self._flush_tasks.discard(task)
        if task.cancelled():
            return
        if task.exception():
            logger.error("LLM log flush failed: %s", task.exception())

    def _llm_log_record(self, entry: dict) -> LlmCallLog:
        """One ``llm_call_logs`` row from one buffered callback payload.

        Extracted so the batch write and the per-row recovery build the row the
        same way — a recovery that mapped the fields differently would silently
        store a different row than the one that failed.
        """
        return LlmCallLog(
            simulation_run_id=self.simulation_run_id,
            agent_id=entry.get("agent_id", "unknown"),
            phase=entry.get("phase", "unknown"),
            channel=entry.get("channel"),
            thread_ts=entry.get("thread_ts"),
            thread_phase=entry.get("thread_phase"),
            message_ordinal=entry.get("message_ordinal"),
            model=entry.get("model", ""),
            system_prompt=entry.get("system_prompt", ""),
            messages_json=entry.get("messages", []),
            response_text=entry.get("response_text", ""),
            input_tokens=entry.get("input_tokens", 0),
            output_tokens=entry.get("output_tokens", 0),
            latency_ms=entry.get("latency_ms", 0.0),
            # The turn's real wall time, which `latency_ms` above is not — it
            # carries only the LAST API call's latency, so summing that column
            # understated true LLM wait by 25% on run 8b64a0e0. Default None
            # rather than 0.0: a producer that supplied nothing means "not
            # recorded", and 0.0 would read as an instantaneous turn.
            wall_ms=entry.get("wall_ms"),
            # The cached input this turn read and wrote, which `input_tokens`
            # above EXCLUDES — `usage.input_tokens` counts only the uncached
            # tail, so on a cached turn it can read 2 for a 30 KB prompt (see
            # LlmCallLog's own comment). Billable input volume is the sum of the
            # three columns, and that sum is unavailable while these two are
            # NULL on every row. `.get`, not `[...]`: every other field here is
            # read defensively and a producer that predates the keys (or a
            # hand-built entry in a test) must still write a complete row.
            #
            # Default None, not 0, matching the nullable columns and
            # `llm._sum_reported`: "no API call reported the field" and "the
            # cache was read zero times" are different answers, and only the
            # second licenses a conclusion.
            cache_read_input_tokens=entry.get("cache_read_input_tokens"),
            cache_creation_input_tokens=entry.get("cache_creation_input_tokens"),
            # Per-API-call breakdown (stop_reason, the requested max_tokens
            # ceiling, thinking/text split) that the three cumulative columns
            # above cannot carry. Default None, not [] — a producer that
            # supplied nothing means "not recorded", and an empty array would
            # read as "recorded, zero calls", which never happens.
            call_stats=entry.get("call_stats"),
            created_at=entry.get("completed_at"),
        )

    async def _flush_llm_logs(self, *, final: bool = False) -> None:
        """Write buffered LLM call logs to the database."""
        if not self._llm_log_buffer or not self.session_factory or not self.simulation_run_id:
            return
        batch = self._llm_log_buffer[:]
        self._llm_log_buffer.clear()
        try:
            async with self.session_factory() as db:
                for entry in batch:
                    db.add(self._llm_log_record(entry))
                await db.commit()
            logger.debug("Flushed %d LLM call logs to DB", len(batch))
        except Exception as exc:
            # Re-queue the failed batch instead of dropping it, exactly like
            # _flush_persisted does for its own buffer: new entries may have
            # been appended to _llm_log_buffer while we were awaiting the
            # (failed) commit, so put the failed batch back in front to
            # preserve chronological order for the next flush attempt. On a
            # ROW-level error only, isolate the poison row first.
            requeue = batch
            if isinstance(exc, _ROW_LEVEL_DB_ERRORS):
                async def _one(db, entry):
                    db.add(self._llm_log_record(entry))

                _written, _lost, requeue = await self._recover_rows_individually(
                    batch, _one, what="LLM call log",
                )
            if self._report_flush_failure(
                what="LLM call log", requeue=requeue, exc=exc, final=final,
                log=logger.warning,
            ):
                self._llm_log_buffer[0:0] = requeue

"""A background LLM-log flush must not be cancelled mid-commit at shutdown.

`_on_llm_call` spawns `_flush_llm_logs` with `loop.create_task` once the buffer
crosses `_llm_log_flush_size`. That function takes the batch OUT of the buffer
before awaiting the commit, and `stop()` awaited nothing — so `asyncio.run`'s
end-of-loop cancellation killed it mid-commit and the batch was gone from both
the buffer and the database. Executed against the pre-fix code:
``rows the fake DB saw = 10 | 'COMMITTED' present: False | task: cancelled``.

`_on_flush_done` then called `task.exception()` on a CANCELLED task, which
re-raises `CancelledError` inside the done-callback — so the only thing the
operator saw was a traceback that says nothing about the lost rows.

**Every test here has to force the spawned TASK to take the batch before
`stop()` runs** (`await asyncio.sleep(0)` right after the spawn, with an
assertion that it did). Without that the tests are vacuous: awaiting a
coroutine that never itself suspends does not yield to the loop, so `stop()`'s
own `await self._flush_llm_logs()` reached the still-full buffer first and did
the work the spawned task was supposed to be doing. All three of these passed
against the broken code in that shape.
"""
import asyncio

import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine


def _engine() -> SimulationEngine:
    return SimulationEngine(
        agents=[Agent("hub", "HubBot", "PI hub")], slack_clients={},
    )


def _slow_flusher(eng, events):
    """A `_flush_llm_logs` stand-in with the real one's take-then-commit shape."""
    async def _slow_flush():
        batch = eng._llm_log_buffer[:]
        eng._llm_log_buffer.clear()
        if not batch:
            events.append("nothing-to-flush")
            return
        events.append("start")
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise
        events.append("committed")

    return _slow_flush


def test_a_spawned_flush_is_awaited_before_shutdown_completes():
    """Driven through `asyncio.run` on purpose — that is what does the killing.

    `main.py` calls `asyncio.run(_run_simulation(...))`, whose finally-block
    awaits `stop()`. Anything still pending when that coroutine returns is
    cancelled by `asyncio.run` itself, so a test that only awaits `stop()`
    inside an already-running loop cannot see the bug.
    """
    events: list[str] = []

    async def _main():
        eng = _engine()
        eng._llm_log_flush_size = 1
        eng._flush_llm_logs = _slow_flusher(eng, events)
        # The real producer: llm.py's call-log callback, at the buffer threshold.
        eng._on_llm_call({"agent_id": "hub", "phase": "thread_reply"})
        await asyncio.sleep(0)  # let the SPAWNED task take the batch
        assert events == ["start"], (
            f"setup: the spawned task never took the batch: {events}"
        )
        await eng.stop()

    asyncio.run(_main())

    # "committed" BEFORE "nothing-to-flush": the gather sits ahead of stop()'s
    # own final flush, which by then has an empty buffer to look at.
    assert events == ["start", "committed", "nothing-to-flush"], (
        "the spawned flush was cancelled mid-commit at shutdown — its batch was "
        f"already out of the buffer, so those rows are gone: {events}"
    )


def test_a_flush_spawned_by_the_shutdown_memory_drain_is_also_awaited():
    """Why the gather cannot sit at the TOP of `stop()`.

    `stop()`'s first act is `_drain_memory_events`, which makes real LLM calls
    and can therefore push the buffer past the threshold and spawn a NEW flush
    task. Gathering before that runs would re-introduce exactly the orphan this
    fixes.
    """
    events: list[str] = []

    async def _main():
        eng = _engine()
        eng._llm_log_flush_size = 1
        eng._flush_llm_logs = _slow_flusher(eng, events)

        async def _drain(limit=None):
            eng._on_llm_call({"agent_id": "hub", "phase": "memory"})
            await asyncio.sleep(0)  # let the SPAWNED task take the batch
            assert events == ["start"], (
                f"setup: the spawned task never took the batch: {events}"
            )
            return 1

        eng._drain_memory_events = _drain
        await eng.stop()

    asyncio.run(_main())

    assert events == ["start", "committed", "nothing-to-flush"], (
        "a flush spawned by stop()'s own memory drain was left orphaned: "
        f"{events}"
    )


async def test_a_cancelled_flush_task_does_not_raise_in_its_done_callback():
    eng = _engine()

    async def _never():
        await asyncio.sleep(10)

    task = asyncio.create_task(_never())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # `task.exception()` re-raises CancelledError for a cancelled task, so the
    # pre-fix callback turned a lost batch into an unrelated traceback.
    eng._on_flush_done(task)


async def test_a_completed_flush_task_is_discarded_from_the_tracking_set():
    """The set must not grow for the life of the run."""
    eng = _engine()
    eng._llm_log_flush_size = 1

    async def _quick_flush():
        eng._llm_log_buffer.clear()

    eng._flush_llm_logs = _quick_flush
    eng._on_llm_call({"agent_id": "hub", "phase": "thread_reply"})
    assert len(eng._flush_tasks) == 1
    await asyncio.gather(*eng._flush_tasks)
    # add_done_callback runs on the next loop pass.
    await asyncio.sleep(0)
    assert eng._flush_tasks == set(), (
        "completed flush tasks accumulate for the life of the run"
    )


async def test_a_failing_flush_task_does_not_abort_the_rest_of_stop():
    """`return_exceptions=True`, and the ordering that lets the final flush recover.

    A gathered task that raises would otherwise propagate out of `stop()` before
    `_flush_pending_assessments` ever ran. And because the gather sits BEFORE the
    final `_flush_llm_logs()`, a batch the failed task re-queued still gets one
    more attempt.
    """
    eng = _engine()
    eng._llm_log_flush_size = 1
    seen: list[str] = []

    async def _boom():
        batch = eng._llm_log_buffer[:]
        eng._llm_log_buffer.clear()
        if not batch:
            seen.append("final-saw-nothing")
            return
        seen.append("attempt")
        await asyncio.sleep(0)
        if len(seen) == 1:
            # Same re-queue-in-front contract as the real flusher. The real one
            # swallows its own exception; this stub lets the FIRST attempt (the
            # spawned task) raise, which is what `gather` has to tolerate.
            eng._llm_log_buffer[0:0] = batch
            raise RuntimeError("commit failed")
        seen.append("recovered")

    async def _assessments():
        seen.append("assessments")

    eng._flush_llm_logs = _boom
    eng._flush_pending_assessments = _assessments
    eng._on_llm_call({"agent_id": "hub", "phase": "thread_reply"})
    await asyncio.sleep(0)  # let the SPAWNED task take the batch
    assert seen == ["attempt"], f"setup: the task never ran: {seen}"

    await eng.stop()

    assert seen == ["attempt", "attempt", "recovered", "assessments"], (
        "expected: the spawned task failed and re-queued, the final flush "
        "retried it, and stop() still reached the assessment flush. Got: "
        f"{seen}"
    )

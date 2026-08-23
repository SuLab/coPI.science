"""Every exit from a main-loop iteration must drain and flush.

`_run_main_loop` put the working-memory drain and the three durable flushes
(`_flush_persisted`, `_flush_llm_logs`, `_flush_pending_assessments`) at the
BOTTOM of the loop body, and then jumped over all four with two `continue`
statements in the no-eligible-agent branch. Both of those branches are the
common case for a throttled or reply-only roster, and the documented exit path
for this process is a `docker stop` that can end in SIGKILL — so the buffers
they stranded are lost outright. Harness result over 5 productive ticks:
``{'flush_persisted': 0, 'flush_llm': 0, 'flush_assess': 0, 'drain': 0}`` with
5 rows stranded in each buffer.

`_flush_llm_logs` is partially protected (`_on_llm_call` spawns it at a buffer
threshold); `_flush_persisted` and `_flush_pending_assessments` genuinely have
no other caller inside the loop.
"""
import types

import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine


def _settings(**kw):
    base = dict(
        cohort_isolation_enabled=False,
        cohort_default_policy="open",
        turn_delay_seconds=0.0,
        active_thread_threshold=12,
        llm_rate_window_seconds=600,
        llm_calls_per_load_per_window=8,
        reply_lane_max_in_flight=1,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _engine(monkeypatch):
    monkeypatch.setattr("src.agent.simulation.get_settings", lambda: _settings())
    agent = Agent("hub", "HubBot", "PI hub")
    eng = SimulationEngine(agents=[agent], slack_clients={})
    eng._running = True
    return eng, agent


def _instrument(eng, monkeypatch):
    """Stub every per-tick I/O call; count the four we care about.

    The counters' stubs deliberately do NOT clear the buffers they stand for,
    so each tick re-enters every guarded branch and the count equals the number
    of ticks rather than 1.
    """
    async def _noop(*a, **kw):
        return None

    for name in ("_poll_slack_for_bot_messages", "_poll_inbound_from_db",
                 "_sync_private_channels_from_db", "_sync_roster_from_db"):
        monkeypatch.setattr(eng, name, _noop)
    monkeypatch.setattr(eng, "_sync_profiles_from_disk", lambda *a, **kw: None)

    counts = {"drain": 0, "persist": 0, "llm": 0, "assess": 0}

    async def _drain(*a, **kw):
        counts["drain"] += 1
        return 0

    async def _persist(*a, **kw):
        counts["persist"] += 1

    async def _llm(*a, **kw):
        counts["llm"] += 1

    async def _assess(*a, **kw):
        counts["assess"] += 1

    monkeypatch.setattr(eng, "_drain_memory_events", _drain)
    monkeypatch.setattr(eng, "_flush_persisted", _persist)
    monkeypatch.setattr(eng, "_flush_llm_logs", _llm)
    monkeypatch.setattr(eng, "_flush_pending_assessments", _assess)

    # Non-empty buffers, so the guarded branches are live on every tick.
    eng._pending_memory_events.append(("hub", "t1", "closed", None))
    eng._llm_log_buffer.append({"agent_id": "hub"})
    eng._pending_assessments.append({"agent_id": "hub"})
    return counts


@pytest.mark.asyncio
async def test_every_loop_exit_flushes_its_buffers(monkeypatch):
    """The idle `continue`: no eligible agent, no reply-lane spend."""
    eng, _agent = _engine(monkeypatch)
    counts = _instrument(eng, monkeypatch)
    monkeypatch.setattr(eng, "_select_agent", lambda: None)

    async def _no_reply_lane():
        return 0

    monkeypatch.setattr(eng, "_dispatch_reply_lane", _no_reply_lane)

    ticks = 5
    sleeps = []

    async def _sleep(delay):
        sleeps.append(delay)
        if len(sleeps) >= ticks:
            eng._running = False

    monkeypatch.setattr(eng, "_sleep", _sleep)

    await eng._run_main_loop()

    assert len(sleeps) == ticks
    # `drain` is ticks-1, not ticks, and that is the FIX-1 guard working: the
    # LAST tick is the one whose `_sleep` stub stops the engine, and a stopping
    # tick must not enter the unbounded drain (see
    # `test_a_shutdown_request_stops_the_hoisted_drain`). The FLUSHES still run
    # on all five, which is the property this test is about.
    assert counts == {"drain": ticks - 1, "persist": ticks, "llm": ticks,
                      "assess": ticks}, (
        "the idle-backoff `continue` jumped over the memory drain and all "
        f"three flushes: {counts}"
    )


@pytest.mark.asyncio
async def test_the_reply_lane_continue_also_flushes(monkeypatch):
    """The OTHER `continue`: the reply lane spent, no post-lane agent was eligible.

    This branch does not even sleep, so a hub that only ever replies rode it
    every tick for the life of the run.
    """
    eng, agent = _engine(monkeypatch)
    counts = _instrument(eng, monkeypatch)
    monkeypatch.setattr(eng, "_select_agent", lambda: None)

    ticks = 5
    dispatched = []

    async def _reply_lane():
        # Real SPEND, which is what `reply_lane_did_work` measures.
        agent.api_call_count += 1
        dispatched.append(1)
        if len(dispatched) >= ticks:
            eng._running = False
        return 1

    monkeypatch.setattr(eng, "_dispatch_reply_lane", _reply_lane)

    async def _sleep(delay):
        raise AssertionError("the reply-lane branch must not sleep")

    monkeypatch.setattr(eng, "_sleep", _sleep)

    await eng._run_main_loop()

    assert len(dispatched) == ticks
    # ticks-1 drains for the same reason as the test above: the last tick is the
    # one that stops the engine, and a stopping tick skips the unbounded drain.
    assert counts == {"drain": ticks - 1, "persist": ticks, "llm": ticks,
                      "assess": ticks}, (
        "the reply-lane `continue` jumped over the memory drain and all three "
        f"flushes: {counts}"
    )


@pytest.mark.asyncio
async def test_a_terminal_stall_still_flushes_on_the_way_out(monkeypatch):
    """`break` is a loop exit too — an empty roster must not strand the buffers."""
    eng, _agent = _engine(monkeypatch)
    counts = _instrument(eng, monkeypatch)
    monkeypatch.setattr(eng, "_select_agent", lambda: None)
    # An empty roster is _terminal_stall_reason's first permanent case.
    eng.agents.clear()

    async def _no_reply_lane():
        return 0

    monkeypatch.setattr(eng, "_dispatch_reply_lane", _no_reply_lane)

    async def _sleep(delay):
        raise AssertionError("a terminal stall must break, not back off")

    monkeypatch.setattr(eng, "_sleep", _sleep)

    await eng._run_main_loop()

    assert counts == {"drain": 1, "persist": 1, "llm": 1, "assess": 1}, (
        f"the terminal-stall `break` stranded the buffers: {counts}"
    )


@pytest.mark.asyncio
async def test_a_turn_that_raises_still_flushes(monkeypatch):
    """A crashing turn is caught inside the loop, but the flush must not be skipped.

    CONTROL, NOT A TDD CYCLE — this passed before the fix as well as after.
    `_run_post_turn`'s exception is caught *inside* the loop body, so the old
    bottom-of-loop flushes were reached anyway. It is here to pin that the
    `finally` did not make a previously-covered path worse, and it would catch a
    future edit that let a turn's exception escape the body.
    """
    eng, agent = _engine(monkeypatch)
    counts = _instrument(eng, monkeypatch)
    monkeypatch.setattr(eng, "_select_agent", lambda: agent)

    async def _no_reply_lane():
        return 0

    monkeypatch.setattr(eng, "_dispatch_reply_lane", _no_reply_lane)

    async def _boom(_agent):
        eng._running = False
        raise RuntimeError("turn exploded")

    monkeypatch.setattr(eng, "_run_post_turn", _boom)

    async def _sleep(delay):
        return None

    monkeypatch.setattr(eng, "_sleep", _sleep)

    await eng._run_main_loop()

    # drain 0: `_boom` stops the engine before it raises, so this is also a
    # stopping tick. The flushes are what this test is about.
    assert counts == {"drain": 0, "persist": 1, "llm": 1, "assess": 1}, (
        f"a failed turn stranded the buffers: {counts}"
    )


@pytest.mark.asyncio
async def test_a_shutdown_request_stops_the_hoisted_drain(monkeypatch):
    """The drain is UNBOUNDED; `stop()`'s is not. It must not run past a stop.

    Hoisting the drain into the `finally` (A3.2) put an unbounded
    `_drain_memory_events()` on a path a SIGTERM can reach: `docker stop -t 420`
    lands while the loop sits in the idle backoff, `request_stop()` clears
    `_running` and wakes `_sleep` early, the branch hits `continue` — and the
    `finally` then runs N real LLM calls at 10-40s each BEFORE the `while`
    condition is even re-tested. Pre-A3.2 that `continue` skipped the drain
    entirely, so this path is one the fix opened.

    `stop()` deliberately bounds its own drain with
    `MEMORY_EVENTS_MAX_AT_SHUTDOWN` ("each is a real LLM call and the stop grace
    period is finite"), and it runs immediately after the loop returns — so
    skipping the drain here loses nothing, it just stops the loop from spending
    the grace period the bounded drain was sized for.

    The FLUSHES must still run: they are cheap, they are pure I/O against the DB,
    and skipping them is exactly the data loss A3.2 exists to prevent.
    """
    eng, _agent = _engine(monkeypatch)
    counts = _instrument(eng, monkeypatch)
    monkeypatch.setattr(eng, "_select_agent", lambda: None)

    async def _no_reply_lane():
        return 0

    monkeypatch.setattr(eng, "_dispatch_reply_lane", _no_reply_lane)

    async def _sleep(delay):
        # SIGTERM during the idle backoff: request_stop() flips _running and
        # wakes the sleep, and the branch then `continue`s into the finally.
        eng.request_stop()

    monkeypatch.setattr(eng, "_sleep", _sleep)

    await eng._run_main_loop()

    assert counts["drain"] == 0, (
        "a stop request did not stop the unbounded memory drain — N real LLM "
        "calls now run inside the container's stop grace period, ahead of the "
        "bounded drain stop() was sized for"
    )
    assert (counts["persist"], counts["llm"], counts["assess"]) == (1, 1, 1), (
        f"the flushes must still run on the stopping tick: {counts}"
    )

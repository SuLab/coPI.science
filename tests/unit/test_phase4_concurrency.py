"""Phase 4's fan-out is bounded.

`_phase4_reply_threads` gathers one `_reply_to_thread` per pending thread. Until
the event-loop fix (tests/unit/test_llm_event_loop.py) those coroutines could not
actually overlap — the synchronous Anthropic client pinned the loop, so `gather`
was a sequential queue. Now that the call is awaited off-thread they really do
run concurrently, and the hub is the agent that makes that dangerous: it sits on
every spoke edge and a production run logged `Phase 4: Replying to 37 threads`
in one turn with only ~15 spokes live.

Unbounded, that is 37 simultaneous Opus requests from one turn — a burst the
sliding-window limiter cannot shape, because `_within_rate_limit` is consulted
once per turn at selection, not per call. So the fan-out is capped.
"""

import asyncio

import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from tests.fakes import FakeSlackClient


def _hub_with_threads(n: int):
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    for i in range(n):
        hub.state.active_threads[f"t{i}"] = ThreadState(
            thread_id=f"t{i}", channel="general", other_agent_id=f"pi{i}",
            message_count=1, has_pending_reply=True,
        )
    engine = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    return engine, hub


@pytest.mark.asyncio
async def test_phase4_fanout_is_bounded(monkeypatch):
    from src.config import get_settings

    n_threads = 20
    cap = get_settings().phase4_max_concurrent_replies
    assert cap >= 1

    engine, hub = _hub_with_threads(n_threads)

    live = 0
    peak = 0
    done = []

    async def _fake_reply(agent, thread):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        done.append(thread.thread_id)
        live -= 1

    monkeypatch.setattr(engine, "_reply_to_thread", _fake_reply)

    replied = await engine._phase4_reply_threads(hub)

    # Every thread still gets its reply — the cap paces the work, never drops it.
    assert len(done) == n_threads
    assert replied == {f"t{i}" for i in range(n_threads)}
    assert peak <= cap, f"peak concurrency {peak} exceeded the cap of {cap}"


@pytest.mark.asyncio
async def test_phase4_still_overlaps_up_to_the_cap(monkeypatch):
    """The cap must not accidentally serialize the fan-out back to one at a time —
    that would reintroduce the latency the event-loop fix removed."""
    from src.config import get_settings

    cap = get_settings().phase4_max_concurrent_replies
    if cap < 2:
        pytest.skip("cap is 1; overlap is intentionally disabled")

    engine, hub = _hub_with_threads(cap * 2)

    live = 0
    peak = 0

    async def _fake_reply(agent, thread):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1

    monkeypatch.setattr(engine, "_reply_to_thread", _fake_reply)
    await engine._phase4_reply_threads(hub)

    assert peak > 1, "fan-out serialized to one reply at a time"


@pytest.mark.asyncio
async def test_the_fanout_bound_is_global_not_per_turn(monkeypatch):
    """Two agents replying at once must share one budget. A per-call semaphore
    bounds each turn separately, so N turns give N x cap concurrent calls."""
    from src.config import get_settings

    cap = get_settings().phase4_max_concurrent_replies
    engine_a, hub_a = _hub_with_threads(cap * 2)
    # Second agent inside the SAME engine.
    from src.agent.agent import Agent
    from src.agent.state import ThreadState

    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    for i in range(cap * 2):
        lab.state.active_threads[f"L{i}"] = ThreadState(
            thread_id=f"L{i}", channel="general", other_agent_id="blackbird",
            message_count=1, has_pending_reply=True,
        )
    engine_a.agents["wang"] = lab

    live = 0
    peak = 0

    async def _fake_reply(agent, thread):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1

    monkeypatch.setattr(engine_a, "_reply_to_thread", _fake_reply)

    await asyncio.gather(
        engine_a._phase4_reply_threads(hub_a),
        engine_a._phase4_reply_threads(lab),
    )

    assert peak <= cap, f"two concurrent turns reached {peak}, above the global cap {cap}"

"""The post lane (`_run_post_turn`) is Phase 1 + Phase 5 only.

Task 11 moved Phase 3 (thread activation) and Phase 4 (thread reply) out to
the reply lane entirely (`_dispatch_reply_lane` / `_service_reply` — see
tests/unit/test_reply_lane.py). `_run_post_turn` therefore has no Phase-4
concept left to couple Phase 5 to at all: the tests below that used to pin
"Phase 4 work must not trigger/suppress Phase 5" (Task 10, Ruling R6) are
superseded by a structural guarantee (`test_run_post_turn_never_touches_the_
reply_lane`), and the skip-streak-reset / no-timer-stamp invariants that used
to live in `_run_turn`'s Phase-4 block moved to `_service_reply` — see
test_reply_lane.py's `test_service_reply_resets_the_skip_streak` and
`test_service_reply_does_not_stamp_the_spontaneous_timer`.
"""
import time as _time

import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient


def test_run_post_turn_never_touches_the_reply_lane():
    """Construction-level guarantee: the paced post lane cannot be driven by
    reply volume from the unpaced reply lane because it has no path to Phase
    3/4 at all any more."""
    import inspect

    src = inspect.getsource(SimulationEngine._run_post_turn)
    assert "_phase3_activate_threads(" not in src
    assert "_phase4_reply_threads(" not in src
    assert "_service_reply(" not in src
    assert "_dispatch_reply_lane(" not in src


@pytest.mark.asyncio
async def test_spontaneous_ready_triggers_phase5(monkeypatch):
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent], slack_clients={"wang": FakeSlackClient(agent_id="wang")}
    )
    # Timer is already long overdue for a spontaneous post.
    stale_time = _time.time() - 10**9
    agent.state.last_phase5_action_time = stale_time

    called = []

    async def _phase5(_a):
        called.append(_a.agent_id)

    monkeypatch.setattr(eng, "_phase1_channel_discovery", lambda a: None)
    monkeypatch.setattr(eng, "_phase5_new_post", _phase5)

    did_work = await eng._run_post_turn(agent)

    assert called == ["wang"]
    assert did_work is False, "did_work reflects api_call_count, not whether Phase 5 ran"


@pytest.mark.asyncio
async def test_not_yet_due_skips_phase5(monkeypatch):
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent], slack_clients={"wang": FakeSlackClient(agent_id="wang")}
    )
    # Just acted — not due for another spontaneous post.
    agent.state.last_phase5_action_time = _time.time()

    called = []

    async def _phase5(_a):
        called.append(_a.agent_id)

    monkeypatch.setattr(eng, "_phase1_channel_discovery", lambda a: None)
    monkeypatch.setattr(eng, "_phase5_new_post", _phase5)

    await eng._run_post_turn(agent)

    assert called == [], "Phase 5 must not fire before its spontaneous timer is due"


@pytest.mark.asyncio
async def test_run_post_turn_calls_phase1(monkeypatch):
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent], slack_clients={"wang": FakeSlackClient(agent_id="wang")}
    )
    agent.state.last_phase5_action_time = _time.time()  # not due — isolates Phase 1

    called = []
    monkeypatch.setattr(eng, "_phase1_channel_discovery", lambda a: called.append(a.agent_id))
    monkeypatch.setattr(eng, "_phase5_new_post", _noop_async)

    await eng._run_post_turn(agent)

    assert called == ["wang"]


@pytest.mark.asyncio
async def test_in_flight_is_true_during_the_turn_and_false_after(monkeypatch):
    """AgentState.in_flight excludes an agent from post-lane selection while its
    own turn is running — a no-op today (the post lane is strictly sequential)
    but the intended replacement for `_last_llm_caller`'s back-to-back guard
    once loop iterations can overlap."""
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent], slack_clients={"wang": FakeSlackClient(agent_id="wang")}
    )
    agent.state.last_phase5_action_time = _time.time() - 10**9  # due
    monkeypatch.setattr(eng, "_phase1_channel_discovery", lambda a: None)

    seen_in_flight = []

    async def _phase5(a):
        seen_in_flight.append(a.state.in_flight)

    monkeypatch.setattr(eng, "_phase5_new_post", _phase5)
    assert agent.state.in_flight is False

    await eng._run_post_turn(agent)

    assert seen_in_flight == [True], "in_flight must be set for the duration of the turn"
    assert agent.state.in_flight is False


@pytest.mark.asyncio
async def test_in_flight_resets_even_if_the_turn_raises(monkeypatch):
    """A stray exception mid-turn must not permanently strand the agent
    ineligible — in_flight is reset in a finally."""
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent], slack_clients={"wang": FakeSlackClient(agent_id="wang")}
    )
    agent.state.last_phase5_action_time = _time.time() - 10**9  # due

    def _boom(_a):
        raise RuntimeError("boom")

    monkeypatch.setattr(eng, "_phase1_channel_discovery", _boom)

    with pytest.raises(RuntimeError):
        await eng._run_post_turn(agent)

    assert agent.state.in_flight is False


async def _noop_async(*_a, **_kw):
    return None

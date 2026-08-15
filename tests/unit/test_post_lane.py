"""Replying must not earn a top-level post. Otherwise the 'staggered' post lane
is driven by reply volume — exactly the coupling the split removes.

Fix round 1 (Ruling R6): the coupling was initially inverted rather than
removed. `_run_turn`'s surviving "Phase 4 activity resets skip backoff" block
was also stamping `last_phase5_action_time = time.time()` on every turn with
Phase 4 work. Once `spontaneous_ready` became the ONLY Phase 5 gate, that
stamp meant an always-replying agent perpetually pushed its own spontaneous
timer back and could never become eligible to post — replying still drove the
post lane, just with the sign flipped from "triggers" to "suppresses". The
tests below pin the corrected behaviour: Phase 4 work resets the skip-backoff
streak (still correct) but must not touch the timer that gates the
spontaneous check.
"""
import time as _time

import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient


@pytest.mark.asyncio
async def test_phase4_work_does_not_trigger_phase5(monkeypatch):
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent], slack_clients={"wang": FakeSlackClient(agent_id="wang")}
    )
    # Not yet due for a spontaneous post.
    agent.state.last_phase5_action_time = __import__("time").time()

    called = []

    async def _phase4(_a):
        return {"t1"}          # did reply work

    async def _phase5(_a):
        called.append(_a.agent_id)

    monkeypatch.setattr(eng, "_phase1_channel_discovery", lambda a: None)
    monkeypatch.setattr(eng, "_phase3_activate_threads", lambda a: None)
    monkeypatch.setattr(eng, "_phase4_reply_threads", _phase4)
    monkeypatch.setattr(eng, "_phase5_new_post", _phase5)

    await eng._run_turn(agent)

    assert called == [], "Phase 4 work must not earn a Phase 5 post"


@pytest.mark.asyncio
async def test_phase4_work_does_not_push_back_the_spontaneous_timer(monkeypatch):
    """An agent whose spontaneous timer was already due must still fire
    Phase 5 on a turn that also does Phase 4 work — and the timer itself
    must be left exactly as it was, not re-stamped to "now" by Phase 4."""
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent], slack_clients={"wang": FakeSlackClient(agent_id="wang")}
    )
    # Timer is already long overdue for a spontaneous post before this turn.
    stale_time = _time.time() - 10**9
    agent.state.last_phase5_action_time = stale_time

    called = []

    async def _phase4(_a):
        return {"t1"}          # did reply work

    async def _phase5(_a):
        called.append(_a.agent_id)

    monkeypatch.setattr(eng, "_phase1_channel_discovery", lambda a: None)
    monkeypatch.setattr(eng, "_phase3_activate_threads", lambda a: None)
    monkeypatch.setattr(eng, "_phase4_reply_threads", _phase4)
    monkeypatch.setattr(eng, "_phase5_new_post", _phase5)

    await eng._run_turn(agent)

    assert called == ["wang"], (
        "an already-due spontaneous timer must still fire Phase 5 despite "
        "ongoing Phase 4 reply activity"
    )
    assert agent.state.last_phase5_action_time == stale_time, (
        "Phase 4 work must not push back the spontaneous timer — only a "
        "real Phase 5 action (inside _phase5_new_post) may stamp it"
    )


@pytest.mark.asyncio
async def test_phase4_work_still_resets_the_skip_streak(monkeypatch):
    """The skip-backoff streak reset is a separate, still-correct behaviour:
    an engaged agent should not carry a stretched-out backoff interval."""
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent], slack_clients={"wang": FakeSlackClient(agent_id="wang")}
    )
    agent.state.consecutive_phase5_skips = 3
    # Not due for a spontaneous post — isolates the skip-streak reset from
    # the (removed) timer stamp.
    agent.state.last_phase5_action_time = _time.time()

    async def _phase4(_a):
        return {"t1"}

    async def _phase5(_a):
        pass

    monkeypatch.setattr(eng, "_phase1_channel_discovery", lambda a: None)
    monkeypatch.setattr(eng, "_phase3_activate_threads", lambda a: None)
    monkeypatch.setattr(eng, "_phase4_reply_threads", _phase4)
    monkeypatch.setattr(eng, "_phase5_new_post", _phase5)

    await eng._run_turn(agent)

    assert agent.state.consecutive_phase5_skips == 0, (
        "Phase 4 activity must still clear the skip-backoff streak"
    )

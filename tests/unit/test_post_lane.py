"""Replying must not earn a top-level post. Otherwise the 'staggered' post lane
is driven by reply volume — exactly the coupling the split removes."""
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

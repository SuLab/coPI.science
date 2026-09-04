"""Proposal-count run limit: stop pitching at N, drain interviews, then stop.
See docs/plans/2026-09-04-assessment-ui-and-proposal-limit-design.md, Feature 4.
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.simulation import PROPOSAL_DRAIN_SETTLE_TICKS, SimulationEngine
from src.models import AgentMessage, SimulationRun
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio


def _engine(max_proposals):
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    return SimulationEngine(
        agents=[agent],
        slack_clients={"wang": FakeSlackClient(agent_id="wang")},
        max_proposals=max_proposals,
    ), agent


async def test_phase5_is_throttled_once_the_proposal_cap_is_reached(monkeypatch):
    eng, agent = _engine(max_proposals=2)
    eng._proposals_posted = 2  # cap already reached

    posted = []
    async def _post(*a, **k):
        posted.append(a)
        return "ts-1"
    monkeypatch.setattr(eng, "_post_message", _post)

    await eng._phase5_new_post(agent)
    assert posted == [], "no new pitch may be posted at/over the cap"


async def test_zero_max_proposals_never_throttles():
    eng, _ = _engine(max_proposals=0)
    assert eng.max_proposals == 0
    assert eng._proposals_posted == 0


async def test_rehydrates_the_proposal_count_from_new_post_rows(engine):
    """Mirrors the fixture idiom in tests/unit/test_engine_control_poll.py."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun(status="running", config={})
        db.add(run)
        await db.commit()
        run_id = run.id

    try:
        async with factory() as db:
            for ts, phase in (
                ("100.1", "new_post"),
                ("100.2", "new_post"),
                ("100.3", "thread_reply"),
            ):
                db.add(AgentMessage(
                    simulation_run_id=run_id,
                    agent_id="wang",
                    channel_id="local:general", channel_name="general",
                    message_ts=ts, message_length=10,
                    thread_ts=None if phase == "new_post" else "100.1",
                    phase=phase, content="hello",
                    sender_name="WangBot",
                    is_bot=True, posted_at=float(ts),
                ))
            await db.commit()

        agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
        eng = SimulationEngine(
            agents=[agent],
            slack_clients={"wang": FakeSlackClient(agent_id="wang")},
            session_factory=factory,
            simulation_run_id=run_id,
        )
        await eng._rehydrate_proposal_count()
        assert eng._proposals_posted == 2, "only the two new_post rows count"
    finally:
        async with factory() as db:
            stale = await db.get(SimulationRun, run_id)
            if stale is not None:
                await db.delete(stale)  # cascades agent_messages
                await db.commit()


def test_not_drained_before_cap_reached():
    eng, _ = _engine(max_proposals=2)
    eng._proposals_posted = 1
    assert eng._proposal_target_drained() is False


def test_not_drained_while_interviews_open(monkeypatch):
    eng, _ = _engine(max_proposals=2)
    eng._proposals_posted = 2
    monkeypatch.setattr(eng, "_open_interview_count", lambda: 1)
    # Even called enough times to exhaust the settle window, an open interview
    # blocks the drain.
    for _ in range(10):
        assert eng._proposal_target_drained() is False


def test_drains_after_settle_window_once_cap_reached_and_no_open(monkeypatch):
    eng, _ = _engine(max_proposals=2)
    eng._proposals_posted = 2
    monkeypatch.setattr(eng, "_open_interview_count", lambda: 0)
    results = [eng._proposal_target_drained() for _ in range(PROPOSAL_DRAIN_SETTLE_TICKS)]
    assert results[-1] is True
    assert results[0] is False  # not on the first drained tick


def test_engine_accepts_and_stores_max_proposals():
    eng, _ = _engine(max_proposals=5)
    assert eng.max_proposals == 5

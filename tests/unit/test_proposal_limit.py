"""Proposal-count run limit: stop pitching at N, drain interviews, then stop.
See docs/plans/2026-09-04-assessment-ui-and-proposal-limit-design.md, Feature 4.
"""

import inspect
from datetime import UTC, datetime, timedelta

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
            max_proposals=2,  # nonzero: N3 skips the query entirely when the cap is off
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


def test_run_main_loop_gates_on_proposal_target_drained():
    """Source-contract pin (I2a): the main loop's while-condition must call
    `_proposal_target_drained()` directly. A regression that mis-places or
    drops the check from that condition would otherwise still pass every
    other test in this file — they all call `_proposal_target_drained()`
    themselves rather than driving the real loop."""
    src = inspect.getsource(SimulationEngine._run_main_loop)
    assert "_proposal_target_drained()" in src


async def test_resumed_engine_gates_phase5_after_rehydrating_at_cap(engine, monkeypatch):
    """End-to-end pin for I1: an engine built with the SAME nonzero
    max_proposals a resume now inherits from the run's stored config
    rehydrates its count from the durable `new_post` rows and then actually
    refuses to open a new pitch once that count is at the cap. This is the
    path that a regression in main.py's resume branch (silently zeroing
    max_proposals) would break, even though the unit-level gate/drain tests
    above would still pass."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun(status="running", config={"max_proposals": 2})
        db.add(run)
        await db.commit()
        run_id = run.id

    try:
        async with factory() as db:
            for ts in ("200.1", "200.2"):
                db.add(AgentMessage(
                    simulation_run_id=run_id,
                    agent_id="wang",
                    channel_id="local:general", channel_name="general",
                    message_ts=ts, message_length=10,
                    thread_ts=None,
                    phase="new_post", content="hello",
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
            max_proposals=2,
        )
        await eng._rehydrate_proposal_count()
        assert eng._proposals_posted == 2

        posted = []

        async def _post(*a, **k):
            posted.append(a)
            return "ts-1"

        monkeypatch.setattr(eng, "_post_message", _post)
        # Isolate the proposal-cap gate from the unrelated daily post cap,
        # which reads from the in-memory message_log (never populated here).
        monkeypatch.setattr(eng, "_count_today_posts", lambda a: 0)

        await eng._phase5_new_post(agent)
        assert posted == [], "resume must not silently re-enable pitching past the cap"
    finally:
        async with factory() as db:
            stale = await db.get(SimulationRun, run_id)
            if stale is not None:
                await db.delete(stale)  # cascades agent_messages
                await db.commit()


def test_max_runtime_and_proposal_cap_are_independent_stop_conditions():
    """I2c: the main loop's while-condition ANDs three independent things —
    `_running`, `is_within_time_limit`, and `not _proposal_target_drained()`.
    Pin that each stop mechanism works on its own, without the other being
    configured at all, without driving the real async loop."""
    # max_proposals off (0): the drain never fires, so an elapsed max_runtime
    # is the only thing that can stop the loop — and it still does.
    eng, _ = _engine(max_proposals=0)
    eng._running = True
    eng.max_runtime_minutes = 1
    eng._start_time = datetime.now(UTC) - timedelta(minutes=5)
    assert eng.is_within_time_limit is False
    assert eng._proposal_target_drained() is False

    # max_proposals cap reached and drained, with no max_runtime configured
    # at all: the drain alone satisfies the loop-exit condition regardless
    # of the (permanently True, since max_runtime_minutes defaults to 0/off)
    # time check.
    eng2, _ = _engine(max_proposals=2)
    eng2._running = True
    eng2._proposals_posted = 2
    drained = False
    for _ in range(PROPOSAL_DRAIN_SETTLE_TICKS):
        drained = eng2._proposal_target_drained()
    assert drained is True
    assert eng2.is_within_time_limit is True
    loop_would_continue = eng2._running and eng2.is_within_time_limit and not drained
    assert loop_would_continue is False

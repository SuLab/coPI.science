"""The orchestrator and the sessions (independent simulation controls, Task 2).

The main loop runs while any session is active and the last session's end decides the run's
end reason; ``_apply_session_events`` applies a Stop now's closes and an Interviews join's
pitch catch-up; ``stop()`` ends every active session and writes them to the run's config.
"""
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.sessions import (
    INTERVIEWS,
    PROJECTS,
    STOP_EVERYTHING,
    STOP_NOW,
    Limits,
    Sessions,
)
from src.agent.simulation import STOPPED_OUTCOME, SimulationEngine, _HeldVerdict
from src.agent.state import ThreadState
from src.models import SimulationRun
from src.models.hub_project import REASON_STOPPED
from tests.fakes import FakeSlackClient
from tests.integration.test_hub_assessment_capture_gate import (
    _decisions,
    _delete_run,
    _new_run,
)

pytestmark = pytest.mark.integration


def _now():
    return datetime.now(UTC)


async def _sim(engine, monkeypatch, tmp_path, sessions=None):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("gordy", "GordyBot", "Gordy", role="pi_lab")
    sim = SimulationEngine(
        agents=[hub, lab],
        slack_clients={aid: FakeSlackClient(agent_id=aid) for aid in ("blackbird", "gordy")},
        session_factory=factory, simulation_run_id=run_id, sessions=sessions,
    )
    return sim, factory, run_id


def _quiet_loop(monkeypatch, sim, *, poll=None):
    """Every inbound source and every lane off: the loop only polls (``poll``, when
    given), applies the session events, ticks projects and observes interviews."""
    async def _noop(*a, **k):
        return None

    async def _zero(*a, **k):
        return 0

    for name in (
        "_poll_slack_for_bot_messages", "_sync_roster_from_db", "_sync_publication_records",
        "_drain_and_flush", "_sleep",
    ):
        monkeypatch.setattr(sim, name, _noop)
    monkeypatch.setattr(sim, "_poll_control_plane", poll or _noop)
    monkeypatch.setattr(sim, "_dispatch_reply_lane", _zero)
    monkeypatch.setattr(sim, "_sync_profiles_from_disk", lambda: None)
    monkeypatch.setattr(sim, "_select_agent", lambda: None)
    sim._running = True


def _open_thread(sim, thread_id):
    """An open thread between the hub and the lab, on both sides."""
    for aid, other in (("blackbird", "gordy"), ("gordy", "blackbird")):
        sim.agents[aid].state.active_threads[thread_id] = ThreadState(
            thread_id=thread_id, channel="single-cell-omics", other_agent_id=other,
            message_count=4,
        )


async def _run_row(factory, run_id):
    async with factory() as db:
        return await db.get(SimulationRun, run_id)


async def _hold(factory, run_id):
    async with factory() as db:
        await db.execute(
            update(SimulationRun).where(SimulationRun.id == run_id).values(held_at=func.now())
        )
        await db.commit()


async def test_the_loop_ends_target_drained_when_the_only_session_reaches_its_count(
    engine, monkeypatch, tmp_path,
):
    sessions = Sessions.legacy(count=1, runtime_minutes=0, now=_now())
    sim, factory, run_id = await _sim(engine, monkeypatch, tmp_path, sessions)
    observed = []
    real_observe = sim.post_lane._observe_interview_session

    def observe():
        observed.append(1)
        real_observe()

    monkeypatch.setattr(sim, "_observe_interview_session", observe)
    _quiet_loop(monkeypatch, sim)
    try:
        sessions.add_count(INTERVIEWS, _now())  # the one booked pitch reaches the count
        await sim._run_main_loop()
        assert sim._end_reason == "target_drained" and sim._running is False
        assert len(observed) == 3, "three quiet observations end the wind-down"
    finally:
        await _delete_run(factory, run_id)


async def test_the_loop_ends_operator_after_a_hand_stop(engine, monkeypatch, tmp_path):
    sim, factory, run_id = await _sim(engine, monkeypatch, tmp_path)

    async def poll(now):
        sim.ctx.sessions.end(INTERVIEWS, STOP_NOW, _now())

    _quiet_loop(monkeypatch, sim, poll=poll)
    try:
        await sim._run_main_loop()
        assert sim._end_reason == "operator"
    finally:
        await _delete_run(factory, run_id)


async def test_a_carried_hold_ends_held_over(engine, monkeypatch, tmp_path):
    sessions = Sessions(hold_carried=True)
    sessions.start(PROJECTS, Limits(count=1), _now(), initial=True)
    sim, factory, run_id = await _sim(engine, monkeypatch, tmp_path, sessions)

    async def poll(now):
        sim.ctx.sessions.end(PROJECTS, STOP_NOW, _now())

    _quiet_loop(monkeypatch, sim, poll=poll)
    try:
        await _hold(factory, run_id)
        await sim._run_main_loop()
        assert sim._end_reason == "held_over"
        await sim.stop()
        assert (await _run_row(factory, run_id)).held_at is not None, "still held"
    finally:
        await _delete_run(factory, run_id)


async def test_stop_now_closes_every_open_interview_as_stopped(engine, monkeypatch, tmp_path):
    sim, factory, run_id = await _sim(engine, monkeypatch, tmp_path)
    try:
        _open_thread(sim, "t-held")
        _open_thread(sim, "t-plain")
        sim._assessed_threads["t-held"] = _HeldVerdict(ordinal=4, final=False, slack_ts="100.1")
        sim.ctx.sessions.end(INTERVIEWS, STOP_NOW, _now())

        await sim._apply_session_events()
        await sim.threads.flush_pending_decisions(final=True)

        decisions = {d.thread_id: d.outcome for d in await _decisions(factory, run_id)}
        assert decisions == {"t-held": STOPPED_OUTCOME, "t-plain": STOPPED_OUTCOME}
        for agent in sim.agents.values():
            assert agent.state.active_threads == {}
        assert sim._pending_headlines == ["t-held"], "the held verdict's headline is owed"
        assert sim._pending_memory_events == [], "an operator's stop is not a memory"
    finally:
        await _delete_run(factory, run_id)


async def test_projects_stop_now_closes_the_threads_end_open_projects_returns(
    engine, monkeypatch, tmp_path,
):
    sessions = Sessions()
    sessions.start(INTERVIEWS, Limits(), _now(), initial=True)
    sessions.start(PROJECTS, Limits(), _now(), initial=True)
    sim, factory, run_id = await _sim(engine, monkeypatch, tmp_path, sessions)
    reasons = []

    async def end_open_projects(reason):
        reasons.append(reason)
        return {"p-1", "p-gone"}

    monkeypatch.setattr(sim.projects, "end_open_projects", end_open_projects)
    try:
        _open_thread(sim, "p-1")
        _open_thread(sim, "i-1")
        sessions.end(PROJECTS, STOP_NOW, _now())

        await sim._apply_session_events()
        await sim.threads.flush_pending_decisions(final=True)

        assert reasons == [REASON_STOPPED]
        decisions = {d.thread_id: d.outcome for d in await _decisions(factory, run_id)}
        assert decisions == {"p-1": STOPPED_OUTCOME}, "a thread nobody holds is skipped"
        assert "i-1" in sim.agents["blackbird"].state.active_threads, "interviews go on"
        assert "p-1" not in sim.agents["gordy"].state.active_threads
    finally:
        await _delete_run(factory, run_id)


async def test_an_interviews_join_runs_the_pitch_catch_up_and_clears_the_hold(
    engine, monkeypatch, tmp_path,
):
    sessions = Sessions(hold_carried=True)
    sessions.start(PROJECTS, Limits(), _now(), initial=True)
    sim, factory, run_id = await _sim(engine, monkeypatch, tmp_path, sessions)
    calls = []

    async def recover():
        calls.append(sim.ctx.sessions.hold_carried)

    monkeypatch.setattr(sim, "_recover_reply_less_pitches", recover)
    try:
        await _hold(factory, run_id)
        await sim._apply_session_events()
        assert calls == [], "the engine's own Projects start runs no catch-up"

        sessions.start(INTERVIEWS, Limits(count=3), _now())
        await sim._apply_session_events()

        assert calls == [False]
        assert (await _run_row(factory, run_id)).held_at is None
    finally:
        await _delete_run(factory, run_id)


async def test_stop_marks_active_sessions_and_persists_them(engine, monkeypatch, tmp_path):
    sim, factory, run_id = await _sim(engine, monkeypatch, tmp_path)
    try:
        await sim.stop()
        assert not sim.ctx.sessions.any_active() and sim.ctx.sessions.dirty is False
        config = (await _run_row(factory, run_id)).config
        [record] = config["sessions"]
        assert record["system"] == INTERVIEWS
        assert record["state"] == "ended" and record["reason"] == STOP_EVERYTHING
        assert record["initial"] is True and record["ended_at"] is not None
    finally:
        await _delete_run(factory, run_id)

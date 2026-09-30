import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.engine.context import RunState
from src.agent.engine.control import EngineConfigError, EngineHeartbeat, validate_engine_settings
from src.models import SimulationCommand, SimulationProcessStatus
from src.services.simulation_control import enqueue_command, read_status


async def _delete_status(factory):
    """Shared session DB: never leave the singleton status row behind."""
    async with factory() as db:
        status = await db.get(SimulationProcessStatus, 1)
        if status is not None:
            await db.delete(status)
            await db.commit()


class _Lock:
    def __init__(self, fail=False):
        self.fail = fail

    async def check(self):
        if self.fail:
            raise ConnectionResetError("lock connection gone")


@pytest.mark.asyncio
async def test_a_long_tick_keeps_the_heartbeat_fresh(engine):
    """S1-05: the heartbeat is its own task, so a tick blocked for longer than
    the 120 s stale window (here, a blocking await standing in for a 150 s
    tick with the interval scaled down) still refreshes the row."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    hb = EngineHeartbeat(lock=_Lock(), run_state=RunState(), session_factory=factory, interval=0.05)
    hb.start()
    try:
        await asyncio.sleep(0.1)   # let the first tick land
        async with factory() as db:
            first = (await read_status(db)).updated_at
        await asyncio.sleep(0.3)
        async with factory() as db:
            later = await read_status(db)
        assert later.updated_at > first
        assert later.state == "starting"
    finally:
        await hb.stop()
        await _delete_status(factory)


@pytest.mark.asyncio
async def test_heartbeat_never_claims_a_pending_stop(engine):
    """Review Focus 3 / FA-4 N2: commands stay with _poll_control_plane."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_state = RunState()
    hb = EngineHeartbeat(lock=_Lock(), run_state=run_state, session_factory=factory)
    hb.mark_running()
    async with factory() as db:
        cmd = await enqueue_command(db, command="stop", payload=None, requested_by_user_id=None)
    try:
        for _ in range(3):
            await hb.tick()
        async with factory() as db:
            row = await db.get(SimulationCommand, cmd.id)
            assert row.status == "pending"
        assert not run_state.stop_event.is_set()
        async with factory() as db:
            assert (await read_status(db)).state == "running"
    finally:
        async with factory() as db:
            await db.delete(await db.get(SimulationCommand, cmd.id))
            await db.commit()
        await _delete_status(factory)


@pytest.mark.asyncio
async def test_a_lost_lock_requests_a_hold_stop():
    run_state = RunState()
    hb = EngineHeartbeat(lock=_Lock(fail=True), run_state=run_state, session_factory=None)
    await hb.tick()
    assert run_state.end_reason == "lock_lost"
    assert hb.state() == "stopping"


def test_thread_length_other_than_12_refuses_to_start():
    from types import SimpleNamespace

    for bad in (10, 20):
        with pytest.raises(EngineConfigError, match="max_thread_messages"):
            validate_engine_settings(SimpleNamespace(max_thread_messages=bad))
    validate_engine_settings(SimpleNamespace(max_thread_messages=12))


@pytest.mark.asyncio
async def test_a_stop_requested_before_start_ends_the_run_with_the_final_flush(monkeypatch):
    """AG-3: a signal that fired during startup is replayed into RunState; start()
    must not clear it, the main loop runs no turn, and stop() still flushes."""
    from src.agent.agent import Agent
    from src.agent.simulation import SimulationEngine
    from tests.fakes import FakeSlackClient

    run_state = RunState()
    run_state.request_stop("signal")
    sim = SimulationEngine(agents=[Agent("a", "ABot", "A")],
                           slack_clients={"a": FakeSlackClient(agent_id="a")}, run_state=run_state)

    def _no_turns():
        raise AssertionError("no agent may be selected after a startup signal")

    monkeypatch.setattr(sim.scheduler, "_select_agent", _no_turns)
    finals = []
    real_flush = sim.persistence._flush_persisted

    async def _record(*a, final=False, **kw):
        finals.append(final)
        return await real_flush(*a, final=final, **kw)

    monkeypatch.setattr(sim.persistence, "_flush_persisted", _record)
    await sim.start()
    assert run_state.running is False
    await sim.stop()
    assert True in finals

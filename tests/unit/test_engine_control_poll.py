"""_poll_control_plane: the engine's own half of the operator control plane.

Each main-loop tick, the engine claims at most one pending `stop` command
(requesting a graceful stop and marking the command done) or, with no stop, at
most one pending `system` command (a per-system start, wind down or stop now,
applied to the run's sessions), writes changed sessions to the run's config, and
upserts a heartbeat carrying a per-agent and per-system detail snapshot — so
/workspace/simulation can see the run is alive and control it without touching the
container. The whole body is wrapped in try/except: a control-plane hiccup
must never take down a running simulation, only cost one missed heartbeat.
"""
import logging
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.sessions import (
    INTERVIEWS,
    PROJECTS,
    STOP_NOW,
    WIND_DOWN,
    WINDING_DOWN,
    Limits,
)
from src.agent.simulation import CONTROL_POLL_INTERVAL, SimulationEngine
from src.models import SimulationCommand, SimulationProcessStatus, SimulationRun
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio


def _engine(monkeypatch, tmp_path):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    clients = {
        "blackbird": FakeSlackClient(agent_id="blackbird"),
        "wang": FakeSlackClient(agent_id="wang"),
    }
    return SimulationEngine(agents=[hub, lab], slack_clients=clients)


async def _seed_run(factory):
    async with factory() as db:
        run = SimulationRun(status="running", config={})
        db.add(run)
        await db.commit()
        return run.id


async def _cleanup(factory, run_id):
    """Shared session DB: never leave a run row or the singleton status row."""
    async with factory() as db:
        run = await db.get(SimulationRun, run_id)
        if run is not None:
            await db.delete(run)
        status = await db.get(SimulationProcessStatus, 1)
        if status is not None:
            await db.delete(status)
        await db.commit()


async def test_a_pending_stop_command_is_claimed_and_requests_stop(
    monkeypatch, tmp_path, engine,
):
    eng = _engine(monkeypatch, tmp_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _seed_run(factory)
    eng.session_factory = factory
    eng.simulation_run_id = run_id

    # request_stop(reason) sets _end_reason/_running/_stop_event; record the
    # call directly on the instance.
    stop_calls: list[str] = []
    monkeypatch.setattr(
        eng, "request_stop", lambda reason="operator": stop_calls.append(reason),
    )

    async with factory() as db:
        cmd = SimulationCommand(command="stop", payload=None)
        db.add(cmd)
        await db.commit()
        cmd_id = cmd.id

    try:
        await eng._poll_control_plane(now=1e9)

        assert stop_calls == ["operator"]

        async with factory() as db:
            row = await db.get(SimulationCommand, cmd_id)
            assert row.status == "done"
            assert row.result == f"run {run_id}"

            status = await db.get(SimulationProcessStatus, 1)
            assert status is not None
            assert status.state == "stopping"
            assert status.simulation_run_id == run_id
    finally:
        async with factory() as db:
            row = await db.get(SimulationCommand, cmd_id)
            if row is not None:
                await db.delete(row)
            await db.commit()
        await _cleanup(factory, run_id)


async def test_no_command_upserts_a_running_heartbeat_with_agent_detail(
    monkeypatch, tmp_path, engine,
):
    eng = _engine(monkeypatch, tmp_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _seed_run(factory)
    eng.session_factory = factory
    eng.simulation_run_id = run_id

    eng.agents["wang"].api_call_count = 3
    eng.agents["wang"].message_count = 5

    try:
        await eng._poll_control_plane(now=1e9)

        async with factory() as db:
            status = await db.get(SimulationProcessStatus, 1)
            assert status is not None
            assert status.state == "running"
            assert status.simulation_run_id == run_id
            detail = status.detail
            assert detail["roster_size"] == 2
            assert set(detail["agents"]) == {"blackbird", "wang"}
            assert detail["agents"]["wang"] == {
                "active_threads": 0,
                "calls_in_window": 0,
                "api_calls": 3,
                "messages": 5,
            }
            assert "tick_at" in detail
            assert detail["systems"][INTERVIEWS]["state"] == "running"
            assert PROJECTS not in detail["systems"]
    finally:
        await _cleanup(factory, run_id)


async def test_second_call_within_the_interval_is_a_no_op(monkeypatch, tmp_path, engine):
    eng = _engine(monkeypatch, tmp_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _seed_run(factory)
    eng.session_factory = factory
    eng.simulation_run_id = run_id

    try:
        await eng._poll_control_plane(now=1000.0)
        async with factory() as db:
            first = await db.get(SimulationProcessStatus, 1)
            first_updated_at = first.updated_at

        # Still inside CONTROL_POLL_INTERVAL of the first call's `now`.
        await eng._poll_control_plane(now=1000.0 + CONTROL_POLL_INTERVAL - 1)

        async with factory() as db:
            rows = (await db.execute(select(SimulationProcessStatus))).scalars().all()
            assert len(rows) == 1
            assert rows[0].updated_at == first_updated_at
    finally:
        await _cleanup(factory, run_id)


async def test_no_session_factory_returns_without_raising(monkeypatch, tmp_path):
    eng = _engine(monkeypatch, tmp_path)
    assert eng.session_factory is None

    await eng._poll_control_plane(now=1e9)  # must not raise


async def test_a_service_error_is_swallowed_and_logged(
    monkeypatch, tmp_path, engine, caplog,
):
    eng = _engine(monkeypatch, tmp_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _seed_run(factory)
    eng.session_factory = factory
    eng.simulation_run_id = run_id

    def _boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr("src.services.simulation_control.claim_pending", _boom)
    caplog.set_level(logging.WARNING, logger="src.agent.simulation")

    try:
        await eng._poll_control_plane(now=1e9)  # must not raise
    finally:
        await _cleanup(factory, run_id)

    assert any(
        r.levelno == logging.WARNING and "control" in r.getMessage().lower()
        for r in caplog.records
    )


async def test_a_hold_open_stop_requests_operator_hold(monkeypatch, tmp_path, engine):
    eng = _engine(monkeypatch, tmp_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _seed_run(factory)
    eng.session_factory = factory
    eng.simulation_run_id = run_id
    stop_calls: list[str] = []
    monkeypatch.setattr(
        eng, "request_stop", lambda reason="operator": stop_calls.append(reason),
    )
    async with factory() as db:
        cmd = SimulationCommand(command="stop", payload={"hold_open": True})
        db.add(cmd)
        await db.commit()
        cmd_id = cmd.id
    try:
        await eng._poll_control_plane(now=1e9)
        assert stop_calls == ["operator_hold"]
        async with factory() as db:
            row = await db.get(SimulationCommand, cmd_id)
            assert row.status == "done"
            assert row.result == f"run {run_id} (hold open interviews)"
    finally:
        async with factory() as db:
            row = await db.get(SimulationCommand, cmd_id)
            if row is not None:
                await db.delete(row)
                await db.commit()
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_engine_fails_a_finalize_stop_and_keeps_running(monkeypatch, tmp_path, engine):
    """Review Focus 3."""
    from src.services.simulation_control import enqueue_command

    eng = _engine(monkeypatch, tmp_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _seed_run(factory)
    eng.session_factory = factory
    eng.simulation_run_id = run_id
    async with factory() as db:
        cmd = await enqueue_command(db, command="stop",
                                    payload={"finalize": True, "run_id": str(run_id)},
                                    requested_by_user_id=None)
        cmd_id = cmd.id
    eng.run_state.running = True
    try:
        await eng.control._poll_control_plane(10_000.0)
        async with factory() as db:
            row = await db.get(SimulationCommand, cmd_id)
            assert row.status == "failed"
            assert row.result == "Finalize run applies to a stopped run"
        assert eng.run_state.running is True
        assert not eng.run_state.stop_event.is_set()
    finally:
        async with factory() as db:
            row = await db.get(SimulationCommand, cmd_id)
            if row is not None:
                await db.delete(row)
                await db.commit()
        await _cleanup(factory, run_id)


async def _system_command(factory, payload):
    async with factory() as db:
        cmd = SimulationCommand(command="system", payload=payload)
        db.add(cmd)
        await db.commit()
        return cmd.id


async def _command_row(factory, cmd_id):
    async with factory() as db:
        return await db.get(SimulationCommand, cmd_id)


async def _delete_commands(factory, ids):
    async with factory() as db:
        for cmd_id in ids:
            row = await db.get(SimulationCommand, cmd_id)
            if row is not None:
                await db.delete(row)
        await db.commit()


async def _wired(monkeypatch, tmp_path, engine):
    eng = _engine(monkeypatch, tmp_path)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _seed_run(factory)
    eng.session_factory = factory
    eng.simulation_run_id = run_id
    return eng, factory, run_id


async def test_system_commands_for_this_run_start_wind_down_and_stop_now(
    monkeypatch, tmp_path, engine,
):
    eng, factory, run_id = await _wired(monkeypatch, tmp_path, engine)
    sessions = eng.ctx.sessions
    ids = []
    try:
        ids.append(await _system_command(factory, {
            "action": "start", "system": PROJECTS, "run_id": str(run_id),
            "limits": {"count": 2, "runtime_minutes": 0},
        }))
        await eng._poll_control_plane(now=1e9)
        row = await _command_row(factory, ids[-1])
        assert row.status == "done" and row.consumed_at is not None
        assert sessions.accepting(PROJECTS)
        assert sessions.current(PROJECTS).limits == Limits(count=2)
        assert sessions.current(PROJECTS).initial is False

        ids.append(await _system_command(factory, {
            "action": "wind_down", "system": PROJECTS, "run_id": str(run_id),
        }))
        await eng._poll_control_plane(now=2e9)
        assert (await _command_row(factory, ids[-1])).status == "done"
        assert sessions.current(PROJECTS).state == WINDING_DOWN
        assert sessions.current(PROJECTS).reason == WIND_DOWN

        ids.append(await _system_command(factory, {
            "action": "stop_now", "system": INTERVIEWS, "run_id": str(run_id),
        }))
        await eng._poll_control_plane(now=3e9)
        assert (await _command_row(factory, ids[-1])).status == "done"
        assert not sessions.active(INTERVIEWS)
        assert sessions.current(INTERVIEWS).reason == STOP_NOW
        assert eng._end_reason is None, "a system's stop is not the run's stop"
    finally:
        await _delete_commands(factory, ids)
        await _cleanup(factory, run_id)


async def test_a_system_command_for_another_run_is_finished_stale(monkeypatch, tmp_path, engine):
    eng, factory, run_id = await _wired(monkeypatch, tmp_path, engine)
    other = uuid.uuid4()
    cmd_id = await _system_command(factory, {
        "action": "start", "system": PROJECTS, "run_id": str(other), "limits": {},
    })
    try:
        await eng._poll_control_plane(now=1e9)
        row = await _command_row(factory, cmd_id)
        assert row.status == "stale"
        assert row.result == f"for run {other}, not this one"
        assert not eng.ctx.sessions.active(PROJECTS)
    finally:
        await _delete_commands(factory, [cmd_id])
        await _cleanup(factory, run_id)


@pytest.mark.parametrize("payload_fields,result", [
    ({"action": "start", "system": INTERVIEWS, "limits": {}}, "Interviews is already running"),
    ({"action": "stop_now", "system": PROJECTS}, "Projects is not running"),
    ({"action": "wind_down", "system": PROJECTS}, "Projects is not running"),
    ({"action": "start", "system": "grants", "limits": {}}, "unknown system 'grants'"),
    ({"action": "pause", "system": INTERVIEWS}, "unknown action 'pause'"),
])
async def test_an_impossible_system_command_is_finished_failed_with_the_reason(
    monkeypatch, tmp_path, engine, payload_fields, result,
):
    eng, factory, run_id = await _wired(monkeypatch, tmp_path, engine)
    cmd_id = await _system_command(factory, {**payload_fields, "run_id": str(run_id)})
    try:
        await eng._poll_control_plane(now=1e9)
        row = await _command_row(factory, cmd_id)
        assert row.status == "failed" and row.result == result
        assert eng.ctx.sessions.accepting(INTERVIEWS), "the running session is untouched"
    finally:
        await _delete_commands(factory, [cmd_id])
        await _cleanup(factory, run_id)


async def test_a_pending_stop_wins_and_the_system_command_waits(monkeypatch, tmp_path, engine):
    eng, factory, run_id = await _wired(monkeypatch, tmp_path, engine)
    monkeypatch.setattr(eng, "request_stop", lambda reason="operator": None)
    async with factory() as db:
        stop = SimulationCommand(command="stop", payload=None)
        db.add(stop)
        await db.commit()
        stop_id = stop.id
    cmd_id = await _system_command(factory, {
        "action": "start", "system": PROJECTS, "run_id": str(run_id), "limits": {},
    })
    try:
        await eng._poll_control_plane(now=1e9)
        assert (await _command_row(factory, stop_id)).status == "done"
        assert (await _command_row(factory, cmd_id)).status == "pending"
        assert not eng.ctx.sessions.active(PROJECTS)
    finally:
        await _delete_commands(factory, [stop_id, cmd_id])
        await _cleanup(factory, run_id)


async def test_dirty_sessions_are_written_to_the_run_config_and_marked_clean(
    monkeypatch, tmp_path, engine,
):
    eng, factory, run_id = await _wired(monkeypatch, tmp_path, engine)
    async with factory() as db:
        run = await db.get(SimulationRun, run_id)
        run.config = {"max_runtime": 60}
        await db.commit()
    assert eng.ctx.sessions.dirty, "the engine's own interviews start is a change"
    try:
        await eng._poll_control_plane(now=1e9)
        assert eng.ctx.sessions.dirty is False
        async with factory() as db:
            config = (await db.get(SimulationRun, run_id)).config
        assert config["max_runtime"] == 60, "the rest of config is kept"
        assert config["sessions"] == eng.ctx.sessions.records()
        assert [r["system"] for r in config["sessions"]] == [INTERVIEWS]
    finally:
        await _cleanup(factory, run_id)


async def test_a_failed_sessions_write_warns_and_stays_dirty(monkeypatch, tmp_path, caplog):
    eng = _engine(monkeypatch, tmp_path)
    eng.simulation_run_id = uuid.uuid4()

    class _Db:
        async def get(self, *a, **k):
            raise RuntimeError("db exploded")

        async def rollback(self):
            return None

    caplog.set_level(logging.WARNING, logger="src.agent.simulation")
    await eng.control._persist_sessions(_Db())  # must not raise
    assert eng.ctx.sessions.dirty is True
    assert any("sessions" in r.getMessage() for r in caplog.records)

"""_poll_control_plane: the engine's own half of the operator control plane.

Each main-loop tick, the engine claims at most one pending `stop` command
(requesting a graceful stop and marking the command done) and otherwise
upserts a running heartbeat carrying a per-agent detail snapshot — so
/admin/simulation can see the run is alive and stop it without touching the
container. The whole body is wrapped in try/except: a control-plane hiccup
must never take down a running simulation, only cost one missed heartbeat.
"""
import logging

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
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

    # There is no `_stop_requested` attribute anywhere on the engine —
    # `request_stop` sets `_running`/`_stop_event` instead — so the only way
    # to observe the call is to record it directly on the instance.
    stop_calls: list[bool] = []
    monkeypatch.setattr(eng, "request_stop", lambda: stop_calls.append(True))

    async with factory() as db:
        cmd = SimulationCommand(command="stop", payload=None)
        db.add(cmd)
        await db.commit()
        cmd_id = cmd.id

    try:
        await eng._poll_control_plane(now=1e9)

        assert stop_calls == [True]

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

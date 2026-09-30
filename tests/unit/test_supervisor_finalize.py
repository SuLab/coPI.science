"""§8.2/§8.3 supervisor: finalize stops run under the engine lock; a live
(lock-holding) engine is never overwritten; plain stops after a run settle."""
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.supervisor import run_supervisor
from src.models import SimulationCommand, SimulationProcessStatus, SimulationRun
from src.services.advisory_locks import ENGINE_LOCK_KEY, SessionAdvisoryLock
from src.services.simulation_control import read_status, upsert_status


async def _run(factory, **kw):
    async with factory() as db:
        run = SimulationRun(status="stopped", ended_at=datetime.now(UTC), **kw)
        db.add(run)
        await db.commit()
        return run.id


async def _cmd(factory, command, payload):
    async with factory() as db:
        cmd = SimulationCommand(command=command, payload=payload)
        db.add(cmd)
        await db.commit()
        return cmd.id


async def _cleanup(factory, ids, run_ids):
    """Shared session DB: never leave a command, a run row or the singleton status row."""
    async with factory() as db:
        for i in ids:
            row = await db.get(SimulationCommand, i)
            if row is not None:
                await db.delete(row)
        for r in run_ids:
            row = await db.get(SimulationRun, r)
            if row is not None:
                await db.delete(row)
        status = await db.get(SimulationProcessStatus, 1)
        if status is not None:
            await db.delete(status)
        await db.commit()


@pytest.mark.asyncio
async def test_boot_spares_and_runs_a_finalize_stop(engine, pg_url, monkeypatch):
    monkeypatch.setenv("SLACK_ENABLED", "false")
    from src.config import get_settings
    get_settings.cache_clear()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _run(factory, held_at=datetime.now(UTC))
    cmd_id = await _cmd(factory, "stop", {"finalize": True, "run_id": str(run_id)})

    async def stub(*a):
        raise AssertionError("no start")

    try:
        await run_supervisor(session_factory=factory, run_fn=stub, max_loops=1,
                             poll_seconds=0.01, database_url=pg_url)
        async with factory() as db:
            cmd = await db.get(SimulationCommand, cmd_id)
            run = await db.get(SimulationRun, run_id)
        assert cmd.status == "done" and "finalized run" in cmd.result
        assert run.finalized_at is not None and run.held_at is None
    finally:
        get_settings.cache_clear()
        await _cleanup(factory, [cmd_id], [run_id])


@pytest.mark.asyncio
async def test_a_finalize_stop_fails_when_the_engine_lock_is_held(engine, pg_url):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _run(factory)
    holder = SessionAdvisoryLock(pg_url, ENGINE_LOCK_KEY)
    assert await holder.acquire()
    cmd_id = None
    try:
        from src.agent.supervisor import _settle_pending_stops

        cmd_id = await _cmd(factory, "stop", {"finalize": True, "run_id": str(run_id)})
        await _settle_pending_stops(factory, pg_url)
        async with factory() as db:
            cmd = await db.get(SimulationCommand, cmd_id)
        assert cmd.status == "failed"
        assert cmd.result == "engine lock held — use Finalize run when no engine is alive"
    finally:
        await holder.release()
        await _cleanup(factory, [cmd_id] if cmd_id else [], [run_id])


@pytest.mark.asyncio
async def test_a_plain_stop_pending_after_the_run_is_settled_done(engine, pg_url):
    """A stop enqueued after the run's last control poll (here, by run_fn
    itself) is claimed after run_fn returns, not left for the next boot."""
    from tests.unit.test_supervisor import _hook_before_nth_session

    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded: dict[str, uuid.UUID] = {}

    async def seed_start():
        seeded["start"] = await _cmd(factory, "start", {"fresh": True})

    async def run_fn(*a):
        seeded["stop"] = await _cmd(factory, "stop", None)

    # Session 1 is boot's stale, session 2 boot's finalize-only settle; the
    # start is seeded before session 3, the loop's first iteration.
    hooked = _hook_before_nth_session(factory, n=3, hook=seed_start)
    try:
        await run_supervisor(session_factory=hooked, run_fn=run_fn, poll_seconds=0.01,
                             database_url=pg_url)
        async with factory() as db:
            stop = await db.get(SimulationCommand, seeded["stop"])
        assert stop.status == "done" and stop.result == "run already stopped"
    finally:
        await _cleanup(factory, list(seeded.values()), [])


@pytest.mark.asyncio
async def test_a_start_claimed_while_an_engine_is_alive_fails(engine, pg_url):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    holder = SessionAdvisoryLock(pg_url, ENGINE_LOCK_KEY)
    assert await holder.acquire()

    async def stub(*a):
        raise AssertionError("run_fn must not be called while an engine is alive")

    from tests.unit.test_supervisor import _hook_before_nth_session

    seeded: dict[str, uuid.UUID] = {}

    async def seed_start():
        async with factory() as db:
            await upsert_status(db, state="running")
        seeded["start"] = await _cmd(factory, "start", {"fresh": True})

    # Session 1 is boot's stale, session 2 boot's finalize-only settle.
    hooked = _hook_before_nth_session(factory, n=3, hook=seed_start)
    try:
        await run_supervisor(session_factory=hooked, run_fn=stub, max_loops=1,
                             poll_seconds=0.01, database_url=pg_url)
        async with factory() as db:
            start = await db.get(SimulationCommand, seeded["start"])
            status = await read_status(db)
        assert start.status == "failed" and start.result == "engine already running"
        assert status.state == "running", "the supervisor never writes idle/starting over a live engine"
    finally:
        await holder.release()
        await _cleanup(factory, list(seeded.values()), [])

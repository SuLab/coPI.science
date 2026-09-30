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


@pytest.mark.asyncio
async def test_a_sigterm_during_the_post_run_settle_reaches_the_supervisor(engine, pg_url, monkeypatch):
    """Correction 9: after run_fn returns, the supervisor's own handlers are back,
    so a SIGTERM during the post-run settle (a finalize can post to Slack) sets
    its shutdown flag instead of landing in the engine's spent handler."""
    import asyncio
    import os
    import signal

    import src.agent.supervisor as sup
    from tests.unit.test_supervisor import _hook_before_nth_session

    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded: dict[str, uuid.UUID] = {}
    engine_handler_hits: list[int] = []

    async def seed_start():
        seeded["start"] = await _cmd(factory, "start", {"fresh": True})

    async def run_fn(*a):
        # What _run_simulation does: the engine installs its own handlers.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: engine_handler_hits.append(1))

    real_settle = sup._settle_pending_stops
    seen: dict[str, bool] = {}

    async def settle(session_factory, database_url, *, finalize_only=False):
        if finalize_only:
            return await real_settle(session_factory, database_url, finalize_only=True)
        os.kill(os.getpid(), signal.SIGTERM)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if sup._shutdown or engine_handler_hits:
                break
        seen["shutdown"] = sup._shutdown
        return 0

    monkeypatch.setattr(sup, "_settle_pending_stops", settle)
    loop = asyncio.get_running_loop()
    # Session 1 is boot's stale, session 2 boot's finalize-only settle.
    hooked = _hook_before_nth_session(factory, n=3, hook=seed_start)
    try:
        await run_supervisor(session_factory=hooked, run_fn=run_fn, poll_seconds=0.01,
                             database_url=pg_url)
        assert seen == {"shutdown": True}
        assert engine_handler_hits == []
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        sup._shutdown = False
        await _cleanup(factory, list(seeded.values()), [])


@pytest.mark.asyncio
async def test_a_plain_stop_stays_pending_while_another_session_holds_the_engine_lock(engine, pg_url):
    """Correction 14: a CLI engine may hold the lock and owns that stop."""
    from src.agent.supervisor import _settle_pending_stops

    factory = async_sessionmaker(engine, expire_on_commit=False)
    holder = SessionAdvisoryLock(pg_url, ENGINE_LOCK_KEY)
    assert await holder.acquire()
    cmd_id = None
    try:
        cmd_id = await _cmd(factory, "stop", None)
        assert await _settle_pending_stops(factory, pg_url) == 0
        async with factory() as db:
            cmd = await db.get(SimulationCommand, cmd_id)
        assert cmd.status == "pending" and cmd.result is None
    finally:
        await holder.release()
        await _cleanup(factory, [cmd_id] if cmd_id else [], [])

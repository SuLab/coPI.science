"""The supervisor: an idle loop that starts a run only on an explicit
`simulation_commands` row and stales every pending command at boot (the
never-auto-start pin). See src/agent/supervisor.py.

Two test shapes are used, deliberately:

- (b)/(c)/(f) drive `run_supervisor` as a real background `asyncio.create_task`
  with `poll_seconds` small and `max_loops` left at its default (None,
  forever) — the loop is unbounded so it does not matter exactly which tick
  notices a command enqueued after boot; a completed run's own `break` (or,
  for (f), the run's completion) is what ends the task.
- (a)/(d)/(e) need EXACTLY one relevant loop iteration (`max_loops=1`), and a
  pending `stop` command can only be tested post-boot (boot's own
  `mark_pending_stale` has no `command=` filter, so it stales a pending stop
  exactly as it does a pending start — confirmed against
  tests/unit/test_simulation_control_service.py's
  test_mark_pending_stale_flips_pending_only_and_is_command_scoped). Seeding
  a command from a concurrently-running task can land on either side of that
  one iteration's DB read with no way to force the ordering, so (d)/(e) use a
  `session_factory` wrapper that runs a hook synchronously immediately before
  the loop's first post-boot session opens — after boot's own session has
  closed, before the one counted iteration's session opens. No sleeping, no
  polling, no flakiness.
"""
import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.supervisor import run_supervisor
from src.models import SimulationCommand
from src.services.simulation_control import read_status, upsert_status


def _hook_before_nth_session(real_factory, *, n, hook):
    """Wrap a session factory so `hook()` (a zero-arg async callable) is
    awaited to completion immediately before the Nth call to the factory
    opens its real session — a deterministic injection point between two
    specific `async with session_factory() as db:` calls in run_supervisor,
    with no race against the supervisor's own scheduling."""
    calls = {"count": 0}

    def factory():
        calls["count"] += 1
        call_n = calls["count"]

        class _HookedSession:
            async def __aenter__(self):
                if call_n == n:
                    await hook()
                self._cm = real_factory()
                return await self._cm.__aenter__()

            async def __aexit__(self, exc_type, exc, tb):
                return await self._cm.__aexit__(exc_type, exc, tb)

        return _HookedSession()

    return factory


async def _poll_until(factory, check, *, attempts=200, interval=0.02):
    """Poll `check(db)` (given a FRESH session each attempt, so we never read
    a stale identity-mapped row from a session with expire_on_commit=False)
    until it returns a truthy value. Raises AssertionError on exhaustion."""
    for _ in range(attempts):
        async with factory() as db:
            result = await check(db)
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


async def _clear_status(factory):
    async with factory() as db:
        existing = await read_status(db)
        if existing is not None:
            await db.delete(existing)
            await db.commit()


async def _cleanup(factory, *, command_ids=()):
    async with factory() as db:
        for cmd_id in command_ids:
            if cmd_id is None:
                continue
            row = await db.get(SimulationCommand, cmd_id)
            if row is not None:
                await db.delete(row)
        status = await read_status(db)
        if status is not None:
            await db.delete(status)
        await db.commit()


@pytest.mark.asyncio
async def test_boot_stales_a_pre_seeded_pending_start_and_never_calls_run_fn(engine):
    """(a) The never-auto-start pin: a start pending before the process ever
    ran must never be honored, however it got there (crash, redeploy, reboot)."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _clear_status(factory)

    async with factory() as db:
        cmd = SimulationCommand(command="start", payload={"fresh": True, "max_runtime": 30})
        db.add(cmd)
        await db.commit()
        cmd_id = cmd.id

    calls = []

    async def stub(*args):
        calls.append(args)

    try:
        await run_supervisor(session_factory=factory, run_fn=stub, max_loops=1)

        assert calls == []
        async with factory() as db:
            row = await db.get(SimulationCommand, cmd_id)
            assert row.status == "stale"
            assert row.result == "supervisor boot — request again"
    finally:
        await _cleanup(factory, command_ids=[cmd_id])


@pytest.mark.asyncio
async def test_start_enqueued_after_boot_runs_positionally_and_the_loop_exits(engine):
    """(b) A start enqueued only once the supervisor is already idle (i.e.
    definitely past boot staling) is claimed, run positionally, and the loop
    exits on its own because a completed run always breaks."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _clear_status(factory)

    calls = []

    async def stub(*args):
        calls.append(args)

    task = asyncio.create_task(
        run_supervisor(session_factory=factory, run_fn=stub, poll_seconds=0.05)
    )
    cmd_id = None
    try:
        status = await _poll_until(factory, lambda db: read_status(db))
        assert status.state == "idle"  # boot staling has committed

        async with factory() as db:
            cmd = SimulationCommand(command="start", payload={"fresh": True, "max_runtime": 60})
            db.add(cmd)
            await db.commit()
            cmd_id = cmd.id

        await asyncio.wait_for(task, timeout=10)

        assert calls == [(60, 0, False, False, True, False, False)]
        async with factory() as db:
            row = await db.get(SimulationCommand, cmd_id)
            assert row.status == "done"
            final_status = await read_status(db)
            assert final_status.state == "idle"
    finally:
        if not task.done():
            task.cancel()
        await _cleanup(factory, command_ids=[cmd_id])


@pytest.mark.asyncio
async def test_a_raising_run_fn_ends_the_command_failed_but_the_loop_still_exits(engine):
    """(c) Same shape as (b), but run_fn raises: the command is 'failed' with
    the exception text recorded, and the supervisor still exits cleanly
    rather than looping forever on a wedged run."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _clear_status(factory)

    calls = []

    async def stub(*args):
        calls.append(args)
        raise RuntimeError("boom")

    task = asyncio.create_task(
        run_supervisor(session_factory=factory, run_fn=stub, poll_seconds=0.05)
    )
    cmd_id = None
    try:
        status = await _poll_until(factory, lambda db: read_status(db))
        assert status.state == "idle"

        async with factory() as db:
            cmd = SimulationCommand(command="start", payload={"fresh": False, "max_runtime": 15})
            db.add(cmd)
            await db.commit()
            cmd_id = cmd.id

        await asyncio.wait_for(task, timeout=10)

        assert len(calls) == 1
        async with factory() as db:
            row = await db.get(SimulationCommand, cmd_id)
            assert row.status == "failed"
            assert "RuntimeError" in row.result
            assert "boom" in row.result
            final_status = await read_status(db)
            assert final_status.state == "idle"
    finally:
        if not task.done():
            task.cancel()
        await _cleanup(factory, command_ids=[cmd_id])


@pytest.mark.asyncio
async def test_pending_stop_with_nothing_running_finishes_done(engine):
    """(d) A stop with no live engine (no status row, or a stale one) is
    finished done — there is nothing to stop."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _clear_status(factory)

    seeded = {"cmd_id": None}

    async def seed_stop():
        async with factory() as db:
            cmd = SimulationCommand(command="stop", payload=None)
            db.add(cmd)
            await db.commit()
            seeded["cmd_id"] = cmd.id

    hooked_factory = _hook_before_nth_session(factory, n=2, hook=seed_stop)

    async def stub(*args):
        raise AssertionError("run_fn must not be called for a stop-only scenario")

    try:
        await run_supervisor(session_factory=hooked_factory, run_fn=stub, max_loops=1)

        async with factory() as db:
            row = await db.get(SimulationCommand, seeded["cmd_id"])
            assert row.status == "done"
            assert row.result == "nothing running"
    finally:
        await _cleanup(factory, command_ids=[seeded["cmd_id"]])


@pytest.mark.asyncio
async def test_pending_stop_beside_a_fresh_running_heartbeat_is_left_pending(engine):
    """(e) A stop alongside a FRESH 'running' status row (a live CLI-run
    engine) is left pending for that engine's own control poll to claim."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _clear_status(factory)

    seeded = {"cmd_id": None}

    async def seed_live_engine_and_stop():
        async with factory() as db:
            await upsert_status(db, state="running")  # fresh updated_at
            cmd = SimulationCommand(command="stop", payload=None)
            db.add(cmd)
            await db.commit()
            seeded["cmd_id"] = cmd.id

    hooked_factory = _hook_before_nth_session(factory, n=2, hook=seed_live_engine_and_stop)

    async def stub(*args):
        raise AssertionError("run_fn must not be called here")

    try:
        await run_supervisor(session_factory=hooked_factory, run_fn=stub, max_loops=1)

        async with factory() as db:
            row = await db.get(SimulationCommand, seeded["cmd_id"])
            assert row.status == "pending"
    finally:
        await _cleanup(factory, command_ids=[seeded["cmd_id"]])


@pytest.mark.asyncio
async def test_a_start_enqueued_while_a_run_is_live_is_staled_at_run_end(engine, monkeypatch):
    """(f) A start slipping in while a run is live (racing the page's
    is-running refusal) is never run — it is staled once the live run ends,
    and the live run's own stub is called exactly once.

    The 0042 partial unique index allows at most one PENDING `start` row at a
    time, and `claim_pending` never changes a claimed row's status away from
    'pending' — only `finish_command` does, after `run_fn` returns. So a
    second `start` genuinely cannot be INSERTed while the first is still
    live; the only window where a second row exists pending is the one this
    module's own post-run cleanup exists to close: between `finish_command`
    marking the first row done and `mark_pending_stale(command="start")`
    sweeping up whatever is pending at that instant. This patches
    `mark_pending_stale` to insert that second row immediately before the
    real sweep runs, landing exactly in that window without racing real
    asyncio scheduling."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _clear_status(factory)

    first_holder = {"id": None}
    second_holder = {"id": None}
    calls = []

    async def seed_first_start():
        async with factory() as db:
            cmd = SimulationCommand(command="start", payload={"fresh": True, "max_runtime": 10})
            db.add(cmd)
            await db.commit()
            first_holder["id"] = cmd.id

    hooked_factory = _hook_before_nth_session(factory, n=2, hook=seed_first_start)

    async def stub(*args):
        calls.append(args)

    import src.agent.supervisor as supervisor_module

    real_mark_pending_stale = supervisor_module.mark_pending_stale

    async def spying_mark_pending_stale(db, *, reason, command=None):
        if command == "start" and first_holder["id"] is not None:
            second = SimulationCommand(
                command="start", payload={"fresh": False, "max_runtime": 5}
            )
            db.add(second)
            await db.commit()
            second_holder["id"] = second.id
        return await real_mark_pending_stale(db, reason=reason, command=command)

    monkeypatch.setattr(supervisor_module, "mark_pending_stale", spying_mark_pending_stale)

    try:
        await run_supervisor(session_factory=hooked_factory, run_fn=stub, max_loops=1)

        assert calls == [(10, 0, False, False, True, False, False)]
        async with factory() as db:
            first = await db.get(SimulationCommand, first_holder["id"])
            second = await db.get(SimulationCommand, second_holder["id"])
            assert first.status == "done"
            assert second is not None
            assert second.status == "stale"
            assert second.result == "requested while a run was live — request again"
    finally:
        await _cleanup(factory, command_ids=[first_holder["id"], second_holder["id"]])

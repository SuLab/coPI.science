"""Spec P0-04: resuming a finalized run is refused, and a resume clears held_at."""
import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.main import RunFinalized, _reopen_run_for_resume
from src.agent.supervisor import run_supervisor
from src.models import SimulationCommand, SimulationRun
from tests.unit.test_supervisor import _cleanup, _clear_status, _poll_until


def _run(**kw):
    return SimulationRun(id=uuid.uuid4(), status="stopped", config={"max_proposals": 4}, **kw)


def test_a_finalized_run_is_refused():
    run = _run(finalized_at=datetime(2026, 9, 29, 12, 0, tzinfo=UTC))
    with pytest.raises(RunFinalized, match="finalized"):
        _reopen_run_for_resume(run, 0)
    assert run.status == "stopped", "nothing was reopened"


def test_a_resume_clears_held_at_and_keeps_todays_bookkeeping():
    run = _run(held_at=datetime(2026, 9, 29, 12, 0, tzinfo=UTC),
               ended_at=datetime(2026, 9, 29, 12, 1, tzinfo=UTC))
    assert _reopen_run_for_resume(run, 0) == 4, "max_proposals inherited as before"
    assert run.held_at is None
    assert run.status == "running" and run.ended_at is None
    assert run.config["max_proposals"] == 4


@pytest.mark.asyncio
async def test_the_supervisor_records_the_refused_start_as_failed(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _clear_status(factory)

    async def refuse(*args):
        raise RunFinalized("run x was finalized at 2026-09-29; start a fresh run")

    task = asyncio.create_task(
        run_supervisor(session_factory=factory, run_fn=refuse, poll_seconds=0.05)
    )
    cmd_id = None
    try:
        await _poll_until(factory, lambda db: _idle(db))
        async with factory() as db:
            cmd = SimulationCommand(command="start", payload={"fresh": False, "max_runtime": 0})
            db.add(cmd)
            await db.commit()
            cmd_id = cmd.id
        await asyncio.wait_for(task, timeout=10)
        async with factory() as db:
            row = await db.get(SimulationCommand, cmd_id)
            assert row.status == "failed"
            assert row.result.startswith("RunFinalized: run x was finalized")
    finally:
        if not task.done():
            task.cancel()
        await _cleanup(factory, command_ids=[cmd_id])


async def _idle(db):
    from src.services.simulation_control import read_status

    status = await read_status(db)
    return status is not None and status.state == "idle"

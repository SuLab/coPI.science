"""Spec P0-04: resuming a finalized run is refused. A resume that starts interviews clears
held_at; one that does not keeps the hold and reports it carried (independent-controls
spec §4.9)."""
import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.main import RunFinalized, _reopen_run_for_resume
from src.agent.supervisor import run_supervisor
from src.models import SimulationCommand, SimulationRun
from tests.unit.test_supervisor import _cleanup, _clear_status, _poll_until

HELD_AT = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
ENDED_AT = datetime(2026, 9, 29, 12, 1, tzinfo=UTC)


def _run(**kw):
    return SimulationRun(id=uuid.uuid4(), status="stopped", config={"max_proposals": 4}, **kw)


@pytest.mark.parametrize("starts_interviews", [True, False])
def test_a_finalized_run_is_refused(starts_interviews):
    run = _run(finalized_at=datetime(2026, 9, 29, 12, 0, tzinfo=UTC))
    with pytest.raises(RunFinalized, match="finalized"):
        _reopen_run_for_resume(run, starts_interviews=starts_interviews)
    assert run.status == "stopped", "nothing was reopened"


def test_a_resume_that_starts_interviews_clears_held_at():
    run = _run(held_at=HELD_AT, ended_at=ENDED_AT)
    assert _reopen_run_for_resume(run, starts_interviews=True) is False
    assert run.held_at is None
    assert run.status == "running" and run.ended_at is None


def test_a_resume_without_interviews_keeps_the_hold_and_carries_it():
    run = _run(held_at=HELD_AT, ended_at=ENDED_AT)
    assert _reopen_run_for_resume(run, starts_interviews=False) is True
    assert run.held_at == HELD_AT
    assert run.status == "running" and run.ended_at is None


def test_a_run_that_was_not_held_carries_no_hold():
    run = _run(ended_at=ENDED_AT)
    assert _reopen_run_for_resume(run, starts_interviews=False) is False
    assert run.held_at is None


def test_a_resume_no_longer_inherits_or_restamps_max_proposals():
    run = _run(ended_at=ENDED_AT)
    _reopen_run_for_resume(run, starts_interviews=True)
    assert run.config == {"max_proposals": 4}, "the stored config is left as it was"


@pytest.mark.asyncio
async def test_the_supervisor_records_the_refused_start_as_failed(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _clear_status(factory)

    async def refuse(*args, **kwargs):
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

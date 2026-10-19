"""scripts/repair_stale_running_runs.py: selects only old, non-live ``running`` runs, and
repairs them to ``stopped`` with ``ended_at`` set and nothing else changed."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from scripts.repair_stale_running_runs import find_stale_runs, repair
from src.models import SimulationProcessStatus, SimulationRun
from tests import factories

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=30)


async def _clear_status(db):
    status = await db.get(SimulationProcessStatus, 1)
    if status is not None:
        await db.delete(status)
        await db.flush()


async def test_selects_only_old_running_runs_that_are_not_live(db_session):
    await _clear_status(db_session)
    stale = await factories.make_simulation_run(db_session, started_at=OLD)
    recent = await factories.make_simulation_run(
        db_session, started_at=NOW - timedelta(hours=2)
    )
    # Old start, but an LLM call within the last day keeps it active.
    active = await factories.make_simulation_run(db_session, started_at=OLD)
    await factories.make_llm_call_log(
        db_session, run=active, created_at=NOW - timedelta(hours=3)
    )
    stopped = await factories.make_simulation_run(
        db_session, started_at=OLD, status="stopped"
    )
    live = await factories.make_simulation_run(db_session, started_at=OLD)
    db_session.add(SimulationProcessStatus(
        id=1, state="running", simulation_run_id=live.id, updated_at=NOW,
    ))
    await db_session.flush()

    found = {s.id for s in await find_stale_runs(db_session, now=NOW)}

    assert stale.id in found
    assert not found & {recent.id, active.id, stopped.id, live.id}


async def test_repair_sets_stopped_and_ended_at_only(db_session):
    await _clear_status(db_session)
    run = await factories.make_simulation_run(db_session, started_at=OLD)
    last_call = OLD + timedelta(hours=5)
    await factories.make_llm_call_log(db_session, run=run, created_at=OLD + timedelta(hours=1))
    await factories.make_llm_call_log(db_session, run=run, created_at=last_call)
    await db_session.flush()

    stale = [s for s in await find_stale_runs(db_session, now=NOW) if s.id == run.id]
    assert [s.ended_at for s in stale] == [last_call]

    assert await repair(db_session, stale) == 1

    row = (await db_session.execute(
        select(SimulationRun)
        .where(SimulationRun.id == run.id)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert row.status == "stopped"
    assert row.ended_at == last_call
    assert row.started_at == OLD
    assert row.finalized_at is None
    assert row.held_at is None
    assert await repair(db_session, stale) == 0


async def test_run_with_no_activity_ends_at_its_start(db_session):
    await _clear_status(db_session)
    run = await factories.make_simulation_run(db_session, started_at=OLD)

    stale = [s for s in await find_stale_runs(db_session, now=NOW) if s.id == run.id]

    assert len(stale) == 1
    assert stale[0].ended_at == OLD


async def test_a_run_with_messages_but_no_llm_calls_ends_at_its_last_message(db_session):
    await _clear_status(db_session)
    run = await factories.make_simulation_run(db_session, started_at=OLD)
    await factories.make_agent_message(db_session, run=run, created_at=OLD + timedelta(hours=3))

    stale = [s for s in await find_stale_runs(db_session, now=NOW) if s.id == run.id]

    assert len(stale) == 1
    assert stale[0].ended_at == OLD + timedelta(hours=3)

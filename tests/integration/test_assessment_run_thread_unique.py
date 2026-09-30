"""0055: one assessment row per (run, thread); NULL threads never conflict
(C24); the migration refuses while a run is live or duplicates remain."""
import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import OpportunityAssessment, SimulationRun

pytestmark = pytest.mark.integration

_MIGRATION = Path(__file__).resolve().parents[2] / "alembic/versions/0055_assessment_run_thread_unique.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("_m0055", _MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(run_id, thread_id):
    return OpportunityAssessment(
        id=uuid.uuid4(), simulation_run_id=run_id, agent_id="blackbird",
        channel_name="c", thread_id=thread_id,
    )


@pytest.mark.asyncio
async def test_the_constraint_rejects_a_second_row_for_one_thread(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.flush()
        db.add(_row(run.id, "t1"))
        await db.flush()
        db.add(_row(run.id, "t1"))
        with pytest.raises(IntegrityError, match="uq_opportunity_assessments_run_thread"):
            await db.flush()
        await db.rollback()


@pytest.mark.asyncio
async def test_null_threads_never_conflict(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.flush()
        db.add_all([_row(run.id, None), _row(run.id, None)])
        await db.flush()
        await db.rollback()


@pytest.mark.asyncio
async def test_precheck_refuses_duplicates_and_a_live_run(engine):
    mod = _load_migration()
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(text(
                "ALTER TABLE opportunity_assessments "
                "DROP CONSTRAINT uq_opportunity_assessments_run_thread"
            ))
            # Correction 6: "live" is checked before "duplicate", so first make the
            # id=1 status row idle and stale inside this transaction; a row an
            # earlier test left behind must not make the order matter.
            stale = datetime.now(UTC) - timedelta(seconds=300)
            await conn.execute(text(
                "INSERT INTO simulation_process_status(id, state, updated_at) "
                "VALUES (1, 'idle', :t) ON CONFLICT (id) DO UPDATE "
                "SET state = 'idle', updated_at = :t"
            ), {"t": stale})
            run_id = (await conn.execute(text(
                "INSERT INTO simulation_runs(id, started_at, status, total_messages, "
                "total_api_calls, config) VALUES (gen_random_uuid(), now(), 'stopped', 0, 0, '{}') "
                "RETURNING id"
            ))).scalar_one()
            for _ in range(2):
                await conn.execute(text(
                    "INSERT INTO opportunity_assessments(id, simulation_run_id, agent_id, "
                    "channel_name, thread_id, panel_incomplete, created_at) VALUES "
                    "(gen_random_uuid(), :r, 'blackbird', 'c', 'dup', false, now())"
                ), {"r": run_id})
            with pytest.raises(RuntimeError, match="duplicate"):
                await conn.run_sync(lambda sync_conn: mod.precheck(sync_conn))
            await conn.execute(text(
                "DELETE FROM opportunity_assessments WHERE simulation_run_id = :r"
            ), {"r": run_id})
            await conn.execute(text(
                "UPDATE simulation_process_status SET state = 'running', updated_at = now() "
                "WHERE id = 1"
            ))
            with pytest.raises(RuntimeError, match="live"):
                await conn.run_sync(lambda sync_conn: mod.precheck(sync_conn))
            await conn.execute(text(
                "UPDATE simulation_process_status SET updated_at = :t WHERE id = 1"
            ), {"t": stale})
            await conn.run_sync(lambda sync_conn: mod.precheck(sync_conn))
        finally:
            await trans.rollback()

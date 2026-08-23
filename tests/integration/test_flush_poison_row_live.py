"""The per-row recovery, against a real Postgres.

`tests/unit/test_flush_poison_row.py` pins the CONTRACT (row-level errors only,
a fresh session, a savepoint per row) against a fake session. A fake cannot
prove that `begin_nested()` actually recovers a Postgres transaction that a
constraint violation has already aborted — which is the whole mechanism — so
this drives the real thing.

Deliberately NOT on the `db_session` fixture: that session is the one whose
commit fails, and the fix's entire point is that the recovery opens a DIFFERENT
session. Rows written here are real, so the test cleans up after itself.
"""
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agent.simulation import SimulationEngine
from src.models import OpportunityAssessment, SimulationRun

pytestmark = pytest.mark.integration


def _row(run_id, agent_id: str) -> dict:
    return {
        "simulation_run_id": run_id,
        "agent_id": agent_id,
        "channel_name": "general",
    }


async def test_a_real_poison_row_is_isolated_by_a_savepoint(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun(status="running", config={})
        db.add(run)
        await db.commit()
        run_id = run.id

    try:
        eng = SimulationEngine(
            agents=[], slack_clients={},
            session_factory=factory, simulation_run_id=run_id,
        )
        # The middle row points at a simulation_run_id that does not exist, so
        # the batch commit dies on a real FK violation (IntegrityError) and
        # Postgres aborts the transaction. Only ROLLBACK TO SAVEPOINT gets the
        # rows after it written.
        eng._pending_assessments = [
            _row(run_id, "good-a"),
            _row(uuid.uuid4(), "poison"),
            _row(run_id, "good-b"),
        ]

        await eng._flush_pending_assessments()

        async with factory() as db:
            stored = sorted((await db.execute(
                select(OpportunityAssessment.agent_id).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().all())
        assert stored == ["good-a", "good-b"], (
            f"the good verdicts went down with the poison row: {stored}"
        )
        assert eng._pending_assessments == [], (
            "the poison row was re-queued, so every later flush would fail the "
            "same way and take the whole buffer with it"
        )
    finally:
        async with factory() as db:
            await db.execute(delete(OpportunityAssessment).where(
                OpportunityAssessment.simulation_run_id == run_id
            ))
            await db.execute(
                delete(SimulationRun).where(SimulationRun.id == run_id)
            )
            await db.commit()

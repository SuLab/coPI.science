"""Every assessment whose interview has ENDED gets exactly one
#assessments-summary headline, exactly once, durably.

See docs/audits/2026-08-29-lost-assessment-headlines/README.md. Production run
61ccad6d lost the rothstein verdict's headline (conditional, 2.85 — the run's
highest score) because the interview ended by `max_thread_messages` timeout
instead of by a terminal reply, and nothing announces on that path.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import OpportunityAssessment, SimulationRun

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_summary_posted_at_defaults_to_null(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.commit()
        run_id = run.id
    try:
        async with factory() as db:
            row = OpportunityAssessment(
                simulation_run_id=run_id, agent_id="blackbird",
                channel_name="general", thread_id="t1",
            )
            db.add(row)
            await db.commit()
            row_id = row.id
        async with factory() as db:
            stored = (await db.execute(
                select(OpportunityAssessment).where(OpportunityAssessment.id == row_id)
            )).scalar_one()
            assert stored.summary_posted_at is None, (
                "a fresh verdict has not been announced"
            )
    finally:
        async with factory() as db:
            stale = (await db.execute(
                select(SimulationRun).where(SimulationRun.id == run_id)
            )).scalar_one_or_none()
            if stale is not None:
                await db.delete(stale)
                await db.commit()

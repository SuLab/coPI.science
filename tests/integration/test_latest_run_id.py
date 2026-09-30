"""`latest_run_id` — the newest run, with a total order on ties."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete

from src.models import SimulationRun
from src.services.runs import latest_run_id
from tests import factories

pytestmark = pytest.mark.integration


async def test_a_deleted_run_is_never_returned(db_session):
    run = await factories.make_simulation_run(
        db_session, started_at=datetime(2999, 1, 1, tzinfo=UTC)
    )
    await db_session.flush()
    assert await latest_run_id(db_session) == run.id
    await db_session.execute(delete(SimulationRun).where(SimulationRun.id == run.id))
    assert await latest_run_id(db_session) != run.id


async def test_the_newest_started_at_wins(db_session):
    await factories.make_simulation_run(db_session, started_at=datetime(2999, 9, 1, tzinfo=UTC))
    newest = await factories.make_simulation_run(
        db_session, started_at=datetime(2999, 9, 2, tzinfo=UTC)
    )
    await db_session.flush()
    assert await latest_run_id(db_session) == newest.id


async def test_a_started_at_tie_is_broken_by_id_descending(db_session):
    at = datetime(2999, 12, 3, tzinfo=UTC)
    low = uuid.UUID("00000000-0000-4000-8000-000000000001")
    high = uuid.UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")
    await factories.make_simulation_run(db_session, id=low, started_at=at)
    await factories.make_simulation_run(db_session, id=high, started_at=at)
    await db_session.flush()
    assert await latest_run_id(db_session) == high

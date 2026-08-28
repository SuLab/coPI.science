"""A run row must say which rubric opened it: with pre-v3 assessment rows
purged, nothing in the DB could name a run's rubric at all (audit A5)."""
import pytest
from sqlalchemy import select

from src.agent.main import _open_fresh_run
from src.models import SimulationRun
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION

pytestmark = pytest.mark.integration


class _FixtureSessionFactory:
    """Same shim as test_state_rebuild.py: route the engine-owned session at
    the test's rolled-back session; __aexit__ must not close it."""

    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False


async def test_open_fresh_run_stamps_the_rubric(db_session):
    run_id = await _open_fresh_run(
        _FixtureSessionFactory(db_session), {"max_runtime": 0}
    )
    run = (
        await db_session.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )
    ).scalar_one()
    assert run.config["rubric_version"] == RUBRIC_VERSION
    assert run.config["rubric_content_hash"] == RUBRIC_CONTENT_HASH
    assert run.config["max_runtime"] == 0, "caller's config keys must survive"

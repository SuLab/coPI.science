"""Integration test for reconciling stale `running` SimulationRun rows at agent
startup (issue #25 D2). A crash / OOM-kill / `docker kill` leaves a run's status
stuck at "running" forever: neither --fresh (inserts a new row) nor resume
(repairs only the single latest row by started_at) ever touches an older
stale row.
"""

import pytest

from src.agent.main import _reconcile_stale_runs
from tests import factories

pytestmark = pytest.mark.integration


class _FixtureSessionFactory:
    """Route _reconcile_stale_runs' self-opened session at the rolled-back test
    session. __aexit__ must NOT close the fixture-owned session."""

    def __init__(self, session):
        self._s = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._s

    async def __aexit__(self, *exc):
        return False


async def test_reconcile_stops_every_other_running_run(db_session):
    stale = await factories.make_simulation_run(db_session, status="running")
    already_stopped = await factories.make_simulation_run(db_session, status="stopped")
    current = await factories.make_simulation_run(db_session, status="running")

    await _reconcile_stale_runs(_FixtureSessionFactory(db_session), current.id)

    await db_session.refresh(stale)
    await db_session.refresh(already_stopped)
    await db_session.refresh(current)
    assert stale.status == "stopped"
    assert stale.ended_at is not None
    assert already_stopped.status == "stopped"  # untouched, was already stopped
    assert current.status == "running"
    assert current.ended_at is None

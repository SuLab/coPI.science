import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.models import SimulationProcessStatus
from src.services.runs import latest_run_id, live_projects_run_id, runs_ordered
from src.services.simulation_control import HEARTBEAT_STALE_SECONDS
from tests import factories

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]


async def test_order_breaks_ties_by_id(db_session):
    t = datetime(2100, 1, 1, tzinfo=UTC)  # later than any other row in the DB
    a = await factories.make_simulation_run(db_session, id=uuid.UUID(int=1), started_at=t)
    b = await factories.make_simulation_run(db_session, id=uuid.UUID(int=2), started_at=t)
    runs = await runs_ordered(db_session)
    assert [r.id for r in runs[:2]] == [b.id, a.id]
    assert await latest_run_id(db_session) == b.id


def test_no_inline_run_ordering_left():
    offenders = []
    for rel in ("src/services/directory.py", "src/services/assessment_chat.py",
                "src/services/simulation_view.py", "src/routers/admin/simulation.py",
                "src/routers/workspace/simulation.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        if "SimulationRun.started_at.desc()" in text:
            offenders.append(rel)
    assert offenders == []


async def _heartbeat(db, run_id, *, detail, age_seconds=0.0, state="running"):
    row = await db.get(SimulationProcessStatus, 1)
    if row is None:
        row = SimulationProcessStatus(id=1)
        db.add(row)
    row.state = state
    row.simulation_run_id = run_id
    row.detail = detail
    row.updated_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    await db.flush()


def _projects(state):
    return {"systems": {"projects": {"system": "projects", "state": state}}}


@pytest.mark.parametrize("state", ["running", "winding_down"])
async def test_live_projects_run_id_while_projects_are_active(db_session, state):
    run = await factories.make_simulation_run(db_session)
    await _heartbeat(db_session, run.id, detail=_projects(state))
    assert await live_projects_run_id(db_session) == run.id


@pytest.mark.parametrize(
    "detail",
    [None, {}, {"systems": {}}, {"systems": {"interviews": {"state": "running"}}},
     {"systems": {"projects": "running"}}, _projects("ended"), _projects("starting")],
)
async def test_live_projects_run_id_none_unless_projects_are_active(db_session, detail):
    run = await factories.make_simulation_run(db_session)
    await _heartbeat(db_session, run.id, detail=detail)
    assert await live_projects_run_id(db_session) is None


async def test_live_projects_run_id_none_for_a_stale_or_stopped_heartbeat(db_session):
    run = await factories.make_simulation_run(db_session)
    await _heartbeat(
        db_session, run.id, detail=_projects("running"), age_seconds=HEARTBEAT_STALE_SECONDS + 5
    )
    assert await live_projects_run_id(db_session) is None
    await _heartbeat(db_session, run.id, detail=_projects("running"), state="idle")
    assert await live_projects_run_id(db_session) is None


async def test_live_projects_run_id_none_while_the_engine_stops(db_session):
    """A stop has ended every session; the heartbeat may still carry the last poll's
    ``systems`` until the engine exits, and the worker must not research meanwhile."""
    run = await factories.make_simulation_run(db_session)
    await _heartbeat(db_session, run.id, detail=_projects("running"), state="stopping")
    assert await live_projects_run_id(db_session) is None

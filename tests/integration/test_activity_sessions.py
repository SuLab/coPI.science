"""A run's Activity page lists the sessions its config records, and none when it has none."""
from datetime import UTC, datetime

import pytest

from src.models import USER_ROLE_MANAGER, SimulationProcessStatus, SimulationRun
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_SESSIONS = [
    {"system": "interviews", "state": "ended", "reason": "limit_count",
     "started_at": "2026-10-20T09:00:00+00:00", "ended_at": "2026-10-20T12:30:00+00:00",
     "limits": {"count": 20, "runtime_minutes": 240}, "count": 20, "open_work": 0,
     "failures_in_a_row": 0, "initial": True, "waiting_on": []},
    {"system": "projects", "state": "running", "reason": None,
     "started_at": "2026-10-20T10:00:00+00:00", "ended_at": None,
     "limits": {"count": 10, "runtime_minutes": 0}, "count": 3, "open_work": 2,
     "failures_in_a_row": 0, "initial": False, "waiting_on": ["gray"]},
]


async def _run(db_session, config, *, status="stopped"):
    ended_at = datetime.now(UTC) if status == "stopped" else None
    run = SimulationRun(status=status, ended_at=ended_at, config=config)
    db_session.add(run)
    await db_session.commit()
    return run


async def test_run_page_lists_its_sessions(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER,
                                        email="sess-a@example.org")
    run = await _run(db_session, {"sessions": _SESSIONS})
    resp = await client.get(f"/workspace/activity/{run.id}", headers=auth_headers(manager.id))
    assert resp.status_code == 200
    assert ">Sessions</h2>" in resp.text
    for text in ("Interviews", "20 proposals, 240 minutes", "reached its count",
                 "Projects", "10 finished projects"):
        assert text in resp.text
    # The projects session never recorded its end, and the run has stopped: it reads as
    # the run stopped, as a resume records it, never as still running.
    sessions = resp.text[resp.text.index(">Sessions</h2>"):]
    assert "the run stopped" in sessions


async def test_an_open_session_reads_running_only_while_its_run_is_live(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER,
                                        email="sess-c@example.org")
    run = await _run(db_session, {"sessions": _SESSIONS}, status="running")
    for live, expected in ((False, "the run stopped"), (True, ">running<")):
        # Live is the heartbeat rule (``live_run_id``): a fresh running heartbeat naming the run.
        status = await db_session.get(SimulationProcessStatus, 1) or SimulationProcessStatus(id=1)
        status.state = "running" if live else "idle"
        status.simulation_run_id = run.id
        status.updated_at = datetime.now(UTC)
        db_session.add(status)
        await db_session.commit()
        resp = await client.get(
            f"/workspace/activity/{run.id}", headers=auth_headers(manager.id),
        )
        sessions = resp.text[resp.text.index(">Sessions</h2>"):]
        assert expected in sessions, live


async def test_run_page_without_sessions_has_no_sessions_card(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER,
                                        email="sess-b@example.org")
    run = await _run(db_session, {})
    resp = await client.get(f"/workspace/activity/{run.id}", headers=auth_headers(manager.id))
    assert resp.status_code == 200
    assert ">Sessions</h2>" not in resp.text

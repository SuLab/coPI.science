from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, SimulationCommand, SimulationProcessStatus
from tests import factories
from tests.flash_support import session_flashes
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _alive(monkeypatch, value):
    import src.routers.admin.simulation as sim_routes

    async def _engine_alive(db):
        return value

    monkeypatch.setattr(sim_routes, "engine_alive", _engine_alive)
    import src.services.simulation_control as control

    monkeypatch.setattr(control, "engine_alive", _engine_alive)


async def test_stop_works_when_unresponsive(client, db_session, monkeypatch):
    _alive(monkeypatch, True)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email="live-a@example.org")
    db_session.add(SimulationProcessStatus(id=1, state="running",
                                           updated_at=datetime.now(UTC) - timedelta(minutes=10)))
    await db_session.commit()
    page = await client.get("/admin/simulation", headers=auth_headers(admin.id))
    assert "Unresponsive" in page.text and "Run in progress" in page.text
    resp = await client.post("/admin/simulation/stop", headers=auth_headers(admin.id), follow_redirects=False)
    assert "msg=" in resp.headers["location"]
    cmd = (await db_session.execute(select(SimulationCommand))).scalar_one()
    assert cmd.command == "stop"


async def test_stop_refused_when_no_engine_holds_the_lock(client, db_session, monkeypatch):
    """Review Focus 5."""
    _alive(monkeypatch, False)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email="live-b@example.org")
    db_session.add(SimulationProcessStatus(id=1, state="running", updated_at=datetime.now(UTC)))
    await db_session.commit()
    resp = await client.post("/admin/simulation/stop", headers=auth_headers(admin.id), follow_redirects=False)
    assert resp.headers["location"] == "/admin/simulation"
    assert session_flashes(resp) == [{"text": "Nothing is running.", "kind": "error"}]
    assert (await db_session.execute(select(SimulationCommand))).scalars().all() == []


async def test_start_refused_while_alive(client, db_session, monkeypatch):
    _alive(monkeypatch, True)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email="live-c@example.org")
    resp = await client.post("/admin/simulation/start", data={"fresh": "true"},
                             headers=auth_headers(admin.id), follow_redirects=False)
    assert session_flashes(resp) == [
        {"text": "A run is already starting or in progress.", "kind": "error"}
    ]


async def test_circuit_open_is_shown(client, db_session, monkeypatch):
    _alive(monkeypatch, True)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email="live-d@example.org")
    db_session.add(SimulationProcessStatus(id=1, state="running", updated_at=datetime.now(UTC),
                                           detail={"circuit_open": True}))
    await db_session.commit()
    page = await client.get("/admin/simulation", headers=auth_headers(admin.id))
    assert "LLM calls paused" in page.text

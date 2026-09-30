"""§8.2 Finalize run: the admin route, the held counts and the button."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    OpportunityAssessment,
    SimulationCommand,
    SimulationRun,
    ThreadDecision,
)
from src.services.headline_claims import held_headline_counts
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _stopped_run(db_session):
    run = SimulationRun(status="stopped", ended_at=datetime.now(UTC))
    db_session.add(run)
    await db_session.flush()
    for tid in ("open1", "open2", "ended1"):
        db_session.add(OpportunityAssessment(simulation_run_id=run.id, agent_id="blackbird",
                                             channel_name="c", thread_id=tid))
    db_session.add(OpportunityAssessment(simulation_run_id=run.id, agent_id="blackbird",
                                         channel_name="c", thread_id="posted",
                                         summary_posted_at=datetime.now(UTC)))
    db_session.add(ThreadDecision(simulation_run_id=run.id, thread_id="ended1", channel="c",
                                  agent_a="blackbird", agent_b="gordy", outcome="timeout"))
    await db_session.commit()
    return run


async def test_held_counts_match_a_fixture(db_session):
    run = await _stopped_run(db_session)
    assert await held_headline_counts(db_session, run.id) == (2, 1)


async def test_run_page_shows_the_button_and_counts(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email="fin-a@example.org")
    run = await _stopped_run(db_session)
    resp = await client.get(f"/admin/activity/{run.id}", headers=auth_headers(admin.id))
    assert resp.status_code == 200
    assert "Finalize run" in resp.text
    assert "2 open interviews" in resp.text and "1 ended" in resp.text


async def test_run_page_lists_in_doubt_claims(client, db_session):
    from datetime import timedelta

    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email="fin-e@example.org")
    run = await _stopped_run(db_session)
    stuck = OpportunityAssessment(simulation_run_id=run.id, agent_id="blackbird", channel_name="c",
                                  thread_id="stuck",
                                  summary_claimed_at=datetime.now(UTC) - timedelta(minutes=30))
    db_session.add(stuck)
    await db_session.commit()
    resp = await client.get(f"/admin/activity/{run.id}", headers=auth_headers(admin.id))
    assert "Headlines in doubt (1)" in resp.text and str(stuck.id) in resp.text


async def test_finalize_enqueues_a_stop_with_the_run_id(client, db_session, monkeypatch):
    import src.routers.admin.simulation as sim_routes

    async def _dead(db):
        return False

    monkeypatch.setattr(sim_routes, "engine_alive", _dead)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email="fin-b@example.org")
    run = await _stopped_run(db_session)
    resp = await client.post("/admin/simulation/finalize-run", data={"run_id": str(run.id)},
                             headers=auth_headers(admin.id), follow_redirects=False)
    assert resp.status_code == 302
    cmd = (await db_session.execute(select(SimulationCommand).where(
        SimulationCommand.status == "pending"))).scalar_one()
    assert cmd.command == "stop" and cmd.payload == {"finalize": True, "run_id": str(run.id)}


async def test_finalize_is_refused_while_an_engine_is_alive(client, db_session, monkeypatch):
    import src.routers.admin.simulation as sim_routes

    async def _alive(db):
        return True

    monkeypatch.setattr(sim_routes, "engine_alive", _alive)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email="fin-c@example.org")
    run = await _stopped_run(db_session)
    resp = await client.post("/admin/simulation/finalize-run", data={"run_id": str(run.id)},
                             headers=auth_headers(admin.id), follow_redirects=False)
    assert resp.status_code == 302 and "error=" in resp.headers["location"]
    assert (await db_session.execute(select(SimulationCommand))).scalars().all() == []


async def test_start_is_forced_fresh_when_the_latest_run_is_finalized(client, db_session, monkeypatch):
    import src.routers.admin.simulation as sim_routes

    async def _dead(db):
        return False

    monkeypatch.setattr(sim_routes, "engine_alive", _dead)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, email="fin-d@example.org")
    db_session.add(SimulationRun(status="stopped", finalized_at=datetime.now(UTC)))
    await db_session.commit()
    await client.post("/admin/simulation/start", data={"max_runtime": "0", "max_proposals": "0"},
                      headers=auth_headers(admin.id), follow_redirects=False)
    cmd = (await db_session.execute(select(SimulationCommand).where(
        SimulationCommand.command == "start"))).scalar_one()
    assert cmd.payload["fresh"] is True

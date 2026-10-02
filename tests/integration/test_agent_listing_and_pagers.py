"""D-20: the My Agents listing links only agents whose dashboard opens (active or
inactive); any other status used to bounce /agent/<id>/dashboard -> /agent -> the
same listing. D-26 (Task 2A-28): conversations page past the first 50 roots.
D-27 (Task 2A-29): the run timeline pager survives a page past the end."""

import pytest

from src.models import USER_ROLE_ADMIN, AgentDelegate
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_the_listing_does_not_link_an_agent_without_a_dashboard(client, db_session):
    pi = await factories.make_user(db_session)
    own = await factories.make_agent(db_session, user=pi, status="active")
    other = await factories.make_user(db_session)
    parked = await factories.make_agent(db_session, user=other, status="suspended")
    db_session.add(AgentDelegate(agent_registry_id=parked.id, user_id=pi.id))
    await db_session.flush()
    r = await client.get("/agent", headers=auth_headers(pi.id), follow_redirects=False)
    assert r.status_code == 200
    assert f'href="/agent/{own.agent_id}/dashboard"' in r.text
    assert f'href="/agent/{parked.agent_id}/dashboard"' not in r.text
    assert "Not available while suspended" in r.text


async def _roots(db, run, agent_id, n):
    for i in range(n):
        await factories.make_agent_message(
            db, run=run, agent_id=agent_id, channel_name="general", channel_id="C1",
            visibility="public", message_ts=f"9.{i:04d}", phase="new_post",
            content=f"ROOT-{i + 1:03d}", sender_name="PagedBot", posted_at=1000.0 + i,
        )


async def test_conversations_page_past_the_first_fifty_roots(client, db_session):
    from src.routers.agent_page import _ROOT_LIMIT

    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="paged", status="active")
    run = await factories.make_simulation_run(db_session)
    await _roots(db_session, run, "paged", _ROOT_LIMIT + 1)
    await db_session.flush()

    first = await client.get("/agent/paged/conversations", headers=auth_headers(pi.id))
    assert first.status_code == 200
    assert f"ROOT-{_ROOT_LIMIT + 1:03d}" in first.text and "ROOT-002" in first.text
    assert "ROOT-001" not in first.text
    assert 'href="?page=2"' in first.text

    second = await client.get("/agent/paged/conversations?page=2", headers=auth_headers(pi.id))
    assert "ROOT-001" in second.text and "ROOT-002" not in second.text
    assert 'href="?page=1"' in second.text and 'href="?page=3"' not in second.text


async def test_a_page_past_the_end_is_clamped_and_keeps_its_pager(client, db_session, monkeypatch):
    monkeypatch.setattr("src.services.directory.RUN_MESSAGES_PAGE_SIZE", 2)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session)
    for i in range(3):
        await factories.make_agent_message(
            db_session, run=run, agent_id="su", channel_name="general",
            message_ts=f"5.{i:04d}", phase="new_post",
        )
    r = await client.get(f"/admin/activity/{run.id}?page=99", headers=auth_headers(admin.id))
    assert r.status_code == 200
    assert "Page 2 of 2" in r.text
    assert 'href="?page=1"' in r.text


async def test_a_run_with_no_messages_shows_no_pager(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session)
    r = await client.get(f"/admin/activity/{run.id}?page=5", headers=auth_headers(admin.id))
    assert r.status_code == 200
    assert 'id="run-messages-pager"' not in r.text

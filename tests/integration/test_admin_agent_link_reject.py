"""C-06: POST /admin/agents/{id}/link refuses, with a message and nothing written,
a malformed or unknown user id, a user already linked to another agent, and a role
that cannot own a lab. C-29 (Task 2A-15): reject applies only to a pending request."""

import uuid

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AgentRegistry,
)
from tests import factories
from tests.integration._webui_helpers import follow
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _link(client, admin, agent, user_id):
    return await client.post(
        f"/admin/agents/{agent.id}/link", data={"user_id": user_id},
        headers=auth_headers(admin.id), follow_redirects=False,
    )


async def _linked_user(db, agent):
    return (await db.execute(
        select(AgentRegistry.user_id).where(AgentRegistry.id == agent.id)
    )).scalar_one()


@pytest.mark.parametrize(
    "value,message",
    [
        ("", "Choose a user to link."),
        ("not-a-uuid", "That user id is not valid."),
        (str(uuid.uuid4()), "No such user."),
    ],
)
async def test_link_refuses_a_missing_malformed_or_unknown_user(client, db_session, value, message):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    agent = await factories.make_agent(db_session, status="pending")
    r = await _link(client, admin, agent, value)
    assert r.status_code == 302
    assert await _linked_user(db_session, agent) is None
    page = await follow(client, r)
    assert message in page.text


async def test_link_refuses_a_user_already_linked_to_another_agent(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await factories.make_agent(db_session, user=pi)
    orphan = await factories.make_agent(db_session, status="pending")
    r = await _link(client, admin, orphan, str(pi.id))
    assert await _linked_user(db_session, orphan) is None
    page = await follow(client, r)
    assert "already linked to another agent" in page.text


@pytest.mark.parametrize("role", [USER_ROLE_MANAGER, USER_ROLE_REVIEWER])
async def test_link_refuses_a_role_that_cannot_own_a_lab(client, db_session, role):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    staff = await factories.make_user(db_session, user_role=role)
    agent = await factories.make_agent(db_session, status="pending")
    r = await _link(client, admin, agent, str(staff.id))
    assert await _linked_user(db_session, agent) is None
    page = await follow(client, r)
    assert "cannot own a lab" in page.text


async def test_link_links_an_unlinked_pi(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    agent = await factories.make_agent(db_session, status="pending")
    r = await _link(client, admin, agent, str(pi.id))
    assert r.status_code == 302
    assert await _linked_user(db_session, agent) == pi.id


async def _status_of(db, agent):
    return (await db.execute(
        select(AgentRegistry.status).where(AgentRegistry.id == agent.id)
    )).scalar_one()


async def test_reject_refuses_an_active_agent(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    agent = await factories.make_agent(db_session, status="active")
    r = await client.post(
        f"/admin/agents/{agent.id}/reject", headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert await _status_of(db_session, agent) == "active"
    page = await follow(client, r)
    assert "Only a pending request can be rejected" in page.text


async def test_reject_suspends_a_pending_request(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    agent = await factories.make_agent(db_session, status="pending")
    await client.post(
        f"/admin/agents/{agent.id}/reject", headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert await _status_of(db_session, agent) == "suspended"

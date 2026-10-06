"""D25 (spec 2026-10-05 §6.4): a role change that removes the PI surfaces is refused while
the account owns a lab agent that is active, pending or inactive."""
import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI
from src.services.user_roles import RoleChangeRefused, change_user_role
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("status", ["active", "pending", "inactive"])
async def test_an_owner_of_a_live_agent_cannot_become_a_manager(db_session, status):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await factories.make_agent(db_session, user=pi, status=status)
    with pytest.raises(RoleChangeRefused):
        await change_user_role(db_session, pi, USER_ROLE_MANAGER)
    assert pi.user_role == USER_ROLE_PI


async def test_a_suspended_agent_does_not_block(db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await factories.make_agent(db_session, user=pi, status="suspended")
    assert await change_user_role(db_session, pi, USER_ROLE_MANAGER) is True
    assert pi.user_role == USER_ROLE_MANAGER


async def test_pi_to_admin_keeps_the_surfaces_and_is_allowed(db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await factories.make_agent(db_session, user=pi, status="active")
    assert await change_user_role(db_session, pi, USER_ROLE_ADMIN) is True


async def test_the_admin_route_refuses_with_the_reason(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    agent = await factories.make_agent(db_session, user=pi, status="active")
    r = await client.post(f"/admin/users/{pi.id}/role", data={"user_role": USER_ROLE_MANAGER},
                          headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 400
    assert agent.agent_id in r.text
    await db_session.refresh(pi)
    assert pi.user_role == USER_ROLE_PI

"""M-08: a manager or reviewer has no lab, so the PI-only pages (whose POSTs
get_pi_user refuses) send each to its own landing page instead of rendering."""

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "role,path,target",
    [
        (USER_ROLE_MANAGER, "/profile", "/workspace/pis"),
        (USER_ROLE_MANAGER, "/profile/edit", "/workspace/pis"),
        (USER_ROLE_MANAGER, "/agent", "/workspace/pis"),
        (USER_ROLE_REVIEWER, "/profile", "/workspace/assessments"),
        (USER_ROLE_REVIEWER, "/profile/edit", "/workspace/assessments"),
        (USER_ROLE_REVIEWER, "/agent", "/workspace/assessments"),
    ],
)
async def test_staff_are_redirected_from_pi_only_pages(client, db_session, role, path, target):
    user = await factories.make_user(db_session, user_role=role)
    r = await client.get(path, headers=auth_headers(user.id), follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == target


@pytest.mark.parametrize("path", ["/profile", "/profile/edit", "/agent"])
async def test_an_admin_keeps_the_pi_pages(client, db_session, path):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_profile(db_session, user=admin)
    r = await client.get(path, headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 200

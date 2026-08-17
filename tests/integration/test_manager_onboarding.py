"""A manager is not a PI (D7): no onboarding, no profile pipeline, no PI nav.

An ADMIN is not a `pi` either, but keeps all three — the tests here pin that
distinction, because collapsing it to "non-PI" locked admins out of their own
profile.
"""

import pytest
from sqlalchemy import func, select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, Job
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_manager_visiting_onboarding_is_bounced_to_the_manager_view(
    client, db_session
):
    mgr = await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, onboarding_complete=False
    )
    r = await client.get(
        "/onboarding", headers=auth_headers(mgr.id), follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/manager/pis"


async def test_manager_visiting_onboarding_enqueues_no_profile_job(client, db_session):
    """F8: onboarding.py:75 self-heals by enqueuing generate_profile for any
    allowed user with no profile. A manager must not trip it."""
    mgr = await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, onboarding_complete=False
    )
    await client.get("/onboarding", headers=auth_headers(mgr.id), follow_redirects=False)
    count = await db_session.scalar(
        select(func.count(Job.id)).where(
            Job.user_id == mgr.id, Job.type == "generate_profile"
        )
    )
    assert count == 0


@pytest.mark.parametrize("role", [USER_ROLE_PI, USER_ROLE_ADMIN])
async def test_a_non_manager_visiting_onboarding_still_gets_the_self_heal(
    client, db_session, role
):
    """The guard must narrow the self-heal to managers, not to non-PIs.

    The admin case is the regression: an admin is not a `pi` either, so a
    `user_role == 'pi'` self-heal left them on "Building Your Profile" with no
    job, no profile and (per the template) no retry control — the exact spin
    the self-heal exists to prevent.
    """
    u = await factories.make_user(
        db_session, user_role=role, onboarding_complete=False
    )
    await client.get("/onboarding", headers=auth_headers(u.id), follow_redirects=False)
    count = await db_session.scalar(
        select(func.count(Job.id)).where(
            Job.user_id == u.id, Job.type == "generate_profile"
        )
    )
    assert count == 1


async def test_an_admin_with_incomplete_onboarding_is_not_locked_out(client, db_session):
    """templates/base.html still offers admins the My Profile link, and
    /profile bounces anyone with onboarding_complete=False to /onboarding. A
    `user_role != 'pi'` bounce there therefore sent admins on to /manager/pis
    forever: the page whose form is the ONLY writer of onboarding_complete was
    unreachable, so the state could never be cleared.

    Both hops are asserted. /onboarding must render (not deflect), and the
    /profile entry point must arrive there rather than at /manager/pis — a fix
    applied to only one of the two would still leave the link in the nav
    broken.
    """
    admin = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, onboarding_complete=False
    )
    direct = await client.get(
        "/onboarding", headers=auth_headers(admin.id), follow_redirects=False
    )
    assert direct.status_code == 200, "an admin was deflected out of onboarding"
    assert "Building Your Profile" in direct.text

    cookie_value = auth_headers(admin.id)["Cookie"].split("=", 1)[1]
    client.cookies.set("copi-session", cookie_value)
    followed = await client.get("/profile", follow_redirects=True)
    assert followed.status_code == 200
    assert str(followed.url).endswith("/onboarding"), (
        "the My Profile link still dead-ends for an admin mid-onboarding"
    )


async def test_manager_profile_url_bounce_terminates(client, db_session):
    """manager -> /profile -> /onboarding -> /manager/pis, with no loop."""
    mgr = await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, onboarding_complete=False
    )
    # httpx strips a manually-set Cookie header on every redirect hop and
    # rebuilds "Cookie" from the client's cookie jar instead
    # (httpx._client.Client._redirect_headers unconditionally pops it), so a
    # header-only auth_headers() cookie silently disappears on the second hop
    # of this two-hop chain. Seed the jar directly so follow_redirects=True
    # actually exercises both hops instead of bouncing to /login on hop two.
    cookie_value = auth_headers(mgr.id)["Cookie"].split("=", 1)[1]
    client.cookies.set("copi-session", cookie_value)
    r = await client.get("/profile", follow_redirects=True)
    assert r.status_code == 200
    assert str(r.url).endswith("/manager/pis")


async def test_manager_nav_hides_the_pi_surfaces(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    body = (await client.get("/manager/pis", headers=auth_headers(mgr.id))).text
    assert "My Agent" not in body
    assert "My Profile" not in body
    assert "Settings" in body       # email preferences stay available to everyone
    assert "Manager" in body


async def test_pi_nav_is_unchanged(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    body = (await client.get("/profile", headers=auth_headers(pi.id))).text
    assert "My Agent" in body
    assert "My Profile" in body
    assert "Manager" not in body

"""A manager is not a PI (D7): no onboarding, no profile pipeline, no PI nav."""

import pytest
from sqlalchemy import func, select

from src.models import USER_ROLE_MANAGER, USER_ROLE_PI, Job
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


async def test_pi_visiting_onboarding_still_gets_the_self_heal(client, db_session):
    """The guard must narrow the self-heal, not delete it."""
    pi = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, onboarding_complete=False
    )
    await client.get("/onboarding", headers=auth_headers(pi.id), follow_redirects=False)
    count = await db_session.scalar(
        select(func.count(Job.id)).where(
            Job.user_id == pi.id, Job.type == "generate_profile"
        )
    )
    assert count == 1


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

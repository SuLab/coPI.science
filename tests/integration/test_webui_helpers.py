"""The Phase 2 helpers mint a session the app accepts and follow a redirect with
the session it set (which is where a flash lives)."""

import pytest

from src.models import USER_ROLE_ADMIN
from tests import factories
from tests.integration._webui_helpers import follow, impersonation_headers, session_from
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_impersonation_headers_make_an_impersonated_session(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, name="Worn Pi")
    r = await client.get("/profile", headers=impersonation_headers(admin.id, pi.id))
    assert r.status_code == 200
    assert "Viewing as Worn Pi" in r.text


async def test_a_session_without_the_expiry_key_is_not_impersonated(client, db_session):
    """Control for the helper above: the id alone (no expiry) must not impersonate."""
    from tests.session_support import session_headers

    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, name="Unexpired Pi")
    headers = session_headers(admin.id, impersonate_user_id=str(pi.id))
    r = await client.get("/profile", headers=headers)
    assert "Viewing as Unexpired Pi" not in r.text


async def test_follow_carries_the_session_the_redirect_set(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session, name="Followed Pi")
    r = await client.post(
        "/admin/impersonate", data={"orcid": pi.orcid},
        headers=auth_headers(admin.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert session_from(r)["Cookie"].split("=", 1)[0] == auth_headers(admin.id)["Cookie"].split("=", 1)[0]
    page = await follow(client, r)
    assert page.status_code in (200, 302)

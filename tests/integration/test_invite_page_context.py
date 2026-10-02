"""FN-08: invite pages are rendered with the same current_user / banner context
as every other page, so a signed-in user sees their own nav, not "Sign in"."""

import pytest

from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def test_a_signed_in_user_sees_their_nav_on_an_invite_error_page(client, db_session):
    pi = await factories.make_user(db_session, name="Inviteviewer Pi")
    r = await client.get("/invite/no-such-token", headers=auth_headers(pi.id))
    assert r.status_code == 200
    assert "Inviteviewer Pi" in r.text
    assert ">Sign in</a>" not in r.text


async def test_a_visitor_still_sees_sign_in(client):
    r = await client.get("/invite/no-such-token")
    assert r.status_code == 200
    assert ">Sign in</a>" in r.text

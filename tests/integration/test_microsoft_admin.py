"""Admins see and remove a user's Microsoft sign-in; removal ends every session."""
import logging
import uuid

import pytest
from sqlalchemy import func, select

from src.models import UserMicrosoftIdentity
from tests import factories
from tests.oidc_fakes import OID, TENANT, make_config
from tests.session_support import session_headers

pytestmark = pytest.mark.integration


@pytest.fixture
def on(asgi_app):
    asgi_app.state.microsoft_sign_in = make_config()


async def _linked_user(db_session, **overrides):
    user = await factories.make_user(db_session, **overrides)
    db_session.add(UserMicrosoftIdentity(
        user_id=user.id, tenant_id=uuid.UUID(TENANT), object_id=uuid.UUID(OID),
        sign_in_name="staff@example.edu",
    ))
    await db_session.flush()
    return user


async def _admin(db_session):
    return await factories.make_user(db_session, is_admin=True)


async def _link_count(db_session):
    return await db_session.scalar(select(func.count()).select_from(UserMicrosoftIdentity))


async def test_an_admin_removes_a_link_and_ends_the_users_sessions(client, db_session, on, caplog):
    admin, user = await _admin(db_session), await _linked_user(db_session)
    old_session = session_headers(user.id)
    assert (await client.get("/settings", headers=old_session)).status_code == 200
    with caplog.at_level(logging.INFO, logger="src.routers.admin"):
        r = await client.post(f"/admin/users/{user.id}/microsoft/unlink",
                              headers=session_headers(admin.id))
    assert r.status_code == 302 and r.headers["location"] == f"/admin/users/{user.id}"
    assert await _link_count(db_session) == 0
    assert (
        f"Admin {admin.id} removed the Microsoft sign-in of user {user.id} (tid={TENANT})"
        in caplog.text
    )
    r = await client.get("/settings", headers=old_session)
    assert r.status_code == 302 and r.headers["location"].startswith("/login")


async def test_an_admin_removing_their_own_link_is_signed_out_everywhere(client, db_session, on):
    admin = await _linked_user(db_session, is_admin=True)
    old_session = session_headers(admin.id)
    r = await client.post(f"/admin/users/{admin.id}/microsoft/unlink", headers=old_session)
    assert r.status_code == 302
    assert await _link_count(db_session) == 0
    r = await client.get("/settings", headers=old_session)
    assert r.status_code == 302 and r.headers["location"].startswith("/login")


async def test_removing_a_missing_link_logs_nothing(client, db_session, on, caplog):
    admin = await _admin(db_session)
    user = await factories.make_user(db_session)
    with caplog.at_level(logging.INFO, logger="src.routers.admin"):
        r = await client.post(f"/admin/users/{user.id}/microsoft/unlink",
                              headers=session_headers(admin.id))
    assert r.status_code == 302
    assert "removed the Microsoft sign-in" not in caplog.text
    # The user's session is untouched when there was nothing to remove.
    assert (await client.get("/settings", headers=session_headers(user.id))).status_code == 200


async def test_non_admins_impersonation_and_feature_off_are_refused(client, db_session, asgi_app):
    admin, user = await _admin(db_session), await _linked_user(db_session)
    path = f"/admin/users/{user.id}/microsoft/unlink"
    assert (await client.post(path, headers=session_headers(admin.id))).status_code == 404
    asgi_app.state.microsoft_sign_in = make_config()
    plain = await factories.make_user(db_session)
    assert (await client.post(path, headers=session_headers(plain.id))).status_code == 403
    r = await client.post(path, headers=session_headers(admin.id, impersonate=plain.id))
    assert r.status_code == 403
    other_admin = await _admin(db_session)  # passes get_admin_user; refuse_impersonation stops it
    r = await client.post(path, headers=session_headers(admin.id, impersonate=other_admin.id))
    assert r.status_code == 403
    assert await _link_count(db_session) == 1


async def test_an_unknown_user_is_404(client, db_session, on):
    admin = await _admin(db_session)
    r = await client.post(f"/admin/users/{uuid.uuid4()}/microsoft/unlink",
                          headers=session_headers(admin.id))
    assert r.status_code == 404


async def test_the_admin_user_page_shows_the_link_only_when_on(client, db_session, asgi_app):
    admin, user = await _admin(db_session), await _linked_user(db_session)
    path = f"/admin/users/{user.id}"
    r = await client.get(path, headers=session_headers(admin.id))
    assert r.status_code == 200 and "microsoft-sign-in" not in r.text
    asgi_app.state.microsoft_sign_in = make_config()
    r = await client.get(path, headers=session_headers(admin.id))
    assert "staff@example.edu" in r.text and TENANT in r.text
    assert "Remove Microsoft sign-in" in r.text
    linked_at = await db_session.scalar(
        select(UserMicrosoftIdentity.linked_at).where(UserMicrosoftIdentity.user_id == user.id)
    )
    block = r.text.split('id="microsoft-sign-in"', 1)[1].split("</form>", 1)[0]
    assert linked_at.strftime("%b %d, %Y") in block and "Never" in block

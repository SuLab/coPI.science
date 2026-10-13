"""The Settings section and the user's own unlink (spec 2026-10-13 §5.3, §5.7, D11)."""
import time
import uuid

import pytest
from sqlalchemy import func, select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, User, UserMicrosoftIdentity
from tests import factories
from tests.oidc_fakes import OID, TENANT, make_config
from tests.session_support import session_from_response, session_headers

pytestmark = pytest.mark.integration


@pytest.fixture
def on(asgi_app):
    asgi_app.state.microsoft_sign_in = make_config()


async def _user(db_session, role=USER_ROLE_MANAGER):
    return await factories.make_user(db_session, user_role=role)


async def _link(db_session, user):
    db_session.add(UserMicrosoftIdentity(user_id=user.id, tenant_id=uuid.UUID(TENANT),
                                         object_id=uuid.UUID(OID), sign_in_name="staff@example.edu"))
    await db_session.flush()


def _fresh(user, **extra):
    """A session that began with a proven ORCID re-sign-in 10 s ago (D10)."""
    now = int(time.time())
    return session_headers(user.id, **{"signed_in_at": now - 10, "signed_in_via": "orcid",
                                       "orcid_reauth_at": now - 10, **extra})


async def test_the_section_is_hidden_when_off_for_pis_and_while_impersonating(client, db_session, asgi_app):
    manager = await _user(db_session)
    assert "Microsoft sign-in" not in (await client.get("/settings", headers=_fresh(manager))).text
    asgi_app.state.microsoft_sign_in = make_config()
    pi = await _user(db_session, USER_ROLE_PI)
    assert "Microsoft sign-in" not in (await client.get("/settings", headers=_fresh(pi))).text
    admin = await _user(db_session, USER_ROLE_ADMIN)
    r = await client.get("/settings", headers=_fresh(admin, impersonate=manager.id))
    assert "Microsoft sign-in" not in r.text


async def test_a_fresh_orcid_session_sees_the_link_button(client, db_session, on):
    r = await client.get("/settings", headers=_fresh(await _user(db_session)))
    assert 'href="/auth/microsoft/link"' in r.text


async def test_an_ordinary_orcid_sign_in_must_sign_in_again(client, db_session, on):
    user = await _user(db_session)
    r = await client.get("/settings", headers=session_headers(
        user.id, signed_in_at=int(time.time()) - 10, signed_in_via="orcid"))
    assert 'href="/login/start?reauth=1"' in r.text
    assert 'href="/auth/microsoft/link"' not in r.text


async def test_a_pre_deploy_session_must_sign_in_again(client, db_session, on):
    user = await _user(db_session)
    r = await client.get("/settings", headers=session_headers(user.id))
    assert 'href="/login/start?reauth=1"' in r.text
    assert 'href="/auth/microsoft/link"' not in r.text
    r = await client.get("/auth/microsoft/link", headers=session_headers(user.id))
    assert r.headers["location"] == "/settings"


async def test_a_linked_account_shows_its_name_and_the_unlink_form(client, db_session, on):
    user = await _user(db_session)
    await _link(db_session, user)
    r = await client.get("/settings", headers=_fresh(user))
    assert "staff@example.edu" in r.text
    assert 'action="/settings/microsoft/unlink"' in r.text


async def test_unlink_removes_the_link_and_ends_every_session(client, db_session, on):
    user = await _user(db_session)
    await _link(db_session, user)
    r = await client.post("/settings/microsoft/unlink", headers=session_headers(user.id))
    assert r.status_code == 302 and r.headers["location"] == "/login"
    session = session_from_response(r)
    assert "user_id" not in session and session["_flashes"][0]["kind"] == "success"
    assert await db_session.scalar(select(func.count()).select_from(UserMicrosoftIdentity)) == 0
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.session_epoch == 1
    old = await client.get("/settings", headers=session_headers(user.id))  # epoch 0: revoked
    assert old.status_code == 302 and old.headers["location"].startswith("/login")


async def test_unlink_ends_the_session_at_any_epoch(client, db_session, on):
    user = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, session_epoch=3)
    await _link(db_session, user)
    r = await client.post("/settings/microsoft/unlink", headers=session_headers(user.id, epoch=3))
    assert r.headers["location"] == "/login"
    assert "user_id" not in session_from_response(r)
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.session_epoch == 4


async def test_unlink_ends_the_session_when_a_concurrent_bump_got_there_first(
        client, db_session, on, monkeypatch):
    from src.routers import settings as settings_router
    from src.services.session_epoch import bump_session_epoch

    user = await _user(db_session)
    await _link(db_session, user)
    real_unlink = settings_router.unlink

    async def unlink_after_another_devices_logout(db, user_id):
        await bump_session_epoch(db, user_id)
        return await real_unlink(db, user_id)

    monkeypatch.setattr(settings_router, "unlink", unlink_after_another_devices_logout)
    r = await client.post("/settings/microsoft/unlink", headers=session_headers(user.id))
    assert r.headers["location"] == "/login" and "user_id" not in session_from_response(r)
    assert await db_session.scalar(select(func.count()).select_from(UserMicrosoftIdentity)) == 0
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.session_epoch == 1  # the conditional bump saw 1 != 0 and added nothing


async def test_a_non_admins_impersonation_cookie_only_reaches_their_own_link(client, db_session, on):
    victim, attacker = await _user(db_session), await _user(db_session)
    await _link(db_session, victim)
    r = await client.post("/settings/microsoft/unlink",
                          headers=session_headers(attacker.id, impersonate=victim.id))
    assert r.headers["location"] == "/settings"  # acted as the attacker, who has no link
    assert await db_session.scalar(select(func.count()).select_from(UserMicrosoftIdentity)) == 1


async def test_unlink_redirects_a_logged_out_caller_and_changes_nothing(client, db_session, on):
    user = await _user(db_session)
    await _link(db_session, user)
    client.cookies.clear()
    r = await client.post("/settings/microsoft/unlink")
    assert r.status_code == 302 and r.headers["location"].startswith("/login")
    assert await db_session.scalar(select(func.count()).select_from(UserMicrosoftIdentity)) == 1


async def test_unlink_with_nothing_linked_changes_nothing(client, db_session, on):
    user = await _user(db_session)
    r = await client.post("/settings/microsoft/unlink", headers=session_headers(user.id))
    assert r.headers["location"] == "/settings"
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    assert refreshed.session_epoch in (None, 0)


async def test_unlink_is_404_when_off_and_403_while_impersonating(client, db_session, asgi_app):
    user = await _user(db_session)
    assert (await client.post("/settings/microsoft/unlink", headers=session_headers(user.id))).status_code == 404
    asgi_app.state.microsoft_sign_in = make_config()
    admin = await _user(db_session, USER_ROLE_ADMIN)
    r = await client.post("/settings/microsoft/unlink", headers=session_headers(admin.id, impersonate=user.id))
    assert r.status_code == 403

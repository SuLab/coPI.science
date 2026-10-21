"""The Settings section and the user's own unlink (spec
docs/specs/2026-10-21-microsoft-sign-in-design.md §5.4, §5.8)."""

import logging
import time
import uuid

import pytest
from sqlalchemy import func, select

from src.models import AgentDelegate, User, UserMicrosoftIdentity
from tests import factories
from tests.oidc_fakes import OID, TENANT, make_config
from tests.session_support import session_from_response, session_headers

pytestmark = pytest.mark.integration


@pytest.fixture
def on(asgi_app):
    asgi_app.state.microsoft_sign_in = make_config()


async def _link(db_session, user):
    db_session.add(
        UserMicrosoftIdentity(
            user_id=user.id,
            tenant_id=uuid.UUID(TENANT),
            object_id=uuid.UUID(OID),
            sign_in_name="staff@example.edu",
        )
    )
    await db_session.flush()


def _fresh(user, **extra):
    """A session that began with a proven ORCID re-sign-in 10 s ago."""
    now = int(time.time())
    return session_headers(
        user.id,
        **{"signed_in_at": now - 10, "signed_in_via": "orcid", "orcid_reauth_at": now - 10, **extra},
    )


async def _links(db_session) -> int:
    return await db_session.scalar(select(func.count()).select_from(UserMicrosoftIdentity))


async def _epoch(db_session, user) -> int | None:
    refreshed = await db_session.get(User, user.id)
    await db_session.refresh(refreshed)
    return refreshed.session_epoch


async def test_the_section_is_hidden_when_off_and_while_impersonating(client, db_session, asgi_app):
    pi = await factories.make_user(db_session)
    assert "Microsoft sign-in" not in (await client.get("/settings", headers=_fresh(pi))).text
    asgi_app.state.microsoft_sign_in = make_config()
    admin = await factories.make_user(db_session, is_admin=True)
    r = await client.get("/settings", headers=_fresh(admin, impersonate=pi.id))
    assert r.status_code == 200 and "Microsoft sign-in" not in r.text
    # An impersonation cookie that names no user still hides it from the admin.
    r = await client.get("/settings", headers=_fresh(admin, impersonate=uuid.uuid4()))
    assert r.status_code == 200 and "Microsoft sign-in" not in r.text
    # Control: the same admin without the cookie sees it.
    assert "Microsoft sign-in" in (await client.get("/settings", headers=_fresh(admin))).text


async def test_the_section_shows_for_a_pi_and_a_delegate(client, db_session, on):
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi)
    delegate = await factories.make_user(db_session, name="Delegate Only")
    db_session.add(AgentDelegate(agent_registry_id=agent.id, user_id=delegate.id))
    await db_session.flush()
    for user in (pi, delegate):
        r = await client.get("/settings", headers=_fresh(user))
        assert "Microsoft sign-in" in r.text
        assert 'href="/auth/microsoft/link"' in r.text


async def test_a_fresh_orcid_session_sees_the_link_button(client, db_session, on):
    r = await client.get("/settings", headers=_fresh(await factories.make_user(db_session)))
    assert 'href="/auth/microsoft/link"' in r.text
    assert 'href="/login/start?reauth=1"' not in r.text


async def test_an_ordinary_orcid_sign_in_must_sign_in_again(client, db_session, on):
    user = await factories.make_user(db_session)
    r = await client.get(
        "/settings",
        headers=session_headers(
            user.id, signed_in_at=int(time.time()) - 10, signed_in_via="orcid"
        ),
    )
    assert 'href="/login/start?reauth=1"' in r.text
    assert 'href="/auth/microsoft/link"' not in r.text


async def test_a_pre_deploy_session_must_sign_in_again(client, db_session, on):
    user = await factories.make_user(db_session)
    r = await client.get("/settings", headers=session_headers(user.id))
    assert r.status_code == 200
    assert 'href="/login/start?reauth=1"' in r.text
    assert 'href="/auth/microsoft/link"' not in r.text
    r = await client.get("/auth/microsoft/link", headers=session_headers(user.id))
    assert r.status_code == 302 and r.headers["location"] == "/settings?microsoft=link_refused"


async def test_a_linked_account_shows_its_name_date_and_the_unlink_form(client, db_session, on):
    user = await factories.make_user(db_session)
    await _link(db_session, user)
    r = await client.get("/settings", headers=_fresh(user))
    assert "staff@example.edu" in r.text
    assert "Last used:" in r.text and "never" in r.text
    assert 'data-utc="' in r.text.split('id="microsoft-sign-in"')[1]
    assert 'action="/settings/microsoft/unlink"' in r.text
    onsubmit = r.text.split('action="/settings/microsoft/unlink"')[1].split("onsubmit=")[1]
    handler = onsubmit.split(">")[0]
    assert "staff@example.edu" not in handler and user.name not in handler
    assert "Remove Microsoft sign-in? You will be signed out everywhere." in handler
    assert 'href="/auth/microsoft/link"' not in r.text


async def test_unlink_removes_the_link_and_ends_every_session(client, db_session, on, caplog):
    user = await factories.make_user(db_session)
    await _link(db_session, user)
    with caplog.at_level(logging.INFO, logger="src.routers.settings"):
        r = await client.post("/settings/microsoft/unlink", headers=session_headers(user.id))
    assert r.status_code == 302 and r.headers["location"] == "/login?error=microsoft_unlinked"
    assert any(str(user.id) in rec.getMessage() and f"tid={TENANT}" in rec.getMessage()
               for rec in caplog.records if rec.name == "src.routers.settings")
    assert "user_id" not in session_from_response(r)
    assert await _links(db_session) == 0
    assert await _epoch(db_session, user) == 1
    # Another device's session, issued at epoch 0, is revoked too.
    client.cookies.clear()
    old = await client.get("/settings", headers=session_headers(user.id))
    assert old.status_code == 302 and old.headers["location"].startswith("/login")


async def test_unlink_ends_the_session_at_any_epoch(client, db_session, on):
    user = await factories.make_user(db_session, session_epoch=3)
    await _link(db_session, user)
    r = await client.post("/settings/microsoft/unlink", headers=session_headers(user.id, epoch=3))
    assert r.headers["location"] == "/login?error=microsoft_unlinked"
    assert "user_id" not in session_from_response(r)
    assert await _epoch(db_session, user) == 4


async def test_unlink_ends_the_session_when_a_concurrent_bump_got_there_first(
    client, db_session, on, monkeypatch
):
    from src.routers import settings as settings_router
    from src.services.session_epoch import bump_session_epoch

    user = await factories.make_user(db_session)
    await _link(db_session, user)
    real_unlink = settings_router.unlink

    async def unlink_after_another_devices_logout(db, user_id):
        await bump_session_epoch(db, user_id)
        return await real_unlink(db, user_id)

    monkeypatch.setattr(settings_router, "unlink", unlink_after_another_devices_logout)
    r = await client.post("/settings/microsoft/unlink", headers=session_headers(user.id))
    assert r.headers["location"] == "/login?error=microsoft_unlinked"
    assert "user_id" not in session_from_response(r)
    assert await _links(db_session) == 0
    # The unlink bumps again: a session issued at epoch 1 in between (say, a Microsoft sign-in
    # through the link being removed) must not survive it.
    assert await _epoch(db_session, user) == 2
    client.cookies.clear()
    in_between = await client.get("/settings", headers=session_headers(user.id, epoch=1))
    assert in_between.status_code == 302 and in_between.headers["location"].startswith("/login")


async def test_a_non_admins_impersonation_cookie_only_reaches_their_own_link(
    client, db_session, on
):
    victim = await factories.make_user(db_session)
    attacker = await factories.make_user(db_session)
    await _link(db_session, victim)
    r = await client.post(
        "/settings/microsoft/unlink",
        headers=session_headers(attacker.id, impersonate=victim.id),
    )
    # Acted as the attacker, who has no link.
    assert r.headers["location"] == "/settings?microsoft=nothing_linked"
    assert await _links(db_session) == 1


async def test_unlink_redirects_a_logged_out_caller_and_changes_nothing(client, db_session, on):
    user = await factories.make_user(db_session)
    await _link(db_session, user)
    client.cookies.clear()
    r = await client.post("/settings/microsoft/unlink")
    assert r.status_code == 302 and r.headers["location"].startswith("/login")
    assert await _links(db_session) == 1
    assert await _epoch(db_session, user) in (None, 0)


async def test_unlink_with_nothing_linked_changes_nothing(client, db_session, on):
    user = await factories.make_user(db_session)
    r = await client.post("/settings/microsoft/unlink", headers=session_headers(user.id))
    assert r.status_code == 302 and r.headers["location"] == "/settings?microsoft=nothing_linked"
    assert session_from_response(r) in (None, {"user_id": str(user.id)})
    assert await _epoch(db_session, user) in (None, 0)
    page = await client.get(r.headers["location"], headers=session_headers(user.id))
    assert "No Microsoft account is linked." in page.text


async def test_unlink_is_404_when_off_and_403_while_impersonating(client, db_session, asgi_app):
    user = await factories.make_user(db_session)
    await _link(db_session, user)
    off = await client.post("/settings/microsoft/unlink", headers=session_headers(user.id))
    assert off.status_code == 404
    asgi_app.state.microsoft_sign_in = make_config()
    admin = await factories.make_user(db_session, is_admin=True)
    r = await client.post(
        "/settings/microsoft/unlink", headers=session_headers(admin.id, impersonate=user.id)
    )
    assert r.status_code == 403
    assert await _links(db_session) == 1
    assert await _epoch(db_session, user) in (None, 0)


async def test_a_known_message_code_renders_and_an_unknown_one_does_not(client, db_session, on):
    user = await factories.make_user(db_session)
    known = await client.get("/settings?microsoft=linked", headers=_fresh(user))
    assert "Microsoft sign-in linked. You can now use it on the sign-in page." in known.text
    unknown = await client.get("/settings?microsoft=<b>x</b>", headers=_fresh(user))
    assert unknown.status_code == 200
    assert "<b>x</b>" not in unknown.text
    assert "Microsoft sign-in linked." not in unknown.text

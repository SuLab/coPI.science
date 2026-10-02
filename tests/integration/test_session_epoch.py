"""Sessions are revocable server-side and reset at login (spec 2026-10-01 §6.7, A-11)."""
import pytest

from src.database import get_db
from src.dependencies import get_current_user
from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI
from src.routers import auth as auth_module
from tests import factories
from tests.integration.test_auth_allowlist_gate import _fake_oauth
from tests.session_support import raw_session_headers, session_from_response, session_headers

pytestmark = pytest.mark.integration


async def _pi(db_session, **overrides):
    user = await factories.make_user(db_session, user_role=USER_ROLE_PI, **overrides)
    await factories.make_profile(db_session, user=user)
    await db_session.flush()
    return user


async def _callback(client, monkeypatch, orcid: str, session: dict):
    monkeypatch.setattr(auth_module, "_get_oauth_client", lambda: _fake_oauth(orcid))

    async def _profile(orcid_id):
        return {"orcid": orcid_id, "name": "Test User"}

    monkeypatch.setattr(auth_module, "fetch_orcid_profile", _profile)
    return await client.get(
        "/auth/callback?code=c&state=s",
        headers=raw_session_headers({"oauth_state": "s", **session}),
    )


# --- login --------------------------------------------------------------------

async def test_login_clears_the_pre_login_session_and_keeps_the_vetted_next(
    client, db_session, monkeypatch
):
    user = await _pi(db_session)
    r = await _callback(client, monkeypatch, user.orcid, {
        "post_login_redirect": "/profile/edit",
        "impersonate_user_id": str(user.id), "impersonate_expires_at": 9_999_999_999,
        "planted": "by an attacker",
    })
    assert r.status_code == 302 and r.headers["location"] == "/profile/edit"
    assert session_from_response(r) == {"user_id": str(user.id), "epoch": 0}


async def test_login_drops_an_unsafe_next(client, db_session, monkeypatch):
    user = await _pi(db_session)
    r = await _callback(client, monkeypatch, user.orcid,
                        {"post_login_redirect": "https://evil.example/"})
    assert r.headers["location"] == "/profile"
    assert session_from_response(r) == {"user_id": str(user.id), "epoch": 0}


async def test_login_keeps_a_pending_invite_token(client, db_session, monkeypatch):
    user = await _pi(db_session)
    r = await _callback(client, monkeypatch, user.orcid,
                        {"pending_invite_token": "tok-9", "planted": 1})
    assert r.headers["location"] == "/invite/tok-9"
    assert session_from_response(r) == {"user_id": str(user.id), "epoch": 0}


async def test_login_stores_the_accounts_epoch(client, db_session, monkeypatch):
    user = await _pi(db_session, session_epoch=4)
    r = await _callback(client, monkeypatch, user.orcid, {})
    assert session_from_response(r) == {"user_id": str(user.id), "epoch": 4}


async def test_a_refused_login_carries_only_pending_access(client, db_session, monkeypatch):
    user = await _pi(db_session, access_status="denied")
    r = await _callback(client, monkeypatch, user.orcid, {"planted": 1})
    assert r.headers["location"] == "/access-pending"
    assert set(session_from_response(r)) == {"pending_access"}


# --- the epoch check ------------------------------------------------------------

async def test_a_session_from_an_older_epoch_is_refused_and_cleared(client, db_session):
    user = await _pi(db_session, session_epoch=1)
    stale = await client.get("/profile", headers=session_headers(user.id, epoch=0))
    assert stale.status_code == 302 and stale.headers["location"] == "/login?next=%2Fprofile"
    assert session_from_response(stale) == {}
    current = await client.get("/profile", headers=session_headers(user.id, epoch=1))
    assert current.status_code == 200


async def test_a_session_with_no_epoch_key_counts_as_epoch_zero(client, db_session):
    user = await _pi(db_session)
    assert (await client.get("/profile", headers=session_headers(user.id))).status_code == 200
    user.session_epoch = 1
    await db_session.flush()
    refused = await client.get("/profile", headers=session_headers(user.id))
    assert refused.headers["location"] == "/login?next=%2Fprofile"


@pytest.mark.parametrize("epoch", ["0", True, None, 0.0])
async def test_a_malformed_epoch_is_refused(client, db_session, epoch):
    user = await _pi(db_session)
    r = await client.get("/profile", headers=session_headers(user.id, epoch=epoch))
    assert r.status_code == 302 and r.headers["location"] == "/login?next=%2Fprofile"


async def test_a_stale_session_at_login_ends_on_the_login_page(client, db_session):
    user = await _pi(db_session, session_epoch=1)
    stale = session_headers(user.id, epoch=0)
    assert (await client.get("/login", headers=stale)).headers["location"] == "/"
    assert (await client.get("/", headers=stale)).headers["location"] == "/profile"
    bounced = await client.get("/profile", headers=stale)
    assert bounced.headers["location"] == "/login?next=%2Fprofile"
    assert session_from_response(bounced) == {}
    # The browser now holds no session, so the next hop renders the login page.
    assert (await client.get("/login?next=%2Fprofile")).status_code == 200


# --- logout -------------------------------------------------------------------

async def test_logout_signs_out_every_device(client, db_session):
    user = await _pi(db_session)
    device_a = session_headers(user.id, epoch=0)
    device_b = session_headers(user.id, epoch=0)
    assert (await client.get("/profile", headers=device_b)).status_code == 200

    r = await client.post("/logout", headers=device_a)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    assert session_from_response(r) == {}
    await db_session.refresh(user)
    assert user.session_epoch == 1

    other = await client.get("/profile", headers=device_b)
    assert other.status_code == 302 and other.headers["location"] == "/login?next=%2Fprofile"
    relogin = await client.get("/profile", headers=session_headers(user.id, epoch=1))
    assert relogin.status_code == 200


async def test_a_revoked_session_cannot_sign_out_the_newer_ones(client, db_session):
    user = await _pi(db_session, session_epoch=2)
    r = await client.post("/logout", headers=session_headers(user.id, epoch=0))
    assert r.status_code == 302 and r.headers["location"] == "/login"
    await db_session.refresh(user)
    assert user.session_epoch == 2


async def test_anonymous_logout_still_redirects(client):
    r = await client.post("/logout")
    assert r.status_code == 302 and r.headers["location"] == "/login"


def test_logout_takes_no_auth_dependency():
    route = next(r for r in auth_module.router.routes if getattr(r, "path", None) == "/logout")
    calls = {d.call for d in route.dependant.dependencies}
    assert calls == {get_db}, calls
    assert get_current_user not in calls


# --- deny and role change -------------------------------------------------------

async def test_denying_access_bumps_the_epoch(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    user = await _pi(db_session)
    r = await client.post(
        f"/admin/access-requests/{user.id}/deny", headers=session_headers(admin.id)
    )
    assert r.status_code == 302
    await db_session.refresh(user)
    assert (user.access_status, user.session_epoch) == ("denied", 1)


async def test_a_role_change_bumps_the_epoch_and_ends_the_old_session(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    user = await _pi(db_session)
    r = await client.post(
        f"/admin/users/{user.id}/role", data={"user_role": USER_ROLE_MANAGER},
        headers=session_headers(admin.id),
    )
    assert r.status_code == 302
    await db_session.refresh(user)
    assert (user.user_role, user.session_epoch) == (USER_ROLE_MANAGER, 1)
    old = await client.get("/profile", headers=session_headers(user.id, epoch=0))
    assert old.headers["location"] == "/login?next=%2Fprofile"


async def test_saving_the_same_role_signs_nobody_out(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    user = await _pi(db_session)
    r = await client.post(
        f"/admin/users/{user.id}/role", data={"user_role": USER_ROLE_PI},
        headers=session_headers(admin.id),
    )
    assert r.status_code == 302
    await db_session.refresh(user)
    assert user.session_epoch is None

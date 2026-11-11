"""The ORCID callback's shared sign-in tail, logout, and the login page."""

import logging
import time

import pytest

from src.models import AccessAllowlist
from src.routers import auth as auth_module
from src.services.microsoft_identity import message_for
from tests import factories
from tests.oidc_fakes import make_config
from tests.session_support import raw_session_headers, session_from_response, session_headers

pytestmark = pytest.mark.integration


def _fake_orcid(monkeypatch, orcid):
    class _Client:
        async def fetch_token(self, url, code, grant_type):
            return {"orcid": orcid, "name": "Test User"}

    async def _profile(orcid_id):
        return {"orcid": orcid_id, "name": "Test User"}

    monkeypatch.setattr(auth_module, "_get_oauth_client", lambda: _Client())
    monkeypatch.setattr(auth_module, "fetch_orcid_profile", _profile)


async def _callback(client, monkeypatch, orcid, **session):
    _fake_orcid(monkeypatch, orcid)
    return await client.get(
        "/auth/callback?code=c&state=s",
        headers=raw_session_headers({"oauth_state": "s", **session}),
    )


async def test_a_first_orcid_sign_in_creates_the_user_and_starts_at_epoch_zero(
    client, db_session, monkeypatch
):
    orcid = "0000-0002-1825-0097"
    db_session.add(AccessAllowlist(orcid=orcid))
    await db_session.flush()
    r = await _callback(client, monkeypatch, orcid)
    assert r.status_code == 302 and r.headers["location"] == "/onboarding"
    session = session_from_response(r)
    assert set(session) == {"user_id", "epoch", "signed_in_at", "signed_in_via"}
    assert session["epoch"] == 0 and session["signed_in_via"] == "orcid"
    assert int(time.time()) - session["signed_in_at"] < 60


async def test_sign_in_clears_the_pre_login_session_and_keeps_the_vetted_next(
    client, db_session, monkeypatch
):
    user = await factories.make_user(db_session, onboarding_complete=False)
    r = await _callback(
        client, monkeypatch, user.orcid,
        planted="x", pending_access={"orcid": "other"}, post_login_redirect="/profile/edit",
    )
    assert r.headers["location"] == "/onboarding"
    session = session_from_response(r)
    assert session["post_login_redirect"] == "/profile/edit"
    assert not {"planted", "pending_access", "oauth_state"} & set(session)

    done = await factories.make_user(db_session)
    r = await _callback(
        client, monkeypatch, done.orcid, planted="x", post_login_redirect="/profile/edit"
    )
    assert r.headers["location"] == "/profile/edit"
    session = session_from_response(r)
    assert "planted" not in session and "post_login_redirect" not in session


async def test_sign_in_drops_an_unsafe_next(client, db_session, monkeypatch):
    user = await factories.make_user(db_session)
    r = await _callback(
        client, monkeypatch, user.orcid, post_login_redirect="https://evil.example/x"
    )
    assert r.headers["location"] == "/profile"
    assert "post_login_redirect" not in session_from_response(r)


async def test_sign_in_keeps_a_pending_invite_token(client, db_session, monkeypatch):
    user = await factories.make_user(db_session)
    r = await _callback(client, monkeypatch, user.orcid, pending_invite_token="tok123")
    assert r.headers["location"] == "/invite/tok123"
    assert "pending_invite_token" not in session_from_response(r)


async def test_sign_in_stores_the_accounts_epoch(client, db_session, monkeypatch):
    user = await factories.make_user(db_session, session_epoch=4)
    r = await _callback(client, monkeypatch, user.orcid)
    assert session_from_response(r)["epoch"] == 4


async def test_sign_in_deletes_the_impersonation_cookie(client, db_session, monkeypatch):
    user = await factories.make_user(db_session)
    r = await _callback(client, monkeypatch, user.orcid)
    cookies = [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie"]
    deleted = [c for c in cookies if c.startswith("copi-impersonate=")]
    assert deleted and "Max-Age=0" in deleted[0]


async def test_a_refused_sign_in_carries_only_pending_access(client, db_session, monkeypatch):
    user = await factories.make_user(db_session, access_status="denied")
    r = await _callback(client, monkeypatch, user.orcid, planted="x")
    assert r.headers["location"] == "/access-pending"
    session = session_from_response(r)
    assert set(session) == {"pending_access"}
    assert session["pending_access"]["orcid"] == user.orcid


async def test_the_orcid_error_is_logged_with_repr_and_a_cap(client, caplog):
    with caplog.at_level(logging.WARNING, logger="src.routers.auth"):
        r = await client.get("/auth/callback?error=a%0Ab" + "x" * 200)
    assert r.headers["location"] == "/login?error=oauth_error"
    assert "'a\\nb" in caplog.text
    assert "x" * 77 in caplog.text and "x" * 78 not in caplog.text


async def test_logout_ends_every_session(client, db_session):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user)
    before = await client.get("/profile", headers=session_headers(user.id, epoch=0))
    assert before.status_code == 200
    out = await client.post("/logout", headers=session_headers(user.id, epoch=0))
    assert out.status_code == 302 and out.headers["location"] == "/login"
    assert session_from_response(out) == {}
    await db_session.refresh(user)
    assert user.session_epoch == 1
    other = await client.get("/profile", headers=session_headers(user.id, epoch=0))
    assert other.status_code == 302 and other.headers["location"].startswith("/login")


async def test_logout_with_a_stale_session_bumps_nothing(client, db_session):
    user = await factories.make_user(db_session, session_epoch=3)
    out = await client.post("/logout", headers=session_headers(user.id, epoch=1))
    assert out.status_code == 302 and out.headers["location"] == "/login"
    await db_session.refresh(user)
    assert user.session_epoch == 3


async def test_logout_while_signed_out_still_redirects_to_login(client):
    out = await client.post("/logout")
    assert out.status_code == 302 and out.headers["location"] == "/login"


async def test_the_login_page_shows_microsoft_only_when_on(client, asgi_app):
    asgi_app.state.microsoft_sign_in = None
    assert 'href="/auth/microsoft/start"' not in (await client.get("/login")).text
    asgi_app.state.microsoft_sign_in = make_config()
    assert 'href="/auth/microsoft/start"' in (await client.get("/login")).text


async def test_the_login_page_shows_microsoft_messages(client):
    not_linked = message_for("microsoft_not_linked")[0]
    r = await client.get("/login?error=microsoft_not_linked")
    assert not_linked in r.text and "bg-red-50" in r.text
    unlinked = message_for("microsoft_unlinked")[0]
    r = await client.get("/login?error=microsoft_unlinked")
    assert unlinked in r.text and "bg-red-50 border border-red-200" not in r.text


async def test_an_unknown_error_code_shows_the_generic_text(client):
    r = await client.get("/login", params={"error": "qq9<script>"})
    assert "Sign in failed. Please try again." in r.text
    assert "qq9" not in r.text

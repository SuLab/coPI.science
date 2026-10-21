"""The ORCID re-sign-in before linking Microsoft (spec §5.4, §5.7)."""

import time
from urllib.parse import parse_qs, urlsplit

import pytest

from src.config import get_settings
from src.routers import auth as auth_module
from tests import factories
from tests.oidc_fakes import sign
from tests.session_support import raw_session_headers, session_from_response, session_headers

pytestmark = pytest.mark.integration

FAILED = "/settings?microsoft=reauth_failed"


@pytest.fixture(autouse=True)
def _orcid_client_id(monkeypatch):
    monkeypatch.setattr(get_settings(), "orcid_client_id", "APP-TEST")


def _orcid_client(orcid, id_token=None):
    class _Client:
        async def fetch_token(self, url, code, grant_type):
            token = {"orcid": orcid, "name": "Test User"}
            if id_token is not None:
                token["id_token"] = id_token
            return token

    return _Client()


def _claims(orcid, *, nonce="rn", auth_age=5, **overrides):
    now = int(time.time())
    claims = {"iss": "https://orcid.org", "aud": "APP-TEST", "sub": orcid, "nonce": nonce,
              "iat": now, "exp": now + 600, "auth_time": now - auth_age}
    claims.update(overrides)
    return claims


def _stub_orcid(monkeypatch, orcid, id_token=None):
    monkeypatch.setattr(auth_module, "_get_oauth_client", lambda: _orcid_client(orcid, id_token))

    async def _profile(orcid_id):
        return {"orcid": orcid_id, "name": "Test User"}

    monkeypatch.setattr(auth_module, "fetch_orcid_profile", _profile)


async def _reauth_callback(client, monkeypatch, user, *, token_orcid=None, id_token=None,
                           started_at=None):
    _stub_orcid(monkeypatch, token_orcid or user.orcid, id_token)
    started = int(time.time()) if started_at is None else started_at
    return await client.get("/auth/callback?code=c&state=s", headers=session_headers(
        user.id, oauth_state="s", orcid_reauth={"nonce": "rn", "started_at": started},
        post_login_redirect="/settings", signed_in_at=started - 3600, signed_in_via="orcid",
    ))


async def test_plain_login_start_still_sends_a_signed_in_user_home(client, db_session):
    user = await factories.make_user(db_session)
    r = await client.get("/login/start", headers=session_headers(user.id))
    assert r.status_code == 302 and r.headers["location"] == "/"


async def test_reauth_start_forces_an_orcid_password_prompt(client, db_session):
    user = await factories.make_user(db_session)
    r = await client.get("/login/start?reauth=1", headers=session_headers(user.id))
    assert r.status_code == 302
    location = urlsplit(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert location.netloc == "orcid.org"
    assert q["scope"] == "openid" and q["prompt"] == "login" and q["nonce"]
    session = session_from_response(r)
    assert session["orcid_reauth"]["nonce"] == q["nonce"]
    assert session["post_login_redirect"] == "/settings"
    assert session["oauth_state"] == q["state"]


async def test_ordinary_login_start_is_unchanged(client):
    r = await client.get("/login/start")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["scope"] == ["/authenticate"] and "prompt" not in q and "nonce" not in q


async def test_reauth_start_is_refused_while_impersonating(client, db_session):
    admin = await factories.make_user(db_session, is_admin=True)
    other = await factories.make_user(db_session)
    r = await client.get(
        "/login/start?reauth=1", headers=session_headers(admin.id, impersonate=other.id)
    )
    assert r.status_code == 302 and r.headers["location"] == "/settings?microsoft=impersonating"
    assert "orcid_reauth" not in (session_from_response(r) or {})


async def test_reauth_with_a_dangling_impersonation_cookie_is_refused(client, db_session):
    admin = await factories.make_user(db_session, is_admin=True)
    r = await client.get(
        "/login/start?reauth=1",
        headers=session_headers(admin.id, impersonate="00000000-0000-0000-0000-000000000000"),
    )
    assert r.headers["location"] == "/settings?microsoft=impersonating"


async def test_a_proven_reauth_signs_in_afresh_and_returns_to_settings(
        client, db_session, monkeypatch, fake_idp):
    user = await factories.make_user(db_session)
    r = await _reauth_callback(client, monkeypatch, user, id_token=sign(_claims(user.orcid)))
    assert r.status_code == 302 and r.headers["location"] == "/settings"
    session = session_from_response(r)
    assert session["user_id"] == str(user.id) and session["signed_in_via"] == "orcid"
    assert int(time.time()) - session["signed_in_at"] < 60
    assert int(time.time()) - session["orcid_reauth_at"] < 60
    assert "orcid_reauth" not in session


async def test_an_ordinary_orcid_sign_in_carries_no_reauth_proof(client, db_session, monkeypatch):
    user = await factories.make_user(db_session)
    _stub_orcid(monkeypatch, user.orcid)
    r = await client.get("/auth/callback?code=c&state=s",
                         headers=raw_session_headers({"oauth_state": "s"}))
    session = session_from_response(r)
    assert session["user_id"] == str(user.id) and session["signed_in_via"] == "orcid"
    assert "orcid_reauth_at" not in session


async def test_a_reauth_callback_without_a_signed_in_user_signs_no_one_in(
        client, db_session, monkeypatch, fake_idp):
    user = await factories.make_user(db_session)
    _stub_orcid(monkeypatch, user.orcid, sign(_claims(user.orcid)))
    r = await client.get("/auth/callback?code=c&state=s", headers=raw_session_headers({
        "oauth_state": "s", "orcid_reauth": {"nonce": "rn", "started_at": int(time.time())}}))
    assert r.headers["location"] == FAILED
    session = session_from_response(r)
    assert "user_id" not in session and "orcid_reauth_at" not in session


async def test_a_proven_reauth_of_a_denied_account_carries_no_proof(
        client, db_session, monkeypatch, fake_idp):
    user = await factories.make_user(db_session, access_status="denied")
    r = await _reauth_callback(client, monkeypatch, user, id_token=sign(_claims(user.orcid)))
    assert r.headers["location"] == "/access-pending"
    session = session_from_response(r)
    assert "user_id" not in session and "orcid_reauth_at" not in session


async def test_an_ordinary_sign_in_drops_an_abandoned_reauth(client):
    r = await client.get("/login/start", headers=raw_session_headers(
        {"orcid_reauth": {"nonce": "rn", "started_at": int(time.time())}}))
    assert "orcid_reauth" not in session_from_response(r)


@pytest.mark.parametrize(
    "case", ["stale_auth_time", "other_orcid", "no_id_token", "wrong_nonce", "expired_flow"]
)
async def test_an_unproven_reauth_keeps_the_session_and_says_so(
        client, db_session, monkeypatch, fake_idp, case):
    user = await factories.make_user(db_session)
    other = await factories.make_user(db_session)
    kwargs = {"id_token": sign(_claims(user.orcid))}
    if case == "stale_auth_time":
        kwargs["id_token"] = sign(_claims(user.orcid, auth_age=301))
    elif case == "other_orcid":
        kwargs = {"token_orcid": other.orcid, "id_token": sign(_claims(other.orcid))}
    elif case == "no_id_token":
        kwargs = {}
    elif case == "wrong_nonce":
        kwargs["id_token"] = sign(_claims(user.orcid, nonce="other"))
    elif case == "expired_flow":
        kwargs["started_at"] = int(time.time()) - 601
    r = await _reauth_callback(client, monkeypatch, user, **kwargs)
    assert r.status_code == 302 and r.headers["location"] == FAILED
    session = session_from_response(r)
    assert session["user_id"] == str(user.id)
    assert "post_login_redirect" not in session and "orcid_reauth_at" not in session


async def test_the_reauth_callback_refuses_while_impersonating(
        client, db_session, monkeypatch, fake_idp):
    admin = await factories.make_user(db_session, is_admin=True)
    other = await factories.make_user(db_session)
    _stub_orcid(monkeypatch, admin.orcid, sign(_claims(admin.orcid)))
    headers = session_headers(
        admin.id, impersonate=other.id, oauth_state="s",
        orcid_reauth={"nonce": "rn", "started_at": int(time.time())},
    )
    r = await client.get("/auth/callback?code=c&state=s", headers=headers)
    assert r.headers["location"] == FAILED
    session = session_from_response(r)
    assert session["user_id"] == str(admin.id) and "orcid_reauth_at" not in session

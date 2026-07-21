"""Characterization pins for auth, session-gated, and admin routes.

Unauthenticated protected routes are pinned as they behave today; a forged
signed session cookie (built exactly like starlette's SessionMiddleware) drives
the authenticated paths without a real ORCID OAuth round-trip.
"""

import base64
import json

import pytest
from itsdangerous import TimestampSigner

from src.config import get_settings
from tests import factories

pytestmark = pytest.mark.characterization

# AgentBadgeMiddleware's session-factory bypass is handled centrally by the `client`
# fixture (tests/conftest.py) — see the note there.


def _session_cookie(user_id) -> str:
    """Forge a 'copi-session' cookie the same way SessionMiddleware signs it."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return signer.sign(data).decode("utf-8")


def _auth_headers(user_id) -> dict:
    return {"Cookie": f"copi-session={_session_cookie(user_id)}"}


# --- auth router -------------------------------------------------------------

async def test_login_page_anonymous_200(client):
    r = await client.get("/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_login_page_with_session_redirects_home(client):
    # login() short-circuits to "/" when the session already has a user_id.
    r = await client.get("/login", headers={"Cookie": f"copi-session={_session_cookie('irrelevant')}"})
    assert r.status_code == 302
    assert r.headers["location"] == "/"


async def test_login_start_redirects_to_orcid(client):
    r = await client.get("/login/start")
    assert r.status_code == 302
    assert "orcid.org" in r.headers["location"]


async def test_auth_callback_error_param_redirects(client):
    r = await client.get("/auth/callback", params={"error": "access_denied"})
    assert r.status_code == 302
    assert r.headers["location"] == "/login?error=oauth_error"


async def test_auth_callback_no_code_redirects(client):
    r = await client.get("/auth/callback")
    assert r.status_code == 302
    assert r.headers["location"] == "/login?error=no_code"


async def test_auth_callback_state_mismatch_fails_closed(client):
    # SEC-3: no stored state in a fresh session -> reject even with code+state.
    r = await client.get("/auth/callback", params={"code": "abc", "state": "forged"})
    assert r.status_code == 302
    assert r.headers["location"] == "/login?error=state_mismatch"


async def test_logout_post_redirects_to_login(client):
    r = await client.post("/logout")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


async def test_logout_get_not_allowed(client):
    # SEC-8: logout is POST-only (was a CSRF target as a GET).
    r = await client.get("/logout")
    assert r.status_code == 405


# --- protected routes: unauthenticated redirect to /login -------------------

@pytest.mark.parametrize("path", ["/profile", "/settings", "/onboarding", "/agent"])
async def test_protected_route_unauth_redirects_login(client, path):
    r = await client.get(path)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


async def test_admin_unauth_redirects_login(client):
    r = await client.get("/admin")
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


# --- authenticated paths -----------------------------------------------------

async def test_profile_authenticated_200(client, db_session):
    u = await factories.make_user(db_session, onboarding_complete=True)
    r = await client.get("/profile", headers=_auth_headers(u.id))
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_profile_authed_incomplete_onboarding_redirects(client, db_session):
    u = await factories.make_user(db_session, onboarding_complete=False)
    r = await client.get("/profile", headers=_auth_headers(u.id))
    assert r.status_code == 302
    assert r.headers["location"] == "/onboarding"


async def test_settings_authenticated_200(client, db_session):
    u = await factories.make_user(db_session, onboarding_complete=True)
    r = await client.get("/settings", headers=_auth_headers(u.id))
    assert r.status_code == 200


async def test_onboarding_authed_complete_redirects_profile(client, db_session):
    u = await factories.make_user(db_session, onboarding_complete=True)
    r = await client.get("/onboarding", headers=_auth_headers(u.id))
    assert r.status_code == 302
    assert r.headers["location"] == "/profile"


async def test_onboarding_authed_incomplete_200(client, db_session):
    u = await factories.make_user(db_session, onboarding_complete=False)
    r = await client.get("/onboarding", headers=_auth_headers(u.id))
    assert r.status_code == 200


async def test_agent_landing_authenticated_200(client, db_session):
    u = await factories.make_user(db_session, onboarding_complete=True)
    r = await client.get("/agent", headers=_auth_headers(u.id))
    assert r.status_code == 200


async def test_admin_non_admin_user_403(client, db_session):
    u = await factories.make_user(db_session, is_admin=False)
    r = await client.get("/admin", headers=_auth_headers(u.id))
    assert r.status_code == 403


async def test_admin_admin_user_200(client, db_session):
    u = await factories.make_user(db_session, is_admin=True)
    r = await client.get("/admin", headers=_auth_headers(u.id))
    assert r.status_code == 200


# --- token-gated public pages (no login) ------------------------------------

async def test_invite_invalid_token_renders_error_200(client):
    r = await client.get("/invite/not-a-real-token")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_unsubscribe_invalid_token_renders_error_200(client):
    r = await client.get("/settings/unsubscribe/bogus-token")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]

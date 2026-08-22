"""Revoking access must end the session (E1.2).

``get_current_user`` reloads the ``User`` row on every request but never read
``access_status``; ``admin_deny_access`` sets it to ``'denied'`` and does
nothing else. Sessions here are unkeyed signed cookies with a 30-day
``max_age`` and no server-side store, so there is nothing to revoke and the
column was the only revocation signal available. Probed against the live app
before this fix: a denied user's ``GET /profile`` returned **200**.

The observable in every test below is the session cookie the response carries,
not only the status code — "this request was bounced" and "the session is over"
are different claims, and only the second one is a revocation.
"""

import base64
import json
from http.cookies import SimpleCookie

import pytest
from itsdangerous import TimestampSigner

from src.config import get_settings
from src.models import USER_ROLE_PI
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

SESSION_COOKIE = "copi-session"


def _session_from(response) -> dict | None:
    """Decode the session SessionMiddleware set on ``response``.

    ``None`` means the response set no session cookie at all (so the request's
    session was carried forward unchanged); ``{}`` means it set an empty one.
    Decoding rather than string-matching because "user_id is gone" is the claim
    under test and a substring search would also match, say, a ``pending_access``
    entry that happens to carry a ``user_id`` key — which this one does.
    """
    raw = None
    for k, v in response.headers.multi_items():
        if k.lower() == "set-cookie" and v.startswith(f"{SESSION_COOKIE}="):
            raw = v
    if raw is None:
        return None
    jar = SimpleCookie()
    jar.load(raw)
    value = jar[SESSION_COOKIE].value
    # Starlette clears the session by re-setting the cookie to the literal
    # string "null" (an unsigned sentinel), not to an empty value.
    if not value or value == "null":
        return {}
    signer = TimestampSigner(get_settings().secret_key)
    return json.loads(base64.b64decode(signer.unsign(value, max_age=30 * 24 * 3600)))


def _cookie_header(response) -> dict:
    """Re-send whatever session the app just handed back, as a browser would."""
    for k, v in response.headers.multi_items():
        if k.lower() == "set-cookie" and v.startswith(f"{SESSION_COOKIE}="):
            jar = SimpleCookie()
            jar.load(v)
            return {"Cookie": f"{SESSION_COOKIE}={jar[SESSION_COOKIE].value}"}
    raise AssertionError(f"no {SESSION_COOKIE} cookie on the response: {response.headers}")


async def test_a_denied_user_is_logged_out_on_the_next_request(client, db_session):
    """Allowed, then revoked, then bounced — and the session is actually over.

    The first GET is the control: without it a 302 on the second could just as
    well mean the fixture never authenticated at all.
    """
    user = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, onboarding_complete=True
    )
    await factories.make_profile(db_session, user=user)
    await db_session.flush()

    ok = await client.get("/profile", headers=auth_headers(user.id))
    assert ok.status_code == 200, "the control request was never authenticated"

    user.access_status = "denied"
    await db_session.flush()

    bounced = await client.get("/profile", headers=auth_headers(user.id))
    assert bounced.status_code == 302
    assert bounced.headers["location"] == "/access-pending"

    # The session itself is over, not merely this one request refused.
    ended = _session_from(bounced)
    assert ended is not None, "no session cookie was re-set, so nothing was revoked"
    assert "user_id" not in ended, f"user_id survived the revocation: {ended}"

    # ...and replaying the session the app just handed back gets nowhere either.
    replay = await client.get("/profile", headers=_cookie_header(bounced))
    assert replay.status_code == 302
    assert replay.headers["location"].startswith("/login")


async def test_a_denied_user_can_still_reach_access_pending(client, db_session):
    """The dead-end trap: ``request.session.clear()`` would empty this page.

    /access-pending renders ``request.session["pending_access"]``, so a
    revocation that cleared the whole session would land the user on a page that
    cannot even tell them which account is blocked. get_current_user therefore
    POPS ``user_id`` and repopulates ``pending_access``, mirroring what
    auth.py's own access gate stores at login.

    This also pins that /access-pending takes no auth dependency of its own — if
    someone adds ``Depends(get_current_user)`` to it, a revoked user has nowhere
    to be redirected to and the bounce becomes an infinite loop.
    """
    user = await factories.make_user(
        db_session,
        user_role=USER_ROLE_PI,
        name="Reva Voked",
        onboarding_complete=True,
        access_status="denied",
    )
    await db_session.flush()

    bounced = await client.get("/profile", headers=auth_headers(user.id))
    assert bounced.headers["location"] == "/access-pending"
    carried = _session_from(bounced)
    assert carried["pending_access"]["orcid"] == user.orcid, carried

    landed = await client.get("/access-pending", headers=_cookie_header(bounced))
    assert landed.status_code == 200
    assert user.orcid in landed.text, "the access-pending page came back empty"

    # And with no session at all it is still a reachable, rendering page.
    anonymous = await client.get("/access-pending")
    assert anonymous.status_code == 200


async def test_logout_still_works_for_a_denied_user(client, db_session):
    """POST /logout must not depend on get_current_user, or a revoked user is
    stuck with a cookie they cannot clear from the UI."""
    user = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, access_status="denied"
    )
    await db_session.flush()

    r = await client.post("/logout", headers=auth_headers(user.id))
    assert r.status_code == 302
    assert r.headers["location"] == "/login"
    assert _session_from(r) in (None, {}), _session_from(r)


async def test_a_pending_user_is_also_logged_out__synthetic(client, db_session):
    """SYNTHETIC — this state is not reachable through the login flow.

    auth.py never puts ``user_id`` in the session for a user whose
    ``access_status`` is not ``'allowed'``: it stores ``pending_access`` and
    redirects to /access-pending instead. So a *pending* user holding a valid
    session cookie can only be forged, as it is here. It is kept because the
    guard is written against ``!= "allowed"`` rather than ``== "denied"``, and
    this is the only thing that would notice if it were narrowed — but it must
    not be read as a reproduction of anything observed in production.
    """
    user = await factories.make_user(
        db_session, user_role=USER_ROLE_PI, access_status="pending"
    )
    await db_session.flush()

    r = await client.get("/profile", headers=auth_headers(user.id))
    assert r.status_code == 302
    assert r.headers["location"] == "/access-pending"

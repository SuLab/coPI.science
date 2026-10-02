"""Helpers for the web UI remediation Phase 2 tests (no ``test_`` prefix, so not
collected).

Thin wrappers over ``tests/session_support.py`` and ``tests/flash_support.py``, which
ask the app for the session cookie name and sign the session as ``SessionMiddleware``
does; nothing here re-derives the cookie.

``follow`` exists because httpx ignores its cookie jar whenever a request carries an
explicit ``Cookie`` header, which every ``auth_headers`` request does: a flash set
by a POST lives in the Set-Cookie of that response and must be re-sent by hand.
"""

from tests.flash_support import session_cookie_header
from tests.integration.test_manager_access import auth_headers


def impersonation_headers(admin_id, target_id) -> dict[str, str]:
    """Request headers for ``admin_id``'s session impersonating ``target_id``, signing
    both ``impersonate_user_id`` and the expiry that POST /admin/impersonate sets."""
    return auth_headers(admin_id, impersonate=target_id)


def session_from(response) -> dict[str, str]:
    """A ``Cookie`` header re-sending the session ``response`` set, as a browser would.
    Fails the test when the response set no session cookie."""
    return session_cookie_header(response)


async def follow(client, response):
    """GET the redirect target of ``response`` with the session it set."""
    assert response.status_code in (302, 303), response.status_code
    return await client.get(
        response.headers["location"], headers=session_from(response), follow_redirects=False
    )

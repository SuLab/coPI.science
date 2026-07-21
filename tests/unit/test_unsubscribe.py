"""Regression tests for the email unsubscribe flow.

Two coupled bugs previously masked each other (see project memory
"unsubscribe GET landmine"):

1. The GET handler mutated state (set frequency to "off" and committed), so
   email-security scanners that pre-fetch links (Outlook SafeLinks, Defender,
   Proofpoint, ...) silently unsubscribed recipients.
2. Every emailed unsubscribe URL was built without the "/settings" prefix, so
   the links 404'd — which is the only reason #1 never caused damage.

These tests lock in the fix: GET is read-only (shows a confirmation page), POST
performs the change, and emailed links point at the real "/settings/unsubscribe"
route.
"""

import uuid

import pytest
from starlette.requests import Request

from src.models import User
from src.routers.settings import unsubscribe, unsubscribe_post
from src.services.email import build_welcome_email
from src.services.email_notifications import _generate_unsubscribe_token


def _request(method: str) -> Request:
    """Minimal ASGI Request sufficient for rendering a TemplateResponse."""
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/settings/unsubscribe/x",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


class _Result:
    def __init__(self, user):
        self._user = user

    def scalar_one_or_none(self):
        return self._user


class _FakeSession:
    """Stands in for AsyncSession; records whether commit() was called."""

    def __init__(self, user):
        self._user = user
        self.committed = False

    async def execute(self, *args, **kwargs):
        return _Result(self._user)

    async def commit(self):
        self.committed = True


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        email="pi@example.com",
        email_notification_frequency="weekly",
    )


@pytest.mark.asyncio
async def test_get_unsubscribe_is_read_only():
    """GET must NOT unsubscribe — it only renders the confirmation page.

    This is the core landmine guard: link-scanner pre-fetches (GET) must never
    change a user's preferences.
    """
    user = _user()
    token = _generate_unsubscribe_token(str(user.id))
    db = _FakeSession(user)

    resp = await unsubscribe(_request("GET"), token, db=db)

    assert user.email_notification_frequency == "weekly"  # unchanged
    assert db.committed is False
    assert resp.context["state"] == "confirm"
    assert resp.context["token"] == token


@pytest.mark.asyncio
async def test_post_unsubscribe_performs_change():
    """POST (confirmation button / RFC 8058 one-click) actually unsubscribes."""
    user = _user()
    token = _generate_unsubscribe_token(str(user.id))
    db = _FakeSession(user)

    resp = await unsubscribe_post(_request("POST"), token, db=db)

    assert user.email_notification_frequency == "off"
    assert db.committed is True
    assert resp.context["state"] == "success"


@pytest.mark.asyncio
async def test_get_unsubscribe_invalid_token_does_not_mutate():
    """An invalid/garbage token shows an error and never touches the DB."""
    user = _user()
    db = _FakeSession(user)

    resp = await unsubscribe(_request("GET"), "not-a-valid-token", db=db)

    assert user.email_notification_frequency == "weekly"
    assert db.committed is False
    assert resp.context["state"] == "error"


def test_welcome_email_unsubscribe_url_has_settings_prefix():
    """Regression: emailed links must target the mounted /settings route.

    The unsubscribe router is included with prefix="/settings" (src/main.py),
    so links without that prefix 404.
    """
    _, msg = build_welcome_email(
        "pi@example.com", name="Dr. Example", user_id=str(uuid.uuid4())
    )

    # MIME text/html parts are base64-encoded, so decode them before searching.
    body = "\n".join(
        part.get_payload(decode=True).decode("utf-8", errors="replace")
        for part in msg.walk()
        if part.get_content_type() in ("text/plain", "text/html")
        and part.get_payload(decode=True)
    )

    assert "/settings/unsubscribe/" in body
    # No bare "/unsubscribe/" (i.e. one missing the /settings prefix).
    assert "/unsubscribe/" not in body.replace("/settings/unsubscribe/", "")

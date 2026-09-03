"""AgentBadgeMiddleware must run ZERO badge-count queries for /static and
/api/health (issue #25 P1.4/P1.5).

No DB: src.main.get_session_factory is monkeypatched with a recording fake, and
the assertion is on WHICH statements were issued, not on whether a session was
opened at all — /api/health legitimately opens one for its own `SELECT 1` DB
probe (#27 I2, Task 27.2), so "no session" is not the invariant. "No query
against the badge tables" is.
"""

import base64
import json
import uuid

import httpx
import pytest
from httpx import ASGITransport
from itsdangerous import TimestampSigner

from src.config import get_settings

# Tables only the badge-count path reads. `users` is in the list because the
# impersonation branch selects from it before the badge queries.
_BADGE_TABLES = ("agents", "agent_delegates", "thread_decisions", "proposal_reviews", "users")


def _auth(user_id) -> dict:
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


class _EmptyResult:
    def __iter__(self):
        return iter(())

    def scalar(self):
        return None

    def scalar_one_or_none(self):
        return None


class _RecordingSession:
    def __init__(self, statements):
        self._statements = statements

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, *args, **kwargs):
        self._statements.append(" ".join(str(stmt).split()))
        return _EmptyResult()


@pytest.fixture
def app_and_statements(monkeypatch):
    from src.main import create_app

    statements: list[str] = []
    monkeypatch.setattr(
        "src.main.get_session_factory", lambda: (lambda: _RecordingSession(statements))
    )
    return create_app(), statements


def _badge_queries(statements: list[str]) -> list[str]:
    return [s for s in statements if any(t in s for t in _BADGE_TABLES)]


async def test_static_path_runs_no_badge_queries(app_and_statements):
    app, statements = app_and_statements
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/static/x.css", headers=_auth(uuid.uuid4()))
    assert _badge_queries(statements) == [], "badge queries ran for a /static/ request"
    assert r.status_code == 404


async def test_health_path_runs_no_badge_queries(app_and_statements):
    app, statements = app_and_statements
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/health", headers=_auth(uuid.uuid4()))
    assert _badge_queries(statements) == [], "badge queries ran for /api/health"
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}

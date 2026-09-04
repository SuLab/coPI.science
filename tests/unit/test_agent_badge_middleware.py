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
import re
import uuid

import httpx
import pytest
from httpx import ASGITransport
from itsdangerous import TimestampSigner

from src.config import get_settings
from src.database import get_db
from src.models import User

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


class _RecordingEngine:
    """Stands in for the health probe's own engine (#27 I2).

    The probe deliberately does NOT use the request session factory: it needs asyncpg
    connect/command timeouts that `asyncio.wait_for` cannot supply (see src/main.py).
    That means patching `get_session_factory` alone leaves this test talking to whatever
    `settings.database_url` happens to point at — which is how it started returning 503
    on any machine without a Postgres on the default DSN. Record through the same list
    so the "no badge queries" assertion still sees everything the request ran.
    """

    def __init__(self, statements):
        self._statements = statements

    def connect(self):
        return _RecordingSession(self._statements)


@pytest.fixture
def app_and_statements(monkeypatch):
    from src.main import create_app

    statements: list[str] = []
    monkeypatch.setattr(
        "src.main.get_session_factory", lambda: (lambda: _RecordingSession(statements))
    )
    monkeypatch.setattr("src.main.get_health_engine", lambda: _RecordingEngine(statements))
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


async def test_normal_path_runs_badge_queries(app_and_statements):
    """Positive control (#25 P1.4/P1.5 D1).

    The two tests above assert an EMPTY badge-query list for /static and /api/health.
    That assertion is also satisfied by a middleware that was deleted, disabled, or
    never wired up at all — in every one of those cases the query list is empty for
    every path, excluded or not, and both tests above would pass for the wrong reason.

    This test hits a normal, non-excluded path ("/") with a valid session cookie and
    asserts the query list is NOT empty: it must contain the AgentRegistry lookup
    against the "agents" table (`own_result` in the middleware), which runs for any
    authenticated request that isn't /static or /api/health. Without a real middleware
    issuing real queries, this fails, which is what proves the recording session and
    the `_BADGE_TABLES` filter are actually capable of observing the badge logic.
    """
    app, statements = app_and_statements
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/", headers=_auth(uuid.uuid4()))
    assert _badge_queries(statements) != [], (
        "no badge queries ran for a normal authenticated path — a middleware that "
        "never runs (deleted, disabled, or not registered) would also pass the "
        "static/health tests above for the wrong reason"
    )
    assert r.status_code in (200, 302, 303, 307, 308)


class _CannedResult:
    """Like `_EmptyResult`, but can answer a query with seeded rows/scalar.

    Needed (unlike the plain recording session above) to observe the
    *computed* badge count rendered into a real page, not just which tables a
    query touched.
    """

    def __init__(self, rows=None, scalar_value=None):
        self._rows = rows or []
        self._scalar_value = scalar_value

    def __iter__(self):
        return iter(self._rows)

    def scalar(self):
        if self._scalar_value is not None:
            return self._scalar_value
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _CannedSession:
    """Answers the badge middleware's queries and the auth lookup with seeded
    data: one active agent owned by `user`, one `ThreadDecision(outcome="proposal")`
    for that agent, and one implicit `ProposalReview(rating=-1)` on it.
    """

    def __init__(self, statements, *, agent_id, user):
        self._statements = statements
        self._agent_id = agent_id
        self._user = user

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, *args, **kwargs):
        sql = " ".join(str(stmt).split())
        self._statements.append(sql)
        if "agent_delegates" in sql:
            return _CannedResult(rows=[])
        if "FROM agents" in sql and "count(" not in sql.lower():
            return _CannedResult(rows=[(self._agent_id,)])
        if "count(thread_decisions.id)" in sql.lower():
            return _CannedResult(scalar_value=1)
        if "count(proposal_reviews.id)" in sql.lower():
            # The one seeded review row has rating=-1. A reader that hasn't
            # been taught to ignore the implicit marker counts it (no
            # "proposal_reviews.rating" filter in its WHERE clause) -> 1. One
            # that filters `rating != -1` excludes it -> 0.
            reviewed = 0 if "proposal_reviews.rating" in sql else 1
            return _CannedResult(scalar_value=reviewed)
        if "FROM users" in sql:
            return _CannedResult(rows=[self._user])
        return _CannedResult()


@pytest.fixture
def client_with_recording_session(monkeypatch):
    """A PI, with one active agent that has one proposal reviewed only by the
    engine's implicit `rating=-1` marker — end-to-end through a real page
    render, not just a query-list assertion.
    """
    from src.main import create_app

    statements: list[str] = []
    user_id = uuid.uuid4()
    agent_id = "alpha"
    user = User(
        id=user_id,
        name="Test PI",
        email=None,
        orcid="0000-0000-0000-0001",
        is_admin=False,
        access_status="allowed",
        email_notification_frequency="weekly",
        email_notifications_paused_by_system=False,
    )

    def _make_session():
        return _CannedSession(statements, agent_id=agent_id, user=user)

    async def _canned_get_db():
        async with _make_session() as session:
            yield session

    app = create_app()
    monkeypatch.setattr("src.main.get_session_factory", lambda: _make_session)
    app.dependency_overrides[get_db] = _canned_get_db
    return app, statements, user_id


async def test_badge_ignores_implicit_minus_one_reviews(client_with_recording_session):
    """Issue #20 COR-5/COR-13 residual (Task 20.9c): the engine's implicit
    `rating=-1` review row must not silence the nav badge. The seeded PI has
    one proposal whose only review is that implicit row, so the badge must
    still show 1 unreviewed proposal, not 0.
    """
    app, _statements, user_id = client_with_recording_session
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/settings", headers=_auth(user_id))
    assert r.status_code == 200
    match = re.search(r"rounded-full[^>]*>\s*(\d+)\s*</span>", r.text)
    assert match is not None, "badge span not rendered — implicit rating=-1 row silenced it"
    assert match.group(1) == "1"

"""The nav badge must not count a reopen marker as a completed review (#20 blocker 5).

`AgentBadgeMiddleware` (src/main.py) computes, per agent the signed-in user owns or
is delegated on, `unreviewed = total proposals - reviewed proposals`, and its
`reviewed` half filtered only the engine's implicit `rating = -1` marker. A PI who
reopens a proposal with guidance gets a second sentinel — `rating = 0`, the
"reopen-with-guidance sentinel" `simulation.py:3510-3512` names next to `-1` — which
is not a score: the dashboard form offers 1-4 only and both writers reject anything
outside that range (`agent_page.py:509`, `email_inbound.py:383`). Counting it as a
review hides outstanding work from the nav badge. Measured read-only on the
disposable production copy (`copi_verify`, 2026-09-04): 233 rating=0 rows against 0
rating=-1 rows, mis-counting 12 of 53 active agents (wiseman by 89, briney 29, su 21).
`src/routers/admin.py`'s two readers were fixed in `9505554`; this is the third.

Harness note: the badge count has no rendered seam a test can read — it lands on
`request.state.agent_badge_count` and only the nav template shows it — so these tests
mount a one-line probe route on their own `create_app()` and read the value the
middleware left there. They also repoint the middleware's session factory at the
rolled-back `db_session`, because the middleware calls `get_session_factory()`
directly rather than the injected `get_db` (see tests/conftest.py:107-121), and would
otherwise not see rows this test never commits.
"""

import base64
import json

import httpx
import pytest
from fastapi import Request
from httpx import ASGITransport
from itsdangerous import TimestampSigner

from src.config import get_settings
from src.models import ProposalReview
from tests import factories

pytestmark = pytest.mark.integration


def _auth(user_id) -> dict:
    """Forge the signed session cookie SessionMiddleware would issue."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return {"Cookie": f"copi-session={signer.sign(data).decode()}"}


class _SessionCM:
    """Stands in for `async with get_session_factory()() as db`."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def _badge_count(db_session, monkeypatch, user):
    from src.main import create_app

    monkeypatch.setattr(
        "src.main.get_session_factory", lambda: lambda: _SessionCM(db_session)
    )
    app = create_app()

    @app.get("/_badge_probe")
    async def _badge_probe(request: Request):
        return {"badge": request.state.agent_badge_count}

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        r = await c.get("/_badge_probe", headers=_auth(user.id))
    assert r.status_code == 200
    return r.json()["badge"]


async def _pi_with_two_proposals(db_session, agent_id, bot_name, email):
    pi = await factories.make_user(db_session, email=email)
    agent = await factories.make_agent(
        session=db_session, user=pi, agent_id=agent_id, bot_name=bot_name, status="active"
    )
    decisions = [
        await factories.make_thread_decision(
            db_session, agent_a=agent.agent_id, agent_b="beta", outcome="proposal"
        )
        for _ in range(2)
    ]
    return pi, agent, decisions


async def test_a_real_rating_counts_as_reviewed(db_session, monkeypatch):
    """Control: the probe reads a genuine count, so a 0 below means 0, not a broken harness."""
    pi, agent, decisions = await _pi_with_two_proposals(
        db_session, "badgerated", "BadgeRatedBot", "badge-rated@example.org"
    )
    db_session.add(
        ProposalReview(
            thread_decision_id=decisions[0].id,
            agent_id=agent.agent_id,
            user_id=pi.id,
            rating=3,
            submitted_via="web",
        )
    )
    await db_session.flush()

    assert await _badge_count(db_session, monkeypatch, pi) == 1


async def test_reopen_marker_does_not_count_as_reviewed(db_session, monkeypatch):
    """rating=0 is a reopen marker, so both proposals are still outstanding."""
    pi, agent, decisions = await _pi_with_two_proposals(
        db_session, "badgereopen", "BadgeReopenBot", "badge-reopen@example.org"
    )
    db_session.add(
        ProposalReview(
            thread_decision_id=decisions[0].id,
            agent_id=agent.agent_id,
            user_id=pi.id,
            rating=0,
            comment="[Reopened] please cost the mouse work first",
            submitted_via="web",
        )
    )
    await db_session.flush()

    assert await _badge_count(db_session, monkeypatch, pi) == 2, (
        "a reopen marker was counted as a completed review, so the badge under-reports "
        "the PI's outstanding proposals"
    )


async def test_engine_implicit_marker_still_does_not_count_as_reviewed(
    db_session, monkeypatch
):
    """The -1 exclusion this replaces must survive: the engine's implicit review."""
    pi, agent, decisions = await _pi_with_two_proposals(
        db_session, "badgeimplicit", "BadgeImplicitBot", "badge-implicit@example.org"
    )
    db_session.add(
        ProposalReview(
            thread_decision_id=decisions[0].id,
            agent_id=agent.agent_id,
            user_id=pi.id,
            rating=-1,
            submitted_via="engine",
        )
    )
    await db_session.flush()

    assert await _badge_count(db_session, monkeypatch, pi) == 2

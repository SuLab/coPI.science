"""The SECOND impersonation gate: ``AgentBadgeMiddleware`` in src/main.py.

``get_current_user`` honours the unsigned, client-supplied ``copi-impersonate``
cookie only for an admin (F7). ``AgentBadgeMiddleware`` re-implements that check
independently, because it runs before any dependency and opens its own session:
it re-reads ``select(User.is_admin)`` and only then swaps the uid it computes the
nav badge count for.

Nothing covered that copy. ``test_manager_views.py``'s
``test_a_hand_set_impersonate_cookie_is_ignored_for_a_manager`` asserts status
codes only (200 on /manager/pis, 403 on /admin/users), and both of those come
from the dependency-side check — deleting ``if is_admin:`` from main.py left the
whole suite green. This file closes that.

The observable is the badge count itself, so the test needs the middleware's
own session rather than the request-scoped ``get_db`` override. ``tests/
conftest.py``'s ``client`` fixture deliberately repoints that factory at a
*separate committed connection*, which by construction cannot see rows written
inside the test's rolled-back transaction — so through the shared ASGI client
every badge count is 0 and the difference under test does not exist. Hence the
small probe app below, whose factory yields the test's own session.
"""

from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport
from starlette.middleware.sessions import SessionMiddleware

from src.config import get_settings
from src.main import AgentBadgeMiddleware
from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, AgentDelegate
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


@pytest.fixture
def probe(db_session, monkeypatch):
    """An ASGI client over just AgentBadgeMiddleware, reading the count back.

    ``request.state.agent_badge_count`` is what templates/base.html renders in
    the My Agent pill; returning it directly keeps the assertion on the value
    the middleware computed rather than on nav markup that is role-gated and
    would hide the difference.
    """

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr("src.main.get_session_factory", lambda: _session)

    app = FastAPI()
    # Added first so it runs INSIDE the session middleware, exactly as
    # create_app() orders them — the middleware reads request.session.
    app.add_middleware(AgentBadgeMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=get_settings().secret_key,
        session_cookie="copi-session",
    )

    @app.get("/probe")
    async def _probe(request: Request):
        return {"badge": request.state.agent_badge_count}

    return app


async def _badge(app, user_id, impersonate=None) -> int:
    headers = auth_headers(user_id)
    if impersonate is not None:
        headers["Cookie"] += f"; copi-impersonate={impersonate}"
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/probe", headers=headers)
    assert r.status_code == 200
    return r.json()["badge"]


@pytest.fixture
async def cast(db_session):
    """A manager whose own badge count is 1, and an admin whose is 3.

    Three distinguishable values matter here. 3 is what the manager would see
    if main.py's ``if is_admin:`` were deleted; 1 is the manager's own; and 0
    is what the middleware yields when it fails outright (it swallows and logs
    every exception). Giving the manager a delegated lab rather than none is
    what separates "saw their own count" from "the middleware crashed" — with
    a manager count of 0 those two are the same observation.
    """
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN, name="Adm Owner")
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER, name="Mgr Badge")
    bystander = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, name="Adm Bystander"
    )
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Pi Owner")

    admin_lab = await factories.make_agent(
        db_session, user=admin, agent_id="hublab", status="active"
    )
    delegated_lab = await factories.make_agent(
        db_session, user=pi, agent_id="delegatedlab", status="active"
    )
    db_session.add(
        AgentDelegate(agent_registry_id=delegated_lab.id, user_id=mgr.id)
    )

    for _ in range(3):
        await factories.make_thread_decision(
            db_session, agent_a=admin_lab.agent_id, agent_b="other", outcome="proposal"
        )
    await factories.make_thread_decision(
        db_session, agent_a=delegated_lab.agent_id, agent_b="other", outcome="proposal"
    )
    await db_session.flush()
    return {"admin": admin, "mgr": mgr, "bystander": bystander}


async def test_the_fixture_counts_are_distinct(probe, cast):
    """Control 1. Without it, every assertion below could be satisfied by a
    middleware that computes nothing at all."""
    assert await _badge(probe, cast["admin"].id) == 3
    assert await _badge(probe, cast["mgr"].id) == 1
    assert await _badge(probe, cast["bystander"].id) == 0


async def test_an_admin_impersonating_gets_the_targets_badge_count(probe, cast):
    """Control 2: the cookie really does swap the uid for an admin. Without
    this, deleting the whole impersonation block (not just its gate) would
    make the assertion below pass vacuously."""
    assert await _badge(probe, cast["bystander"].id, impersonate=cast["admin"].id) == 3


async def test_a_manager_impersonating_still_gets_their_own_badge_count(probe, cast):
    """The finding. This is the assertion that fails if ``if is_admin:`` is
    removed from src/main.py's AgentBadgeMiddleware: the manager's uid would be
    swapped for the admin's and the count would come back 3.

    A manager satisfies neither is_admin nor the SQL form of it — the column
    predicate compiles to ``users.user_role = 'admin'`` — so the swap must not
    happen, and the count must stay the manager's own 1.
    """
    assert await _badge(probe, cast["mgr"].id, impersonate=cast["admin"].id) == 1


async def test_a_pi_impersonating_still_gets_their_own_badge_count(probe, cast, db_session):
    """Same gate from the PI side. The cookie is unsigned and client-supplied,
    so *any* logged-in user can set it; is_admin is the only thing stopping
    them."""
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Pi Nosy")
    await db_session.flush()
    assert await _badge(probe, pi.id, impersonate=cast["admin"].id) == 0

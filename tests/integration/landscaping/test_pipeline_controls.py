"""The pipeline controls router, ``/landscape`` (spec §13.4; L28, L43, L55).

``src/main.py`` does not mount the router here, so each test drives an app built the way
``create_app`` stacks the session and Origin-guard middleware, with ``get_db`` routed to the
test's rolled-back session. Every name is invented.
"""
import json
import uuid
from urllib.parse import urlsplit

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from src.config import get_settings
from src.database import get_db
from src.landscaping.review_batches import EXPERTS_KEY
from src.main import OriginGuardMiddleware, session_cookie_name
from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AppSetting,
)
from src.models.landscape import (
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeReport,
    LandscapeRescue,
    LandscapeScreen,
)
from src.routers import landscape
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


def _own_origin() -> str:
    parts = urlsplit(get_settings().base_url)
    return f"{parts.scheme}://{parts.netloc}"


@pytest_asyncio.fixture
async def controls_app(db_session):
    settings = get_settings()
    app = FastAPI()
    app.include_router(landscape.router, prefix="/landscape")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=session_cookie_name(settings),
        https_only=not settings.allow_http_sessions,
        same_site="lax",
    )
    # Added after the session middleware, so it is outside it, as in create_app().
    app.add_middleware(OriginGuardMiddleware)

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def controls(controls_app):
    async with httpx.AsyncClient(
        transport=ASGITransport(app=controls_app),
        base_url="http://testserver",
        headers={"Origin": _own_origin()},
    ) as c:
        yield c


async def _setting(db, key: str) -> str | None:
    return await db.scalar(select(AppSetting.value).where(AppSetting.key == key))


def test_landscape_router_posts_are_an_explicit_allowlist():
    """Reason-tag approval is deliberately not a route: it is set in criteria.toml (L49, D61)."""
    allowed = {
        "/pause",
        "/resume",
        "/backfill/start",
        "/bakeoff/run",
        "/epochs/{epoch_id}/override",
        "/rescue",
        "/positive-controls",
        "/experts",
    }
    methods = {m for r in landscape.router.routes for m in getattr(r, "methods", ())}
    assert methods == {"POST"}
    assert {r.path for r in landscape.router.routes} == allowed


async def test_backfill_start_refused_outside_pilot_or_without_report(controls, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    headers = auth_headers(admin.id)

    # Pilot (unset) but no stored pilot report.
    r = await controls.post("/landscape/backfill/start", headers=headers)
    assert r.status_code == 409
    assert await _setting(db_session, "landscape_phase") is None

    db_session.add(LandscapeReport(kind="pilot", payload={"documents": 1}))
    db_session.add(AppSetting(key="landscape_phase", value="continuous"))
    await db_session.flush()
    r = await controls.post("/landscape/backfill/start", headers=headers)
    assert r.status_code == 409
    assert await _setting(db_session, "landscape_phase") == "continuous"

    row = await db_session.get(AppSetting, "landscape_phase")
    row.value = '"pilot"'
    await db_session.flush()
    r = await controls.post(
        "/landscape/backfill/start", headers=headers, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/workspace/landscape/pipeline"
    await db_session.refresh(row)
    assert row.value == "backfill"


async def test_bakeoff_run_records_a_request(controls, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)

    r = await controls.post("/landscape/bakeoff/run", headers=auth_headers(manager.id))
    assert r.status_code == 403

    for _ in range(2):
        r = await controls.post(
            "/landscape/bakeoff/run", headers=auth_headers(admin.id), follow_redirects=False
        )
        assert r.status_code == 303
    (request_row,) = (
        await db_session.execute(
            select(LandscapeReport).where(LandscapeReport.kind == "bakeoff_request")
        )
    ).scalars()
    assert request_row.handled_at is None
    assert request_row.payload["requested_by"] == str(admin.id)


async def test_override_requires_reason(controls, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    epoch = LandscapeEpoch(status="held", hold_reason="Invented regression miss.")
    db_session.add(epoch)
    await db_session.flush()
    headers = auth_headers(admin.id)

    r = await controls.post(
        f"/landscape/epochs/{epoch.id}/override", data={"reason": "   "}, headers=headers
    )
    assert r.status_code == 400
    await db_session.refresh(epoch)
    assert epoch.status == "held"

    r = await controls.post(
        f"/landscape/epochs/{epoch.id}/override",
        data={"reason": "Reviewed the misses by hand."},
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    await db_session.refresh(epoch)
    assert epoch.status == "candidate"
    assert epoch.override_actor_id == admin.id
    assert epoch.override_reason == "Reviewed the misses by hand."


async def _excluded_screen(db) -> LandscapeScreen:
    epoch = LandscapeEpoch(status="active")
    db.add(epoch)
    await db.flush()
    doc = LandscapeDocument(
        kind="work", canonical_external_id=f"invented:controls-{uuid.uuid4().hex}",
        title="Invented study", summary_text="An invented summary.",
        summary_hash=uuid.uuid4().hex, s1_status="eligible",
    )
    db.add(doc)
    await db.flush()
    row = LandscapeScreen(
        document_id=doc.id, epoch_id=epoch.id, status="excluded:screen", decision="no"
    )
    db.add(row)
    await db.flush()
    return row


async def test_rescue_staff_only(controls, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    row = await _excluded_screen(db_session)
    data = {
        "item_kind": "screen", "item_id": str(row.id), "stage": "screen",
        "expected_status": "excluded:screen", "reason": "Clearly in scope.",
    }

    for user in (reviewer, pi):
        r = await controls.post("/landscape/rescue", data=data, headers=auth_headers(user.id))
        assert r.status_code == 403
    r = await controls.post(
        "/landscape/rescue", data={**data, "reason": ""}, headers=auth_headers(manager.id)
    )
    assert r.status_code == 400
    assert (await db_session.execute(select(LandscapeRescue))).first() is None

    r = await controls.post(
        "/landscape/rescue", data=data, headers=auth_headers(manager.id), follow_redirects=False
    )
    assert r.status_code == 303
    await db_session.refresh(row)
    assert row.status == "rescued"
    rescue_row = (await db_session.execute(select(LandscapeRescue))).scalar_one()
    assert rescue_row.actor_id == manager.id and rescue_row.reason == "Clearly in scope."

    # The status changed since: a second rescue is a conflict, not a double.
    r = await controls.post("/landscape/rescue", data=data, headers=auth_headers(manager.id))
    assert r.status_code == 409


async def test_impersonation_refused_everywhere(controls, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    other_admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    epoch = LandscapeEpoch(status="held")
    db_session.add(epoch)
    db_session.add(LandscapeReport(kind="pilot", payload={}))
    await db_session.flush()
    headers = auth_headers(admin.id, impersonate=other_admin.id)
    # (route, url, form) for every route of the router.
    posts = [
        ("/pause", "/landscape/pause", {}),
        ("/resume", "/landscape/resume", {}),
        ("/backfill/start", "/landscape/backfill/start", {}),
        ("/bakeoff/run", "/landscape/bakeoff/run", {}),
        (
            "/epochs/{epoch_id}/override",
            f"/landscape/epochs/{epoch.id}/override",
            {"reason": "Invented."},
        ),
        ("/rescue", "/landscape/rescue", {
            "item_kind": "screen", "item_id": str(uuid.uuid4()), "stage": "screen",
            "expected_status": "excluded:screen", "reason": "Invented.",
        }),
        ("/positive-controls", "/landscape/positive-controls", {"identifiers": "10.1234/x.1"}),
        ("/experts", "/landscape/experts", {"user_id": str(reviewer.id), "action": "mark"}),
    ]
    assert {route for route, _, _ in posts} == {r.path for r in landscape.router.routes}

    for _, url, data in posts:
        r = await controls.post(url, data=data, headers=headers, follow_redirects=False)
        assert r.status_code == 403, url

    for key in ("landscape_paused", "landscape_phase", "landscape_positive_controls", EXPERTS_KEY):
        assert await _setting(db_session, key) is None, key
    assert (
        await db_session.execute(
            select(LandscapeReport).where(LandscapeReport.kind == "bakeoff_request")
        )
    ).first() is None
    await db_session.refresh(epoch)
    assert epoch.status == "held"


async def test_origin_guard_applies(controls_app, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=controls_app), base_url="http://testserver"
    ) as no_origin:
        r = await no_origin.post("/landscape/pause", headers=auth_headers(manager.id))
        assert r.status_code == 403
        assert "Cross-site request refused." in r.text
        r = await no_origin.post(
            "/landscape/pause",
            headers={**auth_headers(manager.id), "Origin": "https://elsewhere.example"},
        )
        assert r.status_code == 403
    assert await _setting(db_session, "landscape_paused") is None


async def test_positive_controls_and_experts_store_json_lists(controls, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    headers = auth_headers(manager.id)

    r = await controls.post(
        "/landscape/positive-controls", data={"identifiers": "10.1234/x, not-an-id"},
        headers=headers,
    )
    assert r.status_code == 400
    r = await controls.post(
        "/landscape/positive-controls",
        data={"identifiers": "https://doi.org/10.1234/Invented.A\n12345678; 10.1234/invented.a"},
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert json.loads(await _setting(db_session, "landscape_positive_controls")) == [
        "10.1234/invented.a", "12345678",
    ]

    r = await controls.post(
        "/landscape/experts", data={"user_id": str(pi.id), "action": "mark"}, headers=headers
    )
    assert r.status_code == 400
    for action in ("mark", "mark"):
        r = await controls.post(
            "/landscape/experts", data={"user_id": str(reviewer.id), "action": action},
            headers=headers, follow_redirects=False,
        )
        assert r.status_code == 303
    assert json.loads(await _setting(db_session, EXPERTS_KEY)) == [str(reviewer.id)]
    r = await controls.post(
        "/landscape/experts", data={"user_id": str(reviewer.id), "action": "unmark"},
        headers=headers, follow_redirects=False,
    )
    assert r.status_code == 303
    assert json.loads(await _setting(db_session, EXPERTS_KEY)) == []

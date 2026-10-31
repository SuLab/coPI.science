"""The Landscape workspace pages (spec §13.1, §13.2, §13.4, §12.5; D46, D63).

Seed data comes from ``seed_landscape_opportunity`` (the rendered-page gate's seed); every name
is invented.
"""
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, USER_ROLE_REVIEWER
from src.models.landscape import LandscapeExpertJudgment
from src.routers import workspace
from src.routers.workspace import landscape as landscape_pages
from src.services import landscape_view
from src.web.navigation import WORKSPACE_NAVIGATION, navigation_for
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_rendered_page_gate import page_problems, seed_landscape_opportunity

pytestmark = pytest.mark.integration

_LIST = "/workspace/landscape"
_PIPELINE = "/workspace/landscape/pipeline"


def _routes(router):
    for route in router.routes:
        context = getattr(route, "include_context", None)
        if context:
            yield from _routes(context.included_router)
        else:
            yield route


def _landscape_routes():
    return [r for r in _routes(workspace.router) if "/landscape" in getattr(r, "path", "")]


async def _get(client, user, path):
    return await client.get(path, headers=auth_headers(user.id), follow_redirects=False)


def test_pipeline_route_not_shadowed_by_detail():
    names = [r.name for r in _landscape_routes()]
    assert names.index("workspace_landscape_pipeline") < names.index("workspace_landscape_detail")
    detail = next(r for r in _landscape_routes() if r.name == "workspace_landscape_detail")
    # The UUID converter alone would refuse "pipeline"; the order pins it twice (D46).
    assert detail.path.endswith("{opportunity_id:uuid}")


async def test_pipeline_path_reaches_the_pipeline_handler(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    response = await _get(client, manager, _PIPELINE)
    assert response.status_code == 200
    assert "Landscape pipeline" in response.text


async def test_pi_gets_403(client, db_session):
    opportunity_id = await seed_landscape_opportunity(db_session)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    for path in (_LIST, _PIPELINE, f"{_LIST}/{opportunity_id}"):
        assert (await _get(client, pi, path)).status_code == 403, path


async def test_reviewer_sees_batch_not_lists(client, db_session):
    await seed_landscape_opportunity(db_session)
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)

    response = await _get(client, reviewer, f"{_LIST}?view=current")
    assert response.status_code == 200
    assert "This week&#39;s batch" in response.text or "This week's batch" in response.text
    assert "A gate headline" in response.text
    # No link to the staff lists (the page's own URL still appears in base.html's help
    # dismiss form, as its `next` value).
    assert f'href="{_LIST}?view=current"' not in response.text
    assert "All assessed" not in response.text
    assert (await _get(client, reviewer, _PIPELINE)).status_code == 403

    staff = await _get(client, manager, f"{_LIST}?view=current")
    assert staff.status_code == 200
    assert "Unreviewed (" in staff.text and "All assessed" in staff.text


async def test_nav_item_for_reviewers_and_staff(client, db_session):
    item = next(i for i in WORKSPACE_NAVIGATION if i.section == "landscape")
    assert (item.label, item.route_name) == ("Landscape", "workspace_landscape")
    assert not item.staff_only and not item.admin_only
    for role in (USER_ROLE_REVIEWER, USER_ROLE_MANAGER, USER_ROLE_ADMIN):
        user = await factories.make_user(db_session, user_role=role)
        response = await _get(client, user, "/workspace/assessments")
        assert f'href="{_LIST}"' in response.text, role
    no_research = SimpleNamespace(research=False, staff=False, admin=False)
    assert navigation_for(None, no_research) == ()


async def test_detail_renders_member_sections_and_badges(client, db_session):
    opportunity_id = await seed_landscape_opportunity(db_session)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    response = await _get(client, admin, f"{_LIST}/{opportunity_id}")
    assert response.status_code == 200
    html = response.text
    for anchor in ("brief", "verdict", "gating", "scores", "evidence", "disease-profile",
                   "panel", "reviews", "review"):
        assert f'id="{anchor}"' in html, anchor
    for text in ("Gate programme", "A gate headline", "Target biology", "GATEK drives fibrosis.",
                 "Disease profile", "Rare", "Gate concern", "target essential", "cleared",
                 "A gate experiment.", "Gate red flag", "Raw verdict JSON"):
        assert text in html, text
    assert ">sourced</span>" in html and ">estimate</span>" in html
    assert f'action="/reviews/landscape/{opportunity_id}/decision"' in html


async def test_expert_form_only_on_own_sample_item(client, db_session):
    opportunity_id = await seed_landscape_opportunity(db_session)
    expert = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    other = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    db_session.add(LandscapeExpertJudgment(
        id=uuid.uuid4(), opportunity_id=opportunity_id, expert_id=expert.id,
        expert_name=expert.name, quarter=landscape_view._quarter(datetime.now(UTC)),
    ))
    await db_session.flush()
    action = f'action="/reviews/landscape/{opportunity_id}/expert-judgment"'

    own = await _get(client, expert, f"{_LIST}/{opportunity_id}")
    assert own.status_code == 200 and action in own.text
    # S9's calls are hidden on the expert's own open item (D63).
    assert "Gate score rationale." not in own.text

    theirs = await _get(client, other, f"{_LIST}/{opportunity_id}")
    assert theirs.status_code == 200 and action not in theirs.text


async def test_pages_503_without_0077(client, db_session, monkeypatch):
    opportunity_id = await seed_landscape_opportunity(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)

    async def not_ready(db):
        return False

    monkeypatch.setattr(landscape_pages, "schema_ready", not_ready)
    monkeypatch.setitem(landscape_pages._SCHEMA, "ready", False)
    for path in (_LIST, _PIPELINE, f"{_LIST}/{opportunity_id}"):
        assert (await _get(client, manager, path)).status_code == 503, path


async def test_rendered_gate_passes(client, db_session):
    opportunity_id = await seed_landscape_opportunity(db_session)
    problems: list[str] = []
    for role in (USER_ROLE_REVIEWER, USER_ROLE_MANAGER, USER_ROLE_ADMIN):
        user = await factories.make_user(db_session, user_role=role)
        paths = [_LIST, f"{_LIST}/{opportunity_id}"]
        if role != USER_ROLE_REVIEWER:
            paths += [f"{_LIST}?view=current", f"{_LIST}?view=all&tab=all", _PIPELINE]
        for path in paths:
            response = await _get(client, user, path)
            assert response.status_code == 200, (role, path, response.status_code)
            problems += [f"{role} {path}: {p}" for p in page_problems(response.text)]
    assert not problems, "\n".join(problems)


def test_no_post_on_workspace_router():
    routes = _landscape_routes()
    assert {r.name for r in routes} == {
        "workspace_landscape", "workspace_landscape_pipeline", "workspace_landscape_detail",
    }
    for route in routes:
        assert set(route.methods) - {"HEAD"} == {"GET"}, route.path

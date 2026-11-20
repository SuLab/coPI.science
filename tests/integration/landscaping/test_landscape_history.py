"""An earlier assessment opened from the detail page's History list (contract C8 of
``docs/plans/2026-11-19-landscape-ui-remediation-plan.md``): read-only, no chat, and only for a
row the History list shows.

Seed data comes from ``seed_landscape_opportunity`` (the rendered-page gate's seed); every name
is invented.
"""
import re
import uuid
from datetime import UTC, datetime

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER
from src.models.landscape import LandscapeAssessment, LandscapeEpoch, LandscapeOpportunity
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_rendered_page_gate import page_problems, seed_landscape_opportunity

pytestmark = pytest.mark.integration

_LIST = "/workspace/landscape"
_CHAT_DRAWER = 'id="assessment-chat"'


async def _epoch(db, status: str) -> LandscapeEpoch:
    epoch = LandscapeEpoch(id=uuid.uuid4(), status=status)
    db.add(epoch)
    await db.flush()
    return epoch


async def _assessment(db, epoch: LandscapeEpoch, opportunity_id: uuid.UUID, headline: str):
    row = LandscapeAssessment(
        id=uuid.uuid4(), epoch_id=epoch.id, opportunity_id=opportunity_id,
        opportunity_version=1, status="done", headline=headline,
        elevator_pitch="An earlier invented pitch.", weighted_score=3.2, band="conditional",
        recommendation="conditional", confidence="low", rationale="An earlier rationale.",
        key_points={"why_it_matters": ["An earlier point."]},
    )
    db.add(row)
    await db.flush()
    return row.id


async def _world(db):
    """The seeded opportunity, with one listed History row (a retired epoch's assessment),
    one unlisted row (a candidate epoch's) and another opportunity's assessment."""
    opportunity_id = await seed_landscape_opportunity(db)
    retired = await _epoch(db, "retired")
    listed = await _assessment(db, retired, opportunity_id, "An earlier invented headline")
    candidate = await _assessment(
        db, await _epoch(db, "candidate"), opportunity_id, "A candidate invented headline"
    )
    other = LandscapeOpportunity(id=uuid.uuid4(), version_counter=1)
    db.add(other)
    await db.flush()
    foreign = await _assessment(db, retired, other.id, "A foreign invented headline")
    return opportunity_id, listed, candidate, foreign


async def _get(client, user, path):
    return await client.get(path, headers=auth_headers(user.id), follow_redirects=False)


def _main(html: str) -> str:
    """The page's ``<main>`` minus base.html's first-sign-in box, whose dismiss form is not
    the page's own."""
    # base.html's skip-link comment also spells `<main id="main-content">`; the tag has a class.
    body = html[html.index('<main id="main-content" class='):html.index("</main>")]
    return re.sub(r'<div role="region" aria-label="Getting started".*?</form>\s*</div>', "",
                  body, flags=re.S)


async def test_history_view_listed_row_renders(client, db_session):
    opportunity_id, listed, _, _ = await _world(db_session)
    for role in (USER_ROLE_ADMIN, USER_ROLE_MANAGER):
        user = await factories.make_user(db_session, user_role=role)
        response = await _get(client, user, f"{_LIST}/{opportunity_id}?assessment={listed}")
        assert response.status_code == 200, role
        main = _main(response.text)
        assert "An earlier invented headline" in main, role
        assert "<form" not in main, role


async def test_history_view_rejects_candidate_row_foreign_and_malformed(client, db_session):
    opportunity_id, _, candidate, foreign = await _world(db_session)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    for value in (candidate, foreign, uuid.uuid4(), "bad"):
        response = await _get(client, admin, f"{_LIST}/{opportunity_id}?assessment={value}")
        assert response.status_code == 404, value
    # The opportunity's own page still answers.
    assert (await _get(client, admin, f"{_LIST}/{opportunity_id}")).status_code == 200


async def test_history_view_has_no_chat(client, db_session, asgi_app):
    asgi_app.state.assessment_chat_enabled = True
    asgi_app.state.landscape_chat_enabled = True
    opportunity_id, listed, _, _ = await _world(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)

    # Positive control: with both switches on, the current assessment has the drawer.
    current = await _get(client, manager, f"{_LIST}/{opportunity_id}")
    assert current.status_code == 200 and _CHAT_DRAWER in current.text

    history = await _get(client, manager, f"{_LIST}/{opportunity_id}?assessment={listed}")
    assert history.status_code == 200
    assert _CHAT_DRAWER not in history.text
    assert "data-chat-open" not in history.text


async def test_history_view_page_problems(client, db_session):
    opportunity_id, listed, _, _ = await _world(db_session)
    for role in (USER_ROLE_ADMIN, USER_ROLE_MANAGER):
        user = await factories.make_user(db_session, user_role=role)
        response = await _get(client, user, f"{_LIST}/{opportunity_id}?assessment={listed}")
        assert response.status_code == 200, role
        assert page_problems(response.text) == [], role


async def test_history_links_only_done_rows_and_keep_state(client, db_session):
    """Only a done History row links (a failed one would 404); every link answers 200 and
    carries the list state (audit D2, review L2)."""
    opportunity_id, listed, _, _ = await _world(db_session)
    retired = await _epoch(db_session, "retired")
    failed = LandscapeAssessment(
        id=uuid.uuid4(), epoch_id=retired.id, opportunity_id=opportunity_id,
        opportunity_version=1, status="failed:errored", headline="A failed invented headline",
    )
    db_session.add(failed)
    await db_session.flush()
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    page = await _get(client, manager, f"{_LIST}/{opportunity_id}?view=current&tab=reviewed")
    assert page.status_code == 200
    hrefs = re.findall(r'href="([^"]*assessment=[^"]*)"', page.text)
    assert hrefs and all(str(failed.id) not in h for h in hrefs)
    assert any(str(listed) in h and "view=current" in h and "tab=reviewed" in h for h in hrefs)
    for href in hrefs:
        response = await _get(client, manager, href.replace("&amp;", "&"))
        assert response.status_code == 200, href


async def test_history_view_omits_votes_assignments_and_active_epoch_notes(client, db_session):
    """The history view shows the assessment's own sections only: no decisions, reviews or
    assignments, and nothing about the active epoch's criteria (audit D3, D4)."""
    opportunity_id, listed, _, _ = await _world(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    main = _main((await _get(client, manager, f"{_LIST}/{opportunity_id}?assessment={listed}")).text)
    assert "Decisions and reviews" not in main
    assert "Assigned" not in main
    assert "admin override" not in main
    assert "judged under criteria" not in main


async def test_retired_opportunity_page_keeps_its_forms(client, db_session):
    """A retired opportunity's own page is read as history but keeps its decision form
    (Review Focus 5)."""
    opportunity_id = await seed_landscape_opportunity(db_session)
    opportunity = await db_session.get(LandscapeOpportunity, opportunity_id)
    opportunity.retired_at = datetime.now(UTC)
    await db_session.flush()
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    response = await _get(client, manager, f"{_LIST}/{opportunity_id}")
    assert response.status_code == 200
    assert f'action="/reviews/landscape/{opportunity_id}/decision"' in response.text

"""The open view end to end (src/web/open_view.py): with ``LANDSCAPE_OPEN_VIEW`` on, a visitor
with no session reads the Landscape list and an opportunity's page with no form that writes,
nothing else opens, nothing can be written, and there is no sign-in; with it off, nothing
changes."""
import re

import pytest
from sqlalchemy import func, select

from src.config import get_settings
from src.models import USER_ROLE_MANAGER
from src.models.landscape import (
    LandscapeDecision,
    LandscapeIdea,
    LandscapeOpportunityVersion,
    LandscapeRanking,
)
from src.web.open_view import OPEN_LANDING
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_rendered_page_gate import seed_landscape_opportunity

pytestmark = pytest.mark.integration

_LIST = "/workspace/landscape"
_POST_FORM = re.compile(r'<form[^>]*method="post"', re.IGNORECASE)


@pytest.fixture
def open_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "landscape_open_view", True)


async def _anon(client, path):
    return await client.get(path, follow_redirects=False)


async def test_visitor_reads_the_list_read_only(client, db_session, open_on):
    await seed_landscape_opportunity(db_session)
    response = await _anon(client, OPEN_LANDING)
    assert response.status_code == 200
    page = response.text
    assert "A gate headline" in page
    assert not _POST_FORM.search(page)
    assert "Read-only view" in page and "Sign out" not in page
    assert "workspace/landscape/pipeline" not in page
    assert 'href="/workspace/pis"' not in page and 'href="/settings"' not in page


async def test_visitor_reads_an_opportunity_read_only(client, db_session, open_on):
    opportunity_id = await seed_landscape_opportunity(db_session)
    response = await _anon(client, f"{_LIST}/{opportunity_id}")
    assert response.status_code == 200
    page = response.text
    assert "A gate headline" in page
    assert not _POST_FORM.search(page)
    assert "Your decision and review" not in page and "landscape-human-review" not in page


async def test_head_is_served_too(client, db_session, open_on):
    await seed_landscape_opportunity(db_session)
    assert (await client.head(_LIST, follow_redirects=False)).status_code == 200


@pytest.mark.parametrize("path", [
    f"{_LIST}/pipeline", "/workspace", "/workspace/pis", "/workspace/assessments",
    "/settings", "/admin/users", "/profile",
])
async def test_nothing_else_opens(client, db_session, open_on, path):
    response = await _anon(client, path)
    assert response.status_code == 302
    assert response.headers["location"].startswith("/login")


async def test_sign_in_is_off(client, open_on):
    for path in ("/", "/login", "/login?next=/workspace/pis"):
        response = await _anon(client, path)
        assert response.status_code == 302 and response.headers["location"] == OPEN_LANDING, path
    for path in ("/login/start", "/auth/callback?code=x&state=y", "/auth/microsoft/start",
                 "/auth/microsoft/callback?code=x&state=y"):
        assert (await _anon(client, path)).status_code == 404, path


async def test_visitor_cannot_write(client, db_session, open_on):
    opportunity_id = await seed_landscape_opportunity(db_session)
    before = await db_session.scalar(select(func.count()).select_from(LandscapeDecision))
    for path in (f"/reviews/landscape/{opportunity_id}/decision",
                 f"/reviews/landscape/{opportunity_id}/review",
                 f"/reviews/landscape/{opportunity_id}/assign",
                 "/landscape/pause"):
        response = await client.post(path, data={"decision": "pursue", "score": "5"},
                                     follow_redirects=False)
        assert response.status_code in (302, 401, 403), (path, response.status_code)
        assert not response.headers.get("location", "").startswith(_LIST), path
    after = await db_session.scalar(select(func.count()).select_from(LandscapeDecision))
    assert after == before


async def test_signed_in_user_keeps_their_own_page(client, db_session, open_on):
    opportunity_id = await seed_landscape_opportunity(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    response = await client.get(f"{_LIST}/{opportunity_id}", headers=auth_headers(manager.id),
                                follow_redirects=False)
    assert response.status_code == 200
    assert "Read-only view" not in response.text
    assert _POST_FORM.search(response.text)


async def test_off_means_sign_in_as_before(client, db_session):
    assert get_settings().landscape_open_view is False
    opportunity_id = await seed_landscape_opportunity(db_session)
    for path in (_LIST, f"{_LIST}/{opportunity_id}", "/"):
        response = await _anon(client, path)
        assert response.status_code == 302 and response.headers["location"].startswith("/login")
    assert (await _anon(client, "/login")).status_code == 200


async def test_s9_off_pages_rank_by_s7_and_show_s6(client, db_session, open_on, monkeypatch):
    """The Scripps pages with S9 off (src/landscaping/s9_switch.py): the list and the detail
    page give the pairwise rank and the members' S6 records and sources, and nothing of the
    stored S9 assessment, not even a note that one is missing."""
    monkeypatch.setattr(get_settings(), "landscape_final_enabled", False)
    opportunity_id = await seed_landscape_opportunity(db_session)
    keys = await db_session.scalar(
        select(LandscapeOpportunityVersion.member_idea_keys)
        .where(LandscapeOpportunityVersion.opportunity_id == opportunity_id)
    )
    ideas = (await db_session.scalars(
        select(LandscapeIdea).where(LandscapeIdea.idea_key.in_(keys)).order_by(LandscapeIdea.idea_key)
    )).all()
    for position, idea in enumerate(ideas, start=3):
        db_session.add(LandscapeRanking(epoch_id=idea.epoch_id, idea_id=idea.id, position=position))
    await db_session.flush()
    s9_texts = ("A gate headline", "First gate sentence", "Gate score rationale",
                "Why this score", "Gate red flag", "Gate rationale", "a gate experiment",
                "Gate strength", "No completed assessment", "No assessment to show",
                'id="verdict"', 'id="scores"', 'id="gating"', 'id="evidence"', "landscape-tiles")

    for path in (OPEN_LANDING, _LIST):
        page = (await _anon(client, path)).text
        assert "Gate programme" in page and "Rank 3" in page, path
        assert "Gate second S6 summary." in page and "Gate fibrosis kinase study" in page, path
        assert "In-depth analysis" in page and "pairwise-ranking order" in page, path
        assert not [t for t in s9_texts if t in page], path

    page = (await _anon(client, f"{_LIST}/{opportunity_id}")).text
    assert "Rank 3" in page and "in the pairwise ranking" in page
    assert "Gate second S6 summary." in page and "Gate liability reason." in page
    assert not [t for t in s9_texts if t in page]
    assert not _POST_FORM.search(page)

    # A signed-in manager's review form refers to no assessment or bot score.
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    page = (await client.get(f"{_LIST}/{opportunity_id}", headers=auth_headers(manager.id),
                             follow_redirects=False)).text
    assert "landscape-review-form" in page and "Rank 3" in page
    for text in ("BlackbirdBot did not score this", "BlackbirdBot scored",
                 "BlackbirdBot's own summary", "the assessment got wrong", *s9_texts):
        assert text not in page, text

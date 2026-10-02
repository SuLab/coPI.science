# tests/integration/test_public_profile_empty_save.py
"""D-15: a public-profile save with every field blank, for a PI who has no
ResearcherProfile yet, is refused instead of minting an empty profile."""

import pytest
from sqlalchemy import select

from src.models import ResearcherProfile
from src.services import profile_export
from tests import factories
from tests.integration._webui_helpers import follow
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_BLANK = {"research_summary": "", "techniques": "", "experimental_models": "",
          "disease_areas": "", "key_targets": "", "keywords": "", "profile_version": ""}


async def _profile_of(db, user):
    return (await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user.id)
    )).scalar_one_or_none()


async def test_an_empty_save_with_no_profile_is_refused(client, db_session):
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(
        f"/agent/{agent.agent_id}/public-profile/save", data=_BLANK,
        headers=auth_headers(pi.id), follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == f"/agent/{agent.agent_id}/public-profile/edit"
    assert await _profile_of(db_session, pi) is None
    page = await follow(client, r)
    assert "nothing to save yet" in page.text


async def test_a_non_empty_first_save_still_creates_the_profile(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path)
    pi = await factories.make_user(db_session)
    agent = await factories.make_agent(db_session, user=pi)
    r = await client.post(
        f"/agent/{agent.agent_id}/public-profile/save",
        data={**_BLANK, "research_summary": "First words"},
        headers=auth_headers(pi.id), follow_redirects=False,
    )
    assert r.status_code == 302
    profile = await _profile_of(db_session, pi)
    assert profile is not None and profile.research_summary == "First words"

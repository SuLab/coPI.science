"""/profile and /onboarding show the persona's grant sections (spec 2026-10-05 §6.1)."""
from datetime import UTC, datetime

import pytest

from src.models import Job, PiGrant, PiGrantIdentity, PiOrcidFunding
from src.services.jhu_rules import set_tenure_start
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _seed(db_session, *, onboarding_complete=True):
    pi = await factories.make_user(db_session, onboarding_complete=onboarding_complete)
    await factories.make_profile(db_session, user=pi)
    await set_tenure_start(pi.id, 2010, "manual", db=db_session)
    db_session.add(PiGrantIdentity(user_id=pi.id, status="resolved", accepted_profile_ids=[7]))
    db_session.add(PiGrant(
        user_id=pi.id, core_project_num="R01AA000001", reporter_profile_id=7, title="Live award",
        activity_code="R01", org_name="JHU", tenure_filter_mode="org_and_year", first_fy=2021,
        last_fy=2026, project_end=datetime(2099, 6, 30, tzinfo=UTC),
    ))
    db_session.add(PiOrcidFunding(
        user_id=pi.id, group_key="k", title="Old fellowship", funder_name="Golden Foundation",
        start_year=2012, end_year=2014,
    ))
    db_session.add(Job(type="generate_profile", user_id=pi.id, payload={}, status="completed"))
    await db_session.commit()
    return pi


async def test_profile_page_shows_active_and_past_sections(client, db_session):
    pi = await _seed(db_session)
    html = (await client.get("/profile", headers=auth_headers(pi.id))).text
    assert "Active Grants" in html and "Live award (NIH R01, 2021–2099)" in html
    assert "Past Grants (since 2010)" in html
    assert "Old fellowship (Golden Foundation, 2012–2014)" in html


async def test_onboarding_review_uses_the_new_heading(client, db_session):
    # /onboarding 302s to /profile once onboarding is complete (onboarding_start).
    pi = await _seed(db_session, onboarding_complete=False)
    html = (await client.get("/onboarding", headers=auth_headers(pi.id))).text
    assert "Grants (NIH RePORTER and ORCID)" in html and "Live award (NIH R01, 2021–2099)" in html
    assert "From your ORCID profile" not in html

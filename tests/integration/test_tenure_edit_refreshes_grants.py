"""A manager's tenure edit requests a regeneration (generate_profile, INTERACTIVE), whose
step 10 refreshes grants (spec 2026-10-05 §6.3, D20)."""
import pytest
from sqlalchemy import select

from src.models import USER_ROLE_MANAGER, Job
from src.models.job import INTERACTIVE_PRIORITY
from src.services.jhu_rules import set_tenure_start
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _edit(client, db_session, year: str):
    """Post the manager form with the profile's current version and summary; assert the
    save succeeded (version bumped) and return every job requested for the PI."""
    pi = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=pi)
    await set_tenure_start(pi.id, 2005, "manual", db=db_session)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await db_session.commit()
    version = profile.profile_version
    r = await client.post(f"/manager/pis/{pi.id}/profile", data={
        "name": pi.name, "email": pi.email, "institution": pi.institution or "",
        "department": "", "jhu_tenure_start": year,
        "research_summary": profile.research_summary,
        "profile_version": str(version),
    }, headers=auth_headers(mgr.id), follow_redirects=False)
    assert r.status_code == 302
    assert "error=" not in r.headers["location"]
    await db_session.refresh(profile)
    assert profile.profile_version == version + 1
    return (await db_session.execute(
        select(Job).where(Job.user_id == pi.id)
    )).scalars().all()


async def test_a_new_tenure_year_requests_a_regeneration_only(client, db_session):
    jobs = await _edit(client, db_session, "2012")
    assert [(j.type, j.status, j.priority) for j in jobs] == [
        ("generate_profile", "pending", INTERACTIVE_PRIORITY)
    ]


async def test_reposting_the_recorded_year_requests_nothing(client, db_session):
    assert await _edit(client, db_session, "2005") == []

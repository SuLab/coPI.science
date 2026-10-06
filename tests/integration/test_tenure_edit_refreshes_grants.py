"""A manager's tenure edit requests enrich_grants, so RePORTER rows are re-fetched under
the new fiscal-year filter (spec 2026-10-05 §6.1)."""
import pytest
from sqlalchemy import select

from src.models import USER_ROLE_MANAGER, Job
from src.models.job import INTERACTIVE_PRIORITY
from src.services.jhu_rules import set_tenure_start
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _edit(client, db_session, year: str):
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    await set_tenure_start(pi.id, 2005, "manual", db=db_session)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await db_session.commit()
    r = await client.post(f"/manager/pis/{pi.id}/profile", data={
        "name": pi.name, "email": pi.email, "institution": pi.institution or "",
        "department": "", "jhu_tenure_start": year,
    }, headers=auth_headers(mgr.id), follow_redirects=False)
    assert r.status_code == 302
    return (await db_session.execute(
        select(Job).where(Job.user_id == pi.id, Job.type == "enrich_grants")
    )).scalars().all()


async def test_a_new_tenure_year_requests_enrich_grants(client, db_session):
    [job] = await _edit(client, db_session, "2012")
    assert job.status == "pending" and job.priority == INTERACTIVE_PRIORITY


async def test_reposting_the_recorded_year_requests_nothing(client, db_session):
    assert await _edit(client, db_session, "2005") == []

"""scripts/enqueue_company_discovery.py (spec §7.5 Job, §9 step 6): a dry run unless
--apply, every PI with an AgentRegistry row, bulk priority, idempotent."""
import pytest
from sqlalchemy import select

from scripts.enqueue_company_discovery import enqueue_for_all
from src.models import Job
from src.models.job import BULK_PRIORITY
from tests import factories

pytestmark = pytest.mark.integration


async def _discovery_jobs(db, user_id) -> list[Job]:
    return list((await db.execute(
        select(Job).where(Job.user_id == user_id, Job.type == "company_discovery")
    )).scalars().all())


async def test_dry_run_enqueues_nothing(db_session, capsys):
    user = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=user)
    assert await enqueue_for_all(db_session, apply=False, orcid=user.orcid) == 1
    assert await _discovery_jobs(db_session, user.id) == []
    assert f"would enqueue company_discovery for {user.name} ({user.orcid})" in capsys.readouterr().out


async def test_apply_enqueues_once_at_bulk_priority_for_pis_with_an_agent(db_session):
    with_agent = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=with_agent)
    without_agent = await factories.make_user(db_session)

    await enqueue_for_all(db_session, apply=True, orcid=None)
    await enqueue_for_all(db_session, apply=True, orcid=None)

    (job,) = await _discovery_jobs(db_session, with_agent.id)
    assert job.priority == BULK_PRIORITY and job.status == "pending"
    assert await _discovery_jobs(db_session, without_agent.id) == []


async def test_orcid_limits_the_run_to_one_pi(db_session):
    one = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=one)
    other = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=other)
    assert await enqueue_for_all(db_session, apply=True, orcid=one.orcid) == 1
    assert len(await _discovery_jobs(db_session, one.id)) == 1
    assert await _discovery_jobs(db_session, other.id) == []

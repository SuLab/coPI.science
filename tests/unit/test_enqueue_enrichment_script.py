import pytest
from sqlalchemy import select

from scripts.enqueue_enrichment import enqueue_for_all
from src.models import Job, ResearcherProfile, User

pytestmark = pytest.mark.integration


async def test_dry_run_enqueues_nothing_and_apply_enqueues_two_per_pi(db_session):
    u = User(orcid="0000-0003-0000-0003", name="P I", user_role="pi")
    db_session.add(u)
    await db_session.flush()
    db_session.add(ResearcherProfile(user_id=u.id))
    await db_session.flush()
    n = await enqueue_for_all(db_session, apply=False, only=None, orcid=None)
    assert n == 1
    assert (await db_session.execute(select(Job).where(Job.user_id == u.id))).scalars().all() == []
    await enqueue_for_all(db_session, apply=True, only=None, orcid=None)
    types = sorted(j.type for j in (await db_session.execute(select(Job).where(Job.user_id == u.id))).scalars().all())
    assert types == ["enrich_grants", "industry_evidence"]


async def test_duplicate_pending_jobs_of_same_type_do_not_raise(db_session):
    u = User(orcid="0000-0003-0000-0004", name="Dup PI", user_role="pi")
    db_session.add(u)
    await db_session.flush()
    db_session.add(ResearcherProfile(user_id=u.id))
    db_session.add(Job(type="enrich_grants", user_id=u.id, status="pending", payload={}))
    db_session.add(Job(type="enrich_grants", user_id=u.id, status="pending", payload={}))
    await db_session.flush()

    await enqueue_for_all(db_session, apply=True, only=None, orcid=None)

    jobs = (await db_session.execute(select(Job).where(Job.user_id == u.id))).scalars().all()
    assert sum(1 for j in jobs if j.type == "enrich_grants") == 2
    assert sum(1 for j in jobs if j.type == "industry_evidence") == 1

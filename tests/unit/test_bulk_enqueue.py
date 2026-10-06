"""scripts/_bulk_enqueue.py (spec 2026-10-05 §4.2, D53)."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import scripts._bulk_enqueue as be
from src.models import Job
from src.models.job import BULK_PRIORITY
from tests import factories

START = datetime(2026, 10, 6, tzinfo=UTC)


def test_zero_credit_jobs_are_not_staggered():
    slots = be.plan_schedule([uuid.uuid4(), uuid.uuid4()], job_type="enrich_grants", start=START,
                             credits_per_job=0, daily_credits=500)
    assert all(s.not_before is None and s.followon_not_before is None for s in slots)


def test_slots_keep_one_retry_of_headroom_and_space_followons():
    ids = [uuid.uuid4() for _ in range(3)]
    slots = be.plan_schedule(ids, job_type="generate_profile", start=START, credits_per_job=13, daily_credits=500)
    interval = timedelta(seconds=86400 * 13 * be.RETRY_HEADROOM / 500)
    assert [s.not_before for s in slots] == [START, START + interval, START + 2 * interval]
    assert slots[1].followon_not_before == START + interval + interval / 2


@pytest.mark.integration
async def test_population_is_pis_plus_pi_lab_owners(db_session):
    pi = await factories.make_user(db_session)
    owner = await factories.make_user(db_session, user_role="manager")
    await factories.make_agent(db_session, user=owner, role="pi_lab")
    bystander = await factories.make_user(db_session, user_role="manager")
    ids = {uid for uid, _, _ in await be.pi_population(db_session)}
    assert {pi.id, owner.id} <= ids and bystander.id not in ids


@pytest.mark.integration
async def test_enqueue_is_bulk_tagged_and_resume_requeues_only_dead_tagged_jobs(db_session):
    a = await factories.make_user(db_session)
    b = await factories.make_user(db_session)
    created = await be.enqueue_paced(db_session, job_type="enrich_grants", users=[(a.id, a.orcid), (b.id, b.orcid)],
                                     tag="t1", start=START, credits_per_job=0, daily_credits=500)
    jobs = (await db_session.execute(select(Job).where(Job.id.in_(created)))).scalars().all()
    assert len(jobs) == 2 and all(j.priority == BULK_PRIORITY and j.payload[be.TAG_KEY] == "t1" for j in jobs)
    dead = next(j for j in jobs if j.user_id == a.id)
    dead.status = "dead"
    next(j for j in jobs if j.user_id == b.id).status = "completed"
    await db_session.flush()
    again = await be.resume_dead(db_session, job_type="enrich_grants", tag="t1", credits_per_job=0,
                                 daily_credits=500)
    assert [(await db_session.get(Job, i)).user_id for i in again] == [a.id]


@pytest.mark.integration
async def test_resumed_jobs_are_staggered_from_now(db_session):
    users = [await factories.make_user(db_session) for _ in range(2)]
    for u in users:
        db_session.add(Job(type="enrich_grants", user_id=u.id, status="dead",
                           payload={"user_id": str(u.id), be.TAG_KEY: "t2"}))
    await db_session.flush()
    before = datetime.now(UTC)
    again = await be.resume_dead(db_session, job_type="enrich_grants", tag="t2", credits_per_job=10,
                                 daily_credits=500)
    slots = sorted([(await db_session.get(Job, i)).not_before for i in again])
    interval = timedelta(seconds=86400 * 10 * be.RETRY_HEADROOM / 500)
    assert len(slots) == 2 and slots[0] >= before and slots[1] - slots[0] == interval


@pytest.mark.integration
async def test_run_status_ignores_jobs_from_earlier_runs(db_session):
    old_only = await factories.make_user(db_session)
    rerun = await factories.make_user(db_session)
    long_ago = datetime(2026, 1, 1, tzinfo=UTC)
    db_session.add_all([
        Job(type="enrich_grants", user_id=old_only.id, status="dead", payload={}, enqueued_at=long_ago),
        Job(type="enrich_grants", user_id=rerun.id, status="dead", payload={}, enqueued_at=long_ago),
        Job(type="enrich_grants", user_id=rerun.id, status="completed", payload={be.TAG_KEY: "t3"},
            enqueued_at=long_ago + timedelta(days=1)),
    ])
    await db_session.flush()
    counts = await be.run_status(db_session, job_type="enrich_grants", user_ids=[old_only.id, rerun.id],
                                 tag="t3", since=datetime(2026, 10, 6, tzinfo=UTC))
    assert counts == {"completed": 1}

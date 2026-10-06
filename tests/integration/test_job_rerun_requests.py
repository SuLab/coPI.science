"""Rerun requests (spec 2026-10-05 §4.2). Committing sessions: claim and completion run
on their own connections, so savepoint-isolated fixtures cannot see the rows."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import Job
from src.services import job_queue
from src.services.job_queue import request_job
from src.worker import main as worker_main
from tests import factories

pytestmark = pytest.mark.integration
NB = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _user_with_job(factory, **job):
    async with factory() as s:
        user = await factories.make_user(s)
        row = Job(type="enrich_grants", user_id=user.id, payload={"user_id": str(user.id), "progress": [1]}, **job)
        s.add(row)
        await s.flush()
        await s.commit()
        return user.id, row.id


async def _jobs(factory, user_id):
    async with factory() as s:
        return (await s.execute(select(Job).where(Job.user_id == user_id).order_by(Job.enqueued_at))).scalars().all()


async def _drop(factory, user_id):
    async with factory() as s:
        await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        await s.commit()


async def test_request_while_pending_adds_nothing(db_session):
    user = await factories.make_user(db_session)
    db_session.add(Job(type="enrich_grants", user_id=user.id, payload={}, status="pending"))
    await db_session.flush()
    assert await request_job(db_session, type="enrich_grants", user_id=user.id, payload={}) is None
    rows = (await db_session.execute(select(Job).where(Job.user_id == user.id))).scalars().all()
    assert len(rows) == 1 and rows[0].rerun_requested_at is None


async def test_request_with_no_active_job_inserts_with_not_before(db_session):
    user = await factories.make_user(db_session)
    new_id = await request_job(db_session, type="enrich_grants", user_id=user.id, payload={"a": 1}, not_before=NB)
    job = await db_session.get(Job, new_id)
    assert job.status == "pending" and job.not_before == NB


async def test_request_while_processing_spawns_a_fresh_job_at_completion(factory):
    user_id, job_id = await _user_with_job(factory, status="processing", attempts=1, started_at=datetime.now(UTC))
    try:
        async with factory() as s:
            assert await request_job(s, type="enrich_grants", user_id=user_id, payload={}, not_before=NB) is None
            await s.commit()
        await worker_main._mark_completed(factory, job_id)
        jobs = await _jobs(factory, user_id)
        assert [j.status for j in jobs] == ["completed", "pending"]
        assert jobs[1].not_before == NB and "progress" not in jobs[1].payload
    finally:
        await _drop(factory, user_id)


async def test_a_dead_job_also_spawns_the_requested_rerun(factory):
    user_id, job_id = await _user_with_job(factory, status="processing", attempts=3, max_attempts=3,
                                           rerun_requested_at=datetime.now(UTC))
    try:
        await worker_main._mark_failed(factory, job_id, RuntimeError("boom"))
        assert [j.status for j in await _jobs(factory, user_id)] == ["dead", "pending"]
    finally:
        await _drop(factory, user_id)


async def test_a_retry_satisfies_and_clears_the_request(factory):
    user_id, job_id = await _user_with_job(factory, status="processing", attempts=1,
                                           rerun_requested_at=datetime.now(UTC), rerun_not_before=NB,
                                           rerun_priority=10)
    try:
        await worker_main._mark_failed(factory, job_id, RuntimeError("transient"))
        jobs = await _jobs(factory, user_id)
        assert len(jobs) == 1 and jobs[0].status == "pending"
        assert jobs[0].rerun_requested_at is None and jobs[0].rerun_not_before is None
        assert jobs[0].rerun_priority is None
    finally:
        await _drop(factory, user_id)


async def test_claim_clears_the_flag(factory):
    user_id, job_id = await _user_with_job(factory, status="pending", priority=30000,
                                           rerun_requested_at=datetime.now(UTC), rerun_priority=10)
    try:
        async with factory() as s:
            claimed = await worker_main.claim_job(s)
        assert claimed.id == job_id and claimed.rerun_requested_at is None
        [row] = await _jobs(factory, user_id)
        assert row.rerun_requested_at is None and row.rerun_priority is None
    finally:
        await _drop(factory, user_id)


async def test_stale_sweep_dead_branch_spawns_the_requested_rerun(factory):
    old = datetime.now(UTC) - timedelta(hours=2)
    user_id, job_id = await _user_with_job(factory, status="processing", attempts=3, max_attempts=3,
                                           started_at=old, rerun_requested_at=old, rerun_not_before=NB)
    try:
        async with factory() as s:
            await worker_main.requeue_stale_processing_jobs(s)
        jobs = await _jobs(factory, user_id)
        assert [j.status for j in jobs] == ["dead", "pending"] and jobs[1].not_before == NB
    finally:
        await _drop(factory, user_id)


async def test_request_racing_completion_is_not_lost(factory):
    """request_job's UPDATE holds the row lock; the completion's FOR UPDATE waits for it
    and then sees the flag."""
    user_id, job_id = await _user_with_job(factory, status="processing", attempts=1, started_at=datetime.now(UTC))
    try:
        async with factory() as requester:
            assert await request_job(requester, type="enrich_grants", user_id=user_id, payload={}) is None
            completion = asyncio.create_task(worker_main._mark_completed(factory, job_id))
            await asyncio.sleep(0.5)
            assert not completion.done(), "completion must wait for the in-flight request"
            await requester.commit()
            await asyncio.wait_for(completion, timeout=30)
        assert [j.status for j in await _jobs(factory, user_id)] == ["completed", "pending"]
    finally:
        await _drop(factory, user_id)


async def test_request_after_completion_inserts_a_new_job(factory):
    user_id, job_id = await _user_with_job(factory, status="completed", attempts=1)
    try:
        async with factory() as s:
            assert await request_job(s, type="enrich_grants", user_id=user_id, payload={}) is not None
            await s.commit()
    finally:
        await _drop(factory, user_id)


async def test_a_pending_job_cannot_be_claimed_until_the_request_commits(factory):
    """The lost-pin race: before, request_job matched only 'processing', so a pending job
    could be claimed (and read the old identity) while the pin was still uncommitted."""
    user_id, job_id = await _user_with_job(factory, status="pending", priority=30000)
    try:
        async with factory() as requester:
            assert await request_job(requester, type="enrich_grants", user_id=user_id, payload={}) is None
            async with factory() as worker_session:
                claimed = await asyncio.wait_for(worker_main.claim_job(worker_session), timeout=10)
            assert claimed is None or claimed.id != job_id, "the uncommitted request must hold the row"
            await requester.commit()
        async with factory() as worker_session:
            claimed = await worker_main.claim_job(worker_session)
        assert claimed is not None and claimed.id == job_id
        assert (await _jobs(factory, user_id))[0].rerun_requested_at is None, "a pending row is not flagged"
    finally:
        await _drop(factory, user_id)


async def test_a_pending_conflict_takes_the_earlier_not_before_and_the_higher_priority(db_session):
    """Before: a person's request left a bulk-delayed pending job at its later not_before."""
    user = await factories.make_user(db_session)
    job = Job(type="enrich_grants", user_id=user.id, payload={}, status="pending",
              priority=-10, not_before=NB)
    db_session.add(job)
    await db_session.flush()
    assert await request_job(db_session, type="enrich_grants", user_id=user.id, payload={},
                             priority=10, not_before=datetime.now(UTC)) is None
    await db_session.refresh(job)
    assert job.priority == 10 and job.not_before < NB
    assert await request_job(db_session, type="enrich_grants", user_id=user.id, payload={},
                             priority=-10, not_before=NB) is None
    await db_session.refresh(job)
    assert job.priority == 10 and job.not_before < NB, "a later, lower request never demotes"


async def test_a_pending_conflict_keeps_what_the_request_does_not_state(db_session):
    """Before: an unstated not_before read as now and an unstated priority as 0, so a plain
    request pulled a deferred or backed-off job forward."""
    user = await factories.make_user(db_session)
    job = Job(type="enrich_grants", user_id=user.id, payload={}, status="pending",
              priority=-10, not_before=NB)
    db_session.add(job)
    await db_session.flush()
    assert await request_job(db_session, type="enrich_grants", user_id=user.id,
                             payload={}) is None
    await db_session.refresh(job)
    assert (job.priority, job.not_before) == (-10, NB)
    assert await request_job(db_session, type="enrich_grants", user_id=user.id, payload={},
                             priority=10) is None
    await db_session.refresh(job)
    assert (job.priority, job.not_before) == (10, NB), "a stated priority leaves not_before"
    assert await request_job(db_session, type="enrich_grants", user_id=user.id, payload={},
                             not_before=NB - timedelta(days=1)) is None
    await db_session.refresh(job)
    assert (job.priority, job.not_before) == (10, NB - timedelta(days=1))


async def test_a_rerun_requested_at_a_higher_priority_spawns_at_it(factory):
    user_id, job_id = await _user_with_job(factory, status="processing", attempts=1, priority=-10,
                                           started_at=datetime.now(UTC))
    try:
        async with factory() as s:
            assert await request_job(s, type="enrich_grants", user_id=user_id, payload={},
                                     priority=10) is None
            await s.commit()
        async with factory() as s:
            assert await request_job(s, type="enrich_grants", user_id=user_id, payload={}) is None
            await s.commit()
        assert (await _jobs(factory, user_id))[0].rerun_priority == 10
        await worker_main._mark_completed(factory, job_id)
        jobs = await _jobs(factory, user_id)
        assert [j.status for j in jobs] == ["completed", "pending"] and jobs[1].priority == 10
    finally:
        await _drop(factory, user_id)


async def test_an_insert_losing_a_second_race_updates_the_new_job(db_session, monkeypatch):
    """Before: when the retry insert conflicted with a job committed after the UPDATE,
    request_job returned None with that job neither locked nor flagged."""
    user = await factories.make_user(db_session)
    real_insert = job_queue.insert_job_if_absent
    calls = []

    async def racing_insert(db, **kw):
        calls.append(kw)
        if len(calls) == 1:
            return None  # conflicted with a job that ended before the UPDATE
        if len(calls) == 2:
            db.add(Job(type="enrich_grants", user_id=user.id, payload={}, status="processing",
                       attempts=1))
            await db.flush()
            return None  # conflicted with a job another worker started after the UPDATE
        return await real_insert(db, **kw)

    monkeypatch.setattr(job_queue, "insert_job_if_absent", racing_insert)
    assert await request_job(db_session, type="enrich_grants", user_id=user.id, payload={}) is None
    row = (await db_session.execute(select(Job).where(Job.user_id == user.id))).scalar_one()
    assert len(calls) == 2 and row.rerun_requested_at is not None


async def test_request_job_gives_up_after_bounded_rounds(db_session, monkeypatch):
    user = await factories.make_user(db_session)
    calls = []

    async def always_conflicts(db, **kw):
        calls.append(kw)
        return None

    monkeypatch.setattr(job_queue, "insert_job_if_absent", always_conflicts)
    with pytest.raises(RuntimeError):
        await request_job(db_session, type="enrich_grants", user_id=user.id, payload={})
    assert len(calls) == job_queue.REQUEST_JOB_ROUNDS

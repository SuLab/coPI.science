"""NonRetryableJobError, JobDeferred, JobContext.after_commit, the persona-sweep hook and
the OpenAlex budget gate (spec 2026-10-05 §4.2, §4.3, D54, D67)."""
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import Job, User
from src.models.job import BULK_PRIORITY, INTERACTIVE_PRIORITY
from src.services import openalex_budget
from src.services.job_queue import JobDeferred, NonRetryableJobError
from src.worker import main as worker
from src.worker.main import JobContext
from tests import factories

pytestmark = pytest.mark.integration
LATER = datetime(2031, 1, 1, tzinfo=UTC)


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _seed(factory, attempts=1, type="enrich_grants", priority=None):
    async with factory() as s:
        user = await factories.make_user(s, name="Infra PI")
        job = Job(type=type, user_id=user.id, payload={"user_id": str(user.id)},
                  status="processing", attempts=attempts, priority=priority)
        s.add(job)
        await s.flush()
        await s.commit()
        return user.id, job.id


async def _job(factory, job_id):
    async with factory() as s:
        return (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()


async def _drop(factory, user_id):
    async with factory() as s:
        await s.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        await s.commit()


async def test_non_retryable_error_is_dead_on_the_first_attempt(factory, monkeypatch):
    user_id, job_id = await _seed(factory)

    async def refuse(ctx, db):
        raise NonRetryableJobError("name is an ORCID iD; enter the PI's name")

    monkeypatch.setitem(worker.JOB_HANDLERS, "enrich_grants", refuse)
    try:
        await worker.process_job(job_id, "enrich_grants", 1, 3, factory)
        job = await _job(factory, job_id)
        assert job.status == "dead" and job.attempts == 1 and "ORCID iD" in job.last_error
    finally:
        await _drop(factory, user_id)


async def test_deferral_returns_to_pending_without_counting_the_attempt(factory, monkeypatch):
    user_id, job_id = await _seed(factory, attempts=1)

    async def defer(ctx, db):
        await db.execute(text("UPDATE users SET name = 'ROLLED BACK' WHERE id = :u"), {"u": ctx.user_id})
        raise JobDeferred(LATER, "ceiling")

    monkeypatch.setitem(worker.JOB_HANDLERS, "enrich_grants", defer)
    try:
        await worker.process_job(job_id, "enrich_grants", 1, 3, factory)
        job = await _job(factory, job_id)
        assert (job.status, job.attempts, job.not_before) == ("pending", 0, LATER)
        async with factory() as s:
            assert (await s.execute(select(User.name).where(User.id == user_id))).scalar_one() == "Infra PI"
    finally:
        await _drop(factory, user_id)


async def test_a_bulk_openalex_job_defers_to_the_meter_reset_before_its_handler_runs(factory, monkeypatch):
    user_id, job_id = await _seed(factory, type="generate_profile", priority=BULK_PRIORITY)
    ran = []

    async def low():
        return openalex_budget.Meter(1000, 4, 3600)

    async def handler(ctx, db):
        ran.append(ctx.id)

    monkeypatch.setattr(openalex_budget, "read_meter", low)
    monkeypatch.setitem(worker.JOB_HANDLERS, "generate_profile", handler)
    try:
        await worker.process_job(job_id, "generate_profile", 1, 3, factory)
        job = await _job(factory, job_id)
        assert ran == [] and (job.status, job.attempts) == ("pending", 0)
        assert job.not_before > datetime.now(UTC) and "OpenAlex free budget" in job.last_error
    finally:
        await _drop(factory, user_id)


async def test_exhaustion_inside_a_bulk_handler_rolls_back_without_consuming_an_attempt(
    factory, monkeypatch,
):
    user_id, job_id = await _seed(factory, type="generate_profile", priority=BULK_PRIORITY)
    reads = 0

    async def meter():
        nonlocal reads
        reads += 1
        return openalex_budget.Meter(1000, 5 if reads == 1 else 0, 3600)

    async def handler(ctx, db):
        await db.execute(text("UPDATE users SET name = 'ROLLED BACK' WHERE id = :u"),
                         {"u": ctx.user_id})
        await openalex_budget.check_request_budget()
        raise AssertionError("An exhausted request must be deferred")

    monkeypatch.setattr(openalex_budget, "read_meter", meter)
    monkeypatch.setitem(worker.JOB_HANDLERS, "generate_profile", handler)
    try:
        await worker.process_job(job_id, "generate_profile", 1, 3, factory)
        job = await _job(factory, job_id)
        assert (job.status, job.attempts) == ("pending", 0) and reads == 2
        async with factory() as db:
            assert (await db.get(User, user_id)).name == "Infra PI"
        await openalex_budget.check_request_budget()
        assert reads == 2  # Worker scope was restored, so an interactive call skips it.
    finally:
        await _drop(factory, user_id)


@pytest.mark.parametrize("priority", [None, INTERACTIVE_PRIORITY])
async def test_a_non_bulk_job_never_reads_the_meter(factory, monkeypatch, priority):
    user_id, job_id = await _seed(factory, type="generate_profile", priority=priority)
    ran = []

    async def never():
        raise AssertionError("only BULK jobs read the meter")

    async def handler(ctx, db):
        ran.append(ctx.id)

    monkeypatch.setattr(openalex_budget, "read_meter", never)
    monkeypatch.setitem(worker.JOB_HANDLERS, "generate_profile", handler)
    try:
        await worker.process_job(job_id, "generate_profile", 1, 3, factory)
        assert ran == [job_id] and (await _job(factory, job_id)).status == "completed"
    finally:
        await _drop(factory, user_id)


async def test_after_commit_runs_after_the_commit_and_before_completion(factory, monkeypatch):
    user_id, job_id = await _seed(factory)
    seen = {}

    async def handler(ctx, db):
        await db.execute(text("UPDATE users SET name = 'Committed' WHERE id = :u"), {"u": ctx.user_id})

        async def callback(session):
            async with factory() as other:
                seen["name"] = (await other.execute(select(User.name).where(User.id == ctx.user_id))).scalar_one()
                seen["status"] = (await other.execute(select(Job.status).where(Job.id == ctx.id))).scalar_one()

        ctx.after_commit.append(callback)

    monkeypatch.setitem(worker.JOB_HANDLERS, "enrich_grants", handler)
    try:
        await worker.process_job(job_id, "enrich_grants", 1, 3, factory)
        assert seen == {"name": "Committed", "status": "processing"}
        assert (await _job(factory, job_id)).status == "completed"
    finally:
        await _drop(factory, user_id)


async def test_a_failing_callback_does_not_fail_the_job_and_a_failed_handler_runs_none(factory, monkeypatch):
    user_id, job_id = await _seed(factory)
    ran = []

    async def boom(session):
        raise OSError("disk full")

    async def handler(ctx, db):
        ctx.after_commit.extend([boom, lambda s: _record(ran)])

    async def _record(bucket):
        bucket.append(True)

    monkeypatch.setitem(worker.JOB_HANDLERS, "enrich_grants", handler)
    try:
        await worker.process_job(job_id, "enrich_grants", 1, 3, factory)
        assert (await _job(factory, job_id)).status == "completed" and ran == [True]
    finally:
        await _drop(factory, user_id)

    user_id, job_id = await _seed(factory)

    async def failing(ctx, db):
        ctx.after_commit.append(lambda s: _record(ran))
        raise RuntimeError("handler failed")

    monkeypatch.setitem(worker.JOB_HANDLERS, "enrich_grants", failing)
    try:
        await worker.process_job(job_id, "enrich_grants", 1, 3, factory)
        assert ran == [True], "no callback runs after a failed handler"
    finally:
        await _drop(factory, user_id)


async def test_generate_profile_forwards_after_commit_and_the_followon_slot(db_session, monkeypatch):
    user = await factories.make_user(db_session)
    captured = {}

    async def fake_pipeline(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(worker, "run_profile_pipeline", fake_pipeline)
    ctx = JobContext(id=uuid.uuid4(), type="generate_profile", user_id=user.id,
                     payload={"user_id": str(user.id), "followon_not_before": "2030-05-01T12:00:00+00:00"},
                     attempts=1, max_attempts=3)
    await worker.execute_generate_profile(ctx, db_session)
    assert captured["after_commit"] is ctx.after_commit
    assert captured["followon_not_before"] == datetime(2030, 5, 1, 12, tzinfo=UTC)


async def test_persona_sweep_hook_runs_daily_and_rechecks_while_disabled(monkeypatch):
    calls = []

    async def fake_sweep(factory):
        calls.append(factory)
        return fake_sweep.result

    monkeypatch.setattr(worker, "run_persona_sweep", fake_sweep)
    fake_sweep.result = None
    assert await worker._maybe_sweep_personas("f", 100.0, 0.0) == 100.0 + worker.PERSONA_SWEEP_RECHECK_SECONDS
    fake_sweep.result = 3
    assert await worker._maybe_sweep_personas("f", 500.0, 400.0) == 500.0 + worker.PERSONA_SWEEP_SECONDS
    assert await worker._maybe_sweep_personas("f", 501.0, 90000.0) == 90000.0
    assert len(calls) == 2

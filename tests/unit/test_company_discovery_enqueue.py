"""Company discovery enqueue (spec §7.5 Job, §8, Review Focus #5): one active job per PI,
a Find companies press raises a pending bulk row to interactive, the profile pipeline
requests discovery after every successful generation (spec 2026-10-05 D37), and the
worker dispatches the type."""
import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.config import Settings
from src.models import Job, User
from src.models.job import BULK_PRIORITY, INTERACTIVE_PRIORITY
from src.services import company_discovery as cd
from src.services import profile_pipeline
from src.services.profile_pipeline import run_profile_pipeline
from src.worker import main as worker
from tests import factories
from tests.unit.test_pipeline_corpus_integration import (  # noqa: F401
    _make_pi,
    _rec,
    _uncapped,
    wired,
)

pytestmark = pytest.mark.integration


async def _discovery_jobs(db, user_id) -> list[Job]:
    return list((await db.execute(
        select(Job).where(Job.user_id == user_id, Job.type == "company_discovery")
    )).scalars().all())


async def test_enqueue_writes_the_enrichment_payload_shape(db_session):
    user = await factories.make_user(db_session)
    job_id = await cd.enqueue_company_discovery(db_session, user.id, priority=BULK_PRIORITY)
    (job,) = await _discovery_jobs(db_session, user.id)
    assert job.id == job_id and job.status == "pending" and job.priority == BULK_PRIORITY
    assert job.payload == {"user_id": str(user.id), "orcid": user.orcid}


async def test_two_presses_make_one_row_and_raise_its_priority(db_session):
    user = await factories.make_user(db_session)
    first = await cd.enqueue_company_discovery(db_session, user.id, priority=BULK_PRIORITY)
    second = await cd.enqueue_company_discovery(db_session, user.id, priority=INTERACTIVE_PRIORITY)
    third = await cd.enqueue_company_discovery(db_session, user.id, priority=BULK_PRIORITY)
    assert first is not None and second is None and third is None
    (job,) = await _discovery_jobs(db_session, user.id)
    await db_session.refresh(job)
    assert job.priority == INTERACTIVE_PRIORITY


async def test_a_press_while_processing_is_a_no_op(db_session):
    user = await factories.make_user(db_session)
    db_session.add(Job(type="company_discovery", user_id=user.id, status="processing", payload={}))
    await db_session.flush()
    assert await cd.enqueue_company_discovery(db_session, user.id, priority=INTERACTIVE_PRIORITY) is None
    assert len(await _discovery_jobs(db_session, user.id)) == 1


async def test_a_press_after_a_finished_run_queues_a_new_one(db_session):
    user = await factories.make_user(db_session)
    db_session.add(Job(type="company_discovery", user_id=user.id, status="completed", payload={}))
    await db_session.flush()
    assert await cd.enqueue_company_discovery(db_session, user.id, priority=INTERACTIVE_PRIORITY) is not None
    assert len(await _discovery_jobs(db_session, user.id)) == 2


async def test_an_unknown_user_is_not_enqueued(db_session):
    assert await cd.enqueue_company_discovery(db_session, uuid.uuid4(), priority=BULK_PRIORITY) is None


async def test_concurrent_enqueues_insert_one_row(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        user = User(name="Race Discovery", orcid=f"0000-0006-{uuid.uuid4().int % 10000:04d}-0003",
                    access_status="allowed")
        s.add(user)
        await s.commit()
    try:
        async def one(priority):
            async with factory() as s:
                got = await cd.enqueue_company_discovery(s, user.id, priority=priority)
                await s.commit()
                return got

        results = await asyncio.gather(one(BULK_PRIORITY), one(INTERACTIVE_PRIORITY))
        assert sum(r is not None for r in results) == 1
        async with factory() as s:
            n = (await s.execute(select(func.count()).select_from(Job).where(
                Job.user_id == user.id, Job.type == "company_discovery"))).scalar_one()
        assert n == 1
    finally:
        async with factory() as s:
            await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
            await s.commit()


async def test_latest_discovery_job_is_the_newest_in_any_status(db_session):
    user = await factories.make_user(db_session)
    assert await cd.latest_discovery_job(db_session, user.id) is None
    db_session.add(Job(type="company_discovery", user_id=user.id, status="completed", payload={},
                       enqueued_at=datetime(2026, 1, 1, tzinfo=UTC)))
    await db_session.flush()
    newest = await cd.enqueue_company_discovery(db_session, user.id, priority=INTERACTIVE_PRIORITY)
    latest = await cd.latest_discovery_job(db_session, user.id)
    assert latest is not None and latest.id == newest


@pytest.mark.usefixtures("progress_on_test_connection")
async def test_every_generation_requests_discovery(db_session, wired):  # noqa: F811
    wired.corpus = _uncapped([_rec(1, 2020, "Paper A", hopkins_pi=True)])
    user, _agent, job = await _make_pi(db_session)

    await run_profile_pipeline(user.id, db_session, job.id)
    (discovery,) = await _discovery_jobs(db_session, user.id)
    assert discovery.priority == BULK_PRIORITY
    assert discovery.payload == {"user_id": str(user.id), "orcid": user.orcid}
    types = {j.type for j in (await db_session.execute(select(Job).where(Job.user_id == user.id))).scalars()}
    assert {"enrich_grants", "industry_evidence", "company_discovery"} <= types

    # A regeneration after that run finished queues a new one (D37).
    discovery.status = "completed"
    await db_session.flush()
    run = profile_pipeline.PipelineRun(
        user_id=user.id, db=db_session, job_id=None, user=user, orcid_id=user.orcid)
    await profile_pipeline._enqueue_enrichment(run)
    assert len(await _discovery_jobs(db_session, user.id)) == 2


async def test_a_generation_flags_a_processing_discovery_for_a_rerun(db_session):
    user = await factories.make_user(db_session)
    running = Job(type="company_discovery", user_id=user.id, status="processing", payload={})
    db_session.add(running)
    await db_session.flush()
    assert await cd.request_company_discovery(db_session, user.id, priority=BULK_PRIORITY) is None
    await db_session.refresh(running)
    assert running.rerun_requested_at is not None
    assert len(await _discovery_jobs(db_session, user.id)) == 1


def test_worker_registers_the_handler():
    assert worker.JOB_HANDLERS["company_discovery"] is worker._execute_company_discovery


async def test_worker_handler_delegates_to_the_service(monkeypatch):
    seen = []

    async def spy(ctx, db):
        seen.append((ctx, db))

    monkeypatch.setattr(cd, "execute_company_discovery", spy)
    await worker._execute_company_discovery("ctx", "db")
    assert seen == [("ctx", "db")]


def test_sec_user_agent_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    assert Settings(_env_file=None).sec_user_agent == ""

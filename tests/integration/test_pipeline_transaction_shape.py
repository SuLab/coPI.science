"""DP-09: the pipeline never touches its own jobs row inside its transaction.
Progress lands in a short transaction of its own (visible mid-run from another
session, with no self-deadlock), and status/completed_at are written only after
the handler's commit. Write ORDER inside the pipeline is unchanged (FA3-V2)."""
import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import Job, User
from src.services import job_progress
from src.services.profile_pipeline import run_profile_pipeline
from src.worker import main as worker
from tests.unit.test_pipeline_corpus_integration import _make_pi, _rec, _uncapped, wired  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _committed_user_and_job(engine, job_type="enrich_grants"):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        user = User(name="Tx Shape", orcid=f"0000-0007-{uuid.uuid4().int % 10000:04d}-0001",
                    email=None, access_status="allowed")
        s.add(user)
        await s.flush()
        job = Job(type=job_type, user_id=user.id, payload={"user_id": str(user.id)},
                  status="processing", attempts=1)
        s.add(job)
        await s.commit()
        return factory, user.id, job.id


async def _cleanup(factory, user_id):
    async with factory() as s:
        await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await s.commit()


async def test_progress_is_visible_mid_run_from_another_session(engine, monkeypatch):
    factory, user_id, job_id = await _committed_user_and_job(engine)
    job_progress.configure(factory)
    seen = asyncio.Event()

    async def handler(ctx, db):
        # The handler's own transaction is open (it holds a row it read).
        await db.execute(text("SELECT 1"))
        await job_progress.record(ctx.id, "stepX", "mid-run")
        async with factory() as other:
            payload = (await other.execute(select(Job.payload).where(Job.id == ctx.id))).scalar_one()
        assert payload["progress"][-1] == {"step": "stepX", "detail": "mid-run"}
        seen.set()

    monkeypatch.setitem(worker.JOB_HANDLERS, "enrich_grants", handler)
    try:
        await asyncio.wait_for(worker.process_job(job_id, "enrich_grants", 1, 3, factory), timeout=30)
        assert seen.is_set()
        async with factory() as s:
            status = (await s.execute(select(Job.status).where(Job.id == job_id))).scalar_one()
        assert status == "completed"
    finally:
        job_progress.configure(None)
        await _cleanup(factory, user_id)


async def test_status_written_after_handler_commit_and_crash_is_requeued(engine, monkeypatch):
    factory, user_id, job_id = await _committed_user_and_job(engine)

    async def handler(ctx, db):
        return None

    async def crash(*_a, **_k):
        raise RuntimeError("killed between handler commit and status write")

    monkeypatch.setitem(worker.JOB_HANDLERS, "enrich_grants", handler)
    monkeypatch.setattr(worker, "_mark_completed", crash)
    try:
        with pytest.raises(RuntimeError):
            await worker.process_job(job_id, "enrich_grants", 1, 3, factory)
        async with factory() as s:
            assert (await s.execute(select(Job.status).where(Job.id == job_id))).scalar_one() == "processing"
            moved = await worker.requeue_stale_processing_jobs(s, older_than_seconds=0)
            assert moved >= 1
            assert (await s.execute(select(Job.status).where(Job.id == job_id))).scalar_one() == "pending"
            await worker.requeue_stale_processing_jobs(s, older_than_seconds=0)
            row = (await s.execute(select(Job.status, Job.attempts).where(Job.id == job_id))).one()
            # A second sweep leaves it alone: requeued once, attempts not re-counted.
            assert tuple(row) == ("pending", 1)
    finally:
        await _cleanup(factory, user_id)


async def test_handler_failure_rolls_back_and_records_failure(engine, monkeypatch):
    factory, user_id, job_id = await _committed_user_and_job(engine)

    async def handler(ctx, db):
        await db.execute(text("UPDATE users SET name = 'SHOULD ROLL BACK' WHERE id = :id"),
                         {"id": ctx.user_id})
        raise ValueError("boom")

    monkeypatch.setitem(worker.JOB_HANDLERS, "enrich_grants", handler)
    try:
        await worker.process_job(job_id, "enrich_grants", 1, 3, factory)
        async with factory() as s:
            job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one()
            name = (await s.execute(select(User.name).where(User.id == user_id))).scalar_one()
        assert job.status == "pending" and job.last_error == "boom"
        assert name == "Tx Shape"
    finally:
        await _cleanup(factory, user_id)


@pytest.mark.usefixtures("progress_on_test_connection")
async def test_new_pi_with_jhu_employment_exports_no_pre_tenure_papers(db_session, wired):  # noqa: F811
    wired.profile["employments"] = [
        {"organization": "Johns Hopkins University", "start_year": 2015, "current": True}
    ]
    wired.corpus = _uncapped([
        _rec(1, 2020, "In tenure paper", hopkins_pi=True),
        _rec(2, 2010, "Pre tenure paper"),
    ])
    user, agent, job = await _make_pi(db_session)
    await run_profile_pipeline(user.id, db_session, job.id)
    text_out = (wired.export_dir / f"{agent.agent_id}.md").read_text()
    assert "In tenure paper" in text_out
    assert "Pre tenure paper" not in text_out


@pytest.mark.usefixtures("progress_on_test_connection")
async def test_null_institution_gets_s4_and_an_institution_line(db_session, wired, monkeypatch):  # noqa: F811
    seen = {}

    async def spy_corpus(orcid, name, institution, *, cap=50):
        seen["institution"] = institution
        return _uncapped([_rec(3, 2021, "A paper", hopkins_pi=True)])

    from src.services import profile_pipeline
    wired.profile["institution"] = "Johns Hopkins University"
    user, agent, job = await _make_pi(db_session)
    user.institution = None
    await db_session.flush()
    monkeypatch.setattr(profile_pipeline, "resolve_corpus", spy_corpus)
    await run_profile_pipeline(user.id, db_session, job.id)
    assert seen["institution"] == "Johns Hopkins University"
    assert "**Institution:** Johns Hopkins University" in (
        wired.export_dir / f"{agent.agent_id}.md"
    ).read_text()

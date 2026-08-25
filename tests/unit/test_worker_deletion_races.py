"""process_job vs. a concurrent account deletion (audit F10, decision D8).

These tests CANNOT use the savepoint-isolated ``db_session`` fixture:
``process_job`` opens its own sessions/connections, which can never see rows
that exist only inside another connection's uncommitted savepoint
(tests/conftest.py::db_session, ``join_transaction_mode="create_savepoint"``).
So they commit real rows through the same factory the worker uses, and clean
up in ``finally`` — deleting the seeded user cascades the jobs row, so one
DELETE is the whole cleanup.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import Job
from src.worker import main as worker_main
from tests import factories

pytestmark = pytest.mark.asyncio


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_committed_job(session_factory):
    """A real, committed user + generate_profile job. Returns (user_id, job_id)."""
    async with session_factory() as s:
        user = await factories.make_user(s)
        job = Job(type="generate_profile", user_id=user.id, payload={})
        s.add(job)
        await s.flush()
        user_id, job_id = user.id, job.id
        await s.commit()
    return user_id, job_id


async def _delete_user(session_factory, user_id):
    async with session_factory() as s:
        await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await s.commit()


async def test_vanished_job_row_is_skipped_quietly(session_factory):
    user_id, job_id = await _seed_committed_job(session_factory)
    # The user delete cascades the jobs row — the exact race window between
    # claim_job's commit and process_job's re-fetch.
    await _delete_user(session_factory, user_id)

    # Must return, not raise (NoResultFound used to escape to the loop logger).
    await worker_main.process_job(job_id, "generate_profile", 1, 3, session_factory)


async def test_failure_after_deletion_does_not_raise(session_factory, monkeypatch):
    user_id, job_id = await _seed_committed_job(session_factory)

    async def _delete_user_then_fail(job, db):
        await _delete_user(session_factory, user_id)
        raise RuntimeError("pipeline blew up mid-flight")

    monkeypatch.setattr(
        worker_main, "execute_generate_profile", _delete_user_then_fail
    )
    # Used to raise StaleDataError from inside the except handler.
    await worker_main.process_job(job_id, "generate_profile", 1, 3, session_factory)


async def test_normal_failure_still_marks_retry(session_factory, monkeypatch):
    user_id, job_id = await _seed_committed_job(session_factory)
    try:
        async def _fail(job, db):
            raise RuntimeError("ordinary failure")

        monkeypatch.setattr(worker_main, "execute_generate_profile", _fail)
        await worker_main.process_job(
            job_id, "generate_profile", 1, 3, session_factory
        )

        async with session_factory() as check:
            row = await check.get(Job, job_id)
            assert row.status == "pending"  # attempts < max_attempts: retried
            assert "ordinary failure" in (row.last_error or "")
    finally:
        await _delete_user(session_factory, user_id)

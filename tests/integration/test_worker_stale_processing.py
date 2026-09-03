"""D4 (docs/audits/2026-09-02-review-pipeline/README.md): a worker SIGKILLed
mid-job leaves its row in 'processing' forever — claim_job only takes 'pending'
and nothing reset the row — and enqueue_analysis_if_absent then treated that
zombie as a live job. These tests pin the requeue that now runs at boot and on
a timer.

Committing sessions, like tests/unit/test_worker_deletion_races.py: the
function under test commits, so the savepoint fixture is the wrong tool.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import Job
from src.worker import main as worker_main

pytestmark = pytest.mark.integration

TAG = "stale_processing_test"


@pytest.fixture
def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _seed(session_factory, rows: dict[str, Job]) -> dict[str, uuid.UUID]:
    async with session_factory() as s:
        s.add_all(rows.values())
        await s.flush()
        ids = {name: job.id for name, job in rows.items()}
        await s.commit()
    return ids


async def _statuses(session_factory, ids: dict[str, uuid.UUID]) -> dict[str, str]:
    async with session_factory() as s:
        return {name: (await s.get(Job, jid)).status for name, jid in ids.items()}


async def _sweep(session_factory):
    async with session_factory() as s:
        await s.execute(delete(Job).where(Job.payload["tag"].astext == TAG))
        await s.commit()


def _job(**overrides) -> Job:
    data = dict(type="review_feedback_analysis", status="processing", attempts=1,
                started_at=datetime.now(UTC) - timedelta(hours=2), payload={"tag": TAG})
    data.update(overrides)
    return Job(**data)


async def test_stale_rows_are_requeued_exhausted_ones_die_fresh_ones_stay(session_factory):
    ids = await _seed(session_factory, {
        "stale_retryable": _job(),
        "stale_exhausted": _job(attempts=3, max_attempts=3),
        "stale_no_started_at": _job(started_at=None),
        "fresh": _job(started_at=datetime.now(UTC)),
        "queued": _job(status="pending", attempts=0, started_at=None),
        "done": _job(status="completed"),
    })
    try:
        async with session_factory() as s:
            moved = await worker_main.requeue_stale_processing_jobs(s)
        # The sweep is not tag-scoped — it updates every stale 'processing' row
        # in the table — so `moved` is a GLOBAL count and only a lower bound is
        # safe here. The per-id `_statuses` comparison below is the exact check.
        assert moved >= 3, "the three stale rows this test seeded must all have moved"
        assert await _statuses(session_factory, ids) == {
            "stale_retryable": "pending",
            "stale_exhausted": "dead",
            "stale_no_started_at": "pending",
            "fresh": "processing",
            "queued": "pending",
            "done": "completed",
        }
        async with session_factory() as s:
            moved_row = await s.get(Job, ids["stale_retryable"])
            assert "requeued" in (moved_row.last_error or "")
    finally:
        await _sweep(session_factory)


async def test_boot_requeue_takes_every_processing_row(session_factory):
    """At worker start no other worker exists (single instance), so even a
    seconds-old 'processing' row is a zombie."""
    ids = await _seed(session_factory, {"fresh": _job(started_at=datetime.now(UTC))})
    try:
        async with session_factory() as s:
            moved = await worker_main.requeue_stale_processing_jobs(s, older_than_seconds=0)
        assert moved == 1
        assert (await _statuses(session_factory, ids))["fresh"] == "pending"
    finally:
        await _sweep(session_factory)


async def test_a_requeued_job_is_claimable_again(session_factory):
    ids = await _seed(session_factory, {"zombie": _job()})
    seen: set = set()
    try:
        async with session_factory() as s:
            await worker_main.requeue_stale_processing_jobs(s)
        async with session_factory() as s:
            claimed = await worker_main.claim_job(s)
            # Other tests' pending jobs may exist; ours must be claimable at least once.
            seen = {claimed.id} if claimed else set()
            while claimed and claimed.id != ids["zombie"]:
                claimed = await worker_main.claim_job(s)
                if claimed is None or claimed.id in seen:
                    break
                seen.add(claimed.id)
        assert ids["zombie"] in seen
        async with session_factory() as s:
            row = await s.get(Job, ids["zombie"])
            assert row.status == "processing" and row.attempts == 2
    finally:
        await _sweep(session_factory)
        # claim_job flipped any foreign pending rows it walked past to 'processing';
        # put them back so this test leaves the queue as it found it.
        async with session_factory() as s:
            for jid in seen - {ids["zombie"]}:
                job = await s.get(Job, jid)
                if job is not None and job.status == "processing":
                    job.status = "pending"
                    job.attempts -= 1
            await s.commit()

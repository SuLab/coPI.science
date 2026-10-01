import pytest
from sqlalchemy import text

from src.services.advisory_locks import WORKER_LOCK_KEY
from src.worker import main as worker

pytestmark = pytest.mark.asyncio


async def test_second_worker_is_refused_and_lock_frees_on_close(engine):
    first = await worker.acquire_worker_lock(engine)
    assert first is not None
    try:
        assert await worker.acquire_worker_lock(engine) is None
    finally:
        await first.close()
    again = await worker.acquire_worker_lock(engine)
    assert again is not None
    await again.close()


async def test_second_worker_exits_before_the_stale_sweep(engine, monkeypatch):
    holder = await worker.acquire_worker_lock(engine)
    swept = []

    async def spy(*a, **k):
        swept.append(1)
        return 0

    monkeypatch.setattr(worker, "requeue_stale_processing_jobs", spy)
    # Never build an engine from .env; hand run_worker the test engine instead.
    monkeypatch.setattr(worker, "create_async_engine", lambda *a, **k: engine)
    try:
        with pytest.raises(worker.WorkerAlreadyRunning):
            await worker.run_worker()
        assert swept == []
    finally:
        await holder.close()


async def test_lock_key_is_the_registry_key(engine):
    conn = await worker.acquire_worker_lock(engine)
    try:
        held = (await conn.execute(text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND classid = ((CAST(:k AS bigint) >> 32) & 4294967295)::oid "
            "AND objid = (CAST(:k AS bigint) & 4294967295)::oid"
        ), {"k": WORKER_LOCK_KEY})).scalar_one()
        assert held == 1
    finally:
        await conn.close()

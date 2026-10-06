"""job_progress.record gives up on a locked jobs row instead of waiting behind it (a
deletion holding that row while it waits on the recording pipeline would otherwise hang
the pipeline undetected). Committing sessions: the holder must be another connection."""
import asyncio
import logging
import time

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import Job
from src.services import job_progress
from tests import factories

pytestmark = pytest.mark.integration


async def test_record_returns_without_raising_when_the_job_row_is_locked(
    engine, monkeypatch, caplog
):
    monkeypatch.setattr(job_progress, "RECORD_LOCK_TIMEOUT", "200ms")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        user = await factories.make_user(s)
        job = Job(type="generate_profile", user_id=user.id, payload={"user_id": str(user.id)})
        s.add(job)
        await s.commit()
    job_progress.configure(factory)
    try:
        async with factory() as holder:
            await holder.execute(
                text("SELECT 1 FROM jobs WHERE id = :i FOR UPDATE"), {"i": job.id}
            )
            started = time.monotonic()
            with caplog.at_level(logging.WARNING, logger=job_progress.__name__):
                await asyncio.wait_for(
                    job_progress.record(job.id, "step3", "blocked"), timeout=10
                )
            assert time.monotonic() - started < 5
            assert any("not recorded" in r.getMessage() for r in caplog.records)
            await holder.rollback()
        async with factory() as s:
            payload = (await s.execute(select(Job.payload).where(Job.id == job.id))).scalar_one()
        assert "progress" not in payload
    finally:
        job_progress.configure(None)
        async with factory() as s:
            await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
            await s.commit()

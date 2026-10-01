import asyncio
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import Job, User
from src.services.assessment_reviews import enqueue_analysis_if_absent
from src.services.job_queue import insert_job_if_absent
from tests import factories

pytestmark = pytest.mark.asyncio


async def test_second_insert_returns_none(db_session):
    user = await factories.make_user(db_session)
    first = await insert_job_if_absent(db_session, type="enrich_grants", user_id=user.id, payload={})
    second = await insert_job_if_absent(db_session, type="enrich_grants", user_id=user.id, payload={})
    assert first is not None and second is None


async def test_concurrent_enqueue_does_not_raise(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        user = User(name="Race", orcid=f"0000-0006-{uuid.uuid4().int % 10000:04d}-0002", access_status="allowed")
        s.add(user)
        await s.commit()
    try:
        async def one():
            async with factory() as s:
                got = await insert_job_if_absent(s, type="generate_profile", user_id=user.id, payload={})
                await s.commit()
                return got
        results = await asyncio.gather(one(), one(), one())
        assert sum(r is not None for r in results) == 1
        async with factory() as s:
            n = (await s.execute(select(func.count()).select_from(Job).where(Job.user_id == user.id))).scalar_one()
        assert n == 1
    finally:
        async with factory() as s:
            await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
            await s.commit()


async def test_review_press_still_enqueues_25(db_session):
    user = await factories.make_user(db_session)
    for _ in range(25):
        assert await enqueue_analysis_if_absent(db_session, assessment_id=uuid.uuid4(), user_id=user.id)
    await db_session.flush()
    n = (await db_session.execute(select(func.count()).select_from(Job).where(
        Job.user_id == user.id, Job.type == "review_feedback_analysis"))).scalar_one()
    assert n == 25


async def test_refuses_unconstrained_types(db_session):
    user = await factories.make_user(db_session)
    with pytest.raises(ValueError):
        await insert_job_if_absent(db_session, type="review_feedback_analysis", user_id=user.id, payload={})

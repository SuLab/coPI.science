"""Spec P0-08: the headline claim protocol shared by the engine and the script."""
import asyncio

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import OpportunityAssessment, SimulationRun
from src.services.headline_claims import (
    IN_DOUBT_AFTER,
    claim_row,
    claim_thread,
    list_in_doubt,
    mark_posted,
    release_claim,
)

pytestmark = pytest.mark.integration


async def _run(factory):
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.commit()
        return run.id


async def _row(factory, run_id, *, thread_id, posted=False):
    async with factory() as db:
        row = OpportunityAssessment(
            simulation_run_id=run_id, agent_id="blackbird", channel_name="general",
            thread_id=thread_id, recommendation="conditional",
        )
        db.add(row)
        await db.flush()
        if posted:
            row.summary_posted_at = await db.scalar(text("SELECT now()"))
        await db.commit()
        return row.id


async def _get(factory, row_id):
    async with factory() as db:
        return await db.get(OpportunityAssessment, row_id)


async def _drop(factory, run_id):
    async with factory() as db:
        run = await db.get(SimulationRun, run_id)
        if run is not None:
            await db.delete(run)
            await db.commit()


@pytest.fixture
async def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_a_thread_claim_takes_every_unclaimed_row_once(factory):
    run_id = await _run(factory)
    try:
        a = await _row(factory, run_id, thread_id="t1")
        b = await _row(factory, run_id, thread_id="t1")
        async with factory() as db:
            assert sorted(await claim_thread(db, run_id, "t1")) == sorted([a, b])
        async with factory() as db:
            assert await claim_thread(db, run_id, "t1") == []
        assert (await _get(factory, a)).summary_claimed_at is not None
    finally:
        await _drop(factory, run_id)


async def test_a_posted_sibling_blocks_the_thread(factory):
    """SC-2: one posted row and one NULL row — the thread is never re-posted."""
    run_id = await _run(factory)
    try:
        await _row(factory, run_id, thread_id="t1", posted=True)
        fresh = await _row(factory, run_id, thread_id="t1")
        async with factory() as db:
            assert await claim_thread(db, run_id, "t1") == []
        assert (await _get(factory, fresh)).summary_claimed_at is None
    finally:
        await _drop(factory, run_id)


async def test_a_claimed_but_unposted_sibling_blocks_the_thread(factory):
    """SA5-08: two posters can never claim one thread."""
    run_id = await _run(factory)
    try:
        first = await _row(factory, run_id, thread_id="t1")
        async with factory() as db:
            assert await claim_thread(db, run_id, "t1") == [first]
        await _row(factory, run_id, thread_id="t1")
        async with factory() as db:
            assert await claim_thread(db, run_id, "t1") == []
    finally:
        await _drop(factory, run_id)


async def test_concurrent_thread_claims_give_one_winner(factory):
    run_id = await _run(factory)
    try:
        await _row(factory, run_id, thread_id="t1")

        async def one():
            async with factory() as db:
                return await claim_thread(db, run_id, "t1")

        results = await asyncio.gather(one(), one(), one())
        assert sorted(len(r) for r in results) == [0, 0, 1]
    finally:
        await _drop(factory, run_id)


async def test_a_null_thread_row_is_claimed_by_id(factory):
    run_id = await _run(factory)
    try:
        row = await _row(factory, run_id, thread_id=None)
        async with factory() as db:
            assert await claim_row(db, row) == [row]
        async with factory() as db:
            assert await claim_row(db, row) == []
    finally:
        await _drop(factory, run_id)


async def test_mark_posted_and_release(factory):
    run_id = await _run(factory)
    try:
        posted = await _row(factory, run_id, thread_id="t1")
        released = await _row(factory, run_id, thread_id="t2")
        async with factory() as db:
            await claim_thread(db, run_id, "t1")
            await claim_thread(db, run_id, "t2")
        async with factory() as db:
            await mark_posted(db, [posted])
        async with factory() as db:
            await release_claim(db, [released, posted])
        p = await _get(factory, posted)
        r = await _get(factory, released)
        assert p.summary_posted_at is not None and p.summary_claimed_at is not None
        assert r.summary_claimed_at is None and r.summary_posted_at is None
    finally:
        await _drop(factory, run_id)


async def test_only_old_unposted_claims_are_in_doubt(factory):
    assert IN_DOUBT_AFTER.total_seconds() == 600
    run_id = await _run(factory)
    try:
        old = await _row(factory, run_id, thread_id="t-old")
        young = await _row(factory, run_id, thread_id="t-young")
        done = await _row(factory, run_id, thread_id="t-done")
        async with factory() as db:
            for tid in ("t-old", "t-young", "t-done"):
                await claim_thread(db, run_id, tid)
            await db.execute(
                update(OpportunityAssessment)
                .where(OpportunityAssessment.id.in_([old, done]))
                .values(summary_claimed_at=text("now() - interval '11 minutes'"))
            )
            await db.commit()
        async with factory() as db:
            await mark_posted(db, [done])
        async with factory() as db:
            listed = await list_in_doubt(db, run_id)
        assert [row.id for row in listed] == [old]
        assert young not in [row.id for row in listed]
        async with factory() as db:
            assert (await db.execute(
                select(OpportunityAssessment.id).where(OpportunityAssessment.id == old)
            )).scalar_one() == old
    finally:
        await _drop(factory, run_id)

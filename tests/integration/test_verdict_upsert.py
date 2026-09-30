"""§8.1 write protocol: one row per (run, thread), updated in place."""
import asyncio
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import AssessmentDrop, AssessmentReview, OpportunityAssessment, SimulationRun, User
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration


async def _setup(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.commit()
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    sim = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")},
        session_factory=factory, simulation_run_id=run.id,
    )
    return factory, run.id, sim


def _row(run_id, *, thread_id="t1", n=1, slack_ts="1.1"):
    return {
        "simulation_run_id": run_id, "agent_id": "blackbird", "subject_agent_id": "gordy",
        "channel_name": "c", "slack_ts": slack_ts, "thread_id": thread_id,
        "recommendation": f"rec{n}", "raw_verdict": {"n": n}, "panel_incomplete": False,
        "prose_format": "markdown",
    }


def _thread(tid="t1"):
    return ThreadState(thread_id=tid, channel="c", other_agent_id="gordy")


async def _rows(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(OpportunityAssessment).where(OpportunityAssessment.simulation_run_id == run_id)
        )).scalars().all()


async def _drops(factory, run_id):
    async with factory() as db:
        return (await db.execute(
            select(AssessmentDrop).where(AssessmentDrop.simulation_run_id == run_id)
            .order_by(AssessmentDrop.created_at)
        )).scalars().all()


async def _cleanup(factory, run_id):
    async with factory() as db:
        run = await db.get(SimulationRun, run_id)
        if run is not None:
            await db.delete(run)
            await db.commit()


@pytest.mark.asyncio
async def test_first_write_inserts_revision_one(engine):
    factory, run_id, sim = await _setup(engine)
    try:
        w = uuid.uuid4()
        res = await sim.verdicts.upsert(_thread(), _row(run_id), w, 8)
        assert res.outcome == "inserted"
        (row,) = await _rows(factory, run_id)
        assert (row.verdict_revision, row.verdict_write_id, row.verdict_ordinal) == (1, w, 8)
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_later_verdict_updates_in_place_increments_and_drops_the_old(engine):
    factory, run_id, sim = await _setup(engine)
    try:
        first = await sim.verdicts.upsert(_thread(), _row(run_id, n=1), uuid.uuid4(), 8)
        w2 = uuid.uuid4()
        second = await sim.verdicts.upsert(_thread(), _row(run_id, n=2, slack_ts="2.2"), w2, 10)
        assert second.outcome == "updated"
        assert second.assessment_id == first.assessment_id, "no re-point, no delete"
        (row,) = await _rows(factory, run_id)
        assert row.verdict_revision == 2 and row.verdict_ordinal == 10 and row.verdict_write_id == w2
        assert row.slack_ts == "2.2" and row.raw_verdict == {"n": 2}
        (drop,) = await _drops(factory, run_id)
        assert drop.reason == "duplicate_thread_verdict"
        assert drop.raw_verdict == {"n": 1}
        assert "superseded" in drop.detail
        assert row.created_at == drop.created_at, "created_at is the supersede transaction's now()"
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_same_write_id_retry_is_a_no_op(engine):
    factory, run_id, sim = await _setup(engine)
    try:
        w = uuid.uuid4()
        await sim.verdicts.upsert(_thread(), _row(run_id), w, 8)
        again = await sim.verdicts.upsert(_thread(), _row(run_id, n=9), w, 8)
        assert again.outcome == "already_applied"
        (row,) = await _rows(factory, run_id)
        assert row.raw_verdict == {"n": 1} and row.verdict_revision == 1
        assert await _drops(factory, run_id) == []
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_stale_lower_ordinal_is_dropped_not_applied(engine):
    factory, run_id, sim = await _setup(engine)
    try:
        await sim.verdicts.upsert(_thread(), _row(run_id, n=2), uuid.uuid4(), 10)
        stale = await sim.verdicts.upsert(_thread(), _row(run_id, n=1), uuid.uuid4(), 8)
        assert stale.outcome == "stale"
        (row,) = await _rows(factory, run_id)
        assert row.raw_verdict == {"n": 2} and row.verdict_revision == 1
        (drop,) = await _drops(factory, run_id)
        assert drop.reason == "duplicate_thread_verdict" and drop.raw_verdict == {"n": 1}
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_equal_ordinal_different_write_id_applies_and_drops_the_old(engine):
    """Review Focus 1: step 5 is a strict `>`."""
    factory, run_id, sim = await _setup(engine)
    try:
        await sim.verdicts.upsert(_thread(), _row(run_id, n=1), uuid.uuid4(), 12)
        res = await sim.verdicts.upsert(_thread(), _row(run_id, n=2), uuid.uuid4(), 12)
        assert res.outcome == "updated"
        (row,) = await _rows(factory, run_id)
        assert row.raw_verdict == {"n": 2} and row.verdict_revision == 2
        assert [d.raw_verdict for d in await _drops(factory, run_id)] == [{"n": 1}]
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_superseding_a_pre_0054_row_sets_revision_two(engine):
    """Review Focus 2: NULL revision/ordinal/write id coalesce to 1/0."""
    factory, run_id, sim = await _setup(engine)
    try:
        async with factory() as db:
            db.add(OpportunityAssessment(**_row(run_id, n=0)))
            await db.commit()
        res = await sim.verdicts.upsert(_thread(), _row(run_id, n=1), uuid.uuid4(), 4)
        assert res.outcome == "updated"
        (row,) = await _rows(factory, run_id)
        assert row.verdict_revision == 2 and row.verdict_ordinal == 4
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_a_null_thread_verdict_is_a_plain_insert(engine):
    factory, run_id, sim = await _setup(engine)
    try:
        await sim.verdicts.upsert(None, _row(run_id, thread_id=None, n=1), uuid.uuid4(), None)
        await sim.verdicts.upsert(None, _row(run_id, thread_id=None, n=2), uuid.uuid4(), None)
        assert len(await _rows(factory, run_id)) == 2
    finally:
        await _cleanup(factory, run_id)


@pytest.mark.asyncio
async def test_the_insert_race_ends_with_one_row(engine):
    factory, run_id, sim = await _setup(engine)
    try:
        await asyncio.gather(
            sim.verdicts.upsert(_thread(), _row(run_id, n=1), uuid.uuid4(), 8),
            sim.verdicts.upsert(_thread(), _row(run_id, n=2), uuid.uuid4(), 8),
        )
        (row,) = await _rows(factory, run_id)
        assert row.verdict_revision == 2
        assert len(await _drops(factory, run_id)) == 1
    finally:
        await _cleanup(factory, run_id)


async def _reviewer(factory):
    from tests import factories

    async with factory() as db:
        user = await factories.make_user(db, email=f"rev-{uuid.uuid4().hex[:8]}@example.org")
        await db.commit()
        return user


def _review(assessment_id, reviewer):
    return AssessmentReview(
        assessment_id=assessment_id, reviewer_user_id=reviewer.id,
        reviewer_name=reviewer.name, score=3, feedback_mode="log_only",
    )


async def _drop_user(factory, user):
    async with factory() as db:
        row = await db.get(User, user.id)
        if row is not None:
            await db.delete(row)
            await db.commit()


@pytest.mark.asyncio
async def test_a_supersede_does_not_block_a_concurrent_review_insert(engine):
    """lockmode_check: FOR NO KEY UPDATE does not block an FK child insert (C24)."""
    factory, run_id, sim = await _setup(engine)
    reviewer = await _reviewer(factory)
    try:
        first = await sim.verdicts.upsert(_thread(), _row(run_id, n=1), uuid.uuid4(), 8)
        async with factory() as holder:
            await holder.execute(text(
                "SELECT id FROM opportunity_assessments WHERE id = :i FOR NO KEY UPDATE"
            ), {"i": first.assessment_id})

            async def _insert_review():
                async with factory() as db:
                    db.add(_review(first.assessment_id, reviewer))
                    await db.commit()

            await asyncio.wait_for(_insert_review(), timeout=5)
            await holder.rollback()
    finally:
        await _cleanup(factory, run_id)
        await _drop_user(factory, reviewer)


@pytest.mark.asyncio
async def test_cascade_race_a_review_committed_during_a_supersede_survives(engine):
    """raw/myverify/db_repros.py::cascade_race, rewritten for §8.1."""
    factory, run_id, sim = await _setup(engine)
    reviewer = await _reviewer(factory)
    try:
        first = await sim.verdicts.upsert(_thread(), _row(run_id, n=1), uuid.uuid4(), 8)
        async with factory() as db:
            db.add(_review(first.assessment_id, reviewer))
            await asyncio.gather(
                db.commit(),
                sim.verdicts.upsert(_thread(), _row(run_id, n=2), uuid.uuid4(), 10),
            )
        async with factory() as db:
            reviews = (await db.execute(
                select(AssessmentReview).where(AssessmentReview.assessment_id == first.assessment_id)
            )).scalars().all()
        assert len(reviews) == 1, "the review is attached to the one row, which was never deleted"
    finally:
        await _cleanup(factory, run_id)
        await _drop_user(factory, reviewer)


@pytest.mark.asyncio
async def test_a_retried_stale_write_records_one_drop(engine):
    """Correction 12: a lost-ack retry of a stale write does not add a second drop."""
    factory, run_id, sim = await _setup(engine)
    try:
        await sim.verdicts.upsert(_thread(), _row(run_id, n=2), uuid.uuid4(), 10)
        stale_w = uuid.uuid4()
        await sim.verdicts.upsert(_thread(), _row(run_id, n=1), stale_w, 8)
        again = await sim.verdicts.upsert(_thread(), _row(run_id, n=1), stale_w, 8)
        assert again.outcome == "stale"
        assert [d.raw_verdict for d in await _drops(factory, run_id)] == [{"n": 1}]
    finally:
        await _cleanup(factory, run_id)

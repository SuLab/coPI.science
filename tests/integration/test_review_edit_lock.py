"""An edit loads its review FOR UPDATE, so the review bot's consumed_at stamp can
never land between the edit's read and its write and swallow the edit."""
import asyncio
import inspect

import pytest
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import AssessmentReview, OpportunityAssessment, SimulationRun
from src.routers import reviews as reviews_router
from src.services.assessment_reviews import edit_feedback
from src.services.review_bot import _feedback_snapshot_entry, consumed_at_predicates

pytestmark = pytest.mark.integration


async def _seed(factory):
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.flush()
        assessment = OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        )
        db.add(assessment)
        await db.flush()
        review = AssessmentReview(
            assessment_id=assessment.id, reviewer_name="r", score=3, comment="A",
            feedback_mode="learn",
        )
        db.add(review)
        await db.commit()
        return run.id, review.id


async def _drop(factory, run_id):
    async with factory() as db:
        run = await db.get(SimulationRun, run_id)
        if run is not None:
            await db.delete(run)
            await db.commit()


async def _bot_stamp(factory, snap, review_id):
    async with factory() as b:
        res = await b.execute(
            update(AssessmentReview)
            .where(AssessmentReview.id == review_id, *consumed_at_predicates(snap))
            .values(consumed_at=func.now())
        )
        await b.commit()
        return res.rowcount


async def test_the_bot_waits_for_the_edit_and_then_matches_nothing(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id, review_id = await _seed(factory)
    try:
        async with factory() as a:
            review = await reviews_router._load_review(a, review_id, for_update=True)
            snap = _feedback_snapshot_entry(review)
            bot = asyncio.create_task(_bot_stamp(factory, snap, review_id))
            await asyncio.sleep(0.5)
            assert not bot.done(), "the bot's predicate UPDATE must wait on the row lock"
            await edit_feedback(a, review=review, score=2, comment="B", feedback_mode="learn")
            await a.commit()
        assert await bot == 0
        async with factory() as db:
            row = await db.get(AssessmentReview, review_id)
            assert row.comment == "B" and row.consumed_at is None
    finally:
        await _drop(factory, run_id)


async def test_when_the_bot_goes_first_the_edit_resets_consumed_at(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id, review_id = await _seed(factory)
    try:
        async with factory() as probe:
            snap = _feedback_snapshot_entry(await probe.get(AssessmentReview, review_id))
        assert await _bot_stamp(factory, snap, review_id) == 1
        async with factory() as a:
            review = await reviews_router._load_review(a, review_id, for_update=True)
            assert review.consumed_at is not None
            await edit_feedback(a, review=review, score=2, comment="B", feedback_mode="learn")
            await a.commit()
        async with factory() as db:
            row = await db.get(AssessmentReview, review_id)
            assert row.comment == "B" and row.consumed_at is None, "the edit's None was written"
    finally:
        await _drop(factory, run_id)


def test_the_edit_route_loads_for_update():
    assert "_load_review(db, feedback_id, for_update=True)" in inspect.getsource(
        reviews_router.edit_review_feedback
    )

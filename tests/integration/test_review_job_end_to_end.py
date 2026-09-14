"""The whole review pipeline through the real worker loop with only the model
faked: submit -> manual generate -> job -> claim_job -> process_job ->
execute_review_analysis -> suggestion row + consumed_at + job completed.
(The "manual generate" step is ``enqueue_pending_analyses``, called
explicitly: since F2, 2026-09-14, a review write enqueues nothing by itself.)
The existing dispatch test
(tests/integration/test_worker.py::test_worker_dispatches_review_feedback_analysis)
mocks the handler; this one does not.

Committing sessions (claim_job/process_job commit), tagged rows, sweep in
finally — the tests/integration/test_worker.py pattern.

`_one_round` is robust against a foreign pending job left behind by another
test file: the shared `engine` fixture is session-scoped, so `claim_job` can
return a job this test never enqueued. Rather than assert on whatever it
claims, `_one_round` loops claim+process (bounded at 10 rounds) until the
claimed job's own payload names the `assessment_id` it was given, returning
that job, or returns `None` once the queue empties. Any foreign job it
processes along the way is not restored — a leaked pending job is a bug in
whichever file left it, not something this test can safely put back mid-run,
and `test_worker.py`'s `foreign_pending_jobs() == 0` assertion already catches
that bug where it belongs.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import (
    USER_ROLE_REVIEWER,
    AssessmentReview,
    Job,
    OpportunityAssessment,
    PromptChangeSuggestion,
    SimulationRun,
    User,
)
from src.services import review_bot
from src.services.assessment_reviews import enqueue_pending_analyses, submit_feedback
from src.worker import main as worker_main
from tests import factories

pytestmark = pytest.mark.integration

TAG = "review_e2e"
_HAPPY = json.dumps({"target": "pi_lab", "suggestion": "S", "rationale": "R"})


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _sweep(factory):
    async with factory() as db:
        await db.execute(text(
            "DELETE FROM prompt_change_suggestions WHERE assessment_id IN "
            "(SELECT id FROM opportunity_assessments WHERE simulation_run_id IN "
            "(SELECT id FROM simulation_runs WHERE config->>'tag' = :t))"
        ), {"t": TAG})
        await db.execute(text(
            "DELETE FROM jobs WHERE type = 'review_feedback_analysis' AND payload->>'assessment_id' IN "
            "(SELECT id::text FROM opportunity_assessments WHERE simulation_run_id IN "
            "(SELECT id FROM simulation_runs WHERE config->>'tag' = :t))"
        ), {"t": TAG})
        await db.execute(delete(SimulationRun).where(SimulationRun.config["tag"].astext == TAG))
        await db.execute(delete(User).where(User.orcid.like("E2E-%")))
        await db.commit()


async def _seed(factory):
    async with factory() as db:
        run = SimulationRun(config={"tag": TAG})
        db.add(run)
        await db.flush()
        assessment = OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird", channel_name="e2e",
        )
        reviewer = await factories.make_user(
            db, user_role=USER_ROLE_REVIEWER, orcid="E2E-0000-0000-0001",
        )
        db.add(assessment)
        await db.flush()
        await db.commit()
        return assessment.id, reviewer.id


async def _one_round(factory, assessment_id) -> Job | None:
    """Claim+process until a claimed job names `assessment_id`, or the queue empties.

    See the module docstring for why this loops instead of trusting the first
    `claim_job` result outright.
    """
    for _ in range(10):
        async with factory() as db:
            job = await worker_main.claim_job(db)
        if job is None:
            return None
        await worker_main.process_job(job.id, job.type, job.attempts, job.max_attempts, factory)
        if job.payload.get("assessment_id") == str(assessment_id):
            return job
    return None


async def test_learn_feedback_becomes_a_suggestion_through_the_worker(factory, monkeypatch):
    await _sweep(factory)
    assessment_id, reviewer_id = await _seed(factory)
    try:
        async with factory() as db:
            assessment = await db.get(OpportunityAssessment, assessment_id)
            reviewer = await db.get(User, reviewer_id)
            review = await submit_feedback(
                db, assessment=assessment, reviewer=reviewer,
                score=2, comment="lab bot leaked an IC50", feedback_mode="learn",
            )
            review_id = review.id
            # The enqueue is explicit since F2 (2026-09-14): submitting 'learn'
            # feedback no longer creates the job as a side effect, so without
            # this the worker would have nothing of ours to claim. Scoped to
            # this assessment because the queue is shared and committed here.
            enqueued, eligible = await enqueue_pending_analyses(
                db, requested_by=reviewer, assessment_id=assessment_id
            )
            assert (enqueued, eligible) == (1, 1)
            await db.commit()

        async def _fake(*args, **kwargs):
            return _HAPPY

        monkeypatch.setattr(review_bot, "generate_agent_response", _fake)

        processed = await _one_round(factory, assessment_id)
        assert processed is not None and processed.type == "review_feedback_analysis"

        async with factory() as check:
            job = await check.get(Job, processed.id)
            assert job.status == "completed" and job.last_error is None
            suggestion = (await check.execute(
                select(PromptChangeSuggestion).where(
                    PromptChangeSuggestion.assessment_id == assessment_id
                )
            )).scalar_one()
            assert suggestion.target == "pi_lab"
            assert [f["id"] for f in suggestion.feedback_snapshot] == [str(review_id)]
            assert (await check.get(AssessmentReview, review_id)).consumed_at is not None

        # Queue is drained: nothing else of ours is claimable.
        async with factory() as db:
            remaining = (await db.execute(
                select(Job).where(
                    Job.type == "review_feedback_analysis",
                    Job.payload["assessment_id"].astext == str(assessment_id),
                    Job.status == "pending",
                )
            )).scalars().all()
        assert remaining == []
    finally:
        await _sweep(factory)


async def test_a_noop_job_completes_cleanly_without_a_suggestion(factory, monkeypatch):
    await _sweep(factory)
    assessment_id, _ = await _seed(factory)
    try:
        async with factory() as db:
            db.add(AssessmentReview(
                assessment_id=assessment_id, reviewer_name="r", score=3,
                comment="log only", feedback_mode="log_only",
            ))
            db.add(Job(
                type="review_feedback_analysis", payload={"assessment_id": str(assessment_id)},
            ))
            await db.commit()

        calls = []

        async def _fake(*args, **kwargs):
            calls.append(1)
            return _HAPPY

        monkeypatch.setattr(review_bot, "generate_agent_response", _fake)
        processed = await _one_round(factory, assessment_id)
        assert processed is not None
        assert calls == []
        async with factory() as check:
            job = await check.get(Job, processed.id)
            assert job.status == "completed"
            assert (await check.execute(
                select(PromptChangeSuggestion).where(
                    PromptChangeSuggestion.assessment_id == assessment_id
                )
            )).scalars().all() == []
    finally:
        await _sweep(factory)

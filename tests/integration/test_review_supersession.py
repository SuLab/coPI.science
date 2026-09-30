"""A later verdict updates the interview's one assessment row IN PLACE (§8.1), so
the human-review rows attached to the earlier verdict are never re-pointed or
deleted: the row id is unchanged and the review, assignment and pending
``review_feedback_analysis`` job still reference it.

Three of the four review tables CASCADE off ``opportunity_assessments.id``
(``AssessmentReview``, ``AssessmentReviewEvent``, ``AssessmentReviewAssignment``)
and the fourth (``PromptChangeSuggestion``) is SET NULL, which is why the old
supersede-by-delete needed a re-point at all. Driven through
``_capture_hub_assessment`` so the whole capture path is exercised.
"""

import json

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.engine.verdicts import Verdicts
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import (
    AssessmentDrop,
    AssessmentReview,
    AssessmentReviewAssignment,
    AssessmentReviewEvent,
    Job,
    OpportunityAssessment,
    PromptChangeSuggestion,
    SimulationRun,
    User,
)
from src.services.blackbird_rubric import RUBRIC_WEIGHTS
from tests import factories
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def _reset_review_tables(engine):
    """Start every test in this module with these tables empty.

    Every test here commits for real against the shared session-scoped
    ``engine`` (the same reason ``test_opportunity_assessment_persistence.
    py``'s ``_reset_assessment_tables`` exists) — several assertions below
    query a review table with no filter at all, which is the whole point:
    "exactly one assignment row survives" is only a meaningful claim if the
    table started empty.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        await db.execute(sa_delete(AssessmentReviewAssignment))
        await db.execute(sa_delete(AssessmentReviewEvent))
        await db.execute(sa_delete(AssessmentReview))
        await db.execute(sa_delete(PromptChangeSuggestion))
        await db.execute(sa_delete(AssessmentDrop))
        await db.execute(sa_delete(OpportunityAssessment))
        await db.execute(sa_delete(SimulationRun))
        await db.commit()
    yield


async def _new_run(factory):
    async with factory() as setup:
        run = SimulationRun()
        setup.add(run)
        await setup.commit()
        return run.id


async def _make_user(factory) -> User:
    async with factory() as db:
        user = await factories.make_user(db)
        await db.commit()
        return user


async def _attach_review_rows(factory, assessment_id, assignee_id) -> dict:
    """One row of each of the four review-table kinds, attached to
    ``assessment_id``. Returns each row's own id so the test can look each up
    by primary key afterward rather than trusting an ``assessment_id`` filter
    that is exactly what is under test."""
    async with factory() as db:
        review = AssessmentReview(
            assessment_id=assessment_id, reviewer_name="Dr. Reviewer",
            score=4, feedback_mode="learn",
        )
        event = AssessmentReviewEvent(
            assessment_id=assessment_id, action="approved", actor_name="Dr. Reviewer",
        )
        assignment = AssessmentReviewAssignment(
            assessment_id=assessment_id, assignee_user_id=assignee_id,
            assignee_name="Assignee", assigned_by_name="Boss",
        )
        suggestion = PromptChangeSuggestion(
            assessment_id=assessment_id, subject_label="s",
            feedback_snapshot=[], target="scout_hub", prompt_files=[],
            suggestion="tighten the rubric wording", transcript_available=False,
        )
        db.add_all([review, event, assignment, suggestion])
        await db.flush()
        ids = {
            "review": review.id, "event": event.id,
            "assignment": assignment.id, "suggestion": suggestion.id,
        }
        await db.commit()
        return ids


async def _cleanup(factory, run_id, *, user_ids=()):
    async with factory() as db:
        # PromptChangeSuggestion.assessment_id is SET NULL, not CASCADE — a
        # row that outlives the assessment it was attached to is not cleaned
        # up by deleting the run below, so it is swept explicitly here.
        await db.execute(sa_delete(PromptChangeSuggestion))
        stale = (await db.execute(
            select(SimulationRun).where(SimulationRun.id == run_id)
        )).scalar_one_or_none()
        if stale is not None:
            await db.delete(stale)  # cascades the assessment(s) and their reviews
        if user_ids:
            await db.execute(sa_delete(User).where(User.id.in_(user_ids)))
        await db.commit()


def _reply(score: int) -> str:
    verdict = {
        "subject_agent_id": "wang", "recommendation": "route-to-incubation",
        "rationale": f"rationale {score}", "scores": {k: score for k in RUBRIC_WEIGHTS},
        "company_or_project": f"Project {score}", "elevator_pitch": f"Pitch {score}.",
    }
    return (
        "<slack_message>Noted.</slack_message>\n"
        f"<assessment_json>{json.dumps(verdict)}</assessment_json>"
    )


@pytest.mark.asyncio
async def test_a_supersession_keeps_the_row_and_everything_attached_to_it(engine):
    """Seeds a review, an assignment and a pending ``review_feedback_analysis`` job
    on the first verdict's row, supersedes it through ``_capture_hub_assessment``,
    and asserts the row id is unchanged, all three still reference it, and one
    ``duplicate_thread_verdict`` drop exists."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = await _new_run(factory)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    sim = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")},
        session_factory=factory, simulation_run_id=run_id,
    )
    thread = ThreadState(thread_id="t1", channel="general", other_agent_id="wang", message_count=7)
    assignee = await _make_user(factory)
    job_id = None
    try:
        await sim.verdicts._capture_hub_assessment(
            hub, thread, _reply(3), "1.1", closes_thread=False,
        )
        async with factory() as db:
            (first,) = (await db.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().all()
        first_id = first.id
        ids = await _attach_review_rows(factory, first_id, assignee.id)
        async with factory() as db:
            job = Job(
                type="review_feedback_analysis", payload={"assessment_id": str(first_id)},
            )
            db.add(job)
            await db.flush()
            job_id = job.id
            await db.commit()

        thread.message_count = 9
        await sim.verdicts._capture_hub_assessment(
            hub, thread, _reply(4), "2.2", closes_thread=False,
        )

        async with factory() as check:
            rows = (await check.execute(
                select(OpportunityAssessment).where(
                    OpportunityAssessment.simulation_run_id == run_id
                )
            )).scalars().all()
            assert [r.id for r in rows] == [first_id], "updated in place, same id"
            assert rows[0].slack_ts == "2.2"
            assert (await check.get(AssessmentReview, ids["review"])).assessment_id == first_id
            assert (await check.get(AssessmentReviewEvent, ids["event"])).assessment_id == first_id
            assert (
                await check.get(AssessmentReviewAssignment, ids["assignment"])
            ).assessment_id == first_id
            assert (
                await check.get(PromptChangeSuggestion, ids["suggestion"])
            ).assessment_id == first_id
            assert (await check.get(Job, job_id)).payload == {"assessment_id": str(first_id)}
            drops = (await check.execute(
                select(AssessmentDrop).where(AssessmentDrop.simulation_run_id == run_id)
            )).scalars().all()
            assert [d.reason for d in drops] == ["duplicate_thread_verdict"]
    finally:
        async with factory() as db:
            if job_id is not None:
                await db.execute(sa_delete(Job).where(Job.id == job_id))
            await db.commit()
        await _cleanup(factory, run_id, user_ids=[assignee.id])


@pytest.mark.asyncio
async def test_persist_returns_false_none_with_no_db():
    """No database configured at all (see ``SimulationEngine.__init__``):
    ``_persist_assessment`` must answer ``(False, None)``, not merely a
    falsy first element — ``_capture_hub_assessment``'s call site unpacks
    both, and a caller that only checked truthiness would treat ANY tuple,
    including this one, as HELD."""
    stub = SimulationEngine(
        agents=[], slack_clients={}, session_factory=None, simulation_run_id=None,
    )
    held, replacement_id = await Verdicts._persist_assessment(
        stub, "blackbird", "general", {"scores": {"differentiation": 5}},
    )
    assert held is False
    assert replacement_id is None

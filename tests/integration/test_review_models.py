"""DB-backed proof of migration 0039: the reviewer role, the four review
tables' constraints, and their FK behaviour (CASCADE vs SET NULL) per
docs/plans/2026-08-28-human-review-feedback-adversarial-analysis.md (A-1/A-2/A-3).

conftest.py migrates the real alembic chain to head for every test here, so
these prove the migration actually applies and behaves as designed — not just
that the ORM models match some in-memory expectation.
"""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from src.models import (
    USER_ROLE_REVIEWER,
    AssessmentReview,
    AssessmentReviewAssignment,
    AssessmentReviewEvent,
    Job,
    OpportunityAssessment,
    PromptChangeSuggestion,
    SimulationRun,
)
from tests import factories

pytestmark = pytest.mark.integration


async def _seed_assessment(db):
    run = SimulationRun()
    db.add(run)
    await db.flush()
    a = OpportunityAssessment(simulation_run_id=run.id, agent_id="blackbird", channel_name="c")
    db.add(a)
    await db.flush()
    return a


async def test_reviewer_role_passes_the_check_constraint(db_session):
    u = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assert u.user_role == "reviewer"


async def test_score_check_constraint_rejects_out_of_range(db_session):
    a = await _seed_assessment(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                AssessmentReview(
                    assessment_id=a.id, reviewer_name="R", score=6, feedback_mode="learn"
                )
            )
            await db_session.flush()


async def test_deleting_the_assessment_cascades_reviews_but_nulls_suggestions(db_session):
    a = await _seed_assessment(db_session)
    db_session.add(
        AssessmentReview(
            assessment_id=a.id, reviewer_name="R", score=3, feedback_mode="log_only"
        )
    )
    db_session.add(
        PromptChangeSuggestion(
            assessment_id=a.id,
            subject_label="s",
            feedback_snapshot=[],
            target="scout_hub",
            prompt_files=[],
            suggestion="x",
            transcript_available=False,
        )
    )
    await db_session.flush()
    await db_session.execute(
        text("DELETE FROM opportunity_assessments WHERE id = :i"), {"i": str(a.id)}
    )
    assert await db_session.scalar(select(func.count()).select_from(AssessmentReview)) == 0
    assert (
        await db_session.scalar(select(func.count()).select_from(PromptChangeSuggestion)) == 1
    )
    assert await db_session.scalar(select(PromptChangeSuggestion.assessment_id)) is None


async def test_deleting_the_reviewer_nulls_the_fk_and_keeps_the_name(db_session):
    a = await _seed_assessment(db_session)
    u = await factories.make_user(db_session)
    db_session.add(
        AssessmentReview(
            assessment_id=a.id,
            reviewer_user_id=u.id,
            reviewer_name="Keep Me",
            score=4,
            feedback_mode="learn",
        )
    )
    await db_session.flush()
    await db_session.delete(u)
    await db_session.flush()
    row = (
        await db_session.execute(
            select(AssessmentReview.reviewer_user_id, AssessmentReview.reviewer_name)
        )
    ).one()
    assert row.reviewer_user_id is None and row.reviewer_name == "Keep Me"


async def test_deleting_the_assignee_cascades_the_assignment(db_session):
    """A-3: an assignment TO a deleted user is meaningless and should vanish,
    the opposite FK behaviour from AssessmentReview's reviewer_user_id."""
    a = await _seed_assessment(db_session)
    assignee = await factories.make_user(db_session)
    db_session.add(
        AssessmentReviewAssignment(
            assessment_id=a.id,
            assignee_user_id=assignee.id,
            assignee_name="Assignee",
            assigned_by_name="Boss",
        )
    )
    await db_session.flush()
    await db_session.delete(assignee)
    await db_session.flush()
    assert (
        await db_session.scalar(select(func.count()).select_from(AssessmentReviewAssignment))
        == 0
    )


async def test_assignment_unique_constraint_rejects_a_duplicate(db_session):
    a = await _seed_assessment(db_session)
    assignee = await factories.make_user(db_session)
    db_session.add(
        AssessmentReviewAssignment(
            assessment_id=a.id,
            assignee_user_id=assignee.id,
            assignee_name="Assignee",
            assigned_by_name="Boss",
        )
    )
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                AssessmentReviewAssignment(
                    assessment_id=a.id,
                    assignee_user_id=assignee.id,
                    assignee_name="Assignee",
                    assigned_by_name="Someone Else",
                )
            )
            await db_session.flush()


async def test_review_event_action_check_constraint_rejects_an_unknown_action(db_session):
    a = await _seed_assessment(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                AssessmentReviewEvent(assessment_id=a.id, action="bogus", actor_name="A")
            )
            await db_session.flush()


async def test_suggestion_status_check_constraint_rejects_an_unknown_status(db_session):
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                PromptChangeSuggestion(
                    subject_label="s",
                    feedback_snapshot=[],
                    target="scout_hub",
                    prompt_files=[],
                    suggestion="x",
                    transcript_available=False,
                    status="bogus",
                )
            )
            await db_session.flush()


async def test_job_enum_round_trips_the_new_type(db_session):
    j = Job(type="review_feedback_analysis", payload={})
    db_session.add(j)
    await db_session.flush()
    job_id = j.id  # captured before expire_all() below expires `j` too
    db_session.expire_all()
    fetched = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert fetched.type == "review_feedback_analysis"

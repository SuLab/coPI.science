"""Races between the review-feedback writers and a running
``review_feedback_analysis`` job (spec: docs/audits/2026-09-02-review-pipeline/README.md, D1/D2).

Both tests drive the seam directly: ``submit_feedback``/``edit_feedback`` on one
side, ``execute_review_analysis`` on the other, with the model call replaced by
a fake that performs the concurrent write WHILE the call is in flight — the
window the worker spends waiting on Opus. The savepoint-isolated ``db_session``
fixture is faithful here because the production race is about ORDER (snapshot,
then write, then stamp), not about transaction visibility.
"""

from __future__ import annotations

from sqlalchemy import select, update

from src.models import (
    USER_ROLE_REVIEWER,
    AssessmentReview,
    Job,
    OpportunityAssessment,
    PromptChangeSuggestion,
)
from src.services import review_bot
from src.services.assessment_reviews import edit_feedback, submit_feedback
from src.services.blackbird_rubric import load_rubric
from tests import factories

_HAPPY = '{"target": "scout_hub", "suggestion": "S", "rationale": "R"}'


async def _seed_assessment(db, **overrides) -> OpportunityAssessment:
    run = await factories.make_simulation_run(db)
    data = dict(simulation_run_id=run.id, agent_id="blackbird", channel_name="c1")
    data.update(overrides)
    assessment = OpportunityAssessment(**data)
    db.add(assessment)
    await db.flush()
    return assessment


async def _review_jobs(db) -> list[Job]:
    return list(
        (
            await db.execute(
                select(Job)
                .where(Job.type == "review_feedback_analysis")
                .order_by(Job.enqueued_at, Job.id)
            )
        ).scalars()
    )


async def test_learn_feedback_submitted_mid_job_gets_its_own_job(db_session, monkeypatch):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    second = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    r1 = await submit_feedback(
        db_session, assessment=assessment, reviewer=reviewer,
        score=2, comment="first", feedback_mode="learn",
    )
    (job,) = await _review_jobs(db_session)
    job.status = "processing"  # what claim_job does
    await db_session.flush()

    late: dict = {}

    async def _fake(system_prompt, messages, model=None, max_tokens=None, **kw):
        late["review"] = await submit_feedback(
            db_session, assessment=assessment, reviewer=second,
            score=5, comment="second, mid-flight", feedback_mode="learn",
        )
        return _HAPPY

    monkeypatch.setattr(review_bot, "generate_agent_response", _fake)
    await review_bot.execute_review_analysis(job, db_session)

    r2 = late["review"]
    await db_session.refresh(r1)
    await db_session.refresh(r2)
    assert r1.consumed_at is not None
    assert r2.consumed_at is None, "r2 was never analyzed, so it must stay unconsumed"

    statuses = sorted(j.status for j in await _review_jobs(db_session))
    assert statuses == ["pending", "processing"], (
        "the mid-flight submission must enqueue its own job; a 'processing' job "
        "does not cover feedback it never saw"
    )
    suggestion = (await db_session.execute(select(PromptChangeSuggestion))).scalar_one()
    assert [f["id"] for f in suggestion.feedback_snapshot] == [str(r1.id)]


async def test_edit_mid_job_leaves_the_edited_row_unconsumed_and_requeued(
    db_session, monkeypatch
):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    r1 = await submit_feedback(
        db_session, assessment=assessment, reviewer=reviewer,
        score=2, comment="ORIGINAL", feedback_mode="learn",
    )
    (job,) = await _review_jobs(db_session)
    job.status = "processing"
    await db_session.flush()

    async def _fake(system_prompt, messages, model=None, max_tokens=None, **kw):
        assert "ORIGINAL" in messages[0]["content"]
        await edit_feedback(
            db_session, review=r1, score=1, comment="EDITED", feedback_mode="learn",
        )
        return _HAPPY

    monkeypatch.setattr(review_bot, "generate_agent_response", _fake)
    await review_bot.execute_review_analysis(job, db_session)

    await db_session.refresh(r1)
    assert r1.comment == "EDITED"
    assert r1.consumed_at is None, "only ORIGINAL was analyzed; EDITED is still owed a job"
    suggestion = (await db_session.execute(select(PromptChangeSuggestion))).scalar_one()
    assert suggestion.feedback_snapshot[0]["comment"] == "ORIGINAL"
    assert sorted(j.status for j in await _review_jobs(db_session)) == ["pending", "processing"]


async def test_dimension_only_edit_mid_job_leaves_the_row_unconsumed_and_requeued(
    db_session, monkeypatch
):
    """A1, through the real code path. Sibling to
    ``test_edit_mid_job_leaves_the_edited_row_unconsumed_and_requeued`` above,
    but the mid-flight edit here changes ONLY the per-dimension scores —
    ``score`` and ``comment`` are identical before and after. Before this fix,
    the job's conditional UPDATE compared score/comment alone, so this exact
    edit re-stamped the row consumed and the replacement job (already
    enqueued by ``edit_feedback``) found nothing to analyze: the edit was
    lost with no warning, because the stamp genuinely succeeded. Nothing in
    the suite exercised this shape through ``execute_review_analysis`` itself
    until now — the unit-level `consumed_at_predicates` test proves the
    predicate, this proves the whole job survives the race.
    """
    dims = load_rubric().dimensions
    key_a, key_b = dims[0].key, dims[1].key

    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    r1 = await submit_feedback(
        db_session, assessment=assessment, reviewer=reviewer,
        score=2, comment="steady", feedback_mode="learn",
        dimension_scores={key_a: 3},
    )
    (job,) = await _review_jobs(db_session)
    job.status = "processing"
    await db_session.flush()

    async def _fake(system_prompt, messages, model=None, max_tokens=None, **kw):
        assert "steady" in messages[0]["content"]
        await edit_feedback(
            db_session, review=r1, score=2, comment="steady", feedback_mode="learn",
            dimension_scores={key_a: 3, key_b: 5},
        )
        return _HAPPY

    monkeypatch.setattr(review_bot, "generate_agent_response", _fake)
    await review_bot.execute_review_analysis(job, db_session)

    await db_session.refresh(r1)
    assert r1.dimension_scores == {key_a: 3, key_b: 5}
    assert r1.consumed_at is None, (
        "only the pre-edit dimension scores were analyzed; the edited row is still owed a job"
    )
    suggestion = (await db_session.execute(select(PromptChangeSuggestion))).scalar_one()
    assert suggestion.feedback_snapshot[0]["dimension_scores"] == {key_a: 3}
    assert sorted(j.status for j in await _review_jobs(db_session)) == ["pending", "processing"]


async def test_unchanged_rows_are_still_stamped_and_no_warning_fires(
    db_session, monkeypatch, caplog
):
    """The conditional stamp must not be so strict that the ordinary case
    (nothing changed while the model ran) leaves rows unconsumed."""
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    r1 = await submit_feedback(
        db_session, assessment=assessment, reviewer=reviewer,
        score=3, comment="steady", feedback_mode="learn",
    )
    (job,) = await _review_jobs(db_session)

    async def _fake(*args, **kwargs):
        return _HAPPY

    monkeypatch.setattr(review_bot, "generate_agent_response", _fake)
    with caplog.at_level("WARNING", logger="src.services.review_bot"):
        await review_bot.execute_review_analysis(job, db_session)

    await db_session.refresh(r1)
    assert r1.consumed_at is not None
    assert not [rec for rec in caplog.records if "stamped" in rec.getMessage()]


async def test_repointed_job_consumes_the_repointed_reviews_under_the_new_id(
    db_session, monkeypatch
):
    """What the engine's re-point (Task 2 of the 2026-09-02 plan) hands the
    handler: reviews AND the job now name the replacement; the retired row is
    gone. The handler must analyze under the replacement id."""
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    retired = await _seed_assessment(db_session, slack_ts="1.1")
    replacement = OpportunityAssessment(
        simulation_run_id=retired.simulation_run_id, agent_id="blackbird",
        channel_name="c1", slack_ts="2.2",
    )
    db_session.add(replacement)
    await db_session.flush()

    review = await submit_feedback(
        db_session, assessment=retired, reviewer=reviewer,
        score=3, comment="on the provisional verdict", feedback_mode="learn",
    )
    (job,) = await _review_jobs(db_session)
    assert job.payload == {"assessment_id": str(retired.id)}

    # The engine's re-point, then the delete.
    await db_session.execute(
        update(AssessmentReview)
        .where(AssessmentReview.assessment_id == retired.id)
        .values(assessment_id=replacement.id)
    )
    await db_session.execute(
        update(Job).where(Job.id == job.id)
        .values(payload={"assessment_id": str(replacement.id)})
    )
    await db_session.delete(retired)
    await db_session.flush()
    await db_session.refresh(job)

    async def _fake(*args, **kwargs):
        return _HAPPY

    monkeypatch.setattr(review_bot, "generate_agent_response", _fake)
    await review_bot.execute_review_analysis(job, db_session)

    await db_session.refresh(review)
    assert review.consumed_at is not None
    suggestion = (await db_session.execute(select(PromptChangeSuggestion))).scalar_one()
    assert suggestion.assessment_id == replacement.id

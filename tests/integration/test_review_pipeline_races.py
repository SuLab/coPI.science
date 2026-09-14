"""Races between the review-feedback writers and a running
``review_feedback_analysis`` job (spec: docs/audits/2026-09-02-review-pipeline/README.md, D1/D2).

Every test drives the seam directly: ``submit_feedback``/``edit_feedback`` on one
side, ``execute_review_analysis`` on the other, with the model call replaced by
a fake that performs the concurrent write WHILE the call is in flight — the
window the worker spends waiting on Opus. The savepoint-isolated ``db_session``
fixture is faithful here because the production race is about ORDER (snapshot,
then write, then stamp), not about transaction visibility.

**What F2 (2026-09-14) changed here, and what it did not.** These tests used
to get their job for free: ``submit_feedback`` enqueued one as a side effect,
so ``(job,) = await _review_jobs(...)`` found it. Nothing enqueues
automatically any more, so each test now calls ``_enqueue_for`` — the manual
generate, at the service seam — to get the job it then puts in flight.

The guarantee under test is UNCHANGED and never depended on who enqueued:
``review_bot.consumed_at_predicates`` (untouched by F2) refuses to stamp a row
that no longer reads exactly as snapshotted, so a row written or edited
mid-call stays UNCONSUMED. What used to prove that was a second, automatically
enqueued ``pending`` job sitting behind the ``processing`` one; the queue now
reads ``["processing"]`` alone, so each of those tests asserts the unconsumed
row directly AND that a manual generate still finds and enqueues it. The
anti-loss property is the same; only the scheduler moved.
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
from src.services.assessment_reviews import (
    edit_feedback,
    enqueue_pending_analyses,
    submit_feedback,
)
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


async def _enqueue_for(db, assessment, requester) -> Job:
    """The manual generate (F2), scoped to one assessment, returning its job.

    Scoped rather than global so the count assertion is exact: an unscoped
    press considers every assessment with unconsumed 'learn' feedback in the
    database, and not every test file confines its rows to a savepoint.
    """
    enqueued, eligible = await enqueue_pending_analyses(
        db, requested_by=requester, assessment_id=assessment.id
    )
    assert (enqueued, eligible) == (1, 1)
    await db.flush()
    (job,) = [
        j
        for j in await _review_jobs(db)
        if j.payload["assessment_id"] == str(assessment.id)
    ]
    return job


async def _assert_a_manual_generate_picks_it_up(db, assessment, requester) -> None:
    """The other half of every "stays unconsumed" assertion below: unconsumed
    is only worth anything if the row is still ELIGIBLE. Before F2 this was
    implicit in a second `pending` job appearing by itself."""
    enqueued, eligible = await enqueue_pending_analyses(
        db, requested_by=requester, assessment_id=assessment.id
    )
    assert (enqueued, eligible) == (1, 1)


async def test_learn_feedback_submitted_mid_job_stays_unconsumed_for_the_next_generate(
    db_session, monkeypatch
):
    """The mid-flight submission is never analyzed by the job already in
    flight — a 'processing' job has snapshotted its rows and cannot cover
    feedback it never saw — so it must stay unconsumed and stay eligible.
    Before F2 (2026-09-14) that showed up as its own automatically enqueued
    'pending' job; now it shows up as an unconsumed row that the next manual
    generate picks up. See the module docstring.
    """
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    second = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    r1 = await submit_feedback(
        db_session, assessment=assessment, reviewer=reviewer,
        score=2, comment="first", feedback_mode="learn",
    )
    job = await _enqueue_for(db_session, assessment, reviewer)
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
    assert statuses == ["processing"], (
        "nothing enqueues automatically since F2; the only job here is the one "
        "this test put in flight"
    )
    await _assert_a_manual_generate_picks_it_up(db_session, assessment, reviewer)
    suggestion = (await db_session.execute(select(PromptChangeSuggestion))).scalar_one()
    assert [f["id"] for f in suggestion.feedback_snapshot] == [str(r1.id)]


async def test_edit_mid_job_leaves_the_edited_row_unconsumed_and_requeued(
    db_session, monkeypatch
):
    """The edit lands while the model call is in flight, so the job's
    conditional stamp matches nothing and the row stays unconsumed —
    ``consumed_at_predicates``, untouched by F2 (2026-09-14). "Requeued" in
    the name now means "still eligible for the next manual generate" rather
    than "an automatic replacement job was enqueued"; the asserted property
    is the same. See the module docstring.
    """
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    r1 = await submit_feedback(
        db_session, assessment=assessment, reviewer=reviewer,
        score=2, comment="ORIGINAL", feedback_mode="learn",
    )
    job = await _enqueue_for(db_session, assessment, reviewer)
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
    assert sorted(j.status for j in await _review_jobs(db_session)) == ["processing"]
    await _assert_a_manual_generate_picks_it_up(db_session, assessment, reviewer)


async def test_dimension_only_edit_mid_job_leaves_the_row_unconsumed_and_requeued(
    db_session, monkeypatch
):
    """A1, through the real code path. Sibling to
    ``test_edit_mid_job_leaves_the_edited_row_unconsumed_and_requeued`` above,
    but the mid-flight edit here changes ONLY the per-dimension scores —
    ``score`` and ``comment`` are identical before and after. Before this fix,
    the job's conditional UPDATE compared score/comment alone, so this exact
    edit re-stamped the row consumed and the next analysis pass found nothing
    to analyze: the edit was lost with no warning, because the stamp genuinely
    succeeded. (``edit_feedback`` used to enqueue that replacement pass itself;
    since 2026-09-14 it does not, and the row simply stays eligible for the
    next manual generate — which is what makes leaving it unconsumed the whole
    of the guarantee.) Nothing in
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
    job = await _enqueue_for(db_session, assessment, reviewer)
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
    assert sorted(j.status for j in await _review_jobs(db_session)) == ["processing"]
    await _assert_a_manual_generate_picks_it_up(db_session, assessment, reviewer)


async def test_unchanged_rows_are_still_stamped_and_no_warning_fires(
    db_session, monkeypatch, caplog
):
    """The conditional stamp must not be so strict that the ordinary case
    (nothing changed while the model ran) leaves rows unconsumed.

    The job is enqueued explicitly since F2 (2026-09-14) — ``submit_feedback``
    no longer does it — but nothing else about this test's shape changed, and
    the guarantee it pins (``consumed_at_predicates``) is untouched.
    """
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    r1 = await submit_feedback(
        db_session, assessment=assessment, reviewer=reviewer,
        score=3, comment="steady", feedback_mode="learn",
    )
    job = await _enqueue_for(db_session, assessment, reviewer)

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
    gone. The handler must analyze under the replacement id.

    The explicit enqueue (F2, 2026-09-14) must run BEFORE the re-point, so the
    job's payload still names the RETIRED id and the re-point has something to
    rewrite — which is the whole point of the test. ``consumed_at_predicates``
    is untouched; only the scheduler moved. See the module docstring.
    """
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
    job = await _enqueue_for(db_session, retired, reviewer)
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

"""The /reviews router: feedback submit/edit/delete, deny-by-default via
get_review_user (admin/manager/reviewer), with a narrower author-only check
on edit and an admin-only gate on delete. Same allowlist discipline as
tests/integration/test_manager_views.py's mutation-allowlist test.
"""

import uuid

import pytest
from sqlalchemy import select, text

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AssessmentReview,
    AssessmentReviewAssignment,
    AssessmentReviewEvent,
    Job,
    OpportunityAssessment,
    SimulationRun,
)
from src.routers import reviews as reviews_router
from src.services.assessment_reviews import _MAX_COMMENT_CHARS
from src.services.directory import ASSESSMENT_SORTS
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _seed_assessment(db) -> OpportunityAssessment:
    run = SimulationRun()
    db.add(run)
    await db.flush()
    a = OpportunityAssessment(simulation_run_id=run.id, agent_id="blackbird", channel_name="c")
    db.add(a)
    await db.flush()
    return a


async def _analysis_jobs(db) -> list[Job]:
    return list(
        (
            await db.execute(select(Job).where(Job.type == "review_feedback_analysis"))
        ).scalars()
    )


def test_the_reviews_router_posts_are_an_explicit_allowlist():
    """Same discipline as the manager router: a new write fails loudly.
    This set was EXTENDED by Task 5 (+3) and Task 12 (+1), and by F2
    (2026-09-14, +1) with ``/suggestions/generate`` -- the MANUAL replacement
    for the auto-enqueue ``submit_feedback``/``edit_feedback`` used to do as a
    side effect. Final size 8. It is listed here rather than left implicit
    because that one route is the only thing in the system that spends a
    70-90k-token Opus call on a human's say-so: a ninth path appearing
    without a review is exactly what this test exists to stop."""
    allowed = {
        "/assessments/{assessment_id}/feedback",
        "/feedback/{feedback_id}/edit",
        "/feedback/{feedback_id}/delete",
        "/assessments/{assessment_id}/status",
        "/assessments/{assessment_id}/assign",
        "/assessments/{assessment_id}/unassign",
        "/suggestions/{suggestion_id}/status",
        "/suggestions/generate",
    }
    methods = {m for r in reviews_router.router.routes for m in getattr(r, "methods", ())}
    assert methods == {"POST"}
    assert {r.path for r in reviews_router.router.routes} == allowed


async def test_learn_feedback_enqueues_nothing_until_a_manual_generate(
    client, db_session
):
    """F2 (2026-09-14): 'learn' feedback no longer buys a model call by itself.

    Two submissions used to produce exactly one deduped
    ``review_feedback_analysis`` job; they now produce NONE. The job arrives
    only when a staff member presses "Generate prompt suggestions", and one
    press covers both reviews of the assessment -- the job re-reads every
    unconsumed 'learn' row in one pass, so eligibility is per assessment, not
    per review.
    """
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)

    r1 = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "4", "comment": "Good idea", "feedback_mode": "learn"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r1.status_code == 302, r1.text

    r2 = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "5", "comment": "Even better", "feedback_mode": "learn"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r2.status_code == 302, r2.text

    rows = (
        (
            await db_session.execute(
                select(AssessmentReview).where(AssessmentReview.assessment_id == assessment.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert await _analysis_jobs(db_session) == []

    # Scoped to this assessment so the counts in the redirect are exact: an
    # unscoped press considers every eligible assessment in the database, and
    # `test_review_job_end_to_end` commits rows outside this test's savepoint.
    # The unscoped path has its own test below.
    generated = await client.post(
        "/reviews/suggestions/generate",
        data={"assessment_id": str(assessment.id)},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert generated.status_code == 302, generated.text
    assert generated.headers["location"] == (
        "/manager/prompt-suggestions?generated=1&eligible=1"
    )

    jobs = await _analysis_jobs(db_session)
    assert len(jobs) == 1
    assert jobs[0].status == "pending"
    assert jobs[0].payload == {"assessment_id": str(assessment.id)}
    # Attributed to whoever pressed the button, not to the reviewer.
    assert jobs[0].user_id == manager.id


async def test_two_pending_jobs_already_exist_dedupe_still_succeeds(client, db_session):
    """Regression for the final-review finding: ``enqueue_analysis_if_absent``'s
    dedupe SELECT used ``scalar_one_or_none()``, which raises
    ``MultipleResultsFound`` -- not a clean skip -- the moment more than one
    pending job already names this assessment, a state a race in
    at-least-once enqueueing could produce. Seed that state directly (rather
    than trying to win the race) and prove the write still 302s and the
    dedupe still skips.

    Driven through the MANUAL generate route since F2 (2026-09-14): a
    submission no longer touches the queue at all, so asserting on the job
    count after one would pass for the wrong reason -- it would be true for
    every feedback mode and for a broken dedupe alike. ``generated=0`` is
    what proves the skip happened; ``eligible=1`` is what proves the
    assessment was considered rather than filtered out earlier.
    """
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)
    db_session.add_all(
        [
            Job(
                type="review_feedback_analysis",
                status="pending",
                payload={"assessment_id": str(assessment.id)},
            )
            for _ in range(2)
        ]
    )
    await db_session.flush()

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "4", "comment": "Good idea", "feedback_mode": "learn"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text

    rows = (
        (
            await db_session.execute(
                select(AssessmentReview).where(AssessmentReview.assessment_id == assessment.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1

    generated = await client.post(
        "/reviews/suggestions/generate",
        data={"assessment_id": str(assessment.id)},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert generated.status_code == 302, generated.text
    assert generated.headers["location"] == (
        "/manager/prompt-suggestions?generated=0&eligible=1"
    )
    assert len(await _analysis_jobs(db_session)) == 2


async def test_log_only_feedback_enqueues_nothing(client, db_session):
    """'log_only' is the "record it, do not pay for a model call" mode.

    The submission never enqueues -- which, since F2 (2026-09-14), is true of
    every mode, so that half of the assertion no longer distinguishes
    anything. The part that still does is the second half: a MANUAL generate
    over an assessment whose only feedback is ``log_only`` finds nothing
    eligible (``eligible=0``) and enqueues nothing, so pressing the button
    cannot smuggle a ``log_only`` row into an Opus call.
    """
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "3", "comment": "meh", "feedback_mode": "log_only"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    jobs = (await db_session.execute(select(Job))).scalars().all()
    assert jobs == []

    generated = await client.post(
        "/reviews/suggestions/generate",
        data={"assessment_id": str(assessment.id)},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert generated.status_code == 302, generated.text
    assert generated.headers["location"] == (
        "/manager/prompt-suggestions?generated=0&eligible=0"
    )
    assert (await db_session.execute(select(Job))).scalars().all() == []


async def test_a_pi_is_refused_and_writes_nothing(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    assessment = await _seed_assessment(db_session)

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "3", "comment": "x", "feedback_mode": "learn"},
        headers=auth_headers(pi.id),
        follow_redirects=False,
    )
    assert r.status_code == 403
    rows = (await db_session.execute(select(AssessmentReview))).scalars().all()
    assert rows == []


@pytest.mark.parametrize("score", ["0", "6"])
async def test_score_out_of_range_is_a_400_and_writes_nothing(client, db_session, score):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": score, "comment": "x", "feedback_mode": "learn"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.status_code == 400
    rows = (await db_session.execute(select(AssessmentReview))).scalars().all()
    assert rows == []


async def test_bad_mode_is_a_400(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "3", "comment": "x", "feedback_mode": "bogus"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.status_code == 400
    rows = (await db_session.execute(select(AssessmentReview))).scalars().all()
    assert rows == []


async def test_missing_assessment_is_a_404(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)

    r = await client.post(
        f"/reviews/assessments/{uuid.uuid4()}/feedback",
        data={"score": "3", "comment": "x", "feedback_mode": "learn"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.status_code == 404


async def test_only_the_author_can_edit(client, db_session):
    author = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, name="Author")
    other = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER, name="Other")
    assessment = await _seed_assessment(db_session)
    review = AssessmentReview(
        assessment_id=assessment.id,
        reviewer_user_id=author.id,
        reviewer_name=author.name,
        score=3,
        feedback_mode="log_only",
    )
    db_session.add(review)
    await db_session.flush()

    r_other = await client.post(
        f"/reviews/feedback/{review.id}/edit",
        data={"score": "4", "comment": "nope", "feedback_mode": "log_only"},
        headers=auth_headers(other.id),
        follow_redirects=False,
    )
    assert r_other.status_code == 403

    r_author = await client.post(
        f"/reviews/feedback/{review.id}/edit",
        data={"score": "5", "comment": "revised", "feedback_mode": "learn"},
        headers=auth_headers(author.id),
        follow_redirects=False,
    )
    assert r_author.status_code == 302, r_author.text

    await db_session.refresh(review)
    assert review.edited is True
    assert review.consumed_at is None
    assert review.score == 5


async def test_editing_log_only_to_learn_makes_it_eligible_for_the_next_manual_generate(
    client, db_session
):
    """The "actually, learn from this one" path, asserted for its own sake.

    ``test_only_the_author_can_edit`` above happens to drive the same
    transition, but it is about authorship. This pins the behaviour itself.
    Since F2 (2026-09-14) the edit itself enqueues NOTHING -- ``edit_feedback``
    no longer writes a job, and nothing writes one on its behalf -- so what
    the flip buys is ELIGIBILITY: the next manual generate finds the row and
    enqueues exactly one ``review_feedback_analysis`` job naming that
    assessment. A regression here costs the reviewer the analysis they asked
    for just as silently as before; it is only the moment of payment that
    moved.
    """
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)

    submitted = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "3", "comment": "noting it, no need to learn", "feedback_mode": "log_only"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert submitted.status_code == 302, submitted.text
    assert (await db_session.execute(select(Job))).scalars().all() == []

    review = (await db_session.execute(select(AssessmentReview))).scalar_one()
    edited = await client.post(
        f"/reviews/feedback/{review.id}/edit",
        data={"score": "2", "comment": "on reflection, learn from this", "feedback_mode": "learn"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert edited.status_code == 302, edited.text
    assert (await db_session.execute(select(Job))).scalars().all() == []

    generated = await client.post(
        "/reviews/suggestions/generate",
        data={"assessment_id": str(assessment.id)},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert generated.status_code == 302, generated.text

    jobs = await _analysis_jobs(db_session)
    assert len(jobs) == 1
    assert jobs[0].status == "pending"
    assert jobs[0].payload == {"assessment_id": str(assessment.id)}

    await db_session.refresh(review)
    assert review.feedback_mode == "learn"
    assert review.consumed_at is None


async def test_an_overlong_comment_is_truncated_not_rejected(client, db_session):
    """A reviewer pasting a transcript still gets a saved row, just a clipped one.

    ``_MAX_COMMENT_CHARS`` is imported rather than hardcoded so this tracks the
    cap instead of restating it. The cap is silent — there is no error and no
    flash — so without this test a change to it (or its removal, which would push
    an unbounded comment into every subsequent bot payload) would go unnoticed.
    """
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    overlong = "x" * (_MAX_COMMENT_CHARS + 500) + "TAIL-THAT-MUST-NOT-SURVIVE"

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "4", "comment": overlong, "feedback_mode": "log_only"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text

    review = (await db_session.execute(select(AssessmentReview))).scalar_one()
    assert len(review.comment) == _MAX_COMMENT_CHARS
    assert review.comment == overlong[:_MAX_COMMENT_CHARS]
    assert "TAIL-THAT-MUST-NOT-SURVIVE" not in review.comment


async def test_only_an_admin_can_delete(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    assessment = await _seed_assessment(db_session)
    review = AssessmentReview(
        assessment_id=assessment.id, reviewer_name="R", score=3, feedback_mode="log_only",
    )
    db_session.add(review)
    await db_session.flush()
    review_id = review.id

    r_mgr = await client.post(
        f"/reviews/feedback/{review_id}/delete",
        headers=auth_headers(mgr.id),
        follow_redirects=False,
    )
    assert r_mgr.status_code == 403

    r_admin = await client.post(
        f"/reviews/feedback/{review_id}/delete",
        headers=auth_headers(admin.id),
        follow_redirects=False,
    )
    assert r_admin.status_code == 302, r_admin.text
    rows = (await db_session.execute(select(AssessmentReview))).scalars().all()
    assert rows == []


async def test_an_impersonating_admin_reviews_as_the_impersonated_user(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={mgr.id}"

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "3", "comment": "x", "feedback_mode": "log_only"},
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 302
    row = (await db_session.execute(select(AssessmentReview))).scalar_one()
    assert row.reviewer_user_id == mgr.id
    assert row.reviewer_name == mgr.name
    assert row.recorded_by_user_id == admin.id

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/status",
        data={"action": "approved"},
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 302
    ev = (await db_session.execute(select(AssessmentReviewEvent))).scalar_one()
    assert ev.actor_user_id == mgr.id and ev.recorded_by_user_id == admin.id


async def test_an_impersonating_admin_still_cannot_assign(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={mgr.id}"
    r = await client.post(
        f"/reviews/assessments/{assessment.id}/assign",
        data={"assignee_user_id": str(mgr.id)},
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 403


async def test_cross_site_post_is_refused(client_without_origin, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    r = await client_without_origin.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "3", "comment": "x", "feedback_mode": "log_only"},
        headers=auth_headers(reviewer.id),
    )
    assert r.status_code == 403
    assert "Cross-site request refused." in r.text


async def test_a_reviewer_posting_surface_admin_is_clamped_to_manager(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={
            "score": "3",
            "comment": "x",
            "feedback_mode": "log_only",
            "surface": "admin",
        },
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    assert r.headers["location"] == f"/manager/assessments/{assessment.id}"


async def test_reviewer_can_approve_and_history_appends(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    r1 = await client.post(
        f"/reviews/assessments/{assessment.id}/status",
        data={"action": "approved"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r1.status_code == 302, r1.text

    # The test harness runs each test inside one outer SAVEPOINT transaction,
    # so both POSTs below execute inside that SAME Postgres transaction and
    # `func.now()` -- transaction-start time, not statement time -- returns
    # the IDENTICAL instant for both events' `created_at`. The (created_at,
    # id) ordering the assertion below relies on then falls through to `id`,
    # a random uuid4 with no chronological meaning, making "latest" a coin
    # flip. Backdate the first event by one second with a raw UPDATE so the
    # ordering is deterministic regardless of the harness's transaction
    # semantics, then expire just that one object so the later SELECT
    # re-reads its backdated value instead of the ORM's cached one --
    # `expire_all()` would also expire `assessment`/`reviewer`, and the
    # sync attribute access on those below would then need an implicit
    # (async) refresh and raise `MissingGreenlet`.
    first_event = (
        (
            await db_session.execute(
                select(AssessmentReviewEvent).where(
                    AssessmentReviewEvent.assessment_id == assessment.id
                )
            )
        )
        .scalars()
        .one()
    )
    await db_session.execute(
        text(
            "UPDATE assessment_review_events "
            "SET created_at = created_at - interval '1 second' "
            "WHERE id = :id"
        ),
        {"id": first_event.id},
    )
    db_session.expire(first_event)

    r2 = await client.post(
        f"/reviews/assessments/{assessment.id}/status",
        data={"action": "disapproved"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r2.status_code == 302, r2.text

    events = (
        (
            await db_session.execute(
                select(AssessmentReviewEvent).where(
                    AssessmentReviewEvent.assessment_id == assessment.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 2
    assert all(e.actor_name == reviewer.name for e in events)
    latest = sorted(events, key=lambda e: (e.created_at, e.id))[-1]
    assert latest.action == "disapproved"


async def test_bad_action_is_400(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/status",
        data={"action": "bogus"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.status_code == 400
    events = (await db_session.execute(select(AssessmentReviewEvent))).scalars().all()
    assert events == []


async def test_status_on_missing_assessment_is_404(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)

    r = await client.post(
        f"/reviews/assessments/{uuid.uuid4()}/status",
        data={"action": "approved"},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.status_code == 404


async def test_reviewer_cannot_assign_but_manager_can(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assignee = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    r_reviewer = await client.post(
        f"/reviews/assessments/{assessment.id}/assign",
        data={"assignee_user_id": str(assignee.id)},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r_reviewer.status_code == 403

    r_manager = await client.post(
        f"/reviews/assessments/{assessment.id}/assign",
        data={"assignee_user_id": str(assignee.id)},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r_manager.status_code == 302, r_manager.text

    rows = (
        (
            await db_session.execute(
                select(AssessmentReviewAssignment).where(
                    AssessmentReviewAssignment.assessment_id == assessment.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].assignee_user_id == assignee.id
    assert rows[0].assigned_by_name == manager.name


async def test_assignment_is_idempotent(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assignee = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    for _ in range(2):
        r = await client.post(
            f"/reviews/assessments/{assessment.id}/assign",
            data={"assignee_user_id": str(assignee.id)},
            headers=auth_headers(manager.id),
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text

    rows = (
        (
            await db_session.execute(
                select(AssessmentReviewAssignment).where(
                    AssessmentReviewAssignment.assessment_id == assessment.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_unassign_removes_the_row(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assignee = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    r_assign = await client.post(
        f"/reviews/assessments/{assessment.id}/assign",
        data={"assignee_user_id": str(assignee.id)},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r_assign.status_code == 302, r_assign.text

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/unassign",
        data={"assignee_user_id": str(assignee.id)},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text

    rows = (
        (
            await db_session.execute(
                select(AssessmentReviewAssignment).where(
                    AssessmentReviewAssignment.assessment_id == assessment.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_assignee_must_be_review_capable_and_allowed(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    denied_reviewer = await factories.make_user(
        db_session, user_role=USER_ROLE_REVIEWER, access_status="denied"
    )
    assessment = await _seed_assessment(db_session)

    r_pi = await client.post(
        f"/reviews/assessments/{assessment.id}/assign",
        data={"assignee_user_id": str(pi.id)},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r_pi.status_code == 400

    r_denied = await client.post(
        f"/reviews/assessments/{assessment.id}/assign",
        data={"assignee_user_id": str(denied_reviewer.id)},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r_denied.status_code == 400

    rows = (await db_session.execute(select(AssessmentReviewAssignment))).scalars().all()
    assert rows == []


async def test_malformed_assignee_id_is_400(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/assign",
        data={"assignee_user_id": "not-a-uuid"},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.status_code == 400
    rows = (await db_session.execute(select(AssessmentReviewAssignment))).scalars().all()
    assert rows == []


async def test_a_manager_can_submit_feedback(client, db_session):
    """The manager half of the review gate. The reviewer half is covered by
    test_learn_feedback_enqueues_nothing_until_a_manual_generate above;
    nothing covered a manager, which is the role the 2026-09-09 "cannot add
    reviews" report was actually about."""
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)

    resp = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "3", "comment": "manager review", "feedback_mode": "log_only"},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert resp.status_code == 302, resp.text

    row = (
        await db_session.execute(
            select(AssessmentReview).where(
                AssessmentReview.assessment_id == assessment.id
            )
        )
    ).scalar_one()
    assert row.reviewer_user_id == manager.id
    assert row.score == 3


async def test_a_reviewer_posting_surface_admin_list_is_clamped_to_manager(
    client, db_session
):
    """The `-list` twin of test_a_reviewer_posting_surface_admin_is_clamped_to_manager.

    F1's two new surface tokens go through the same admin whitelist as the
    detail surfaces: `admin-list` reaches /admin only for an admin, and a
    reviewer posting it lands on the manager list page instead. The filters
    ride along either way, and the fragment is what scrolls the reader back to
    the row they just scored.
    """
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    run_id = str(assessment.simulation_run_id)

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={
            "score": "3",
            "comment": "x",
            "feedback_mode": "log_only",
            "surface": "admin-list",
            "run_id": run_id,
            "sort": ASSESSMENT_SORTS[1],
            "lab": "somelab",
        },
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    assert r.headers["location"] == (
        f"/manager/assessments?run_id={run_id}&sort={ASSESSMENT_SORTS[1]}"
        f"&lab=somelab#a-{assessment.id}"
    )


async def test_an_admin_posting_surface_admin_list_returns_to_the_admin_list(
    client, db_session
):
    """The happy path, plus the whole of F1's validation posture in one POST:
    an unknown `sort` and an unparsable `run_id` are DROPPED rather than 400ing
    or being echoed into the Location header, and a blank `lab` is absent.
    With nothing surviving there is no `?` at all — a bare `?` would be a
    second URL for the same page."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    assessment = await _seed_assessment(db_session)

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/status",
        data={
            "action": "approved",
            "surface": "admin-list",
            "run_id": "not-a-uuid",
            "sort": "no-such-sort\r\nX-Injected: 1",
            "lab": "",
        },
        headers=auth_headers(admin.id),
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    assert r.headers["location"] == f"/admin/assessments#a-{assessment.id}"


async def test_run_id_all_survives_and_a_lab_is_urlencoded(client, db_session):
    """`all` is the one non-UUID `run_id` the list page accepts, so it must
    survive; a lab carrying `&` must not be able to smuggle a second
    parameter, which is what `urlencode` (rather than f-string concatenation)
    guarantees."""
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={
            "score": "3",
            "comment": "x",
            "feedback_mode": "log_only",
            "surface": "manager-list",
            "run_id": "all",
            "lab": "a&sort=evil",
        },
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    assert r.headers["location"] == (
        f"/manager/assessments?run_id=all&lab=a%26sort%3Devil#a-{assessment.id}"
    )


async def test_an_unrecognised_surface_still_lands_on_the_manager_detail_page(
    client, db_session
):
    """A surface string is unvalidated form input; a bad one must land the
    reader somewhere real rather than 400. The filters are ignored, not
    appended to a detail path."""
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)

    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={
            "score": "3",
            "comment": "x",
            "feedback_mode": "log_only",
            "surface": "bogus-surface",
            "run_id": "all",
        },
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    assert r.headers["location"] == f"/manager/assessments/{assessment.id}"


async def test_a_reviewer_cannot_generate_prompt_suggestions(client, db_session):
    """`_STAFF`, not `_REVIEW`, deliberately: a reviewer cannot see
    /manager/prompt-suggestions (a suggestion can quote an unpublished PI
    disclosure verbatim), so a reviewer must not be able to create one
    either."""
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    db_session.add(
        AssessmentReview(
            assessment_id=assessment.id,
            reviewer_user_id=reviewer.id,
            reviewer_name=reviewer.name,
            score=3,
            feedback_mode="learn",
        )
    )
    await db_session.flush()

    r = await client.post(
        "/reviews/suggestions/generate",
        data={"assessment_id": str(assessment.id)},
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert await _analysis_jobs(db_session) == []


async def test_an_impersonating_admin_cannot_generate_prompt_suggestions(
    client, db_session
):
    """The review WRITES deliberately allow an impersonated session (operator
    decision 2026-09-10); this route does not, for the same reason
    assign/unassign refuse it — it spends real Opus calls."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    assessment = await _seed_assessment(db_session)
    db_session.add(
        AssessmentReview(
            assessment_id=assessment.id,
            reviewer_user_id=mgr.id,
            reviewer_name=mgr.name,
            score=3,
            feedback_mode="learn",
        )
    )
    await db_session.flush()
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={mgr.id}"

    r = await client.post(
        "/reviews/suggestions/generate",
        data={"assessment_id": str(assessment.id)},
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert await _analysis_jobs(db_session) == []


async def test_a_malformed_assessment_id_is_a_400_and_enqueues_nothing(
    client, db_session
):
    """Never widened into a database-wide batch of Opus calls by accident —
    the same posture as _parse_assignee_id."""
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)

    r = await client.post(
        "/reviews/suggestions/generate",
        data={"assessment_id": "not-a-uuid"},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert await _analysis_jobs(db_session) == []


async def test_an_unscoped_generate_covers_every_eligible_assessment(client, db_session):
    """The global button: no `assessment_id`, so every assessment with
    unconsumed 'learn' feedback gets a job. Asserted with `>=` on the counts
    and by-id on the jobs, because an unscoped press genuinely does consider
    rows this test did not create (test_review_job_end_to_end commits outside
    the savepoint)."""
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    first = await _seed_assessment(db_session)
    second = await _seed_assessment(db_session)
    for assessment in (first, second):
        db_session.add(
            AssessmentReview(
                assessment_id=assessment.id,
                reviewer_user_id=manager.id,
                reviewer_name=manager.name,
                score=4,
                feedback_mode="learn",
            )
        )
    await db_session.flush()

    r = await client.post(
        "/reviews/suggestions/generate",
        data={},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text

    payloads = {j.payload["assessment_id"] for j in await _analysis_jobs(db_session)}
    assert {str(first.id), str(second.id)} <= payloads


async def test_one_press_is_capped_and_a_second_press_drains_the_rest(
    client, db_session
):
    """2026-09-14 security finding. Each queued job is a single ~70-90k-token
    Opus call, and the SIZE of the eligible set is controlled by the
    least-privileged review role: a reviewer makes an assessment eligible by
    leaving `learn` feedback, and cannot see or press the button at all. So one
    privileged click must not be able to spend an unbounded amount. The cap is
    oldest-feedback-first so repeated presses drain the backlog instead of
    re-picking an arbitrary slice.
    """
    from src.services.assessment_reviews import MAX_ANALYSES_PER_PRESS

    staff = await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, email="cap-press@example.org"
    )
    run = await factories.make_simulation_run(db_session)
    total = MAX_ANALYSES_PER_PRESS + 3
    for i in range(total):
        assessment = OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
            channel_name="general", company_or_project=f"Capped {i}",
        )
        db_session.add(assessment)
        await db_session.flush()
        db_session.add(AssessmentReview(
            assessment_id=assessment.id, reviewer_name=f"R{i}", score=3,
            comment="c", feedback_mode="learn",
        ))
    await db_session.flush()

    first = await client.post(
        "/reviews/suggestions/generate", data={}, headers=auth_headers(staff.id),
        follow_redirects=False,
    )
    assert first.status_code == 302
    assert f"generated={MAX_ANALYSES_PER_PRESS}" in first.headers["location"]
    assert f"eligible={total}" in first.headers["location"], (
        "the eligible count must be the UNCAPPED total, so the page can say how "
        "much is left"
    )
    jobs = await _analysis_jobs(db_session)
    assert len(jobs) == MAX_ANALYSES_PER_PRESS

    # The first batch's jobs are still pending, so the dedupe skips them and the
    # second press picks up the remainder rather than re-queueing the same slice.
    second = await client.post(
        "/reviews/suggestions/generate", data={}, headers=auth_headers(staff.id),
        follow_redirects=False,
    )
    assert "generated=3" in second.headers["location"]
    assert len(await _analysis_jobs(db_session)) == total


async def test_the_review_tab_round_trips_through_a_feedback_write(client, db_session):
    """Spec §4/D13. A non-default tab survives the write, so scoring a card
    from the Reviewed tab returns the reader to the Reviewed tab."""
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={
            "score": "4", "comment": "ok", "feedback_mode": "log_only",
            "surface": "manager-list", "review": "reviewed",
        },
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    assert "review=reviewed" in r.headers["location"]


async def test_the_default_review_tab_is_dropped_from_the_redirect(client, db_session):
    """D13: only a non-default value is emitted, which is what keeps the three
    exact-Location assertions in this file byte-identical."""
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)
    r = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={
            "score": "4", "comment": "ok", "feedback_mode": "log_only",
            "surface": "manager-list", "review": "unreviewed",
        },
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/manager/assessments#a-{assessment.id}"

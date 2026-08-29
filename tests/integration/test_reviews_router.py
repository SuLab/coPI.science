"""The /reviews router: feedback submit/edit/delete, deny-by-default via
get_review_user (admin/manager/reviewer), with a narrower author-only check
on edit and an admin-only gate on delete. Same allowlist discipline as
tests/integration/test_manager_views.py's mutation-allowlist test.
"""

import uuid

import pytest
from sqlalchemy import select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    AssessmentReview,
    Job,
    OpportunityAssessment,
    SimulationRun,
)
from src.routers import reviews as reviews_router
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


def test_the_reviews_router_posts_are_an_explicit_allowlist():
    """Same discipline as the manager router: a new write fails loudly.
    This set is EXTENDED by Task 5 (+3) and Task 12 (+1); final size 7."""
    allowed = {
        "/assessments/{assessment_id}/feedback",
        "/feedback/{feedback_id}/edit",
        "/feedback/{feedback_id}/delete",
    }
    methods = {m for r in reviews_router.router.routes for m in getattr(r, "methods", ())}
    assert methods == {"POST"}
    assert {r.path for r in reviews_router.router.routes} == allowed


async def test_reviewer_can_submit_feedback_and_learn_enqueues_one_deduped_job(
    client, db_session
):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
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

    jobs = (
        (await db_session.execute(select(Job).where(Job.type == "review_feedback_analysis")))
        .scalars()
        .all()
    )
    assert len(jobs) == 1
    assert jobs[0].status == "pending"
    assert jobs[0].payload == {"assessment_id": str(assessment.id)}


async def test_log_only_feedback_enqueues_nothing(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
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

    jobs = (await db_session.execute(select(Job))).scalars().all()
    assert len(jobs) == 1


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


async def test_an_impersonating_admin_is_refused(client, db_session):
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

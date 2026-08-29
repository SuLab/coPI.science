"""The "Human review" card on the shared assessment-detail body (Task 6).

Fed by `build_assessment_detail`'s four new keys (`review_feedback`,
`review_status`, `review_status_history`, `review_assignments`, plus
`review_capable_users` gated on `viewer_is_staff`) and posting to the
`/reviews` routes Tasks 4/5 already wired up. These tests exercise the
rendered HTML on all three surfaces a human reviewer can reach it from:
/admin/assessments/{id}, /manager/assessments/{id} as a manager, and
/manager/assessments/{id} as a reviewer (get_review_user admits all three).

Rows are seeded with EXPLICIT `created_at` values throughout: Postgres
`now()` is transaction-start, so two rows written in the same test
transaction would otherwise tie and the (created_at, id) ordering the
service promises would not actually be exercised.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_REVIEWER,
    AssessmentReview,
    AssessmentReviewEvent,
)
from src.services import assessment_detail as assessment_detail_module
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_reviews_router import _seed_assessment

pytestmark = pytest.mark.integration

BASE_TIME = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="review-ui-admin@example.org"
    )


@pytest.fixture
async def manager(db_session):
    return await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, email="review-ui-manager@example.org"
    )


@pytest.fixture
async def reviewer(db_session):
    return await factories.make_user(
        db_session, user_role=USER_ROLE_REVIEWER, email="review-ui-reviewer@example.org"
    )


async def _seed_review_activity(db_session, assessment, *, feedback_user, status_actor):
    """One 'learn' review, one 'log_only' review, and one 'approved' status
    event, each with a distinct explicit `created_at`."""
    learn = AssessmentReview(
        assessment_id=assessment.id,
        reviewer_user_id=feedback_user.id,
        reviewer_name=feedback_user.name,
        score=4,
        comment="LEARN-COMMENT-MARKER",
        feedback_mode="learn",
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )
    log_only = AssessmentReview(
        assessment_id=assessment.id,
        reviewer_user_id=feedback_user.id,
        reviewer_name=feedback_user.name,
        score=2,
        comment="LOG-ONLY-COMMENT-MARKER",
        feedback_mode="log_only",
        created_at=BASE_TIME + timedelta(minutes=1),
        updated_at=BASE_TIME + timedelta(minutes=1),
    )
    db_session.add_all([learn, log_only])
    event = AssessmentReviewEvent(
        assessment_id=assessment.id,
        action="approved",
        actor_user_id=status_actor.id,
        actor_name=status_actor.name,
        created_at=BASE_TIME + timedelta(minutes=2),
    )
    db_session.add(event)
    await db_session.flush()
    return learn, log_only, event


async def test_feedback_and_status_render_on_all_three_surfaces(
    client, db_session, admin, manager, reviewer
):
    assessment = await _seed_assessment(db_session)
    await _seed_review_activity(db_session, assessment, feedback_user=reviewer, status_actor=admin)

    for path, user in (
        (f"/admin/assessments/{assessment.id}", admin),
        (f"/manager/assessments/{assessment.id}", manager),
        (f"/manager/assessments/{assessment.id}", reviewer),
    ):
        resp = await client.get(path, headers=auth_headers(user.id))
        assert resp.status_code == 200, f"{path} as {user.user_role}: {resp.text}"
        html = resp.text
        assert "Human review" in html
        assert reviewer.name in html
        assert "4/5" in html
        assert "Learn" in html
        assert "Don't learn — log only" in html
        assert "Approved" in html
        assert admin.name in html
        assert "LEARN-COMMENT-MARKER" in html
        assert "LOG-ONLY-COMMENT-MARKER" in html


async def test_comment_is_escaped_not_rendered(client, db_session, admin, reviewer):
    assessment = await _seed_assessment(db_session)
    db_session.add(
        AssessmentReview(
            assessment_id=assessment.id,
            reviewer_user_id=reviewer.id,
            reviewer_name=reviewer.name,
            score=3,
            comment="<script>x()</script> **bold**",
            feedback_mode="log_only",
        )
    )
    await db_session.flush()

    resp = await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )
    assert resp.status_code == 200
    html = resp.text
    assert "<script>x()</script>" not in html
    assert "&lt;script&gt;x()&lt;/script&gt;" in html
    # Not converted to markdown — the literal markers survive untouched.
    assert "**bold**" in html
    assert "data-markdown" not in html


async def test_the_forms_post_to_literal_review_paths(
    client, db_session, admin, manager, reviewer
):
    assessment = await _seed_assessment(db_session)

    admin_html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    manager_html = (
        await client.get(
            f"/manager/assessments/{assessment.id}", headers=auth_headers(manager.id)
        )
    ).text
    reviewer_html = (
        await client.get(
            f"/manager/assessments/{assessment.id}", headers=auth_headers(reviewer.id)
        )
    ).text

    for html in (admin_html, manager_html, reviewer_html):
        assert 'action="/reviews/assessments/' in html
        assert f"/reviews/assessments/{assessment.id}/feedback" in html
        assert f"/reviews/assessments/{assessment.id}/status" in html
        assert 'method="post"' in html.lower()

    # Assign only for staff surfaces, never for a reviewer.
    assert f"/reviews/assessments/{assessment.id}/assign" in admin_html
    assert f"/reviews/assessments/{assessment.id}/assign" in manager_html
    assert f"/reviews/assessments/{assessment.id}/assign" not in reviewer_html


async def test_edit_form_renders_only_for_the_author(client, db_session, admin, reviewer):
    assessment = await _seed_assessment(db_session)
    other = await factories.make_user(
        db_session, user_role=USER_ROLE_REVIEWER, email="review-ui-other@example.org"
    )
    review = AssessmentReview(
        assessment_id=assessment.id,
        reviewer_user_id=reviewer.id,
        reviewer_name=reviewer.name,
        score=3,
        feedback_mode="log_only",
    )
    db_session.add(review)
    await db_session.flush()

    author_html = (
        await client.get(
            f"/manager/assessments/{assessment.id}", headers=auth_headers(reviewer.id)
        )
    ).text
    assert f"/reviews/feedback/{review.id}/edit" in author_html

    other_html = (
        await client.get(
            f"/manager/assessments/{assessment.id}", headers=auth_headers(other.id)
        )
    ).text
    assert f"/reviews/feedback/{review.id}/edit" not in other_html

    admin_html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert f"/reviews/feedback/{review.id}/edit" not in admin_html


async def test_delete_button_renders_only_for_admin(
    client, db_session, admin, manager, reviewer
):
    assessment = await _seed_assessment(db_session)
    review = AssessmentReview(
        assessment_id=assessment.id,
        reviewer_user_id=reviewer.id,
        reviewer_name=reviewer.name,
        score=3,
        feedback_mode="log_only",
    )
    db_session.add(review)
    await db_session.flush()

    admin_html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert f"/reviews/feedback/{review.id}/delete" in admin_html

    manager_html = (
        await client.get(
            f"/manager/assessments/{assessment.id}", headers=auth_headers(manager.id)
        )
    ).text
    assert f"/reviews/feedback/{review.id}/delete" not in manager_html

    reviewer_html = (
        await client.get(
            f"/manager/assessments/{assessment.id}", headers=auth_headers(reviewer.id)
        )
    ).text
    assert f"/reviews/feedback/{review.id}/delete" not in reviewer_html


async def test_impersonating_admin_sees_read_only_card(client, db_session, admin, manager):
    assessment = await _seed_assessment(db_session)
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={manager.id}"

    resp = await client.get(f"/manager/assessments/{assessment.id}", headers=headers)
    assert resp.status_code == 200
    html = resp.text
    assert "Human review" in html
    assert 'action="/reviews/assessments/' not in html
    assert 'action="/reviews/feedback/' not in html


async def test_unknown_status_action_and_mode_render_alarming(
    client, db_session, admin, monkeypatch
):
    """The DB CHECKs on `assessment_reviews.feedback_mode` and
    `assessment_review_events.action` make an out-of-vocabulary value
    unstorable — so this drives the real render path with stub rows the
    database itself would refuse, the pragmatic way to exercise a branch the
    schema is supposed to make unreachable (same idea as
    test_an_unknown_panel_state_never_renders_green's monkeypatch of
    `panel_state`, one level up: here the whole detail-builder is wrapped so
    its OWN correctly-computed context is used for everything except the two
    fields under test).
    """
    assessment = await _seed_assessment(db_session)
    original = assessment_detail_module.build_assessment_detail

    async def _patched(db, assessment_id, *, admin_view, viewer_is_staff=False):
        detail = await original(
            db, assessment_id, admin_view=admin_view, viewer_is_staff=viewer_is_staff
        )
        if detail is not None:
            now = datetime.now(UTC)
            stub_review = SimpleNamespace(
                id=uuid.uuid4(),
                reviewer_user_id=None,
                reviewer_name="Stub Reviewer",
                score=3,
                comment="stub comment",
                feedback_mode="maybe",
                edited=False,
                created_at=now,
            )
            stub_event = SimpleNamespace(action="frobbed", actor_name="Stub Actor", created_at=now)
            detail["review_feedback"] = [stub_review]
            detail["review_status"] = stub_event
            detail["review_status_history"] = [stub_event]
        return detail

    monkeypatch.setattr("src.routers.admin.build_assessment_detail", _patched)

    resp = await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )
    assert resp.status_code == 200
    html = resp.text
    assert "Unknown mode: maybe" in html
    assert "Unknown status action: frobbed" in html

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

import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from markupsafe import escape

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


async def test_card_explains_the_score_rates_the_proposal(client, db_session, admin):
    assessment = await _seed_assessment(db_session)
    resp = await client.get(
        f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
    )
    assert resp.status_code == 200
    html = resp.text
    assert "rate the proposal" in html.lower()
    assert "Overall rating" in html


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
        # The approve/disapprove/clear form was removed from the detail page
        # on operator request 2026-09-21 (spec §3): no surface posts to
        # /status any more, though the handler and the history render stay.
        assert f"/reviews/assessments/{assessment.id}/status" not in html
        # The BUTTONS, not just the action path: a status control re-added
        # with a different action would pass the assertion above.
        assert 'name="action"' not in html
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


async def test_impersonating_admin_sees_write_forms_and_the_reviewing_as_notice(
    client, db_session, admin, manager
):
    """F4 (2026-09-10) reverses F6: impersonated review writes are now
    allowed, attributed to the impersonated user, with the admin recorded as
    `recorded_by_user_id`. The notice stays, reworded to say so rather than
    to explain an absence. Assign/unassign remain refused regardless."""
    assessment = await _seed_assessment(db_session)
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={manager.id}"

    resp = await client.get(f"/manager/assessments/{assessment.id}", headers=headers)
    assert resp.status_code == 200
    html = resp.text
    assert "Human review" in html
    assert f'action="/reviews/assessments/{assessment.id}/feedback"' in html
    assert f"/reviews/assessments/{assessment.id}/assign" not in html
    assert "You are reviewing as" in html


async def test_a_real_manager_sees_no_impersonation_notice(client, db_session, manager):
    """Control. The notice must appear ONLY under impersonation — a manager
    signed in as themselves has the full write half and needs no explanation."""
    assessment = await _seed_assessment(db_session)
    resp = await client.get(
        f"/manager/assessments/{assessment.id}", headers=auth_headers(manager.id)
    )
    assert resp.status_code == 200
    assert "review-impersonation-notice" not in resp.text
    assert 'action="/reviews/assessments/' in resp.text


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


async def test_the_add_form_renders_one_select_per_live_rubric_dimension(
    client, db_session, admin
):
    """Rendered from prompts/rubric/blackbird-rubric.toml, not from template
    literals: the form and the validator that stamps the score must read one
    document (design §2.3)."""
    from src.services.blackbird_rubric import load_rubric

    rubric = load_rubric()
    assessment = await _seed_assessment(db_session)

    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text

    assert "review-rubric-instructions" in html
    assert rubric.version in html
    for dim in rubric.dimensions:
        assert f'name="dim_{dim.key}"' in html, dim.key
        # Several dimension titles carry a literal "&" (e.g. "Differentiation
        # & unmet need"), which Jinja's autoescaping renders as "&amp;" — so
        # the raw title never appears verbatim in the HTML. Compare against
        # what autoescaping actually produces, not the source string.
        assert str(escape(dim.title)) in html, dim.key


async def test_the_form_shows_the_bots_own_score_beside_each_dimension(
    client, db_session, admin
):
    """A9, accepted with the anchoring cost recorded: disagreement has to be
    visible at the point of scoring. It must read as the BOT's claim, never as
    a pre-filled default — so the human's own select stays empty."""
    from src.services.blackbird_rubric import load_rubric

    first = load_rubric().dimensions[0]
    assessment = await _seed_assessment(db_session)
    assessment.scores = {first.key: 4}
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text

    # Whitespace-normalized: the literal "BlackbirdBot scored" text and the
    # interpolated score sit on one unwrapped template source line today
    # (the `review-bot-score` span), but nothing pins them there — a harmless
    # reflow of that line's attributes would otherwise break this assertion
    # for a reason that has nothing to do with the behavior under test.
    normalized = " ".join(html.split())
    assert "BlackbirdBot scored 4" in normalized
    # The human's select for that dimension must have nothing selected.
    assert f'name="dim_{first.key}"' in html
    assert 'value="4" selected' not in normalized


async def test_a_stored_review_renders_its_dimension_scores(
    client, db_session, admin, reviewer
):
    from src.services.blackbird_rubric import (
        RUBRIC_CONTENT_HASH,
        RUBRIC_VERSION,
        load_rubric,
    )

    first = load_rubric().dimensions[0]
    assessment = await _seed_assessment(db_session)
    db_session.add(
        AssessmentReview(
            assessment_id=assessment.id,
            reviewer_user_id=reviewer.id,
            reviewer_name=reviewer.name,
            score=4,
            comment="",
            feedback_mode="log_only",
            dimension_scores={first.key: 2},
            rubric_version=RUBRIC_VERSION,
            rubric_content_hash=RUBRIC_CONTENT_HASH,
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
    )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert "review-dimension-list" in html
    # See the escaping note above: the title carries a literal "&".
    assert str(escape(first.title)) in html


async def test_recorded_by_renders_the_impersonation_note_only_when_set(
    client, db_session, admin, reviewer
):
    """A review written while impersonating (`recorded_by_user_id` set) shows
    the "(entered by <admin> while impersonating)" note — NAMING the admin,
    since "an admin" alone is not an attribution anyone can act on — beside
    the reviewer's name; an ordinary review does not."""
    assessment = await _seed_assessment(db_session)
    db_session.add_all(
        [
            AssessmentReview(
                assessment_id=assessment.id,
                reviewer_user_id=reviewer.id,
                reviewer_name=reviewer.name,
                score=3,
                comment="via impersonation",
                feedback_mode="log_only",
                dimension_scores={},
                recorded_by_user_id=admin.id,
                created_at=BASE_TIME,
                updated_at=BASE_TIME,
            ),
            AssessmentReview(
                assessment_id=assessment.id,
                reviewer_user_id=admin.id,
                reviewer_name=admin.name,
                score=4,
                comment="in person",
                feedback_mode="log_only",
                dimension_scores={},
                created_at=BASE_TIME,
                updated_at=BASE_TIME,
            ),
        ]
    )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    marker = f"(entered by {admin.name} while impersonating)"
    assert html.count(marker) == 1
    rows = html.split("review-feedback-row")
    impersonated_row = next(r for r in rows if "via impersonation" in r)
    in_person_row = next(r for r in rows if "in person" in r)
    assert marker in impersonated_row
    assert "while impersonating" not in in_person_row


async def test_a_review_stamped_with_an_unknown_revision_shows_raw_keys(
    client, db_session, admin, reviewer
):
    """A13. Rubric v3.0.0 replaced thirteen dual-scale dimensions with six
    single-scale ones, so a key means nothing outside its own revision.
    Titling an unresolvable stamp from today's document would invent a claim;
    the keys render as stored instead, with a warning."""
    assessment = await _seed_assessment(db_session)
    db_session.add(
        AssessmentReview(
            assessment_id=assessment.id,
            reviewer_user_id=reviewer.id,
            reviewer_name=reviewer.name,
            score=3,
            comment="",
            feedback_mode="log_only",
            dimension_scores={"some_retired_dimension": 5},
            # `rubric_version` is `String(20)` (migration 0043) — this literal
            # is shortened from `"1.0.0-not-in-the-registry"`
            # (25 chars, would raise StringDataRightTruncationError) while
            # keeping the same property under test: a stamp matching no
            # registry entry (there IS a bare "1.0.0" in
            # prompts/rubric/revisions.toml, so the "-not-known" suffix is
            # load-bearing, not decorative).
            rubric_version="1.0.0-not-known",
            rubric_content_hash="ffffffffffff",
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
    )
    await db_session.flush()

    html = (
        await client.get(
            f"/admin/assessments/{assessment.id}", headers=auth_headers(admin.id)
        )
    ).text
    assert "some_retired_dimension" in html
    # Whitespace-normalized: this phrase is rendered prose from the warning
    # paragraph's literal template text, currently unwrapped onto one source
    # line — the same reflow risk as test_manager_views.py:553's pattern.
    assert "unrecognized rubric revision" in " ".join(html.split())


async def test_the_form_renders_for_a_reviewer_on_the_manager_surface(
    client, db_session, reviewer
):
    """The whole point of the feature: a reviewer, signed in as themselves,
    gets the scoring form on the only assessment surface they can reach."""
    from src.services.blackbird_rubric import load_rubric

    first = load_rubric().dimensions[0]
    assessment = await _seed_assessment(db_session)

    html = (
        await client.get(
            f"/manager/assessments/{assessment.id}", headers=auth_headers(reviewer.id)
        )
    ).text
    assert f'name="dim_{first.key}"' in html
    assert "review-rubric-instructions" in html


async def test_every_select_on_the_detail_page_has_an_external_label(
    client, db_session, manager
):
    assessment = await _seed_assessment(db_session)
    html = (
        await client.get(
            f"/manager/assessments/{assessment.id}", headers=auth_headers(manager.id)
        )
    ).text
    assert "Proposal merit" not in html
    assert "Overall rating (1 = weak … 5 = strong)" in html
    select_ids = re.findall(r'<select[^>]*\bid="([^"]+)"', html)
    selects_total = len(re.findall(r"<select\b", html))
    assert selects_total == len(select_ids), "every <select> must carry an id"
    for sid in select_ids:
        assert f'for="{sid}"' in html, f"no <label for> for select #{sid}"
    # The score select keeps a blank first option so an untouched form cannot
    # post score=1 (reviews.py declares score: int = Form(...)).
    m = re.search(r'<select[^>]*id="add-score"[^>]*>\s*<option value=""', html)
    assert m, "score select lost its blank first option"

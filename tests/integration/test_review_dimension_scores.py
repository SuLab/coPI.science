"""Per-rubric-dimension human scores on a review (design §2.2).

Optional and sparse by decision N3: a reviewer may score any subset of the six
dimensions, or none. The required overall "proposal merit" score is unchanged.
"""

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_REVIEWER, AssessmentReview
from src.services import assessment_reviews as assessment_reviews_module
from src.services.assessment_reviews import edit_feedback, submit_feedback
from src.services.blackbird_rubric import (
    RUBRIC_CONTENT_HASH,
    RUBRIC_VERSION,
    load_rubric,
)
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_reviews_router import _seed_assessment

pytestmark = pytest.mark.integration


def _first_two_dimension_keys() -> tuple[str, str]:
    """Read the keys from the document rather than hard-coding them: the six
    keys are a rubric-version fact, and a test that pins them here would fail
    for the wrong reason on the next consolidation."""
    dims = load_rubric().dimensions
    return dims[0].key, dims[1].key


async def test_a_sparse_dimension_set_is_stored_and_stamped(client, db_session):
    key_a, key_b = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    review = await submit_feedback(
        db_session,
        assessment=assessment,
        reviewer=reviewer,
        score=4,
        comment="",
        feedback_mode="log_only",
        dimension_scores={key_a: 5, key_b: 2},
    )
    await db_session.flush()

    assert review.dimension_scores == {key_a: 5, key_b: 2}
    assert review.rubric_version == RUBRIC_VERSION
    assert review.rubric_content_hash == RUBRIC_CONTENT_HASH
    assert review.score == 4  # the overall merit score is untouched


async def test_no_dimension_scores_stores_sql_null_not_an_empty_dict(client, db_session):
    """`{}` and None are one state. Storing both would give the column two
    encodings of absence, which is the JSONB defect 0031 and 0036 each fixed."""
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    review = await submit_feedback(
        db_session, assessment=assessment, reviewer=reviewer, score=3,
        comment="", feedback_mode="log_only", dimension_scores={},
    )
    await db_session.flush()
    assert review.dimension_scores is None

    found = (
        await db_session.execute(
            select(AssessmentReview.id).where(
                AssessmentReview.id == review.id,
                AssessmentReview.dimension_scores.is_(None),
            )
        )
    ).scalar_one_or_none()
    assert found == review.id


async def test_an_unknown_dimension_key_is_rejected(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    with pytest.raises(ValueError, match="unknown rubric dimension"):
        await submit_feedback(
            db_session, assessment=assessment, reviewer=reviewer, score=3,
            comment="", feedback_mode="log_only",
            dimension_scores={"funnel_stage_vibes": 4},
        )


def _out_of_scale_values() -> list[int]:
    """Values just outside, and far outside, the LIVE document's scale —
    never today's 1-5 literals. Same rationale as `_first_two_dimension_keys`:
    if the scale ever widens, this test must keep testing "outside the scale",
    not silently start asserting on values the new scale actually accepts."""
    rubric = load_rubric()
    return [
        rubric.scale_min - 1,
        rubric.scale_max + 1,
        rubric.scale_min - 100,
        rubric.scale_max + 100,
    ]


@pytest.mark.parametrize("bad", _out_of_scale_values())
async def test_an_out_of_scale_dimension_score_is_rejected(client, db_session, bad):
    key_a, _ = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    with pytest.raises(ValueError, match="must be between"):
        await submit_feedback(
            db_session, assessment=assessment, reviewer=reviewer, score=3,
            comment="", feedback_mode="log_only", dimension_scores={key_a: bad},
        )


async def test_a_boolean_is_not_an_integer_score(client, db_session):
    """`bool` subclasses `int`, so `True` passes a naive 1 <= v <= 5. A form
    that ever posts a checkbox into one of these fields must fail, not record
    a 1 nobody chose."""
    key_a, _ = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    with pytest.raises(ValueError, match="must be an integer"):
        await submit_feedback(
            db_session, assessment=assessment, reviewer=reviewer, score=3,
            comment="", feedback_mode="log_only", dimension_scores={key_a: True},
        )


async def test_editing_replaces_the_dimension_set(client, db_session):
    """An edit is a re-entry of the whole set, not a merge: a dimension the
    reviewer cleared must actually clear."""
    key_a, key_b = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    review = await submit_feedback(
        db_session, assessment=assessment, reviewer=reviewer, score=4,
        comment="", feedback_mode="log_only",
        dimension_scores={key_a: 5, key_b: 2},
    )
    await db_session.flush()

    await edit_feedback(
        db_session, review=review, score=4, comment="", feedback_mode="log_only",
        dimension_scores={key_b: 3},
    )
    await db_session.flush()

    assert review.dimension_scores == {key_b: 3}
    assert review.edited is True


async def test_editing_restamps_to_the_rubric_live_at_edit_time(
    client, db_session, monkeypatch
):
    """The stamp is REWRITTEN on every edit, not carried over from the
    original submission. `RUBRIC_VERSION`/`RUBRIC_CONTENT_HASH` are frozen
    module-level constants in the running process, so asserting the row still
    equals them after an edit would pass even if `edit_feedback` never
    touched the stamp at all — the submit call already wrote the same value.
    Proving the re-stamp actually happens means making the document's
    identity move BETWEEN the two calls, here via monkeypatching the two
    names `assessment_reviews` reads them through, and checking the edited
    row picked up the new ones rather than keeping what `submit_feedback`
    wrote."""
    key_a, _ = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    review = await submit_feedback(
        db_session, assessment=assessment, reviewer=reviewer, score=4,
        comment="", feedback_mode="log_only", dimension_scores={key_a: 5},
    )
    await db_session.flush()
    assert review.rubric_version == RUBRIC_VERSION
    assert review.rubric_content_hash == RUBRIC_CONTENT_HASH

    monkeypatch.setattr(assessment_reviews_module, "RUBRIC_VERSION", "9.9.9-sentinel")
    monkeypatch.setattr(
        assessment_reviews_module, "RUBRIC_CONTENT_HASH", "sentinelhash01"
    )

    await edit_feedback(
        db_session, review=review, score=4, comment="", feedback_mode="log_only",
        dimension_scores={key_a: 5},
    )
    await db_session.flush()

    assert review.rubric_version == "9.9.9-sentinel"
    assert review.rubric_content_hash == "sentinelhash01"


async def test_the_form_posts_dimension_scores_and_blanks_are_dropped(
    client, db_session
):
    """An unfilled select posts an empty string. It must be DROPPED, never
    coerced to 0 — the rubric scale starts at 1, so a stored 0 would be a
    score nobody gave (and would drag any future average)."""
    key_a, key_b = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    resp = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={
            "score": "4",
            "comment": "c",
            "feedback_mode": "log_only",
            f"dim_{key_a}": "5",
            f"dim_{key_b}": "",
        },
        headers=auth_headers(reviewer.id),
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
    assert row.dimension_scores == {key_a: 5}


async def test_posting_no_dimension_fields_at_all_still_works(client, db_session):
    """Backwards compatibility with the pre-0043 form shape, and with any
    script that posts only the overall score."""
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    resp = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={"score": "4", "comment": "c", "feedback_mode": "log_only"},
        headers=auth_headers(reviewer.id),
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
    assert row.dimension_scores is None


async def test_an_unknown_dimension_field_is_a_400_and_writes_nothing(
    client, db_session
):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    resp = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={
            "score": "4", "comment": "c", "feedback_mode": "log_only",
            "dim_not_a_real_dimension": "3",
        },
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert resp.status_code == 400
    rows = (
        await db_session.execute(
            select(AssessmentReview).where(
                AssessmentReview.assessment_id == assessment.id
            )
        )
    ).scalars().all()
    assert rows == []


async def test_a_non_numeric_dimension_field_is_a_400_and_writes_nothing(
    client, db_session
):
    key_a, _ = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    resp = await client.post(
        f"/reviews/assessments/{assessment.id}/feedback",
        data={
            "score": "4", "comment": "c", "feedback_mode": "log_only",
            f"dim_{key_a}": "excellent",
        },
        headers=auth_headers(reviewer.id),
        follow_redirects=False,
    )
    assert resp.status_code == 400
    rows = (
        await db_session.execute(
            select(AssessmentReview).where(
                AssessmentReview.assessment_id == assessment.id
            )
        )
    ).scalars().all()
    assert rows == []


async def test_a_dimension_only_edit_survives_an_in_flight_analysis_job(
    client, db_session
):
    """A1. `edit_feedback` resets consumed_at, but the in-flight job's
    conditional UPDATE used to match on (score, comment) alone — so an edit
    that touched ONLY the dimension scores got re-stamped consumed, and the
    next analysis pass then found nothing to analyze. Silent: the stamp
    SUCCEEDED, so the job's own `stamped != len(reviews)` warning never fired.

    Simulated by snapshotting the row, editing it, then running the UPDATE the
    handler runs — the same shape as the handler, without an Opus round trip.

    This test is unaffected by F2 (2026-09-14) and passes unchanged: it builds
    the UPDATE by hand and never reads the job queue. Only the narration above
    needed correcting — `edit_feedback` no longer enqueues a replacement job
    (nothing does, until a human presses "Generate suggestions from current
    reviews"), and
    the guarantee this pins, `consumed_at_predicates`, is exactly what F2 did
    NOT touch: leaving the row unconsumed is what keeps it eligible for that
    next pass, whoever schedules it.
    """
    from sqlalchemy import update

    from src.models import AssessmentReview as AR
    from src.services.review_bot import _feedback_snapshot_entry

    key_a, key_b = _first_two_dimension_keys()
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    assessment = await _seed_assessment(db_session)

    review = await submit_feedback(
        db_session, assessment=assessment, reviewer=reviewer, score=4,
        comment="unchanged", feedback_mode="learn",
        dimension_scores={key_a: 5},
    )
    await db_session.flush()

    # The job snapshots the row, then goes off to the model.
    snap = _feedback_snapshot_entry(review)

    # Meanwhile the reviewer changes ONLY the dimension scores.
    await edit_feedback(
        db_session, review=review, score=4, comment="unchanged",
        feedback_mode="learn", dimension_scores={key_a: 5, key_b: 1},
    )
    await db_session.flush()

    # The job comes back and tries to stamp what it snapshotted.
    from src.services.review_bot import consumed_at_predicates

    result = await db_session.execute(
        update(AR)
        .where(AR.id == review.id, *consumed_at_predicates(snap))
        .values(consumed_at=review.created_at)
    )
    assert result.rowcount == 0, (
        "the in-flight job re-stamped a row whose dimension scores had changed"
    )
    await db_session.refresh(review)
    assert review.consumed_at is None

    # CONTROL, so the assertion above cannot pass for the wrong reason: the
    # same predicate against an UNCHANGED snapshot must still match. Without
    # it, a `consumed_at_predicates` that matched NOTHING at all would look
    # exactly like a pass.
    fresh = _feedback_snapshot_entry(review)
    ok = await db_session.execute(
        update(AR).where(AR.id == review.id, *consumed_at_predicates(fresh))
        .values(consumed_at=review.created_at)
    )
    assert ok.rowcount == 1

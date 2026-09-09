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
from tests.integration.test_manager_access import auth_headers  # noqa: F401
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

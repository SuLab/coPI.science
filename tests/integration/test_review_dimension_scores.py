"""Per-rubric-dimension human scores on a review (design §2.2).

Optional and sparse by decision N3: a reviewer may score any subset of the six
dimensions, or none. The required overall "proposal merit" score is unchanged.
"""

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_REVIEWER, AssessmentReview
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


@pytest.mark.parametrize("bad", [0, 6, -1, 99])
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


async def test_editing_replaces_the_dimension_set_and_restamps(client, db_session):
    """An edit is a re-entry of the whole set, not a merge: a dimension the
    reviewer cleared must actually clear. The stamp moves with it, because the
    scores being stored are the ones given under the CURRENT document."""
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
    assert review.rubric_version == RUBRIC_VERSION
    assert review.edited is True

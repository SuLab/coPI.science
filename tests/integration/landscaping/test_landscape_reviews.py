"""Landscaping decisions, reviews, status events, assignments and expert judgments through the
``/reviews`` routes (spec §13.3, §10.6; L39, L55, D21, D50, D63).

Opportunities, assessments and batches are written directly; every name is invented.
"""
import json
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from src.landscaping import epochs
from src.landscaping.criteria import load_criteria
from src.landscaping.review_batches import EXPERTS_KEY
from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_REVIEWER,
    AppSetting,
    Job,
    PromptChangeSuggestion,
)
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeDecision,
    LandscapeEpoch,
    LandscapeExpertJudgment,
    LandscapeOpportunity,
    LandscapeOpportunityVersion,
    LandscapeReview,
    LandscapeReviewAssignment,
    LandscapeReviewBatch,
    LandscapeReviewBatchItem,
    LandscapeReviewEvent,
)
from src.services import landscape_view
from src.services.blackbird_rubric import RUBRIC_VERSION, load_rubric
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_issued_week = iter(range(1, 400))


async def _epoch(db) -> LandscapeEpoch:
    epoch = await epochs.active_epoch(db)
    if epoch is None:
        epoch = LandscapeEpoch(status="active")
        db.add(epoch)
        await db.flush()
    return epoch


async def _opportunity(db, *, issued: bool = True) -> tuple[uuid.UUID, uuid.UUID]:
    """An opportunity with a completed assessment in the active epoch, issued in its own weekly
    batch unless ``issued`` is false; returns ``(opportunity_id, assessment_id)``."""
    epoch = await _epoch(db)
    opp = LandscapeOpportunity(version_counter=1)
    db.add(opp)
    await db.flush()
    db.add(LandscapeOpportunityVersion(
        opportunity_id=opp.id, version=1, epoch_id=epoch.id, member_idea_keys=["idea-a"],
        label="Invented opportunity", status="done",
    ))
    assessment = LandscapeAssessment(
        opportunity_id=opp.id, opportunity_version=1, epoch_id=epoch.id, status="done",
        headline="An invented headline",
    )
    db.add(assessment)
    await db.flush()
    if issued:
        batch = LandscapeReviewBatch(issued_on=date.fromordinal(739000 + 7 * next(_issued_week)))
        db.add(batch)
        await db.flush()
        db.add(LandscapeReviewBatchItem(batch_id=batch.id, opportunity_id=opp.id))
        await db.flush()
    return opp.id, assessment.id


async def _decisions(db) -> list[LandscapeDecision]:
    return list((await db.execute(select(LandscapeDecision))).scalars())


async def _reviews(db) -> list[LandscapeReview]:
    return list((await db.execute(select(LandscapeReview))).scalars())


async def test_decision_pursue_pass_only(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    opp_id, assessment_id = await _opportunity(db_session)
    headers = auth_headers(manager.id)

    r = await client.post(
        f"/reviews/landscape/{opp_id}/decision", data={"decision": "maybe"}, headers=headers
    )
    assert r.status_code == 400
    assert await _decisions(db_session) == []

    r = await client.post(
        f"/reviews/landscape/{opp_id}/decision",
        data={"decision": "pursue", "reason": "Clear unmet need."},
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/workspace/landscape/{opp_id}"
    (row,) = await _decisions(db_session)
    assert row.decision == "pursue"
    assert row.reviewer_id == manager.id
    assert row.landscape_assessment_id == assessment_id


async def test_unknown_tag_rejected(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    opp_id, _ = await _opportunity(db_session)
    headers = auth_headers(manager.id)

    r = await client.post(
        f"/reviews/landscape/{opp_id}/decision",
        data={"decision": "pass", "reason_tags": ["Not a tag in the criteria file"]},
        headers=headers,
    )
    assert r.status_code == 400
    assert await _decisions(db_session) == []

    tag = load_criteria().reason_tags[0]
    r = await client.post(
        f"/reviews/landscape/{opp_id}/decision",
        data={"decision": "pass", "reason_tags": [tag]},
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    (row,) = await _decisions(db_session)
    assert row.reason_tags == [tag]


async def test_reason_length(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    opp_id, _ = await _opportunity(db_session)
    headers = auth_headers(manager.id)

    r = await client.post(
        f"/reviews/landscape/{opp_id}/decision",
        data={"decision": "pass", "reason": "x" * 1001},
        headers=headers,
    )
    assert r.status_code == 400
    assert await _decisions(db_session) == []

    r = await client.post(
        f"/reviews/landscape/{opp_id}/decision",
        data={"decision": "pass", "reason": "x" * 1000},
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    (row,) = await _decisions(db_session)
    assert len(row.reason) == 1000


async def test_one_decision_per_reviewer(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    other = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    opp_id, _ = await _opportunity(db_session)
    headers = auth_headers(manager.id)

    for decision in ("pursue", "pass"):
        r = await client.post(
            f"/reviews/landscape/{opp_id}/decision",
            data={"decision": decision},
            headers=headers,
            follow_redirects=False,
        )
        assert r.status_code == 303
    (row,) = await _decisions(db_session)
    assert row.decision == "pursue"

    r = await client.post(
        f"/reviews/landscape-decisions/{row.id}/edit",
        data={"decision": "pass"},
        headers=auth_headers(other.id),
    )
    assert r.status_code == 403

    r = await client.post(
        f"/reviews/landscape-decisions/{row.id}/edit",
        data={"decision": "pass"},
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    await db_session.refresh(row)
    assert row.decision == "pass" and row.edited is True
    assert len(await _decisions(db_session)) == 1


async def test_review_dimension_scores_validated_against_live_rubric(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    opp_id, assessment_id = await _opportunity(db_session)
    headers = auth_headers(manager.id)
    rubric = load_rubric()
    key = rubric.dimensions[0].key

    for bad in ({"dim_not_a_dimension": "3"}, {f"dim_{key}": str(rubric.scale_max + 1)}):
        r = await client.post(
            f"/reviews/landscape/{opp_id}/review",
            data={"score": "4", "comment": "Invented comment.", **bad},
            headers=headers,
        )
        assert r.status_code == 400
    assert await _reviews(db_session) == []

    r = await client.post(
        f"/reviews/landscape/{opp_id}/review",
        data={"score": "4", "comment": "Invented comment.", f"dim_{key}": str(rubric.scale_min)},
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    (row,) = await _reviews(db_session)
    assert row.dimension_scores == {key: rubric.scale_min}
    assert row.rubric_version == RUBRIC_VERSION
    assert row.landscape_assessment_id == assessment_id


async def test_feedback_mode_never_stored(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    opp_id, _ = await _opportunity(db_session)

    r = await client.post(
        f"/reviews/landscape/{opp_id}/review",
        data={"score": "3", "comment": "x", "feedback_mode": "learn"},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert len(await _reviews(db_session)) == 1
    for model in (LandscapeReview, LandscapeDecision):
        assert "feedback_mode" not in model.__table__.columns
        assert "consumed_at" not in model.__table__.columns


async def test_never_enqueues_review_bot(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    opp_id, _ = await _opportunity(db_session)
    headers = auth_headers(manager.id)
    jobs_before = await db_session.scalar(select(func.count()).select_from(Job))
    suggestions_before = await db_session.scalar(
        select(func.count()).select_from(PromptChangeSuggestion)
    )

    for path, data in (
        ("decision", {"decision": "pursue"}),
        ("review", {"score": "5", "comment": "Invented."}),
        ("status", {"action": "approved"}),
    ):
        r = await client.post(
            f"/reviews/landscape/{opp_id}/{path}", data=data, headers=headers,
            follow_redirects=False,
        )
        assert r.status_code == 303, path

    assert await db_session.scalar(select(func.count()).select_from(Job)) == jobs_before
    assert await db_session.scalar(
        select(func.count()).select_from(PromptChangeSuggestion)
    ) == suggestions_before


async def test_assign_refuses_impersonation(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    # A separate impersonation target: the app marks the impersonated user's instance in the
    # shared test session, so impersonating ``manager`` would mark it for the last request.
    impersonated = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    opp_id, _ = await _opportunity(db_session)

    for path in ("assign", "unassign"):
        r = await client.post(
            f"/reviews/landscape/{opp_id}/{path}",
            data={"assignee_user_id": str(reviewer.id)},
            headers=auth_headers(admin.id, impersonate=impersonated.id),
            follow_redirects=False,
        )
        assert r.status_code == 403, path
    assert (await db_session.execute(select(LandscapeReviewAssignment))).first() is None

    r = await client.post(
        f"/reviews/landscape/{opp_id}/assign",
        data={"assignee_user_id": str(reviewer.id)},
        headers=auth_headers(manager.id),
        follow_redirects=False,
    )
    assert r.status_code == 303
    row = (await db_session.execute(select(LandscapeReviewAssignment))).scalar_one()
    assert row.assignee_id == reviewer.id and row.assigned_by_user_id == manager.id


async def test_expert_judgment_only_for_own_sample_items(client, db_session):
    expert = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    other_expert = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    db_session.add(AppSetting(
        key=EXPERTS_KEY, value=json.dumps([str(expert.id), str(other_expert.id)])
    ))
    own_opp, own_assessment = await _opportunity(db_session)
    # Issued, so the expert may view it, but drawn for the other expert only.
    others_opp, others_assessment = await _opportunity(db_session)
    own = LandscapeExpertJudgment(
        opportunity_id=own_opp, expert_id=expert.id, expert_name=expert.name,
        quarter="2026-Q4", landscape_assessment_id=own_assessment,
    )
    others = LandscapeExpertJudgment(
        opportunity_id=others_opp, expert_id=other_expert.id, expert_name=other_expert.name,
        quarter="2026-Q4", landscape_assessment_id=others_assessment,
    )
    db_session.add_all([own, others])
    await db_session.flush()
    rubric = load_rubric()
    gate = next(iter(rubric.gating))
    data = {
        f"gate_{gate}": "met",
        f"dim_{rubric.dimensions[0].key}": str(rubric.scale_max),
        "recommendation": "advance",
        "comment": "Invented judgment.",
    }
    headers = auth_headers(expert.id)

    r = await client.post(
        f"/reviews/landscape/{others_opp}/expert-judgment", data=data, headers=headers
    )
    assert r.status_code == 404
    await db_session.refresh(others)
    assert others.submitted_at is None and others.gates is None

    r = await client.post(
        f"/reviews/landscape/{own_opp}/expert-judgment", data=data, headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    await db_session.refresh(own)
    assert own.submitted_at is not None
    assert own.gates == {gate: "met"}
    assert own.scores == {rubric.dimensions[0].key: rubric.scale_max}
    assert own.recommendation == "advance"


async def test_expert_judgment_from_an_earlier_quarter_still_submits(client, db_session):
    """D63: the hiding holds from the draw until submission, whatever the quarter, so the
    expert can still submit an item drawn in an earlier quarter, and only that lifts it."""
    expert = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    db_session.add(AppSetting(key=EXPERTS_KEY, value=json.dumps([str(expert.id)])))
    opp, assessment = await _opportunity(db_session)
    row = LandscapeExpertJudgment(
        opportunity_id=opp, expert_id=expert.id, expert_name=expert.name,
        quarter="2020-Q1", landscape_assessment_id=assessment,
        drawn_at=datetime(2020, 2, 1, tzinfo=UTC),
    )
    db_session.add(row)
    await db_session.flush()
    assert (await landscape_view.hiding_for(db_session, expert, opp)).expert is True

    rubric = load_rubric()
    r = await client.post(
        f"/reviews/landscape/{opp}/expert-judgment", headers=auth_headers(expert.id),
        data={"recommendation": "pass", "comment": "Invented judgment.",
              f"gate_{next(iter(rubric.gating))}": "met",
              f"dim_{rubric.dimensions[0].key}": str(rubric.scale_max)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    await db_session.refresh(row)
    assert row.submitted_at is not None
    assert (await landscape_view.hiding_for(db_session, expert, opp)).expert is False


async def test_reviewer_posts_outside_issued_batches_404(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    unissued, _ = await _opportunity(db_session, issued=False)
    issued, _ = await _opportunity(db_session)
    headers = auth_headers(reviewer.id)
    posts = (
        ("decision", {"decision": "pursue"}),
        ("review", {"score": "3", "comment": "Invented."}),
        ("status", {"action": "approved"}),
    )

    for path, data in posts:
        r = await client.post(
            f"/reviews/landscape/{unissued}/{path}", data=data, headers=headers
        )
        assert r.status_code == 404, path
    assert await _decisions(db_session) == []
    assert await _reviews(db_session) == []
    assert (await db_session.execute(select(LandscapeReviewEvent))).first() is None

    for path, data in posts:
        r = await client.post(
            f"/reviews/landscape/{issued}/{path}", data=data, headers=headers,
            follow_redirects=False,
        )
        assert r.status_code == 303, path

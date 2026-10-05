"""Migrations 0051 and 0059: the assessment-chat tables, their constraints and cascades.

Spec §7. The partial unique index is what makes "one answer in flight per user"
atomic; the cascades are what make a chat private content (it goes with the
assessment and with the user) while the usage ledger survives both. 0059's
suggestion sets go with their assessment; its drawer openings, like the ledger,
outlive both.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.models import (
    AssessmentChatOpen,
    AssessmentChatSuggestionSet,
    AssessmentChatTurn,
    AssessmentChatUsage,
    OpportunityAssessment,
)
from src.models.assessment_chat import ONE_STREAMING_INDEX
from tests import factories

pytestmark = pytest.mark.integration

INDEXES = {
    "ix_assessment_chat_turns_conversation",
    "ix_assessment_chat_turns_user_id",
    ONE_STREAMING_INDEX,
    "ix_assessment_chat_usage_user_created",
    "ix_assessment_chat_usage_created",
    "ix_assessment_chat_usage_turn_id",
    "ix_assessment_chat_usage_assessment_id",
    "uq_assessment_chat_suggestions_key",
    "ix_assessment_chat_suggestions_updated",
    "ix_assessment_chat_opens_user_id",
    "ix_assessment_chat_opens_assessment_id",
    "ix_assessment_chat_opens_created",
}
CHECKS = {
    "ck_assessment_chat_turns_tier",
    "ck_assessment_chat_turns_status",
    "ck_assessment_chat_usage_tier",
    "ck_assessment_chat_usage_question_origin",
    "ck_assessment_chat_suggestions_tier",
    "ck_assessment_chat_suggestions_status",
    "ck_assessment_chat_opens_tier",
    "ck_assessment_chat_opens_via",
}


async def _assessment(db_session) -> OpportunityAssessment:
    run = await factories.make_simulation_run(db_session)
    row = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="schema-channel"
    )
    db_session.add(row)
    await db_session.flush()
    return row


def _turn(assessment_id, user_id, **overrides) -> AssessmentChatTurn:
    data = dict(
        id=uuid.uuid4(),
        assessment_id=assessment_id,
        user_id=user_id,
        context_tier="staff",
        question="q",
        status="complete",
        model="claude-opus-5-5",
        record_sha256_12="0" * 12,
        prompt_sha256_12="1" * 12,
        created_at=datetime.now(UTC),
    )
    data.update(overrides)
    return AssessmentChatTurn(**data)


def _usage(turn: AssessmentChatTurn, **overrides) -> AssessmentChatUsage:
    data = dict(
        id=uuid.uuid4(),
        turn_id=turn.id,
        user_id=turn.user_id,
        assessment_id=turn.assessment_id,
        context_tier=turn.context_tier,
        model=turn.model,
        status=turn.status,
        created_at=turn.created_at,
    )
    data.update(overrides)
    return AssessmentChatUsage(**data)


async def test_the_migration_created_every_index_and_check(db_session):
    indexes = set(
        (
            await db_session.execute(
                text(
                    "SELECT indexname FROM pg_indexes WHERE tablename IN "
                    "('assessment_chat_turns', 'assessment_chat_usage', "
                    "'assessment_chat_suggestions', 'assessment_chat_opens')"
                )
            )
        ).scalars()
    )
    assert INDEXES <= indexes
    checks = set(
        (
            await db_session.execute(
                text("SELECT conname FROM pg_constraint WHERE conname LIKE 'ck_assessment_chat_%'")
            )
        ).scalars()
    )
    assert checks == CHECKS


async def test_a_user_can_have_only_one_answer_in_flight(db_session):
    a = await _assessment(db_session)
    b = await _assessment(db_session)
    user = await factories.make_user(db_session)
    other = await factories.make_user(db_session)
    db_session.add(_turn(a.id, user.id, status="streaming"))
    await db_session.flush()

    with pytest.raises(IntegrityError) as caught:
        async with db_session.begin_nested():
            db_session.add(_turn(b.id, user.id, status="streaming"))
    assert ONE_STREAMING_INDEX in str(caught.value.orig)

    # A finished turn beside the in-flight one, and another user's in-flight
    # turn, are both fine.
    async with db_session.begin_nested():
        db_session.add(_turn(a.id, user.id, status="complete"))
        db_session.add(_turn(a.id, other.id, status="streaming"))


@pytest.mark.parametrize("column,value", [("context_tier", "admin"), ("status", "done")])
async def test_tier_and_status_are_checked(db_session, column, value):
    a = await _assessment(db_session)
    user = await factories.make_user(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(_turn(a.id, user.id, **{column: value}))


async def test_deleting_the_assessment_removes_turns_and_keeps_usage(db_session):
    a = await _assessment(db_session)
    user = await factories.make_user(db_session)
    turn = _turn(a.id, user.id)
    usage = _usage(turn)
    db_session.add_all([turn, usage])
    await db_session.flush()

    await db_session.execute(
        text("DELETE FROM opportunity_assessments WHERE id = :id"), {"id": a.id}
    )

    left = (
        await db_session.execute(
            text("SELECT count(*) FROM assessment_chat_turns WHERE id = :id"), {"id": turn.id}
        )
    ).scalar_one()
    assert left == 0
    row = (
        await db_session.execute(
            text(
                "SELECT turn_id, assessment_id, user_id FROM assessment_chat_usage "
                "WHERE id = :id"
            ),
            {"id": usage.id},
        )
    ).one()
    assert row.turn_id is None
    assert row.assessment_id is None
    assert row.user_id == user.id


async def test_deleting_the_user_removes_turns_and_keeps_usage(db_session):
    a = await _assessment(db_session)
    user = await factories.make_user(db_session)
    turn = _turn(a.id, user.id)
    usage = _usage(turn)
    db_session.add_all([turn, usage])
    await db_session.flush()

    await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})

    left = (
        await db_session.execute(
            text("SELECT count(*) FROM assessment_chat_turns WHERE id = :id"), {"id": turn.id}
        )
    ).scalar_one()
    assert left == 0
    row = (
        await db_session.execute(
            text("SELECT turn_id, user_id, assessment_id FROM assessment_chat_usage WHERE id = :id"),
            {"id": usage.id},
        )
    ).one()
    assert row.turn_id is None
    assert row.user_id is None
    assert row.assessment_id == a.id


async def test_absent_json_is_sql_null(db_session):
    a = await _assessment(db_session)
    user = await factories.make_user(db_session)
    turn = _turn(a.id, user.id, answer_segments=None, citations=None, allowed_links=None)
    usage = _usage(turn, usage_by_model=None)
    db_session.add_all([turn, usage])
    await db_session.flush()

    turns = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM assessment_chat_turns WHERE id = :id AND "
                "answer_segments IS NULL AND citations IS NULL AND allowed_links IS NULL"
            ),
            {"id": turn.id},
        )
    ).scalar_one()
    ledger = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM assessment_chat_usage "
                "WHERE id = :id AND usage_by_model IS NULL"
            ),
            {"id": usage.id},
        )
    ).scalar_one()
    assert (turns, ledger) == (1, 1)


def _suggestion_set(assessment_id, **overrides) -> AssessmentChatSuggestionSet:
    data = dict(
        assessment_id=assessment_id,
        context_tier="staff",
        verdict_revision=1,
        status="ready",
        suggestions=[{"text": "Why?", "anchor": "scores", "label": "Dimension score"}],
        model="claude-opus-5",
        record_sha256_12="0" * 12,
        prompt_sha256_12="1" * 12,
    )
    data.update(overrides)
    return AssessmentChatSuggestionSet(**data)


async def test_the_question_origin_is_checked(db_session):
    a = await _assessment(db_session)
    user = await factories.make_user(db_session)
    turn = _turn(a.id, user.id)
    db_session.add(turn)
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(_usage(turn, question_origin="pasted"))


async def test_a_question_origin_may_be_absent_or_known(db_session):
    a = await _assessment(db_session)
    user = await factories.make_user(db_session)
    first, second = _turn(a.id, user.id), _turn(a.id, user.id)
    db_session.add_all([first, second])
    await db_session.flush()
    db_session.add_all([_usage(first), _usage(second, question_origin="inline_generated")])
    await db_session.flush()


async def test_one_suggestion_set_per_assessment_tier_and_revision(db_session):
    a = await _assessment(db_session)
    db_session.add(_suggestion_set(a.id))
    await db_session.flush()
    db_session.add_all([
        _suggestion_set(a.id, context_tier="reviewer"),
        _suggestion_set(a.id, verdict_revision=2),
    ])
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(_suggestion_set(a.id, status="failed"))


@pytest.mark.parametrize("overrides", [{"status": "pending"}, {"context_tier": "pi"}])
async def test_suggestion_status_and_tier_are_checked(db_session, overrides):
    a = await _assessment(db_session)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(_suggestion_set(a.id, **overrides))


async def test_deleting_the_assessment_removes_its_suggestions_and_keeps_openings(db_session):
    a = await _assessment(db_session)
    user = await factories.make_user(db_session)
    db_session.add(_suggestion_set(a.id))
    opened = AssessmentChatOpen(
        user_id=user.id, assessment_id=a.id, context_tier="staff", opened_via="bubble",
    )
    db_session.add(opened)
    await db_session.flush()
    await db_session.execute(
        text("DELETE FROM opportunity_assessments WHERE id = :id"), {"id": a.id}
    )
    sets = (
        await db_session.execute(
            text("SELECT count(*) FROM assessment_chat_suggestions WHERE assessment_id = :id"),
            {"id": a.id},
        )
    ).scalar_one()
    row = (
        await db_session.execute(
            text("SELECT user_id, assessment_id FROM assessment_chat_opens WHERE id = :id"),
            {"id": opened.id},
        )
    ).one()
    assert sets == 0
    assert (row.user_id, row.assessment_id) == (user.id, None)


async def test_an_opening_outlives_its_user_and_checks_its_via(db_session):
    a = await _assessment(db_session)
    user = await factories.make_user(db_session)
    opened = AssessmentChatOpen(user_id=user.id, assessment_id=a.id, context_tier="reviewer")
    db_session.add(opened)
    await db_session.flush()
    await db_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
    left = (
        await db_session.execute(
            text("SELECT user_id, opened_via FROM assessment_chat_opens WHERE id = :id"),
            {"id": opened.id},
        )
    ).one()
    assert (left.user_id, left.opened_via) == (None, None)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                AssessmentChatOpen(assessment_id=a.id, context_tier="staff", opened_via="email")
            )

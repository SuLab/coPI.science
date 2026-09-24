"""Migration 0051: the two assessment-chat tables, their constraints and cascades.

Spec §7. The partial unique index is what makes "one answer in flight per user"
atomic; the cascades are what make a chat private content (it goes with the
assessment and with the user) while the usage ledger survives both.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.models import AssessmentChatTurn, AssessmentChatUsage, OpportunityAssessment
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
}
CHECKS = {
    "ck_assessment_chat_turns_tier",
    "ck_assessment_chat_turns_status",
    "ck_assessment_chat_usage_tier",
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
                    "('assessment_chat_turns', 'assessment_chat_usage')"
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

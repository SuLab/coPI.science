"""B6: turns of an earlier verdict revision stay visible but leave replay and
the cap; NULL (pre-0054) turns and rows coalesce to 1 on both sides."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import AssessmentChatTurn, OpportunityAssessment, SimulationRun
from src.services import assessment_chat as chat
from tests.factories import make_user

pytestmark = pytest.mark.integration


def _turn(assessment_id, user_id, i, revision):
    return AssessmentChatTurn(
        id=uuid.uuid4(), assessment_id=assessment_id, user_id=user_id, context_tier="staff",
        question=f"q{i}", answer_text=f"a{i}", status="complete", model="m",
        fallback_used=False, record_sha256_12="a" * 12, prompt_sha256_12="b" * 12,
        created_at=datetime.now(UTC) + timedelta(seconds=i), verdict_revision=revision,
    )


def test_current_revision_turns_coalesces_both_sides():
    class T:
        def __init__(self, r):
            self.verdict_revision = r
    turns = [T(None), T(1), T(2), T(None)]
    assert [t.verdict_revision for t in chat.current_revision_turns(turns, 1)] == [None, 1, None]
    assert [t.verdict_revision for t in chat.current_revision_turns(turns, 2)] == [2]


@pytest.mark.asyncio
async def test_null_revision_conversation_is_unchanged_and_earlier_revisions_drop_out(engine, monkeypatch):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.flush()
        row = OpportunityAssessment(id=uuid.uuid4(), simulation_run_id=run.id, agent_id="b",
                                    channel_name="c", thread_id="t")
        db.add(row)
        await db.flush()
        user_id = (await make_user(db)).id
        db.add_all([_turn(row.id, user_id, i, None) for i in range(3)])
        await db.flush()
        assert await chat._assessment_revision(db, row.id) == 1
        turns = await chat._conversation(db, assessment_id=row.id, user_id=user_id, tier="staff")
        assert len(chat.current_revision_turns(turns, 1)) == 3, "pre-0054 history kept, as today"
        row.verdict_revision = 2
        db.add(_turn(row.id, user_id, 3, 2))
        await db.flush()
        rev = await chat._assessment_revision(db, row.id)
        turns = await chat._conversation(db, assessment_id=row.id, user_id=user_id, tier="staff")
        current = chat.current_revision_turns(turns, rev)
        assert [t.question for t in current] == ["q3"]
        assert [t.question for t in chat.replay_window(current)] == ["q3"]
        await db.rollback()


@pytest.mark.asyncio
async def test_the_turn_cap_counts_only_the_current_revision(engine, monkeypatch):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "assessment_chat_max_turns", 2)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun()
        db.add(run)
        await db.flush()
        row = OpportunityAssessment(id=uuid.uuid4(), simulation_run_id=run.id, agent_id="b",
                                    channel_name="c", thread_id="t", verdict_revision=2)
        db.add(row)
        await db.flush()
        user_id = (await make_user(db)).id
        db.add_all([_turn(row.id, user_id, 0, None), _turn(row.id, user_id, 1, 1)])
        await db.flush()
        turns = await chat._refuse_over_caps(db, assessment_id=row.id, user_id=user_id, tier="staff")
        assert turns == [], "two revision-1 turns do not fill a revision-2 conversation"
        db.add(_turn(row.id, user_id, 2, 2))
        db.add(_turn(row.id, user_id, 3, 2))
        await db.flush()
        with pytest.raises(chat.ChatError):
            await chat._refuse_over_caps(db, assessment_id=row.id, user_id=user_id, tier="staff")
        await db.rollback()


def test_new_rows_stamp_the_revision():
    turn, _usage = chat._new_rows(
        assessment_id=uuid.uuid4(), user_id=uuid.uuid4(), tier="staff", question="q",
        model="m", record_sha="a" * 12, prompt_sha="b" * 12,
        created_at=datetime.now(UTC), verdict_revision=3,
    )
    assert turn.verdict_revision == 3

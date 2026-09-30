"""§11 Phase 2 golden additions. The frozen suite itself is never regenerated;
these prove the Phase 2 paths send the SAME requests:

* the assessment-chat request built from non-NULL 0054 columns (revision 2,
  turns stamped 2, plus an excluded revision-1 turn) equals the request built
  from the same conversation with NULL revisions (the pre-0054 shape the
  frozen capture pins);
* agents built through an installed PromptSnapshot compose the same system
  prompts as agents reading the disk."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent import prompt_snapshot as ps
from src.agent.agent import Agent
from src.models import (
    AssessmentChatTurn,
    AssessmentChatUsage,
    OpportunityAssessment,
    SimulationRun,
    User,
)
from src.services import assessment_chat as chat

pytestmark = pytest.mark.integration


def _turn(aid, uid, i, revision, created):
    return AssessmentChatTurn(
        id=uuid.uuid4(), assessment_id=aid, user_id=uid, context_tier="staff", question=f"q{i}",
        answer_text=f"a{i}", status="complete", model="m", fallback_used=False,
        record_sha256_12="a" * 12, prompt_sha256_12="b" * 12, created_at=created,
        verdict_revision=revision,
    )


@pytest.mark.asyncio
async def test_chat_request_is_identical_for_null_and_non_null_revisions(engine, monkeypatch):
    from tests import factories

    factory = async_sessionmaker(engine, expire_on_commit=False)
    base = datetime.now(UTC) - timedelta(hours=1)
    async with factory() as db:
        user = await factories.make_user(db, user_role="admin", email=f"p2-{uuid.uuid4().hex[:6]}@example.org")
        run = SimulationRun()
        db.add(run)
        await db.flush()
        row = OpportunityAssessment(id=uuid.uuid4(), simulation_run_id=run.id, agent_id="blackbird",
                                    channel_name="c", thread_id="t", created_at=base,
                                    recommendation="conditional", headline="H")
        db.add(row)
        db.add_all([_turn(row.id, user.id, i, None, base + timedelta(minutes=i)) for i in (1, 2)])
        await db.commit()
        try:
            first = await chat.prepare_turn(db, assessment_id=row.id, user=user, question_raw="next?")
            await db.execute(delete(AssessmentChatUsage).where(AssessmentChatUsage.id == first.usage_id))
            await db.execute(delete(AssessmentChatTurn).where(AssessmentChatTurn.id == first.turn_id))
            row.verdict_revision = 2
            row.verdict_write_id = uuid.uuid4()
            row.verdict_ordinal = 12
            db.add(_turn(row.id, user.id, 0, 1, base - timedelta(minutes=5)))
            await db.flush()
            for t in (await chat._conversation(db, assessment_id=row.id, user_id=user.id, tier=first.tier)):
                if t.question in ("q1", "q2"):
                    t.verdict_revision = 2
            await db.commit()
            second = await chat.prepare_turn(db, assessment_id=row.id, user=user, question_raw="next?")
            assert second.request == first.request
        finally:
            await db.rollback()
            await db.execute(delete(AssessmentChatUsage).where(AssessmentChatUsage.user_id == user.id))
            await db.execute(delete(AssessmentChatTurn).where(AssessmentChatTurn.assessment_id == row.id))
            await db.delete(await db.get(SimulationRun, run.id))
            await db.commit()
            await db.execute(delete(User).where(User.id == user.id))
            await db.commit()


def test_agents_built_through_the_snapshot_compose_the_same_prompts():
    ps.install(None)
    disk = {role: Agent("x", "XBot", "X", role=role)._load_prompt("agent-system.md", "D")
            for role in ("pi_lab", "scout_hub")}
    ps.install(ps.PromptSnapshot.load())
    try:
        snap = {role: Agent("x", "XBot", "X", role=role)._load_prompt("agent-system.md", "D")
                for role in ("pi_lab", "scout_hub")}
    finally:
        ps.install(None)
    assert snap == disk

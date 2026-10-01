"""AP-11: when every feedback row changed during the model call (stamped == 0),
the review bot writes no suggestion and the job still completes."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models import AssessmentReview, OpportunityAssessment, PromptChangeSuggestion, User
from src.services import review_bot
from src.services.assessment_reviews import submit_feedback
from src.worker.main import JobContext
from tests.integration.test_review_job_end_to_end import _seed, _sweep

pytestmark = pytest.mark.integration

_REPLY = json.dumps({"target": "pi_lab", "suggestion": "x", "rationale": "r"})


@pytest.fixture
def factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def test_no_suggestion_when_every_row_changed_mid_call(factory, monkeypatch):
    await _sweep(factory)
    assessment_id, reviewer_id = await _seed(factory)
    try:
        async with factory() as db:
            assessment = await db.get(OpportunityAssessment, assessment_id)
            reviewer = await db.get(User, reviewer_id)
            await submit_feedback(
                db, assessment=assessment, reviewer=reviewer,
                score=2, comment="original", feedback_mode="learn",
            )
            await db.commit()

        async def reply_and_edit(*a, **k):
            async with factory() as other:
                await other.execute(
                    update(AssessmentReview)
                    .where(AssessmentReview.assessment_id == assessment_id)
                    .values(comment="edited during the model call")
                )
                await other.commit()
            return _REPLY

        monkeypatch.setattr(review_bot, "generate_agent_response", reply_and_edit)
        ctx = JobContext(
            id=uuid.uuid4(), type="review_feedback_analysis", user_id=None,
            payload={"assessment_id": str(assessment_id)}, attempts=1, max_attempts=3,
        )
        async with factory() as db:
            await review_bot.execute_review_analysis(ctx, db)

        async with factory() as check:
            n = (await check.execute(
                select(func.count()).select_from(PromptChangeSuggestion)
                .where(PromptChangeSuggestion.assessment_id == assessment_id)
            )).scalar_one()
        assert n == 0
    finally:
        await _sweep(factory)

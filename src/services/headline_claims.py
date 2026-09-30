"""The `#assessments-summary` headline claim protocol (spec P0-08; S1-06, SC-1, SC-2).

A headline is a public Slack post that cannot be retracted, and two posters can
race for it: the engine (at capture, at an interview's close, in the stop sweep)
and `scripts/backfill_assessment_headlines.py`. Both claim immediately before
posting:

1. **Claim.** By thread, every row of the interview that is neither claimed nor
   posted — refused outright if ANY row of the thread is already claimed or
   posted, so a claimed-but-unposted sibling blocks a second poster (SA5-08) and
   a thread with one posted row is never re-posted (SC-2). A NULL-thread row is
   claimed by id (SA4-15).
2. **Post.**
3. **Settle.** On success ``mark_posted``; on a DEFINITE failure (Slack answered
   and refused, or nothing was attempted) ``release_claim``. A transport
   exception with no Slack response is not a definite failure: the claim STAYS.

A claim with no post after ``IN_DOUBT_AFTER`` is in doubt — the post may or may
not have landed. It is listed (``list_in_doubt``) and never re-posted
automatically; an operator releases it after checking Slack.

Every function commits its own write and returns; callers pass a short-lived
session.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.models import OpportunityAssessment

logger = logging.getLogger(__name__)

#: A claim older than this with no post is "in doubt" (spec P0-08 / §8.2).
IN_DOUBT_AFTER = timedelta(minutes=10)

_UNCLAIMED = (
    OpportunityAssessment.summary_claimed_at.is_(None),
    OpportunityAssessment.summary_posted_at.is_(None),
)


async def claim_thread(db: AsyncSession, run_id: uuid.UUID, thread_id: str) -> list[uuid.UUID]:
    """Claim every owed row of one interview; ``[]`` when anyone already has it."""
    sibling = aliased(OpportunityAssessment)
    blocked = exists().where(
        sibling.simulation_run_id == run_id,
        sibling.thread_id == thread_id,
        or_(sibling.summary_posted_at.is_not(None), sibling.summary_claimed_at.is_not(None)),
    )
    ids = list((await db.execute(
        update(OpportunityAssessment)
        .where(
            OpportunityAssessment.simulation_run_id == run_id,
            OpportunityAssessment.thread_id == thread_id,
            *_UNCLAIMED,
            ~blocked,
        )
        .values(summary_claimed_at=func.now())
        .returning(OpportunityAssessment.id)
        .execution_options(synchronize_session=False)
    )).scalars().all())
    await db.commit()
    if ids:
        logger.info("Claimed the headline of run %s thread %s: %s", run_id, thread_id, ids)
    return ids


async def claim_row(db: AsyncSession, assessment_id: uuid.UUID) -> list[uuid.UUID]:
    """Claim one row by id (NULL-thread rows); ``[]`` when it is claimed or posted."""
    ids = list((await db.execute(
        update(OpportunityAssessment)
        .where(OpportunityAssessment.id == assessment_id, *_UNCLAIMED)
        .values(summary_claimed_at=func.now())
        .returning(OpportunityAssessment.id)
        .execution_options(synchronize_session=False)
    )).scalars().all())
    await db.commit()
    if ids:
        logger.info("Claimed the headline of assessment %s", assessment_id)
    return ids


async def mark_posted(db: AsyncSession, ids: Sequence[uuid.UUID]) -> None:
    """Record that the claimed rows' headline reached Slack. The claim stays set."""
    if not ids:
        return
    await db.execute(
        update(OpportunityAssessment)
        .where(
            OpportunityAssessment.id.in_(list(ids)),
            OpportunityAssessment.summary_posted_at.is_(None),
        )
        .values(summary_posted_at=func.now())
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def release_claim(db: AsyncSession, ids: Sequence[uuid.UUID]) -> None:
    """Clear the claim on rows whose headline definitely did not post."""
    if not ids:
        return
    await db.execute(
        update(OpportunityAssessment)
        .where(
            OpportunityAssessment.id.in_(list(ids)),
            OpportunityAssessment.summary_posted_at.is_(None),
        )
        .values(summary_claimed_at=None)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    logger.info("Released headline claim(s) %s", list(ids))


async def list_in_doubt(db: AsyncSession, run_id: uuid.UUID) -> list[OpportunityAssessment]:
    """Rows claimed more than IN_DOUBT_AFTER ago with no post, oldest claim first."""
    return list((await db.execute(
        select(OpportunityAssessment)
        .where(
            OpportunityAssessment.simulation_run_id == run_id,
            OpportunityAssessment.summary_claimed_at.is_not(None),
            OpportunityAssessment.summary_posted_at.is_(None),
            OpportunityAssessment.summary_claimed_at < func.now() - IN_DOUBT_AFTER,
        )
        .order_by(OpportunityAssessment.summary_claimed_at)
    )).scalars().all())

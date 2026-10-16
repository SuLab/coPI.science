"""The ``#assessments-summary`` headline claim protocol for ``hub_declines`` rows.

The same three steps as an assessment headline (``src/services/headline_claims.py``),
keyed by the row's id because a decline is one row per interview: **claim** (refused when
the row is already claimed or posted), **post**, then **settle** (``mark_posted`` on
success, ``release_claim`` on a definite failure). A transport exception with no Slack
response is not a definite failure: the claim stays, and ``list_in_doubt`` lists it after
``IN_DOUBT_AFTER`` with no post. Every function commits its own write.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import HubDecline
from src.services.headline_claims import IN_DOUBT_AFTER


async def claim(db: AsyncSession, decline_id: uuid.UUID) -> bool:
    """Claim one decline's headline; False when it is already claimed or posted."""
    claimed = (await db.execute(
        update(HubDecline)
        .where(
            HubDecline.id == decline_id,
            HubDecline.summary_claimed_at.is_(None),
            HubDecline.summary_posted_at.is_(None),
        )
        .values(summary_claimed_at=func.now())
        .returning(HubDecline.id)
        .execution_options(synchronize_session=False)
    )).scalar_one_or_none()
    await db.commit()
    return claimed is not None


async def mark_posted(db: AsyncSession, decline_id: uuid.UUID) -> None:
    """Record that the claimed decline's headline reached Slack. The claim stays set."""
    await db.execute(
        update(HubDecline)
        .where(HubDecline.id == decline_id, HubDecline.summary_posted_at.is_(None))
        .values(summary_posted_at=func.now())
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def release_claim(db: AsyncSession, decline_id: uuid.UUID) -> None:
    """Clear the claim of a decline whose headline definitely did not post."""
    await db.execute(
        update(HubDecline)
        .where(HubDecline.id == decline_id, HubDecline.summary_posted_at.is_(None))
        .values(summary_claimed_at=None)
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def owed(db: AsyncSession, run_id: uuid.UUID) -> list[HubDecline]:
    """The run's declines whose headline is neither claimed nor posted, oldest first."""
    return list((await db.execute(
        select(HubDecline)
        .where(
            HubDecline.simulation_run_id == run_id,
            HubDecline.summary_claimed_at.is_(None),
            HubDecline.summary_posted_at.is_(None),
        )
        .order_by(HubDecline.created_at, HubDecline.id)
    )).scalars().all())


async def list_in_doubt(db: AsyncSession, run_id: uuid.UUID) -> list[HubDecline]:
    """Declines claimed more than IN_DOUBT_AFTER ago with no post, oldest claim first."""
    return list((await db.execute(
        select(HubDecline)
        .where(
            HubDecline.simulation_run_id == run_id,
            HubDecline.summary_claimed_at.is_not(None),
            HubDecline.summary_posted_at.is_(None),
            HubDecline.summary_claimed_at < func.now() - IN_DOUBT_AFTER,
        )
        .order_by(HubDecline.summary_claimed_at)
    )).scalars().all())

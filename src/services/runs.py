"""Run-selection queries shared by the web routes."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import SimulationRun


async def latest_run_id(db: AsyncSession) -> uuid.UUID | None:
    """The newest simulation run's id, or None when there are no runs.

    Ordered by ``started_at DESC, id DESC`` so two runs that share a
    ``started_at`` resolve the same way on every call: ``started_at`` alone is
    not a total order, and ``LIMIT 1`` over a tie may return either row.
    """
    return (
        await db.execute(
            select(SimulationRun.id)
            .order_by(SimulationRun.started_at.desc(), SimulationRun.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

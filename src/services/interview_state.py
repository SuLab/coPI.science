"""The one "has this interview ended?" predicate (spec P0-04, SA4-12).

An interview has ended exactly when a ``ThreadDecision`` exists for
(simulation_run_id, thread_id) — ``_close_thread`` writes one for a ⏸️ decline,
a concluding close and the ``max_thread_messages`` timeout. Evictions write none
(the eviction-decision design was withdrawn under B24), so an evicted thread
counts as OPEN: a HOLD end holds its headline, a TODAY end announces it.

Shared by the engine's stop sweep, ``scripts/backfill_assessment_headlines.py``
and, from Phase 2, the headline policy — they must never disagree. A row with no
thread counts as ended at every call site: no interview can still change it.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import ThreadDecision


async def ended_thread_ids(
    db: AsyncSession, run_id: uuid.UUID, thread_ids: Iterable[str] | None = None,
) -> set[str]:
    """The subset of ``thread_ids`` (every thread when None) whose interview ended."""
    stmt = (
        select(ThreadDecision.thread_id)
        .where(ThreadDecision.simulation_run_id == run_id)
        .distinct()
    )
    if thread_ids is not None:
        wanted = list(thread_ids)
        if not wanted:
            return set()
        stmt = stmt.where(ThreadDecision.thread_id.in_(wanted))
    return set((await db.execute(stmt)).scalars().all())

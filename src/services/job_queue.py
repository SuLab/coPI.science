"""Enqueue for the per-user job types, idempotent under concurrency (DB-06).

`INSERT ... ON CONFLICT (user_id, type) WHERE <partial predicate> DO NOTHING`
against 0056's `uq_jobs_one_active_per_user_type`: two overlapping enqueues (a
double click; Add-PI then an approval) produce one row and no IntegrityError.
`review_feedback_analysis` is not a per-user type (one press enqueues up to 25)
and never comes through here."""
from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Job
from src.models.job import ONE_ACTIVE_PER_USER_TYPE_WHERE, PER_USER_JOB_TYPES


async def insert_job_if_absent(
    db: AsyncSession, *, type: str, user_id: uuid.UUID, payload: dict,
    priority: int | None = None,
) -> uuid.UUID | None:
    """Insert a pending job and return its id, or None when the user already has
    a pending or processing job of this type. Adds to the caller's transaction;
    the caller commits."""
    if type not in PER_USER_JOB_TYPES:
        raise ValueError(f"{type!r} is not a per-user job type")
    stmt = (
        pg_insert(Job)
        .values(id=uuid.uuid4(), type=type, user_id=user_id, payload=payload,
                status="pending", attempts=0, max_attempts=3, priority=priority)
        .on_conflict_do_nothing(
            index_elements=["user_id", "type"],
            index_where=text(ONE_ACTIVE_PER_USER_TYPE_WHERE),
        )
        .returning(Job.id)
    )
    return (await db.execute(stmt)).scalar_one_or_none()

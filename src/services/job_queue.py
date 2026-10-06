"""Enqueue for the per-user job types, idempotent under concurrency (DB-06).

`INSERT ... ON CONFLICT (user_id, type) WHERE <partial predicate> DO NOTHING`
against 0056's `uq_jobs_one_active_per_user_type`: two overlapping enqueues (a
double click; Add-PI then an approval) produce one row and no IntegrityError.
`review_feedback_analysis` is not a per-user type (one press enqueues up to 25)
and never comes through here.

Rerun requests (spec 2026-10-05 §4.2): `request_job` is for a caller whose change
must be seen by a run of the job, such as a staff pin that `enrich_grants` reads.
A pending job is row-locked until the caller commits, so it cannot start before the
change is visible; a processing job may already have read the old state, so its
`rerun_requested_at` is set and the worker (`src/worker/main.py`) inserts a fresh
pending job when it completes or dies. The exceptions below let a handler refuse
(`NonRetryableJobError`) or postpone (`JobDeferred`) its job without importing the
worker."""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import DateTime, bindparam, case, func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Job
from src.models.job import ONE_ACTIVE_PER_USER_TYPE_WHERE, PER_USER_JOB_TYPES

#: A step the worker runs on the handler's session after the handler's commit
#: (`JobContext.after_commit`, spec §4.3).
AfterCommit = Callable[[AsyncSession], Awaitable[object]]


class NonRetryableJobError(Exception):
    """A deterministic refusal: the worker marks the job dead at once (spec §4.2)."""


class JobDeferred(Exception):
    """Raised by a handler to return its job to pending until `not_before` without
    counting an attempt (spec §4.2)."""

    def __init__(self, not_before: datetime, reason: str = "") -> None:
        super().__init__(reason or f"deferred until {not_before.isoformat()}")
        self.not_before = not_before
        self.reason = reason


async def insert_job_if_absent(
    db: AsyncSession, *, type: str, user_id: uuid.UUID, payload: dict,
    priority: int | None = None, not_before: datetime | None = None,
) -> uuid.UUID | None:
    """Insert a pending job and return its id, or None when the user already has
    a pending or processing job of this type. On that conflict a higher
    ``priority`` is carried onto the existing PENDING row, so a person now waiting
    on it is not left behind a bulk enqueue's lower priority; ``not_before`` applies
    only to a newly inserted row. Adds to the caller's transaction; the caller
    commits."""
    if type not in PER_USER_JOB_TYPES:
        raise ValueError(f"{type!r} is not a per-user job type")
    stmt = (
        pg_insert(Job)
        .values(id=uuid.uuid4(), type=type, user_id=user_id, payload=payload,
                status="pending", attempts=0, max_attempts=3, priority=priority,
                not_before=not_before)
        .on_conflict_do_nothing(
            index_elements=["user_id", "type"],
            index_where=text(ONE_ACTIVE_PER_USER_TYPE_WHERE),
        )
        .returning(Job.id)
    )
    new_id = (await db.execute(stmt)).scalar_one_or_none()
    if new_id is None and priority is not None:
        await db.execute(
            update(Job)
            .where(Job.user_id == user_id, Job.type == type, Job.status == "pending",
                   func.coalesce(Job.priority, 0) < priority)
            .values(priority=priority)
        )
    return new_id


async def request_job(
    db: AsyncSession, *, type: str, user_id: uuid.UUID, payload: dict,
    priority: int | None = None, not_before: datetime | None = None,
) -> uuid.UUID | None:
    """Make sure a ``type`` job runs for ``user_id`` after this transaction's change.

    Inserts one (returning its id). On a conflict the existing row is UPDATEd: a PENDING
    row is only row-locked, which keeps ``claim_job`` (SKIP LOCKED) from starting it until
    this transaction commits, so it will read the change (None); a PROCESSING row may
    already have read the old state, so it is flagged: ``claim_job`` clears the flag and the
    worker inserts a fresh pending job, not before ``not_before``, when the flagged one
    completes or dies (None). An UPDATE that matches no row means the conflicting job
    finished in between, so the insert is tried once more. Adds to the caller's
    transaction; the caller commits."""
    new_id = await insert_job_if_absent(
        db, type=type, user_id=user_id, payload=payload, priority=priority,
        not_before=not_before,
    )
    if new_id is not None:
        return new_id
    processing = Job.status == "processing"
    flagged = await db.execute(
        update(Job)
        .where(Job.user_id == user_id, Job.type == type,
               Job.status.in_(("pending", "processing")))
        .values(
            rerun_requested_at=case((processing, func.now()), else_=Job.rerun_requested_at),
            rerun_not_before=case(
                (processing, bindparam("rerun_nb", not_before, type_=DateTime(timezone=True))),
                else_=Job.rerun_not_before,
            ),
        )
    )
    if flagged.rowcount:
        return None
    return await insert_job_if_absent(
        db, type=type, user_id=user_id, payload=payload, priority=priority,
        not_before=not_before,
    )

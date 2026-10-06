"""Enqueue for the per-user job types, idempotent under concurrency (DB-06).

`INSERT ... ON CONFLICT (user_id, type) WHERE <partial predicate> DO NOTHING`
against 0056's `uq_jobs_one_active_per_user_type`: two overlapping enqueues (a
double click; Add-PI then an approval) produce one row and no IntegrityError.
`review_feedback_analysis` is not a per-user type (one press enqueues up to 25)
and never comes through here.

Rerun requests (spec 2026-10-05 §4.2): `request_job` is for a caller whose change
must be seen by a run of the job, such as a staff pin that `enrich_grants` reads.
A pending job is row-locked until the caller commits, so it cannot start before the
change is visible, and takes the earlier `not_before` and the higher priority when the
caller states them; a processing job may already have read the old state, so its
`rerun_requested_at` (and `rerun_priority`) is set and the worker
(`src/worker/main.py`) inserts a fresh pending job when it completes or dies. The
exceptions below let a handler refuse (`NonRetryableJobError`) or postpone
(`JobDeferred`) its job without importing the worker."""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import DateTime, SmallInteger, bindparam, case, func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Job
from src.models.job import ONE_ACTIVE_PER_USER_TYPE_WHERE, PER_USER_JOB_TYPES

#: A step the worker runs on the handler's session after the handler's commit
#: (`JobContext.after_commit`, spec §4.3).
AfterCommit = Callable[[AsyncSession], Awaitable[object]]

#: `request_job`'s insert-or-update rounds before it gives up. Each round that fails
#: means a conflicting job ended, or another one committed, between two statements.
REQUEST_JOB_ROUNDS = 3


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


async def _update_active_job(
    db: AsyncSession, *, type: str, user_id: uuid.UUID,
    priority: int | None, not_before: datetime | None,
) -> bool:
    """``request_job``'s UPDATE of the user's pending or processing ``type`` job; True
    when it matched one. A PENDING row is row-locked; a stated ``not_before`` moves it to
    the earlier of the two (a NULL stored value reads as now) and a stated ``priority``
    to the higher (NULL reads as 0). An unstated one leaves the row's value alone, so a
    deferred or backed-off job is not pulled forward and a bulk priority is not lifted.
    A PROCESSING row is flagged, with ``rerun_not_before`` and the higher of its
    ``rerun_priority`` and ``priority``."""
    processing = Job.status == "processing"
    nb = bindparam("rerun_nb", not_before, type_=DateTime(timezone=True))
    prio = bindparam("rerun_prio", priority, type_=SmallInteger)
    values = {
        "rerun_requested_at": case((processing, func.now()), else_=Job.rerun_requested_at),
        "rerun_not_before": case((processing, nb), else_=Job.rerun_not_before),
        # GREATEST ignores NULLs: an unstated priority keeps the stored one.
        "rerun_priority": case(
            (processing, func.greatest(Job.rerun_priority, prio)), else_=Job.rerun_priority,
        ),
    }
    if not_before is not None:
        values["not_before"] = case(
            (processing, Job.not_before),
            else_=func.least(func.coalesce(Job.not_before, func.now()), nb),
        )
    if priority is not None:
        values["priority"] = case(
            (processing, Job.priority),
            else_=func.greatest(func.coalesce(Job.priority, 0), prio),
        )
    result = await db.execute(
        update(Job)
        .where(Job.user_id == user_id, Job.type == type,
               Job.status.in_(("pending", "processing")))
        .values(values)
    )
    return bool(result.rowcount)


async def request_job(
    db: AsyncSession, *, type: str, user_id: uuid.UUID, payload: dict,
    priority: int | None = None, not_before: datetime | None = None,
) -> uuid.UUID | None:
    """Make sure a ``type`` job runs for ``user_id`` after this transaction's change.

    Inserts one (returning its id). On a conflict the existing row is UPDATEd: a PENDING
    row is row-locked, which keeps ``claim_job`` (SKIP LOCKED) from starting it until this
    transaction commits, so it will read the change, and takes the earlier ``not_before``
    and the higher ``priority`` when the caller passes them (None); a PROCESSING row may
    already have read the old state, so it is flagged: ``claim_job`` clears the flag and
    the worker inserts a fresh pending job, not before ``not_before`` and at the higher of
    the two priorities, when the flagged one completes or dies (None). An UPDATE that matches no row means the
    conflicting job finished in between, so the insert is tried again, and an insert that
    conflicts again means another job committed in between, so the UPDATE is; after
    ``REQUEST_JOB_ROUNDS`` rounds this raises RuntimeError. Adds to the caller's
    transaction; the caller commits."""
    for _ in range(REQUEST_JOB_ROUNDS):
        new_id = await insert_job_if_absent(
            db, type=type, user_id=user_id, payload=payload, priority=priority,
            not_before=not_before,
        )
        if new_id is not None:
            return new_id
        if await _update_active_job(
            db, type=type, user_id=user_id, priority=priority, not_before=not_before,
        ):
            return None
    raise RuntimeError(
        f"request_job: no {type} job could be inserted or updated for user {user_id} "
        f"in {REQUEST_JOB_ROUNDS} rounds"
    )

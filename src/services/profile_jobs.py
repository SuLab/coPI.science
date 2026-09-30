"""The one way to enqueue a generate_profile job.

Every web, CLI and script path that starts profile generation goes through
``enqueue_profile_job_if_absent``, so a double click — or manager Add-PI
followed by an approval or a first login before the first pipeline commits —
runs ONE pipeline instead of two. A truly concurrent pair of requests can still
both miss each other's uncommitted row; a partial unique index would be needed
to close that.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import USER_ROLE_MANAGER, USER_ROLE_REVIEWER, Job, User

_NO_PROFILE_ROLES = frozenset({USER_ROLE_MANAGER, USER_ROLE_REVIEWER})
_LIVE_STATUSES = ("pending", "processing")


async def enqueue_profile_job_if_absent(db: AsyncSession, user: User) -> Job | None:
    """The user's live generate_profile job, enqueueing one when none is
    pending or processing.

    Returns None — and enqueues nothing — only for a manager or reviewer (they
    have no research profile) or a denied account. A PENDING-access user still
    gets a job: manager Add-PI, admin impersonation and the CLI create exactly
    those. Adds and flushes; the caller commits. ``user`` must already have an
    id (flush a new User first).
    """
    if user.user_role in _NO_PROFILE_ROLES or user.access_status == "denied":
        return None
    existing = (await db.execute(
        select(Job)
        .where(
            Job.user_id == user.id,
            Job.type == "generate_profile",
            Job.status.in_(_LIVE_STATUSES),
        )
        .order_by(Job.enqueued_at)
        .limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        return existing
    job = Job(
        type="generate_profile",
        user_id=user.id,
        payload={"user_id": str(user.id), "orcid": user.orcid},
    )
    db.add(job)
    await db.flush()
    return job

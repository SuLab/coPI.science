"""The one way to enqueue a generate_profile job.

Every web, CLI and script path that starts profile generation goes through
``enqueue_profile_job_if_absent``, so a double click — or manager Add-PI
followed by an approval or a first login before the first pipeline commits —
runs ONE pipeline instead of two. The insert itself is idempotent under
concurrency (``job_queue.insert_job_if_absent``, backed by 0056's partial unique
index), so a truly concurrent pair also yields one row.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import USER_ROLE_MANAGER, USER_ROLE_REVIEWER, Job, ResearcherProfile, User
from src.services.job_queue import insert_job_if_absent

_NO_PROFILE_ROLES = frozenset({USER_ROLE_MANAGER, USER_ROLE_REVIEWER})
_LIVE_STATUSES = ("pending", "processing")


async def enqueue_profile_job_if_absent(
    db: AsyncSession, user: User, *, priority: int | None = None
) -> Job | None:
    """The user's live generate_profile job, enqueueing one when none is
    pending or processing.

    Returns None — and enqueues nothing — only for a manager or reviewer (they
    have no research profile) or a denied account. A PENDING-access user still
    gets a job: manager Add-PI, admin impersonation and the CLI create exactly
    those. Adds and flushes; the caller commits. ``user`` must already have an
    id (flush a new User first). ``priority`` orders the claim (see
    ``Job.priority``); None is the default, 0.
    """
    if user.user_role in _NO_PROFILE_ROLES or user.access_status == "denied":
        return None
    new_id = await insert_job_if_absent(
        db, type="generate_profile", user_id=user.id,
        payload={"user_id": str(user.id), "orcid": user.orcid}, priority=priority,
    )
    if new_id is not None:
        return await db.get(Job, new_id)
    return (await db.execute(
        select(Job)
        .where(
            Job.user_id == user.id,
            Job.type == "generate_profile",
            Job.status.in_(_LIVE_STATUSES),
        )
        .order_by(Job.enqueued_at)
        .limit(1)
    )).scalar_one_or_none()


def profile_retry_warranted(
    profile: ResearcherProfile | None, latest_job: Job | None
) -> bool:
    """Whether staff may queue this PI's profile generation again (the manager's
    "Retry profile generation"): never while a job is pending or processing; yes
    when the newest generate_profile job is dead, or when there is no profile or
    it is not grounded in a publication abstract — the states the activation gate
    refuses. A grounded profile is not regenerated from here: that would spend a
    pipeline run for nothing the gate needs."""
    if latest_job is not None and latest_job.status in _LIVE_STATUSES:
        return False
    if latest_job is not None and latest_job.status == "dead":
        return True
    return profile is None or profile.evidence_state != "grounded"

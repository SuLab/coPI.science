"""The one way to enqueue a generate_profile job.

Every web, CLI and script path that starts profile generation goes through
``enqueue_profile_job_if_absent``, so a double click — or manager Add-PI
followed by an approval or a first login before the first pipeline commits —
runs ONE pipeline instead of two. The insert itself is idempotent under
concurrency (``job_queue.insert_job_if_absent``, backed by 0056's partial unique
index), so a truly concurrent pair also yields one row.

The four routes a person presses (the PI's Refresh, ``/onboarding/retry``, and the staff
page's Retry and Regenerate) go through ``enqueue_profile_regeneration`` instead. It caps a PI
at ``PROFILE_REGENERATION_DAILY_CAP`` ``generate_profile`` jobs per
``PROFILE_REGENERATION_WINDOW`` (Q25, F2, C8). Operator scripts and the CLI keep calling
``enqueue_profile_job_if_absent`` and are not capped.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import USER_ROLE_MANAGER, USER_ROLE_REVIEWER, Job, ResearcherProfile, User
from src.models.job import INTERACTIVE_PRIORITY
from src.services.advisory_locks import lock_profile_regeneration
from src.services.job_queue import insert_job_if_absent

_NO_PROFILE_ROLES = frozenset({USER_ROLE_MANAGER, USER_ROLE_REVIEWER})
_LIVE_STATUSES = ("pending", "processing")

logger = logging.getLogger(__name__)

#: The most generate_profile jobs one PI may have enqueued in PROFILE_REGENERATION_WINDOW
#: before the four capped routes refuse another (Q25, F2).
PROFILE_REGENERATION_DAILY_CAP = 5
PROFILE_REGENERATION_WINDOW = timedelta(hours=24)
#: The staff routes' refusal, and the Regenerate button's reason (R5.16).
REGENERATION_LIMIT_REASON = (
    f"This PI has had {PROFILE_REGENERATION_DAILY_CAP} profile generations in the last "
    "24 hours, the daily limit; try again later."
)
RegenerationStatus = Literal["queued", "in_flight", "limited", "not_eligible"]


@dataclass(frozen=True)
class RegenerationOutcome:
    status: RegenerationStatus
    job: Job | None
    recent_jobs: int


async def _live_profile_job(db: AsyncSession, user_id: uuid.UUID) -> Job | None:
    """The user's oldest pending or processing generate_profile job, or None."""
    return (await db.execute(
        select(Job)
        .where(
            Job.user_id == user_id,
            Job.type == "generate_profile",
            Job.status.in_(_LIVE_STATUSES),
        )
        .order_by(Job.enqueued_at)
        .limit(1)
    )).scalar_one_or_none()


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

    The role and access status are re-read from the row, not taken from
    ``user``: a staff conversion committed after the caller loaded ``user``
    must not leave a manager or reviewer with a queued pipeline run
    (staff-accounts plan §5.5). The re-read is a column select, so autoflush
    sends the caller's pending edits first and nothing in memory is
    overwritten; an entity select would return the stale identity-map object
    and ``refresh()`` would discard those edits. When the row is not yet in
    the database the in-memory values decide, as before.

    The re-read takes ``FOR SHARE``: a plain read would see the old ``pi`` role
    while a staff conversion holds the row ``FOR UPDATE``, and the job would be
    inserted after the conversion commits. ``FOR SHARE`` waits behind that lock
    and returns the committed role. It does not block other ``FOR SHARE`` or
    ``FOR KEY SHARE`` readers, and is held until the caller's transaction ends.
    A caller that already updated the row holds a stronger lock itself, so the
    re-read does not wait on it.
    """
    stored = (await db.execute(
        select(User.user_role, User.access_status)
        .where(User.id == user.id)
        .with_for_update(read=True)
    )).one_or_none()
    role, access = stored if stored is not None else (user.user_role, user.access_status)
    if role in _NO_PROFILE_ROLES or access == "denied":
        return None
    new_id = await insert_job_if_absent(
        db, type="generate_profile", user_id=user.id,
        payload={"user_id": str(user.id), "orcid": user.orcid}, priority=priority,
    )
    if new_id is not None:
        return await db.get(Job, new_id)
    return await _live_profile_job(db, user.id)


async def recent_profile_generation_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    """The user's generate_profile jobs enqueued in the last PROFILE_REGENERATION_WINDOW,
    whatever their status or source (route, operator script, worker rerun)."""
    return await db.scalar(
        select(func.count()).select_from(Job).where(
            Job.user_id == user_id,
            Job.type == "generate_profile",
            Job.enqueued_at > func.now() - PROFILE_REGENERATION_WINDOW,
        )
    ) or 0


async def enqueue_profile_regeneration(
    db: AsyncSession, user_id: uuid.UUID, *, actor_id: uuid.UUID | None,
) -> RegenerationOutcome:
    """Queue the user's profile generation for one of the four capped routes (C8).

    Under a per-user advisory transaction lock: ``not_eligible`` when there is no such user
    or ``enqueue_profile_job_if_absent`` refuses (a manager, a reviewer, a denied account);
    ``limited`` when no job is live and PROFILE_REGENERATION_DAILY_CAP or more were
    enqueued in the window; otherwise the shared enqueue at INTERACTIVE_PRIORITY. With a
    live job that inserts nothing and only lifts its priority (``in_flight``); without one
    it inserts (``queued``). The lock is held until the caller's transaction ends.
    Flushes; never commits."""
    await lock_profile_regeneration(db, user_id)
    user = await db.get(User, user_id)
    if user is None:
        return RegenerationOutcome("not_eligible", None, 0)
    live = await _live_profile_job(db, user_id)
    recent = await recent_profile_generation_count(db, user_id)
    if live is None and recent >= PROFILE_REGENERATION_DAILY_CAP:
        logger.info(
            "generate_profile for user %s refused: %d jobs in the last 24 h (actor %s)",
            user_id, recent, actor_id,
        )
        return RegenerationOutcome("limited", None, recent)
    job = await enqueue_profile_job_if_absent(db, user, priority=INTERACTIVE_PRIORITY)
    if job is None:
        return RegenerationOutcome("not_eligible", None, recent)
    status: RegenerationStatus = "in_flight" if live is not None else "queued"
    logger.info("generate_profile for user %s %s (actor %s)", user_id, status, actor_id)
    return RegenerationOutcome(status, job, recent)


def profile_retry_warranted(
    profile: ResearcherProfile | None, latest_job: Job | None
) -> bool:
    """Whether staff may queue this PI's profile generation again (the manager's
    "Retry profile generation"): never while a job is pending or processing; yes
    when the newest generate_profile job is dead, or when there is no profile, it is
    not grounded in a publication abstract, or its summary is empty — the states the
    activation gate refuses. A grounded profile is not regenerated from here: that would spend a
    pipeline run for nothing the gate needs."""
    if latest_job is not None and latest_job.status in _LIVE_STATUSES:
        return False
    if latest_job is not None and latest_job.status == "dead":
        return True
    return (
        profile is None or profile.evidence_state != "grounded"
        or not (profile.research_summary or "").strip()
    )

"""Job queue worker process.

Polls the jobs table and executes generate_profile and monthly_refresh jobs.
"""

import asyncio
import logging
import signal
import sys
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import Job, User
from src.services.profile_pipeline import run_profile_pipeline
from src.services.review_bot import execute_review_analysis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_shutdown = False


def _handle_sigterm(*args):
    global _shutdown
    logger.info("Received shutdown signal, finishing current job...")
    _shutdown = True


async def claim_job(db: AsyncSession) -> Job | None:
    """Atomically claim the next pending job."""
    result = await db.execute(
        select(Job)
        .where(Job.status == "pending", Job.attempts < Job.max_attempts)
        .order_by(Job.enqueued_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = result.scalar_one_or_none()
    if not job:
        return None

    job.status = "processing"
    job.started_at = datetime.now(timezone.utc)
    job.attempts += 1
    await db.commit()
    return job


#: A job left in 'processing' longer than this is treated as abandoned by a
#: worker that no longer exists: the compose default stop grace is 10 s and a
#: single review-bot call can run to the 300 s read timeout, so a deploy
#: mid-call SIGKILLs the process with the row still 'processing'. Nothing else
#: ever resets that status, `claim_job` only takes 'pending', and the review
#: enqueue dedupe used to count it as live (audit 2026-09-02, D4). The longest
#: legitimate job is a review analysis that hits the read timeout on all three
#: SDK attempts (~15 min); 30 min leaves that margin.
STALE_PROCESSING_SECONDS = 1800

#: How often `run_worker` re-checks (it also checks once at boot with 0).
STALE_CHECK_INTERVAL_SECONDS = 60


async def requeue_stale_processing_jobs(
    db: AsyncSession, *, older_than_seconds: int = STALE_PROCESSING_SECONDS
) -> int:
    """Return abandoned 'processing' rows to the queue; returns how many moved.

    Rows whose attempts are exhausted go to 'dead' rather than 'pending': a
    pending row `claim_job` can never take (attempts >= max_attempts) would sit
    in the queue forever, and `enqueue_analysis_if_absent` counts pending rows
    when deciding whether to enqueue. A NULL `started_at` on a processing row
    is treated as stale too — `claim_job` always sets it, so NULL means the
    row was never claimed by this code path.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=older_than_seconds)
    stale = (
        Job.status == "processing",
        or_(Job.started_at.is_(None), Job.started_at <= cutoff),
    )
    note = (
        "requeued: found in 'processing' past the stale cutoff with no worker on it "
        "(the previous worker exited mid-job)"
    )
    dead = await db.execute(
        update(Job)
        .where(*stale, Job.attempts >= Job.max_attempts)
        .values(status="dead", last_error=note, completed_at=now)
    )
    pending = await db.execute(
        update(Job)
        .where(*stale, Job.attempts < Job.max_attempts)
        .values(status="pending", last_error=note)
    )
    await db.commit()
    n_dead = dead.rowcount or 0
    n_pending = pending.rowcount or 0
    if n_dead or n_pending:
        logger.warning(
            "Requeued %d stale processing job(s): %d back to pending, %d dead",
            n_dead + n_pending, n_pending, n_dead,
        )
    return n_dead + n_pending


async def execute_generate_profile(job: Job, db: AsyncSession) -> None:
    """Execute a generate_profile job."""
    payload = job.payload or {}
    user_id_str = payload.get("user_id") or str(job.user_id)
    if not user_id_str:
        raise ValueError("Job missing user_id in payload")

    user_id = uuid.UUID(user_id_str)

    # Verify user exists
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise ValueError(f"User {user_id} not found")

    logger.info("Running profile pipeline for user %s (%s)", user_id, user.name)
    await run_profile_pipeline(user_id=user_id, db=db, job=job)
    logger.info("Profile pipeline complete for user %s", user_id)


async def execute_monthly_refresh(job: Job, db: AsyncSession) -> None:
    """Execute a monthly_refresh job — same as generate_profile for now."""
    await execute_generate_profile(job, db)


async def process_job(job_id: uuid.UUID, job_type: str, job_attempts: int, job_max_attempts: int, session_factory: async_sessionmaker) -> None:
    """Process a single job. Handles errors and updates job status.

    The jobs row can vanish at any await: jobs.user_id is ON DELETE CASCADE,
    so an account deletion mid-run takes the row with it (deletion audit F10).
    Both the initial re-fetch and the failure bookkeeping tolerate that — the
    account is gone, so there is no state anyone still needs updated.
    """
    async with session_factory() as db:
        # Re-fetch the job in this session so SQLAlchemy tracks changes
        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if job is None:
            logger.info(
                "Job %s no longer exists (user deleted between claim and "
                "processing); skipping", job_id,
            )
            return

        try:
            if job.type == "generate_profile":
                await execute_generate_profile(job, db)
            elif job.type == "monthly_refresh":
                await execute_monthly_refresh(job, db)
            elif job.type == "review_feedback_analysis":
                await execute_review_analysis(job, db)
            else:
                raise ValueError(f"Unknown job type: {job.type}")

            job.status = "completed"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info("Job %s completed", job.id)

        except Exception as exc:
            logger.error("Job %s failed: %s", job_id, exc, exc_info=True)
            # The pipeline may have left the transaction aborted (e.g. an FK
            # violation after a concurrent user delete) — clear it before the
            # bookkeeping writes, then re-check the row still exists.
            await db.rollback()
            result = await db.execute(select(Job).where(Job.id == job_id))
            job = result.scalar_one_or_none()
            if job is None:
                logger.info(
                    "Job %s row is gone (user deleted mid-run); "
                    "dropping the result", job_id,
                )
                return
            job.last_error = str(exc)[:2000]

            if job.attempts >= job.max_attempts:
                job.status = "dead"
                logger.warning("Job %s marked as dead after %d attempts", job_id, job.attempts)
            else:
                job.status = "pending"  # Will be retried

            job.completed_at = datetime.now(timezone.utc)
            try:
                await db.commit()
            except Exception:
                # Lost a second race on the same delete; nothing left to save.
                await db.rollback()
                logger.info("Job %s vanished during failure bookkeeping", job_id)


async def run_worker():
    """Main worker loop."""
    global _shutdown

    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    logger.info("Worker started, polling every %ds", settings.worker_poll_interval)

    # Boot: this is the only worker instance, so every 'processing' row is a
    # zombie from a previous process — take them all, whatever their age.
    async with session_factory() as db:
        await requeue_stale_processing_jobs(db, older_than_seconds=0)
    last_stale_check = asyncio.get_event_loop().time()

    last_notification_check = 0.0
    last_inbound_check = 0.0

    while not _shutdown:
        try:
            async with session_factory() as db:
                job = await claim_job(db)

            if job:
                logger.info("Processing job %s (type=%s)", job.id, job.type)
                await process_job(job.id, job.type, job.attempts, job.max_attempts, session_factory)
            else:
                # No jobs, sleep before polling again
                await asyncio.sleep(settings.worker_poll_interval)

            # Email notification check (throttled)
            now = asyncio.get_event_loop().time()
            if now - last_notification_check >= settings.notification_check_interval:
                last_notification_check = now
                try:
                    from src.services.email_notifications import (
                        check_and_send_new_proposal_emails,
                        check_and_send_notifications,
                        check_and_send_status_overviews,
                    )
                    sent = await check_and_send_notifications(session_factory)
                    if sent:
                        logger.info("Sent %d proposal notification email(s)", sent)
                    new_proposals = await check_and_send_new_proposal_emails(session_factory)
                    if new_proposals:
                        logger.info("Sent %d new-proposal email(s)", new_proposals)
                    overviews = await check_and_send_status_overviews(session_factory)
                    if overviews:
                        logger.info("Sent %d status-overview email(s)", overviews)
                except Exception as exc:
                    logger.error("Notification check error: %s", exc, exc_info=True)

            # Inbound email check (throttled, opt-in)
            if settings.enable_inbound_email and now - last_inbound_check >= settings.inbound_poll_interval:
                last_inbound_check = now
                try:
                    from src.services.email_inbound import poll_inbound_emails
                    await poll_inbound_emails(session_factory)
                except Exception as exc:
                    logger.error("Inbound email check error: %s", exc, exc_info=True)

            # Stale-processing sweep (throttled).
            if now - last_stale_check >= STALE_CHECK_INTERVAL_SECONDS:
                last_stale_check = now
                async with session_factory() as db:
                    await requeue_stale_processing_jobs(db)

        except Exception as exc:
            logger.error("Worker loop error: %s", exc, exc_info=True)
            await asyncio.sleep(settings.worker_poll_interval)

    logger.info("Worker shutting down")
    await engine.dispose()


def main():
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()

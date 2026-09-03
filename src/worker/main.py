"""Job queue worker process.

Polls the jobs table and executes generate_profile and monthly_refresh jobs.
"""

import asyncio
import logging
import signal
import sys
import uuid
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agent.ids import WRITER_WORKER, set_default_writer_id
from src.config import get_settings
from src.database import make_engine
from src.models import Job, User
from src.services.profile_pipeline import run_profile_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_shutdown = False

# Exponential backoff between retry attempts of the SAME job. Module constants, not Settings
# fields, so a test can monkeypatch worker_main.JOB_RETRY_BACKOFF_BASE_SECONDS directly (mirrors
# src/services/slack_web.py's _BACKOFF_BASE) without needing every existing bespoke
# SimpleNamespace(...) stand-in for get_settings() in this test file to grow a new field.
JOB_RETRY_BACKOFF_BASE_SECONDS = 5.0
JOB_RETRY_BACKOFF_CAP_SECONDS = 300.0

# Stale-processing reaper (COR-18a/b): a worker that dies mid-job (OOM, SIGKILL, host
# crash) leaves its claimed row committed 'processing' forever — claim_job only ever
# selects 'pending' rows, so nothing else in the system will ever pick it back up.
#
# 3600s, not 900s (controller ruling, #23.12 review): after Tasks 23.11/23.12/23.13 every
# outbound HTTP client this pipeline calls retries. One _ncbi_get can take up to 4 attempts
# × 60s timeout + backoff ≈ 243s, and convert_dois_to_pmids issues one such call per
# unresolved DOI SEQUENTIALLY, on top of up to 10 PMC methods fetches and three retried
# ORCID calls — so under a sustained NCBI/ORCID brownout a single profile job can
# legitimately exceed 900s. A purely time-based reaper set at 900s would re-queue a job
# whose worker is still running it, causing duplicate synthesis + LLM spend. A heartbeat
# (periodic started_at bump) is the full fix; raising the threshold is a stopgap.
JOB_STALE_PROCESSING_THRESHOLD_SECONDS = 3600   # 1 hour
JOB_REAP_CHECK_INTERVAL_SECONDS = 300           # mirrors notification_check_interval's cadence


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


async def reap_stale_jobs(session_factory: async_sessionmaker) -> int:
    """Re-queue jobs stuck in 'processing' longer than the stale threshold.

    A worker that dies mid-job (OOM, SIGKILL, host crash) leaves its claimed row committed
    'processing' by claim_job — claim_job only ever selects 'pending' rows, so nothing else in
    the system will ever pick this job back up (COR-18a/b). started_at is the column claim_job
    already writes and nothing has read until now.

    Single-replica invariant: this reaper is only safe because run_worker is serial and
    single-replica. It is reached only after process_job returns (see run_worker's loop), so a
    call to this function can never see this SAME process's own in-flight row — by the time it
    runs, this process has no job 'processing'. With 2+ worker replicas that guarantee is gone: a
    job legitimately still running on one replica, past the threshold, would be reaped out from
    under it by another replica's reaper and executed a second time. Before running more than one
    worker replica, either add a heartbeat (a periodic started_at bump from inside process_job
    while the job is still in flight) or raise JOB_STALE_PROCESSING_THRESHOLD_SECONDS above the
    worst-case job duration across ALL replicas, not just one.
    """
    reaped = 0
    threshold = datetime.now(UTC) - timedelta(
        seconds=JOB_STALE_PROCESSING_THRESHOLD_SECONDS
    )
    async with session_factory() as db:
        result = await db.execute(
            select(Job).where(Job.status == "processing", Job.started_at < threshold)
        )
        stale_jobs = result.scalars().all()

    for job_id in (j.id for j in stale_jobs):
        try:
            async with session_factory() as db:
                job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
                if job.status != "processing" or job.started_at is None or job.started_at >= threshold:
                    continue  # claimed/completed by a live worker between the SELECT and here
                if job.attempts >= job.max_attempts:
                    job.status = "failed"
                    logger.warning(
                        "Reaped stale job %s (attempts exhausted) after over %ds in 'processing'",
                        job.id, JOB_STALE_PROCESSING_THRESHOLD_SECONDS,
                    )
                else:
                    job.status = "pending"
                    logger.warning(
                        "Reaped stale job %s stuck in 'processing' for over %ds — re-queued",
                        job.id, JOB_STALE_PROCESSING_THRESHOLD_SECONDS,
                    )
                job.last_error = (
                    f"Reaped: stuck in 'processing' for over "
                    f"{JOB_STALE_PROCESSING_THRESHOLD_SECONDS}s (worker likely crashed)"
                )
                await db.commit()
                reaped += 1
        except Exception as exc:
            logger.error("Failed to reap job %s: %s", job_id, exc, exc_info=True)
    return reaped


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
    """Process a single job. Handles errors and updates job status."""
    async with session_factory() as db:
        # Re-fetch the job in this session so SQLAlchemy tracks changes
        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one()

        try:
            if job.type == "generate_profile":
                await execute_generate_profile(job, db)
            elif job.type == "monthly_refresh":
                await execute_monthly_refresh(job, db)
            else:
                raise ValueError(f"Unknown job type: {job.type}")

            job.status = "completed"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info("Job %s completed", job.id)

        except Exception as exc:
            # job.id (not job_id, the function's own parameter) is UNSAFE here: a flush
            # failure anywhere in this session (e.g. run_profile_pipeline's own db.flush())
            # expires every attribute of every object tracked by the session, INCLUDING
            # job's primary key, before this except block ever runs — reproduced directly:
            # accessing job.id at this exact point, before any rollback, raises
            # sqlalchemy.exc.PendingRollbackError ("this Session's transaction has been
            # rolled back due to a previous exception during flush"). process_job already
            # has the id as a plain parameter; use that instead of touching the ORM object.
            logger.error("Job %s failed: %s", job_id, exc, exc_info=True)
            # A failure in run_profile_pipeline may have poisoned the transaction
            # (e.g. a flush-time IntegrityError) — roll back before writing the
            # failure record, or this commit itself raises PendingRollbackError,
            # which escapes process_job and strands the job in 'processing' until
            # the stale-processing reaper re-queues it after
            # JOB_STALE_PROCESSING_THRESHOLD_SECONDS (COR-17; see
            # tests/integration/test_worker.py).
            await db.rollback()
            # rollback() expires every instrumented attribute on `job` (SQLAlchemy does this
            # unconditionally, regardless of expire_on_commit). On a SYNC session the very next
            # attribute access would trigger an implicit lazy SELECT; on this ASYNC session
            # (AsyncSession, no greenlet context outside an explicit `await`) that same implicit
            # access instead raises `sqlalchemy.exc.MissingGreenlet` — mirror the dossier's C.2
            # pattern (re-fetch via an explicit, awaitable refresh rather than touching the
            # stale object's attributes bare):
            await db.refresh(job)
            job.last_error = str(exc)[:2000]

            if job.attempts >= job.max_attempts:
                # 'failed' (not 'dead'): the enum's 'failed' value is what
                # templates/onboarding/profile_review.html's self-service "Try
                # Again" button keys on (COR-18e/f) — 'dead' matches no branch
                # there and rendered a blank page with no explanation.
                job.status = "failed"
                logger.warning("Job %s marked as failed after %d attempts", job.id, job.attempts)
                await db.commit()
            else:
                job.status = "pending"  # Will be retried
                await db.commit()
                # Exponential backoff so the next claim_job (ordered by enqueued_at) doesn't
                # reclaim THIS job again immediately — without this all max_attempts burn
                # back-to-back with zero delay (COR-18c).
                delay = min(
                    JOB_RETRY_BACKOFF_CAP_SECONDS,
                    JOB_RETRY_BACKOFF_BASE_SECONDS * (2 ** max(0, job.attempts - 1)),
                )
                logger.info(
                    "Job %s will retry in %.1fs (attempt %d/%d)",
                    job.id, delay, job.attempts, job.max_attempts,
                )
                # Sleep in 1s slices so a SIGTERM (_shutdown, set by _handle_sigterm) is
                # honoured instead of blocking run_worker's whole loop for up to
                # JOB_RETRY_BACKOFF_CAP_SECONDS: docker-compose.prod.yml's worker service
                # sets no stop_grace_period, so Docker's 10s SIGKILL default would otherwise
                # hard-kill the container mid-backoff on a `docker compose up -d --build
                # worker` recreate (no data lost either way — the commit above already
                # landed — but this makes the shutdown graceful instead of a hard kill).
                waited = 0.0
                while waited < delay and not _shutdown:
                    step = min(1.0, delay - waited)
                    await asyncio.sleep(step)
                    waited += step
            # completed_at is NOT set here (COR-18d): it now means "genuinely completed",
            # matching how templates/admin/jobs.html and user_detail.html display it. A job
            # that failed or is retrying has not completed.


async def run_worker():
    """Main worker loop."""
    global _shutdown

    settings = get_settings()
    engine = make_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    logger.info("Worker started, polling every %ds", settings.worker_poll_interval)

    last_notification_check = 0.0
    last_inbound_check = 0.0
    last_reap_check = 0.0

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

            now = asyncio.get_event_loop().time()

            # Stale-processing reaper (throttled) — COR-18a/b
            if now - last_reap_check >= JOB_REAP_CHECK_INTERVAL_SECONDS:
                last_reap_check = now
                try:
                    reaped = await reap_stale_jobs(session_factory)
                    if reaped:
                        logger.warning("Reaped %d stale job(s) stuck in 'processing'", reaped)
                except Exception as exc:
                    logger.error("Stale-job reaper error: %s", exc, exc_info=True)

            # Email notification check (throttled)
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

        except Exception as exc:
            logger.error("Worker loop error: %s", exc, exc_info=True)
            await asyncio.sleep(settings.worker_poll_interval)

    logger.info("Worker shutting down")
    await engine.dispose()


def main():
    # Claim this process's canonical-id writer slot before anything mints. The
    # worker only mints when handling an inbound-email PI reply (record_pi_message /
    # migrate_public_thread_to_private, both gated on ENABLE_INBOUND_EMAIL) — without
    # this it mints in the module default's WRITER_WEB residue class and can collide
    # with the web app (R1). See src/agent/ids.py.
    set_default_writer_id(WRITER_WORKER)
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()

"""Job queue worker process.

Polls the jobs table and executes generate_profile and monthly_refresh jobs.
"""

import asyncio
import logging
import signal
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings
from src.models import Job, User
from src.services.profile_pipeline import run_profile_pipeline

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
            logger.error("Job %s failed: %s", job.id, exc, exc_info=True)
            job.last_error = str(exc)[:2000]

            if job.attempts >= job.max_attempts:
                job.status = "dead"
                logger.warning("Job %s marked as dead after %d attempts", job.id, job.attempts)
            else:
                job.status = "pending"  # Will be retried

            job.completed_at = datetime.now(timezone.utc)
            await db.commit()


async def run_worker():
    """Main worker loop."""
    global _shutdown

    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    logger.info("Worker started, polling every %ds", settings.worker_poll_interval)

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

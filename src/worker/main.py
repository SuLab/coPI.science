"""Job queue worker process.

Polls the jobs table and executes the handlers in `JOB_HANDLERS`
(generate_profile, review_feedback_analysis, enrich_grants, industry_evidence).
`monthly_refresh` is retired and fails loudly.
"""

import asyncio
import logging
import signal
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from src.config import get_settings
from src.database import make_engine
from src.models import Job, User
from src.services import job_progress
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
        .where(
            Job.status == "pending",
            Job.attempts < Job.max_attempts,
            # A job re-queued after a failure waits out its backoff.
            or_(Job.not_before.is_(None), Job.not_before <= func.now()),
        )
        # Interactive work (priority 10) before NULL/0 before bulk (-10); age breaks ties.
        .order_by(func.coalesce(Job.priority, 0).desc(), Job.enqueued_at)
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
#: worker that no longer exists. The worker now has a 330 s `stop_grace_period`
#: (working-tree `docker-compose.prod.yml`), which covers ONE 300 s Anthropic
#: read timeout and no more: the SDK retries twice on top of that
#: (`DEFAULT_MAX_RETRIES = 2`, never overridden in `src/services/llm.py`), so a
#: deploy landing on the retry tail is still SIGKILLed with the row still
#: 'processing'. Nothing else ever resets that status, `claim_job` only takes
#: 'pending', and the review enqueue dedupe used to count it as live (audit
#: 2026-09-02, D4) — the boot sweep below is what reclaims the row when that
#: happens, and the grace period only makes it happen less often. The longest
#: legitimate job is a review analysis that hits the read timeout on all three
#: SDK attempts (~15 min); 30 min leaves that margin.
STALE_PROCESSING_SECONDS = 1800

#: How often `run_worker` re-checks (it also checks once at boot with 0).
STALE_CHECK_INTERVAL_SECONDS = 60

#: Retry backoff: a job that failed and will be retried is not claimable again
#: for `retry_delay(attempts)` — 4 minutes after the first failure, 16 after the
#: second. The third failure is dead. An immediate retry used to spend all three
#: attempts inside one upstream outage.
RETRY_BACKOFF_BASE = timedelta(minutes=4)
RETRY_BACKOFF_FACTOR = 4


def retry_delay(attempts: int) -> timedelta:
    """How long a job that has failed ``attempts`` times waits before its next claim."""
    return RETRY_BACKOFF_BASE * (RETRY_BACKOFF_FACTOR ** max(attempts - 1, 0))


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


@dataclass(frozen=True)
class JobContext:
    """What a handler needs from its jobs row, detached from any session: the
    handler's transaction must never modify that row (DP-09)."""

    id: uuid.UUID
    type: str
    user_id: uuid.UUID | None
    payload: dict
    attempts: int
    max_attempts: int


async def execute_generate_profile(ctx: JobContext, db: AsyncSession) -> None:
    """Execute a generate_profile job."""
    user_id_str = ctx.payload.get("user_id") or (str(ctx.user_id) if ctx.user_id else None)
    if not user_id_str:
        raise ValueError("Job missing user_id in payload")

    user_id = uuid.UUID(user_id_str)

    # Verify user exists
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise ValueError(f"User {user_id} not found")

    logger.info("Running profile pipeline for user %s (%s)", user_id, user.name)
    await run_profile_pipeline(user_id=user_id, db=db, job_id=ctx.id)
    logger.info("Profile pipeline complete for user %s", user_id)


class RetiredJobType(ValueError):
    """A job type whose enum value stays (C2: Postgres cannot drop enum values)
    but which no code path enqueues any more. Dispatching one fails the job
    through the normal failure bookkeeping instead of silently regenerating."""


async def execute_monthly_refresh(ctx, db) -> None:
    """Retired (spec §9.2, DP-16). Nothing has enqueued `monthly_refresh` since the
    retry-and-refresh paths moved to `generate_profile`; a legacy row fails loudly."""
    raise RetiredJobType("retired job type: monthly_refresh")


async def _execute_review_analysis(ctx: JobContext, db: AsyncSession) -> None:
    await execute_review_analysis(ctx, db)


async def _execute_enrich_grants(ctx: JobContext, db: AsyncSession) -> None:
    from src.services.grant_enrichment import execute_enrich_grants
    await execute_enrich_grants(ctx, db)


async def _execute_industry_evidence(ctx: JobContext, db: AsyncSession) -> None:
    from src.services.industry_evidence import execute_industry_evidence
    await execute_industry_evidence(ctx, db)


#: job type -> handler. Every live `job_type_enum` value except the retired
#: `monthly_refresh` (tests/unit/test_job_handlers.py pins the coverage).
JOB_HANDLERS = {
    "generate_profile": execute_generate_profile,
    "review_feedback_analysis": _execute_review_analysis,
    "enrich_grants": _execute_enrich_grants,
    "industry_evidence": _execute_industry_evidence,
}


async def _mark_completed(session_factory: async_sessionmaker, job_id: uuid.UUID) -> None:
    async with session_factory() as db:
        await db.execute(
            update(Job).where(Job.id == job_id)
            .values(status="completed", completed_at=datetime.now(timezone.utc))
        )
        await db.commit()


async def _mark_failed(
    session_factory: async_sessionmaker, job_id: uuid.UUID, exc: BaseException
) -> None:
    """Failure bookkeeping in its own transaction: dead at max attempts, else
    pending behind the retry backoff. The row can already be gone (account
    deletion cascades it), which is tolerated."""
    async with session_factory() as db:
        job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
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
            job.status = "pending"  # Will be retried after the backoff
            job.not_before = func.now() + retry_delay(job.attempts)
            logger.info(
                "Job %s will be retried after %s (attempt %d of %d failed)",
                job_id, retry_delay(job.attempts), job.attempts, job.max_attempts,
            )

        job.completed_at = datetime.now(timezone.utc)
        try:
            await db.commit()
        except Exception:
            # Lost a second race on the same delete; nothing left to save.
            await db.rollback()
            logger.info("Job %s vanished during failure bookkeeping", job_id)


async def process_job(job_id: uuid.UUID, job_type: str, job_attempts: int, job_max_attempts: int, session_factory: async_sessionmaker) -> None:
    """Run one job. The handler's transaction holds only the handler's writes: the
    jobs row is read once (detached into a JobContext) and its status is written in
    a separate transaction AFTER the handler commits. A crash between the two
    leaves the row 'processing', which the stale sweep re-queues.

    The jobs row can vanish at any await: jobs.user_id is ON DELETE CASCADE,
    so an account deletion mid-run takes the row with it (deletion audit F10).
    Both the initial re-fetch and the failure bookkeeping tolerate that — the
    account is gone, so there is no state anyone still needs updated.
    """
    async with session_factory() as db:
        row = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
        if row is None:
            logger.info(
                "Job %s no longer exists (user deleted between claim and "
                "processing); skipping", job_id,
            )
            return
        ctx = JobContext(
            id=row.id, type=row.type, user_id=row.user_id, payload=dict(row.payload or {}),
            attempts=row.attempts, max_attempts=row.max_attempts,
        )
        await db.commit()

        try:
            if ctx.type == "monthly_refresh":
                await execute_monthly_refresh(ctx, db)
            handler = JOB_HANDLERS.get(ctx.type)
            if handler is None:
                raise ValueError(f"Unknown job type: {ctx.type}")
            await handler(ctx, db)
            await db.commit()
        except Exception as exc:
            logger.error("Job %s failed: %s", job_id, exc, exc_info=True)
            # The pipeline may have left the transaction aborted (e.g. an FK
            # violation after a concurrent user delete) — clear it before the
            # bookkeeping writes.
            await db.rollback()
            await _mark_failed(session_factory, job_id, exc)
            return
    await _mark_completed(session_factory, job_id)
    logger.info("Job %s completed", job_id)


class WorkerAlreadyRunning(RuntimeError):
    """Another worker holds WORKER_LOCK_KEY; this process exits before touching
    the queue (its boot sweep would re-queue the live worker's 'processing' row)."""


async def acquire_worker_lock(engine: AsyncEngine) -> AsyncConnection | None:
    """Take the worker advisory lock on a dedicated AUTOCOMMIT connection that stays
    open for the process lifetime (outside any transaction, so
    idle_in_transaction_session_timeout cannot drop it). None if another holds it."""
    from src.services.advisory_locks import WORKER_LOCK_KEY

    conn = await engine.connect()
    await conn.execution_options(isolation_level="AUTOCOMMIT")
    got = (await conn.execute(
        text("SELECT pg_try_advisory_lock(CAST(:k AS bigint))"), {"k": WORKER_LOCK_KEY}
    )).scalar_one()
    if not got:
        await conn.close()
        return None
    return conn


async def run_worker():
    """Main worker loop."""
    global _shutdown

    settings = get_settings()
    engine = make_engine("worker")
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    lock_conn = await acquire_worker_lock(engine)
    if lock_conn is None:
        logger.error("Another worker holds the worker lock; exiting before the stale sweep")
        await engine.dispose()
        raise WorkerAlreadyRunning("worker lock held by another process")
    job_progress.configure(session_factory)

    logger.info("Worker started, polling every %ds", settings.worker_poll_interval)

    # Boot: the worker lock guarantees this is the only worker instance, so every
    # 'processing' row is a zombie from a previous process — take them all,
    # whatever their age.
    async with session_factory() as db:
        await requeue_stale_processing_jobs(db, older_than_seconds=0)
    last_stale_check = asyncio.get_event_loop().time()

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

            # Stale-processing sweep (throttled).
            if now - last_stale_check >= STALE_CHECK_INTERVAL_SECONDS:
                last_stale_check = now
                async with session_factory() as db:
                    await requeue_stale_processing_jobs(db)

        except Exception as exc:
            logger.error("Worker loop error: %s", exc, exc_info=True)
            await asyncio.sleep(settings.worker_poll_interval)

    logger.info("Worker shutting down")
    await lock_conn.close()
    await engine.dispose()


def main():
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()

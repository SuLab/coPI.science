"""Job queue worker process.

Polls the jobs table and executes the handlers in `JOB_HANDLERS`
(generate_profile, review_feedback_analysis, enrich_grants, industry_evidence,
company_discovery).
`monthly_refresh` is retired and fails loudly.

While no job is waiting it also generates the assessment chat's opening questions,
one set at a time (`src.services.assessment_chat_suggestions.generate_due`), so a job
waits behind at most one of those calls (CALL_TIMEOUT_SECONDS, 3 minutes, at worst),
and runs the daily persona re-export sweep when it is due
(`src.services.persona_sweep.run_persona_sweep`, spec 2026-10-05 D54).

A BULK-priority job that spends OpenAlex credits first reads OpenAlex's free daily
meter and defers itself to the meter's reset when it would eat into the reserve kept
for org1 (`src.services.openalex_budget`, spec 2026-10-05 D67).

A handler may raise `job_queue.NonRetryableJobError` (dead at once) or
`job_queue.JobDeferred` (back to pending, attempt not counted), and may append
post-commit steps to `JobContext.after_commit`. A `request_job` call that flagged a
processing job gets its fresh job when that job completes or dies (spec 2026-10-05 §4.2).
"""

import asyncio
import logging
import signal
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

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
from src.models.job import BULK_PRIORITY, PER_USER_JOB_TYPES
from src.services import job_progress, openalex_budget
from src.services.assessment_chat_suggestions import (
    SWEEP_INTERVAL_SECONDS as SUGGESTION_SWEEP_SECONDS,
)
from src.services.assessment_chat_suggestions import generate_due as generate_chat_suggestions
from src.services.job_queue import (
    AfterCommit,
    JobDeferred,
    NonRetryableJobError,
    insert_job_if_absent,
)
from src.services.persona_sweep import (
    PERSONA_SWEEP_RECHECK_SECONDS,
    PERSONA_SWEEP_SECONDS,
    run_persona_sweep,
)
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
    """Atomically claim the next pending job. A row an uncommitted `request_job` holds
    locked is skipped until that transaction ends."""
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
    job.started_at = datetime.now(UTC)
    job.attempts += 1
    job.rerun_requested_at = None  # this run will see every change committed before now
    job.rerun_not_before = None
    job.rerun_priority = None
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
    row was never claimed by this code path. A dead row whose rerun was requested
    gets its fresh pending job in the same transaction; a row back to pending has
    its request cleared (the retry will see the change).
    """
    now = datetime.now(UTC)
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
        .returning(Job.type, Job.user_id, Job.payload, Job.priority,
                   Job.rerun_requested_at, Job.rerun_not_before, Job.rerun_priority)
    )
    dead_rows = dead.all()
    pending = await db.execute(
        update(Job)
        .where(*stale, Job.attempts < Job.max_attempts)
        .values(status="pending", last_error=note,
                rerun_requested_at=None, rerun_not_before=None, rerun_priority=None)
    )
    for row in dead_rows:
        await _spawn_requested_rerun(
            db, job_type=row.type, user_id=row.user_id, payload=row.payload,
            priority=row.priority, rerun_requested_at=row.rerun_requested_at,
            rerun_not_before=row.rerun_not_before, rerun_priority=row.rerun_priority,
        )
    await db.commit()
    n_dead = len(dead_rows)
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
    #: Async callables process_job runs, in order, with the handler's session after the
    #: handler's commit (spec §4.3): the post-commit persona write, which opens its own
    #: session on that session's bind.
    after_commit: list[AfterCommit] = field(default_factory=list, compare=False)


async def _spawn_requested_rerun(
    db: AsyncSession, *, job_type: str, user_id: uuid.UUID | None, payload: dict | None,
    priority: int | None, rerun_requested_at: datetime | None,
    rerun_not_before: datetime | None, rerun_priority: int | None = None,
) -> uuid.UUID | None:
    """The fresh job a `request_job` call asked for while this one ran (spec §4.2):
    the payload minus `progress`, the higher of `priority` and `rerun_priority` (NULLs
    ignored, as SQL GREATEST), `not_before = rerun_not_before`. Call after the ending
    status flip is flushed, so the one-active-job index allows it."""
    if rerun_requested_at is None or user_id is None or job_type not in PER_USER_JOB_TYPES:
        return None
    fresh = {k: v for k, v in (payload or {}).items() if k != "progress"}
    stated = [p for p in (priority, rerun_priority) if p is not None]
    new_id = await insert_job_if_absent(
        db, type=job_type, user_id=user_id, payload=fresh,
        priority=max(stated) if stated else None, not_before=rerun_not_before,
    )
    logger.info(
        "Rerun of %s for user %s requested during the last run: job %s",
        job_type, user_id, new_id,
    )
    return new_id


async def _spawn_for(db: AsyncSession, job: Job) -> None:
    """Flush ``job``'s ending status, then spawn its requested rerun, if any."""
    await db.flush()
    await _spawn_requested_rerun(
        db, job_type=job.type, user_id=job.user_id, payload=job.payload,
        priority=job.priority, rerun_requested_at=job.rerun_requested_at,
        rerun_not_before=job.rerun_not_before, rerun_priority=job.rerun_priority,
    )


def _followon_not_before(payload: dict) -> datetime | None:
    """`_bulk_enqueue`'s slot for this parent's step-10 follow-ons (spec §4.2), if any:
    an ISO 8601 string, read as UTC when it has no offset. None when absent or invalid."""
    raw = payload.get("followon_not_before")
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


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
    await run_profile_pipeline(
        user_id=user_id, db=db, job_id=ctx.id, after_commit=ctx.after_commit,
        followon_not_before=_followon_not_before(ctx.payload),
    )
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


async def _execute_company_discovery(ctx: JobContext, db: AsyncSession) -> None:
    from src.services.company_discovery import execute_company_discovery
    await execute_company_discovery(ctx, db)


#: job type -> handler. Every live `job_type_enum` value except the retired
#: `monthly_refresh` (tests/unit/test_job_handlers.py pins the coverage).
JOB_HANDLERS = {
    "generate_profile": execute_generate_profile,
    "review_feedback_analysis": _execute_review_analysis,
    "enrich_grants": _execute_enrich_grants,
    "industry_evidence": _execute_industry_evidence,
    "company_discovery": _execute_company_discovery,
}


async def _mark_completed(session_factory: async_sessionmaker, job_id: uuid.UUID) -> None:
    """Mark the job completed and spawn its requested rerun, if any. A row already
    gone (account deletion cascades it) is tolerated."""
    async with session_factory() as db:
        # FOR UPDATE: waits for a request_job UPDATE in flight on this row, then sees its flag.
        job = (await db.execute(
            select(Job).where(Job.id == job_id).with_for_update()
        )).scalar_one_or_none()
        if job is None:
            return
        job.status = "completed"
        job.completed_at = datetime.now(UTC)
        await _spawn_for(db, job)
        await db.commit()


async def _mark_failed(
    session_factory: async_sessionmaker, job_id: uuid.UUID, exc: BaseException
) -> None:
    """Failure bookkeeping in its own transaction: dead at max attempts or on a
    `NonRetryableJobError` (spawning a requested rerun), else pending behind the
    retry backoff with any rerun request cleared (the retry will see the change).
    The row can already be gone (account deletion cascades it), which is tolerated."""
    async with session_factory() as db:
        # FOR UPDATE: waits for a request_job UPDATE in flight on this row, then sees its flag.
        job = (await db.execute(
            select(Job).where(Job.id == job_id).with_for_update()
        )).scalar_one_or_none()
        if job is None:
            logger.info(
                "Job %s row is gone (user deleted mid-run); "
                "dropping the result", job_id,
            )
            return
        job.last_error = str(exc)[:2000]

        job.completed_at = datetime.now(UTC)
        if isinstance(exc, NonRetryableJobError) or job.attempts >= job.max_attempts:
            job.status = "dead"
            if isinstance(exc, NonRetryableJobError):
                logger.warning("Job %s marked as dead (non-retryable)", job_id)
            else:
                logger.warning("Job %s marked as dead after %d attempts", job_id, job.attempts)
            await _spawn_for(db, job)
        else:
            job.status = "pending"  # Will be retried after the backoff
            job.not_before = func.now() + retry_delay(job.attempts)
            job.rerun_requested_at = None
            job.rerun_not_before = None
            job.rerun_priority = None
            logger.info(
                "Job %s will be retried after %s (attempt %d of %d failed)",
                job_id, retry_delay(job.attempts), job.attempts, job.max_attempts,
            )

        try:
            await db.commit()
        except Exception:
            # Lost a second race on the same delete; nothing left to save.
            await db.rollback()
            logger.info("Job %s vanished during failure bookkeeping", job_id)


async def _mark_deferred(
    session_factory: async_sessionmaker, job_id: uuid.UUID, exc: JobDeferred
) -> None:
    """A handler deferred its own job (spec §4.2): back to pending until exc.not_before,
    without counting the attempt claim_job added. A retry-like return: flags cleared."""
    async with session_factory() as db:
        job = (await db.execute(
            select(Job).where(Job.id == job_id).with_for_update()
        )).scalar_one_or_none()
        if job is None:
            return
        job.status = "pending"
        job.attempts = max(job.attempts - 1, 0)
        job.not_before = exc.not_before
        job.rerun_requested_at = None
        job.rerun_not_before = None
        job.rerun_priority = None
        job.last_error = f"deferred until {exc.not_before.isoformat()}: {exc.reason}"[:2000]
        await db.commit()
    logger.info("Job %s deferred until %s", job_id, exc.not_before)


async def _run_after_commit(ctx: JobContext, db: AsyncSession) -> None:
    """The handler's post-commit steps (spec §4.3), in order; one failing never fails the job."""
    for callback in ctx.after_commit:
        try:
            await callback(db)
        except Exception:
            logger.exception("Job %s: a post-commit step failed", ctx.id)
            await db.rollback()


async def process_job(job_id: uuid.UUID, job_type: str, job_attempts: int, job_max_attempts: int, session_factory: async_sessionmaker) -> None:
    """Run one job. The handler's transaction holds only the handler's writes: the
    jobs row is read once (detached into a JobContext) and its status is written in
    a separate transaction AFTER the handler commits. A crash between the two
    leaves the row 'processing', which the stale sweep re-queues.

    The jobs row can vanish at any await: jobs.user_id is ON DELETE CASCADE,
    so an account deletion mid-run takes the row with it (deletion audit F10).
    Both the initial re-fetch and the failure bookkeeping tolerate that — the
    account is gone, so there is no state anyone still needs updated.

    After the handler's commit, each `ctx.after_commit` callback runs in order,
    before the job is marked completed, and is passed the handler's session; the
    persona writer (`profile_publish.write_persona_files`, the only callback queued
    today) does not write on it but opens its own session on that session's bind and
    commits that. A failed or deferred handler runs none of them. `JobDeferred` returns the job to pending without counting the attempt.
    A BULK-priority job passes `openalex_budget.defer_if_low` before its handler, which
    may defer it the same way.
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
        bulk = (row.priority or 0) <= BULK_PRIORITY
        await db.commit()

        try:
            if bulk:
                await openalex_budget.defer_if_low(ctx.type)
            if ctx.type == "monthly_refresh":
                await execute_monthly_refresh(ctx, db)
            handler = JOB_HANDLERS.get(ctx.type)
            if handler is None:
                raise ValueError(f"Unknown job type: {ctx.type}")
            await handler(ctx, db)
            await db.commit()
        except JobDeferred as exc:
            await db.rollback()
            await _mark_deferred(session_factory, job_id, exc)
            return
        except Exception as exc:
            logger.error("Job %s failed: %s", job_id, exc, exc_info=True)
            # The pipeline may have left the transaction aborted (e.g. an FK
            # violation after a concurrent user delete) — clear it before the
            # bookkeeping writes.
            await db.rollback()
            await _mark_failed(session_factory, job_id, exc)
            return
        await _run_after_commit(ctx, db)
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


async def _maybe_sweep_personas(
    session_factory: async_sessionmaker, now: float, due_at: float
) -> float:
    """Run the daily persona sweep when it is due (spec 2026-10-05 §6.1, D54); returns the
    next due time. While `persona_sweep_enabled` is off, or after the sweep raised, it
    re-checks every PERSONA_SWEEP_RECHECK_SECONDS, so the idle work after it still runs."""
    if now < due_at:
        return due_at
    try:
        swept = await run_persona_sweep(session_factory)
    except Exception:
        logger.exception("Persona sweep failed; retrying in %ds", PERSONA_SWEEP_RECHECK_SECONDS)
        return now + PERSONA_SWEEP_RECHECK_SECONDS
    return now + (PERSONA_SWEEP_RECHECK_SECONDS if swept is None else PERSONA_SWEEP_SECONDS)


async def run_worker():
    """Main worker loop."""
    global _shutdown

    settings = get_settings()
    engine = make_engine("worker", settings.database_url)
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
    last_suggestion_check: float | None = None
    persona_sweep_due = 0.0

    while not _shutdown:
        try:
            async with session_factory() as db:
                job = await claim_job(db)

            if job:
                logger.info("Processing job %s (type=%s)", job.id, job.type)
                await process_job(job.id, job.type, job.attempts, job.max_attempts, session_factory)
            else:
                # No jobs: idle work, then sleep before polling again — unless a
                # suggestion call was made, which took long enough that a job may be
                # waiting.
                generated = False
                now = asyncio.get_event_loop().time()
                persona_sweep_due = await _maybe_sweep_personas(
                    session_factory, now, persona_sweep_due
                )
                if last_suggestion_check is None or now - last_suggestion_check >= SUGGESTION_SWEEP_SECONDS:
                    last_suggestion_check = now
                    generated = await generate_chat_suggestions(session_factory)
                if not generated:
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

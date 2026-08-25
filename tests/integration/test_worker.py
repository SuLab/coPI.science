"""T5 — the job-queue worker (`src/worker/main.py`) against a real Postgres.

`src/worker/main.py` had zero coverage and it is what actually runs profile
generation in production: `claim_job`, `process_job`, `execute_generate_profile`,
`execute_monthly_refresh`, `run_worker`.

Three things about the shape of this module:

1. **The database is real and the sessions commit.** The shared `db_session` fixture
   rolls back, which makes it useless here: `claim_job` commits, `process_job` commits,
   and the whole question in T5.1 is what *another connection* sees. So these tests use
   a committing `async_sessionmaker(engine)` (the pattern from
   `tests/integration/test_cohort_engine_live.py`) and clean up after themselves.
   `expire_on_commit=False` matches what `run_worker` builds.

2. **Only the pipeline is mocked.** `src/worker/main.py` does
   `from src.services.profile_pipeline import run_profile_pipeline` at import time, so
   the effective binding is `src.worker.main.run_profile_pipeline` — patching
   `src.services.profile_pipeline.run_profile_pipeline` would have no effect on the
   worker. Every fake pipeline here writes a real `ResearcherProfile` through the real
   session, because that is the "work" whose ordering against job completion T5.4 is
   about.

3. **What each test would catch.** Named per test in its docstring. Between them the
   suite fails if `SKIP LOCKED` is deleted (T5.1b: `claim_job` blocks instead of
   returning), if the retry cap is removed (T5.2: the claim/process loop does not
   terminate; and the exhausted job gets claimed), and if completion is marked before
   the work is done (T5.4: the in-flight probes see a completed job).
"""

import asyncio
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from src.models import Job, ResearcherProfile, User
from src.worker import main as worker_main

pytestmark = pytest.mark.integration

# Everything this module writes is tagged so a crashed run can be swept next time.
TAG = "t5_worker"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Harness:
    """Committing session factory + row bookkeeping."""

    def __init__(self, factory, pg_url, engine):
        self.factory = factory
        self.pg_url = pg_url
        self.engine = engine

    async def sweep(self):
        """Delete anything a previous (possibly interrupted) run of this file left.

        `users` cascades to jobs/profiles/publications; the extra jobs delete catches
        jobs enqueued with `user_id = NULL`.
        """
        async with self.factory() as db:
            await db.execute(text("DELETE FROM jobs WHERE payload->>'tag' = :t"), {"t": TAG})
            await db.execute(text("DELETE FROM users WHERE orcid LIKE 'T5-%'"))
            await db.commit()

    async def new_user(self, name="T5 PI") -> uuid.UUID:
        uid = uuid.uuid4()
        async with self.factory() as db:
            db.add(User(
                id=uid,
                name=name,
                orcid=f"T5-{uid.hex[:12]}",
                email=f"t5-{uid.hex[:12]}@example.invalid",
                access_status="allowed",
            ))
            await db.commit()
        return uid

    async def enqueue(
        self,
        user_id: uuid.UUID | None = None,
        job_type: str = "generate_profile",
        max_attempts: int = 3,
        enqueued_at: datetime | None = None,
    ) -> uuid.UUID:
        """Enqueue exactly the way `src/cli.py` and `src/routers/onboarding.py` do."""
        jid = uuid.uuid4()
        payload = {"tag": TAG}
        if user_id is not None:
            payload["user_id"] = str(user_id)
        async with self.factory() as db:
            job = Job(
                id=jid,
                type=job_type,
                user_id=user_id,
                payload=payload,
                max_attempts=max_attempts,
            )
            if enqueued_at is not None:
                job.enqueued_at = enqueued_at
            db.add(job)
            await db.commit()
        return jid

    async def enqueue_for_missing_user(
        self, ghost_id: uuid.UUID, job_type: str = "generate_profile"
    ) -> uuid.UUID:
        """A job whose payload names a user that does not exist.

        `jobs.user_id` is ON DELETE CASCADE, so deleting the user takes the job with it;
        a payload pointing at a stranger is the reachable form of this state (and the
        payload is what `execute_generate_profile` reads first).
        """
        jid = await self.enqueue(None, job_type=job_type)
        async with self.factory() as db:
            job = (await db.execute(select(Job).where(Job.id == jid))).scalar_one()
            job.payload = {"tag": TAG, "user_id": str(ghost_id)}
            await db.commit()
        return jid

    async def job(self, job_id: uuid.UUID) -> Job:
        """A fresh read on its own connection — never the worker's session."""
        async with self.factory() as db:
            return (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()

    async def job_state(self, job_id: uuid.UUID):
        async with self.factory() as db:
            return (await db.execute(
                select(Job.status, Job.attempts, Job.completed_at).where(Job.id == job_id)
            )).one()

    async def profile_count(self, user_id: uuid.UUID) -> int:
        async with self.factory() as db:
            return await db.scalar(
                select(func.count())
                .select_from(ResearcherProfile)
                .where(ResearcherProfile.user_id == user_id)
            )

    async def foreign_pending_jobs(self) -> int:
        async with self.factory() as db:
            return await db.scalar(text(
                "SELECT count(*) FROM jobs WHERE status = 'pending' "
                "AND coalesce(payload->>'tag', '') <> :t"
            ), {"t": TAG})


@pytest.fixture
async def wk(engine, pg_url):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    h = _Harness(factory, pg_url, engine)
    await h.sweep()
    yield h
    await h.sweep()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _race_claims(factory, n: int):
    """`n` concurrent `claim_job` calls, each on its own connection.

    The connection is warmed *before* the barrier so the race is over the claim
    statement itself and not over connection setup.

    Returns `(claims, spans)`; `spans` is each call's (start, end) so the caller can
    prove the calls actually overlapped. Without that, "exactly one worker claimed it"
    is also what you get from six calls that happened to run one after another, and the
    test would be asserting nothing about concurrency.
    """
    barrier = asyncio.Barrier(n)
    spans: list[tuple[float, float]] = []

    async def one():
        async with factory() as db:
            await db.execute(text("SELECT 1"))
            await barrier.wait()
            start = time.perf_counter()
            job = await worker_main.claim_job(db)
            spans.append((start, time.perf_counter()))
            return job

    claims = await asyncio.gather(*[one() for _ in range(n)])
    return claims, spans


def _assert_overlapped(spans, n):
    """All `n` calls were in flight at the same instant."""
    assert len(spans) == n
    latest_start = max(s for s, _ in spans)
    earliest_end = min(e for _, e in spans)
    assert latest_start < earliest_end, (
        f"the {n} claim_job calls did not overlap in time (last start {latest_start}, "
        f"first end {earliest_end}); they ran one after another, so this test says "
        "nothing about concurrency"
    )


async def _one_round(factory) -> Job | None:
    """One iteration of `run_worker`'s inner loop: claim, then process in a new session.

    Deliberately the same call shape as `run_worker`, including the arguments
    `process_job` accepts and ignores.
    """
    async with factory() as db:
        job = await worker_main.claim_job(db)
    if job is None:
        return None
    await worker_main.process_job(job.id, job.type, job.attempts, job.max_attempts, factory)
    return job


async def _drain(factory, limit: int = 10) -> int:
    """`_one_round` until the queue is empty. Returns the number of jobs processed.

    Bounded deliberately: if the retry cap is ever removed, a permanently failing job
    is re-claimed forever, and that must surface as a loud failure rather than a hang.
    """
    rounds = 0
    while rounds < limit:
        if await _one_round(factory) is None:
            return rounds
        rounds += 1
    raise AssertionError(
        f"the claim/process loop never terminated ({limit} rounds): a job is being "
        "retried without limit, i.e. the attempts/max_attempts cap is gone"
    )


def _profile_writer(expected_users: dict[uuid.UUID, str]):
    """A fake `run_profile_pipeline` that writes a real profile row for known users.

    Unknown users raise rather than silently succeeding, so a stray pending job from
    somewhere else can never be mistaken for this test's work (and can never reach the
    real ORCID/PubMed/Anthropic calls).
    """

    async def fake(user_id, db, job=None):
        if user_id not in expected_users:
            raise RuntimeError(f"T5: refusing to run the pipeline for unknown user {user_id}")
        profile = ResearcherProfile(
            user_id=user_id, research_summary=expected_users[user_id]
        )
        db.add(profile)
        await db.flush()
        return profile

    return fake


async def _wait_until(pred, timeout=60.0, interval=0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await pred():
            return True
        await asyncio.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# T5.1 — claim_job atomicity
# ---------------------------------------------------------------------------


async def test_claim_job_is_atomic_under_real_concurrency(wk):
    """T5.1 — six concurrent workers, one pending job, exactly one claim.

    Positive control, in this test, per the plan: two jobs and two workers must yield
    *both* jobs claimed. Without it "exactly one claim" is also satisfied by a
    `claim_job` that never claims anything at all.

    Second control: the six calls are asserted to have genuinely overlapped in time. Six
    serialized calls would also produce one claim and would prove nothing.

    `attempts == 1` is the third half: a lost update (two workers both reading
    attempts=0 and both writing 1) would be invisible if only the claim count were
    checked.
    """
    uid = await wk.new_user()
    jid = await wk.enqueue(uid)

    claims, spans = await _race_claims(wk.factory, n=6)
    _assert_overlapped(spans, 6)
    won = [c for c in claims if c is not None]

    assert len(won) == 1, (
        f"{len(won)} of 6 concurrent workers claimed the same job "
        f"({[str(c.id) for c in won]}); the claim is not atomic"
    )
    assert won[0].id == jid
    state = await wk.job_state(jid)
    assert state.status == "processing"
    assert state.attempts == 1, (
        f"attempts={state.attempts} after a single claim — two workers incremented it "
        "(lost update) even though only one returned the job"
    )

    # CONTROL: two jobs, two workers => both claimed.
    #
    # This is a parallel-progress control, not a SKIP LOCKED detector. Measured: with
    # `skip_locked=False` substituted for the real thing, this assertion still passes,
    # because Postgres' LockRows node re-checks the qual after the lock is granted and
    # moves on to the next row when it no longer matches. Removing SKIP LOCKED is caught
    # by the timing test below, and only by it.
    uid2 = await wk.new_user()
    j2 = await wk.enqueue(uid2)
    j3 = await wk.enqueue(uid2)

    claims2, spans2 = await _race_claims(wk.factory, n=2)
    _assert_overlapped(spans2, 2)
    won2 = {c.id for c in claims2 if c is not None}
    assert won2 == {j2, j3}, (
        f"two workers and two pending jobs claimed {len(won2)} job(s) ({won2}); "
        "concurrent workers are not making progress in parallel"
    )


async def test_claim_job_skips_a_row_another_worker_holds_locked(wk):
    """T5.1 — the `SKIP LOCKED` half, which the count-based test above cannot see.

    A worker that has claimed but not yet committed holds `FOR UPDATE` on its row. With
    `SKIP LOCKED` a second worker steps over it *immediately*; with a plain `FOR UPDATE`
    it blocks until the first commits. Both end at "exactly one claim", so the only
    observable difference is time — hence the timeout, which is what makes this the test
    that fails if someone deletes `skip_locked=True`.

    Control: the same call against the same row, once the lock is released, does claim
    it. Otherwise `None` would prove nothing.
    """
    uid = await wk.new_user()
    jid = await wk.enqueue(uid)

    holder = wk.factory()
    await holder.execute(text("SELECT id FROM jobs WHERE id = :i FOR UPDATE"), {"i": jid})
    try:
        try:
            async with wk.factory() as db:
                blocked = await asyncio.wait_for(worker_main.claim_job(db), timeout=5.0)
        except TimeoutError:
            pytest.fail(
                "claim_job blocked for 5s on a row another worker had locked: it waited "
                "for the lock instead of skipping the row. SKIP LOCKED is gone, and a "
                "worker pool now serialises behind whichever job is slowest."
            )
        assert blocked is None, (
            f"claim_job returned job {blocked.id} while another transaction held "
            "FOR UPDATE on it — two workers would run the same job"
        )
    finally:
        await holder.rollback()
        await holder.close()

    # CONTROL: the row was claimable all along; the None above was the lock, not a
    # claim_job that never claims.
    async with wk.factory() as db:
        got = await asyncio.wait_for(worker_main.claim_job(db), timeout=5.0)
    assert got is not None and got.id == jid, (
        "claim_job did not claim an unlocked pending job, so the skip assertion above "
        "was vacuous"
    )


# ---------------------------------------------------------------------------
# T5.2 — retries and the attempt cap
# ---------------------------------------------------------------------------


async def test_a_failing_job_retries_to_max_attempts_and_then_dies(wk, monkeypatch):
    """T5.2 — attempts increments, and the job reaches a terminal state.

    Catches: removing the `job.attempts >= job.max_attempts` branch in `process_job`
    (the job would end 'pending' forever); removing the cap altogether (`_drain` raises
    rather than hanging).

    Control, in this test: a job that fails once and then succeeds ends 'completed' at
    attempts=2 with its profile written — so "terminal state" is not being reached by a
    retry mechanism that simply never retries.
    """
    uid = await wk.new_user()
    jid = await wk.enqueue(uid, max_attempts=3)

    seen_attempts = []

    async def always_fails(user_id, db, job=None):
        seen_attempts.append(job.attempts)
        raise RuntimeError("pipeline exploded (T5.2)")

    monkeypatch.setattr(worker_main, "run_profile_pipeline", always_fails)

    rounds = await _drain(wk.factory, limit=10)

    assert rounds == 3, f"expected exactly max_attempts=3 executions, got {rounds}"
    assert seen_attempts == [1, 2, 3], (
        f"attempts did not increment once per execution: {seen_attempts}"
    )
    state = await wk.job_state(jid)
    assert state.status == "dead", (
        f"a job that failed max_attempts times is {state.status!r}, not 'dead' — "
        "nothing will ever move it out of the queue's way"
    )
    assert state.attempts == 3
    row = await wk.job(jid)
    assert "pipeline exploded (T5.2)" in (row.last_error or ""), (
        f"the failure reason was not recorded: {row.last_error!r}"
    )
    assert await wk.profile_count(uid) == 0

    # CONTROL: retrying is real work, not a state machine that gives up quietly.
    uid2 = await wk.new_user()
    jid2 = await wk.enqueue(uid2, max_attempts=3)
    write = _profile_writer({uid2: "recovered on the second attempt"})
    calls = {"n": 0}

    async def fails_once(user_id, db, job=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient (T5.2 control)")
        return await write(user_id, db, job)

    monkeypatch.setattr(worker_main, "run_profile_pipeline", fails_once)
    rounds2 = await _drain(wk.factory, limit=10)

    assert rounds2 == 2, f"expected fail-then-succeed to take 2 executions, got {rounds2}"
    state2 = await wk.job_state(jid2)
    assert state2.status == "completed" and state2.attempts == 2
    assert await wk.profile_count(uid2) == 1, (
        "the retry was accounted for but the work never landed"
    )


async def test_claim_job_will_not_claim_a_job_whose_attempts_are_exhausted(wk):
    """T5.2 — the cap's other enforcement point: the claim filter itself.

    `max_attempts=0` is the only way to reach `attempts >= max_attempts` while still
    'pending' (the failure path sets 'dead' at the same instant), and it is an input
    value, not hand-written derived state — the column has no CHECK constraint and
    defaults are applied at enqueue.

    Catches: deleting `Job.attempts < Job.max_attempts` from `claim_job`'s WHERE — the
    exhausted job is enqueued *first*, so a claim that ignores the cap returns it.

    Control: a normal job enqueued a second later IS claimed, so "returns None" is not
    the whole story.
    """
    uid = await wk.new_user()
    t0 = datetime.now(UTC)
    exhausted = await wk.enqueue(uid, max_attempts=0, enqueued_at=t0)
    normal = await wk.enqueue(uid, max_attempts=3, enqueued_at=t0 + timedelta(seconds=5))

    async with wk.factory() as db:
        first = await worker_main.claim_job(db)
    assert first is not None, "nothing was claimed at all — the control job is missing"
    assert first.id == normal, (
        "claim_job returned the job whose attempts are already exhausted "
        "(max_attempts=0); the retry cap is not enforced at claim time and this job "
        "will be picked up forever"
    )

    async with wk.factory() as db:
        second = await worker_main.claim_job(db)
    assert second is None, (
        f"claim_job claimed {second.id} — the exhausted job is still reachable"
    )
    assert (await wk.job_state(exhausted)).status == "pending"


# ---------------------------------------------------------------------------
# T5.3 — a crashing job does not kill the worker
# ---------------------------------------------------------------------------


async def test_process_job_swallows_the_failure_so_the_next_job_still_runs(wk, monkeypatch):
    """T5.3 (unit-of-the-loop half) — `process_job` must not propagate.

    If it raised, `run_worker`'s outer handler would catch it but the job's status would
    never be written, leaving it stuck in 'processing' with no reaper.

    The crasher is given `max_attempts=1` so it reaches a terminal state in one round
    and the queue moves on; the retry behaviour itself is T5.2's subject.

    Control: the second job, processed by the same loop right after, completes and
    writes its profile.
    """
    uid_bad = await wk.new_user("T5 crasher")
    uid_good = await wk.new_user("T5 survivor")
    t0 = datetime.now(UTC)
    j_bad = await wk.enqueue(uid_bad, max_attempts=1, enqueued_at=t0)
    j_good = await wk.enqueue(uid_good, enqueued_at=t0 + timedelta(seconds=5))

    write = _profile_writer({uid_good: "survivor profile"})

    async def crash_for_bad(user_id, db, job=None):
        if user_id == uid_bad:
            raise RuntimeError("kaboom (T5.3)")
        return await write(user_id, db, job)

    monkeypatch.setattr(worker_main, "run_profile_pipeline", crash_for_bad)

    # Must return normally, not raise.
    first = await _one_round(wk.factory)
    assert first is not None and first.id == j_bad
    assert (await wk.job_state(j_bad)).status == "dead"

    second = await _one_round(wk.factory)
    assert second is not None and second.id == j_good, (
        f"the queue did not advance past the crashing job: claimed {second!r}"
    )
    assert (await wk.job_state(j_good)).status == "completed"
    assert await wk.profile_count(uid_good) == 1
    assert await wk.profile_count(uid_bad) == 0


async def test_run_worker_loop_survives_a_crashing_job(wk, pg_url, monkeypatch):
    """T5.3 — the real `run_worker` loop, not a reimplementation of it.

    This is the only test that executes `run_worker` itself: its engine construction,
    its claim/process cycle, its idle sleep and its shutdown flag. The email
    notification and inbound blocks are pushed out of reach with an absurd interval
    rather than mocked, since email is out of scope for this plan.

    Control: the good job (enqueued *after* the crasher) reaching 'completed' is the
    positive observation; "the loop did not raise" alone would pass for a loop that
    exited immediately.
    """
    dbname = pg_url.rsplit("/", 1)[-1].split("?")[0]
    assert dbname != "copi", (
        f"refusing to run run_worker against {dbname!r}: this test drives the real "
        "worker loop and it must never touch the live database"
    )
    assert await wk.foreign_pending_jobs() == 0, (
        "there are pending jobs in this database that this file did not enqueue; "
        "run_worker would claim them"
    )

    uid_bad = await wk.new_user("T5 loop crasher")
    uid_good = await wk.new_user("T5 loop survivor")
    t0 = datetime.now(UTC)
    j_bad = await wk.enqueue(uid_bad, max_attempts=2, enqueued_at=t0)
    j_good = await wk.enqueue(uid_good, enqueued_at=t0 + timedelta(seconds=5))

    write = _profile_writer({uid_good: "loop survivor profile"})

    async def crash_for_bad(user_id, db, job=None):
        if user_id == uid_bad:
            raise RuntimeError("kaboom in the loop (T5.3)")
        return await write(user_id, db, job)

    monkeypatch.setattr(worker_main, "run_profile_pipeline", crash_for_bad)
    monkeypatch.setattr(worker_main, "get_settings", lambda: SimpleNamespace(
        database_url=pg_url,
        worker_poll_interval=0.05,
        notification_check_interval=10**9,
        enable_inbound_email=False,
        inbound_poll_interval=10**9,
    ))

    worker_main._shutdown = False
    task = asyncio.create_task(worker_main.run_worker())
    try:
        async def good_done():
            return (await wk.job_state(j_good)).status == "completed"

        finished = await _wait_until(good_done, timeout=45.0)
    finally:
        worker_main._shutdown = True
        try:
            await asyncio.wait_for(task, timeout=30.0)
        except TimeoutError:  # pragma: no cover - only on a hung loop
            task.cancel()
            raise

    assert finished, (
        "run_worker never completed the job queued behind a crashing one: the loop "
        "died, stalled, or is retrying the crasher forever"
    )
    bad = await wk.job_state(j_bad)
    assert bad.status == "dead" and bad.attempts == 2, (
        f"the crashing job ended {bad.status!r} after {bad.attempts} attempts"
    )
    assert await wk.profile_count(uid_good) == 1
    assert await wk.profile_count(uid_bad) == 0


# ---------------------------------------------------------------------------
# T5.4 — execute_generate_profile, and the ordering of completion vs the work
# ---------------------------------------------------------------------------


async def test_execute_generate_profile_calls_the_pipeline_with_the_claimed_job(wk, monkeypatch):
    """T5.4 (wiring half) — the worker hands the pipeline the right user and job.

    `execute_generate_profile` resolves the user from `payload['user_id']`, verifies the
    user exists, and passes the *job* through so the pipeline can record progress. The
    fake writes progress exactly the way `run_profile_pipeline.update_progress` does; if
    the job were not the session's live instance those writes would vanish, and
    /onboarding — which renders `job.payload['progress']` — would show nothing forever.

    Control: the same worker run for a payload whose user does not exist must fail
    loudly rather than complete, so "it called the pipeline" is not satisfied by a
    worker that calls it for anybody.
    """
    uid = await wk.new_user("T5 wiring")
    jid = await wk.enqueue(uid)
    seen = {}

    async def record(user_id, db, job=None):
        seen["user_id"] = user_id
        seen["job_id"] = job.id if job is not None else None
        job.payload = dict(job.payload)
        job.payload["progress"] = [{"step": "t5", "detail": "probe"}]
        return await _profile_writer({uid: "wired"})(user_id, db, job)

    monkeypatch.setattr(worker_main, "run_profile_pipeline", record)
    assert await _drain(wk.factory) == 1

    assert seen["user_id"] == uid
    assert seen["job_id"] == jid, "the pipeline was handed a different job than the claimed one"
    assert (await wk.job(jid)).payload.get("progress") == [{"step": "t5", "detail": "probe"}], (
        "the job the pipeline was given is not the worker session's live instance, so "
        "its progress writes were discarded"
    )
    assert (await wk.job_state(jid)).status == "completed"
    assert await wk.profile_count(uid) == 1

    # CONTROL: a job pointing at a user that does not exist must not complete.
    ghost = uuid.uuid4()
    ghost_job = await wk.enqueue_for_missing_user(ghost)

    claimed = await _one_round(wk.factory)
    assert claimed is not None and claimed.id == ghost_job
    ghost_state = await wk.job_state(ghost_job)
    assert ghost_state.status != "completed", (
        "a job for a nonexistent user was marked completed; the worker will happily "
        "'generate a profile' for anyone"
    )
    assert f"User {ghost} not found" in ((await wk.job(ghost_job)).last_error or "")


async def test_the_job_is_marked_completed_only_after_the_profile_row_exists(wk, monkeypatch):
    """T5.4 (the ordering half) — the assertion that actually protects the data.

    A worker that writes `status='completed'` before the profile exists loses the
    profile on a crash and never retries it. End-state assertions cannot see that: both
    orderings finish with a completed job and a profile row. So the fake pipeline
    observes the world *mid-execution*, from two vantage points:

      * the worker's own session (`job.status` on the tracked ORM object), and
      * a second connection reading committed state.

    Catches: hoisting the `job.status = "completed"` / `db.commit()` block above the
    dispatch, or committing the completion in a separate earlier transaction.

    The probes are proved non-vacuous by re-running the identical committed read after
    `process_job` returns and seeing 'completed' there.
    """
    uid = await wk.new_user("T5 ordering")
    jid = await wk.enqueue(uid)
    seen = {}

    async def observe_then_work(user_id, db, job=None):
        seen["in_session_status"] = job.status
        seen["committed_before"] = await wk.job_state(jid)
        seen["profiles_before"] = await wk.profile_count(uid)
        profile = ResearcherProfile(user_id=user_id, research_summary="ordering probe")
        db.add(profile)
        await db.flush()
        # Flushed but not committed: still invisible to everyone else.
        seen["profiles_after_flush"] = await wk.profile_count(uid)
        return profile

    monkeypatch.setattr(worker_main, "run_profile_pipeline", observe_then_work)
    assert await _drain(wk.factory) == 1

    # The probe is capable of seeing a completed job — proved with the same query.
    after = await wk.job_state(jid)
    assert after.status == "completed" and after.completed_at is not None
    assert await wk.profile_count(uid) == 1

    assert seen["in_session_status"] == "processing", (
        f"the worker's own session had the job at {seen['in_session_status']!r} while "
        "the pipeline was still running — completion is marked before the work"
    )
    assert seen["committed_before"].status == "processing", (
        f"another connection saw the job as {seen['committed_before'].status!r} "
        "mid-execution: the completion was committed before the profile existed, so a "
        "crash here loses the profile permanently and the job is never retried"
    )
    assert seen["committed_before"].completed_at is None
    assert seen["profiles_before"] == 0
    assert seen["profiles_after_flush"] == 0, (
        "the profile became visible to other connections before the job was completed; "
        "the two are no longer in one transaction"
    )


async def test_a_crash_after_partial_work_leaves_a_retryable_job(wk, monkeypatch):
    """T5.4 (crash half) — the inverse of the ordering test.

    A pipeline that gets partway (profile row added and flushed) and then raises must
    not leave the job 'completed'. It must be retryable.

    NOTE: `process_job`'s except branch now rolls back before its bookkeeping writes,
    so the pipeline's partial writes are discarded rather than committed alongside the
    failure record — the retry starts clean.

    Control: the identical pipeline without the raise completes and is not retried.
    """
    uid = await wk.new_user("T5 partial")
    jid = await wk.enqueue(uid)

    async def half_then_crash(user_id, db, job=None):
        db.add(ResearcherProfile(user_id=user_id, research_summary="half written"))
        await db.flush()
        raise RuntimeError("crashed after the profile row (T5.4)")

    monkeypatch.setattr(worker_main, "run_profile_pipeline", half_then_crash)

    async with wk.factory() as db:
        job = await worker_main.claim_job(db)
    await worker_main.process_job(job.id, job.type, job.attempts, job.max_attempts, wk.factory)

    state = await wk.job_state(jid)
    assert state.status != "completed", (
        "a job whose pipeline raised is marked completed; the failure is now invisible"
    )
    assert state.status == "pending", f"expected a retryable job, got {state.status!r}"
    leaked = await wk.profile_count(uid)

    # CONTROL: the same shape of pipeline that does not raise does complete, so the
    # assertions above are about the crash and not about the harness.
    uid2 = await wk.new_user("T5 partial control")
    jid2 = await wk.enqueue(uid2)
    monkeypatch.setattr(worker_main, "run_profile_pipeline", _profile_writer({uid2: "whole"}))
    assert await _drain(wk.factory, limit=10) >= 1
    assert (await wk.job_state(jid2)).status == "completed"
    assert await wk.profile_count(uid2) == 1

    assert leaked == 0, (
        "the pipeline's partial writes were committed alongside the failure "
        "record — the except branch must roll back before its bookkeeping"
    )


async def test_a_database_error_in_the_pipeline_is_recorded_and_retried(wk, monkeypatch):
    """T5.3/T5.4 — a failure that poisons the transaction is recorded and retried too.

    Every failure is caught, recorded and retried, including one that poisons the
    transaction: `process_job`'s except branch now rolls back before its bookkeeping
    writes, so a failed commit from the pipeline no longer prevents `last_error` and
    `status` from being written. The job is claimable again on the next round instead
    of being stranded in 'processing' with nothing in the system to reap it.

    The real pipeline reaches this shape at step 6 — `db.add(ResearcherProfile(...))`
    then `flush()`, with `researcher_profiles.user_id` unique and no try/except — if two
    generate_profile jobs for one user are ever in flight together.

    Control: the same claim/process pair with a plain Python error does record
    'pending' and is retried, so this is a property of the fixed except branch and not
    of the harness.
    """
    uid = await wk.new_user("T5 db error")
    async with wk.factory() as db:
        db.add(ResearcherProfile(user_id=uid, research_summary="already here"))
        await db.commit()
    jid = await wk.enqueue(uid)

    async def duplicate_profile(user_id, db, job=None):
        db.add(ResearcherProfile(user_id=user_id, research_summary="a second row"))
        await db.flush()  # unique violation on researcher_profiles.user_id

    monkeypatch.setattr(worker_main, "run_profile_pipeline", duplicate_profile)

    claimed = await _one_round(wk.factory)
    assert claimed is not None and claimed.id == jid

    state = await wk.job_state(jid)
    assert state.status == "pending", (
        f"the job is {state.status!r}; a database error must be recorded and "
        "retried like any other failure (deletion audit F10 / D8)"
    )
    assert "duplicate key" in ((await wk.job(jid)).last_error or ""), (
        "the failure reason never reached the row"
    )

    # Retire this job before the control section: it is now legitimately 'pending'
    # (the fix under test), and `claim_job` orders by `enqueued_at`, so left alone it
    # would out-race jid2 for the very next claim. Under the old defective handler this
    # never came up — the job was stuck 'processing' and excluded from the claim query
    # by construction. Marking it terminal here is test cleanup, not a new assertion.
    async with wk.factory() as db:
        await db.execute(text("UPDATE jobs SET status = 'dead' WHERE id = :id"), {"id": jid})
        await db.commit()

    # CONTROL: a non-database error through the identical path is recorded and retried.
    uid2 = await wk.new_user("T5 db error control")
    jid2 = await wk.enqueue(uid2)

    async def plain_error(user_id, db, job=None):
        raise RuntimeError("a plain error (T5 control)")

    monkeypatch.setattr(worker_main, "run_profile_pipeline", plain_error)
    claimed2 = await _one_round(wk.factory)
    assert claimed2 is not None and claimed2.id == jid2
    state2 = await wk.job_state(jid2)
    assert state2.status == "pending"
    assert "a plain error (T5 control)" in ((await wk.job(jid2)).last_error or "")


# ---------------------------------------------------------------------------
# T5.5 — execute_monthly_refresh
# ---------------------------------------------------------------------------


async def test_monthly_refresh_reruns_the_pipeline_without_duplicating_the_profile(
    wk, monkeypatch
):
    """T5.5 — what `execute_monthly_refresh` actually does today.

    It delegates to `execute_generate_profile`, i.e. re-runs the pipeline for the same
    user. So the properties worth pinning are: it is dispatched at all (a
    `monthly_refresh` job must not fall through to the unknown-type branch), it targets
    the same user, and re-running does not create a second `ResearcherProfile`.

    It creates no follow-on job, and nothing anywhere in `src/` enqueues a
    `monthly_refresh` — asserted here so that when scheduling is added, this test says
    so rather than silently continuing to pass.

    Control for that absence assertion: the refresh job itself is present and was
    processed, so "no new jobs" is not being satisfied by an empty table.
    """
    uid = await wk.new_user("T5 refresh")
    first = await wk.enqueue(uid, job_type="generate_profile")
    monkeypatch.setattr(worker_main, "run_profile_pipeline", _profile_writer({uid: "v1"}))
    assert await _drain(wk.factory) == 1
    assert (await wk.job_state(first)).status == "completed"
    assert await wk.profile_count(uid) == 1

    calls = []

    async def refresh(user_id, db, job=None):
        calls.append((user_id, job.type))
        existing = (await db.execute(
            select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
        )).scalar_one()
        existing.research_summary = "v2 from the monthly refresh"
        existing.profile_version = (existing.profile_version or 0) + 1
        await db.flush()
        return existing

    monkeypatch.setattr(worker_main, "run_profile_pipeline", refresh)
    refresh_job = await wk.enqueue(uid, job_type="monthly_refresh")
    assert await _drain(wk.factory) == 1

    assert calls == [(uid, "monthly_refresh")], (
        f"monthly_refresh did not reach the pipeline: {calls!r}"
    )
    assert (await wk.job_state(refresh_job)).status == "completed"
    assert await wk.profile_count(uid) == 1, (
        "the refresh created a second ResearcherProfile row instead of updating the "
        "existing one"
    )
    async with wk.factory() as db:
        profile = (await db.execute(
            select(ResearcherProfile).where(ResearcherProfile.user_id == uid)
        )).scalar_one()
    assert profile.research_summary == "v2 from the monthly refresh"
    assert profile.profile_version == 1

    async with wk.factory() as db:
        jobs = (await db.execute(select(Job).where(Job.user_id == uid))).scalars().all()
    assert {j.id for j in jobs} == {first, refresh_job}, (
        "the worker created follow-on job(s) — monthly_refresh now schedules work and "
        "this test needs to describe it"
    )


async def test_monthly_refresh_for_a_missing_user_fails_loudly(wk, monkeypatch):
    """T5.5 — the refresh path shares `execute_generate_profile`'s user check.

    Control in the same test: a refresh for a real user completes, so "did not complete"
    is a property of the missing user and not of the refresh type being unsupported.
    """
    called = []

    async def should_not_run(user_id, db, job=None):
        called.append(user_id)
        raise AssertionError("the pipeline ran for a user that does not exist")

    monkeypatch.setattr(worker_main, "run_profile_pipeline", should_not_run)

    ghost = uuid.uuid4()
    jid = await wk.enqueue_for_missing_user(ghost, job_type="monthly_refresh")

    claimed = await _one_round(wk.factory)
    assert claimed is not None and claimed.id == jid
    assert called == []
    state = await wk.job_state(jid)
    assert state.status == "pending"
    assert f"User {ghost} not found" in ((await wk.job(jid)).last_error or "")

    # CONTROL
    uid = await wk.new_user("T5 refresh control")
    ok = await wk.enqueue(uid, job_type="monthly_refresh")
    monkeypatch.setattr(worker_main, "run_profile_pipeline", _profile_writer({uid: "ok"}))
    assert await _drain(wk.factory, limit=10) >= 1
    assert (await wk.job_state(ok)).status == "completed"


async def test_a_job_with_no_user_at_all_fails_loudly(wk, monkeypatch):
    """T5.5/T5.4 edge — `payload['user_id']` absent and `job.user_id` NULL.

    `execute_generate_profile` guards this with
    `user_id_str = payload.get("user_id") or str(job.user_id)`, which for a NULL
    user_id yields the string "None" — truthy — so the intended
    `ValueError("Job missing user_id in payload")` is unreachable and the failure
    arrives from `uuid.UUID("None")` instead. Reported, not fixed. The property that
    matters is asserted first: the job does not complete.
    """
    called = []

    async def should_not_run(user_id, db, job=None):
        called.append(user_id)
        return None

    monkeypatch.setattr(worker_main, "run_profile_pipeline", should_not_run)
    jid = await wk.enqueue(None)

    claimed = await _one_round(wk.factory)
    assert claimed is not None and claimed.id == jid
    assert called == []
    state = await wk.job_state(jid)
    assert state.status != "completed"
    last_error = (await wk.job(jid)).last_error or ""
    assert last_error, "the job failed without recording why"
    # Characterizes the unreachable guard described above.
    assert "badly formed hexadecimal UUID string" in last_error, (
        f"the failure message changed to {last_error!r}; if the missing-user_id guard "
        "is now reachable, this test should assert the intended message instead"
    )


# ---------------------------------------------------------------------------
# T5.6 — unknown job type
# ---------------------------------------------------------------------------


async def test_an_unknown_job_type_cannot_even_be_enqueued(wk):
    """T5.6 (first line of defence) — `job_type_enum` rejects it at the database.

    This is also the reason the dispatcher test below has to doctor an in-memory
    instance: there is no way to *store* an unknown type.

    Control: the identical raw insert with a legal type succeeds, so the rejection is
    the enum and not a malformed statement.
    """
    uid = await wk.new_user("T5 enum")
    insert = text(
        "INSERT INTO jobs (id, type, status, user_id, payload, attempts, max_attempts) "
        "VALUES (:id, :type, 'pending', :uid, :payload, 0, 3)"
    )
    payload = f'{{"tag": "{TAG}"}}'

    async with wk.factory() as db:
        with pytest.raises(DBAPIError) as exc:
            await db.execute(insert, {
                "id": uuid.uuid4(), "type": "bogus_type", "uid": uid, "payload": payload,
            })
        await db.rollback()
    assert "job_type_enum" in str(exc.value), (
        f"the insert failed for some reason other than the enum: {exc.value}"
    )

    # CONTROL
    legal = uuid.uuid4()
    async with wk.factory() as db:
        await db.execute(insert, {
            "id": legal, "type": "generate_profile", "uid": uid, "payload": payload,
        })
        await db.commit()
    assert (await wk.job_state(legal)).status == "pending"


@contextmanager
def _job_type_forced_on_load(job_id, job_type):
    """Make the worker's own `select(Job)` load `job_id` with `type = job_type`.

    An unknown type cannot be stored (the test above shows `job_type_enum` rejecting
    it), so the only way to reach `process_job`'s else-branch is to change what the
    dispatcher reads. The mapper-level `load` event fires as `process_job` re-fetches
    the job in its real session, and `set_committed_value` writes the value as if it had
    come from the row — so the attribute is not dirty and the subsequent UPDATE never
    tries to push the illegal value back through the enum.

    Nothing about the worker is stubbed: it is the real session, the real query, the
    real dispatch.
    """

    def _on_load(target, _context):
        if target.id == job_id:
            set_committed_value(target, "type", job_type)

    event.listen(Job, "load", _on_load)
    try:
        yield
    finally:
        event.remove(Job, "load", _on_load)


async def test_an_unknown_job_type_is_rejected_loudly_by_the_dispatcher(wk, monkeypatch):
    """T5.6 — `process_job`'s else-branch must not silently succeed.

    Control: the same harness with a legal type runs the pipeline and completes — so a
    failure above is the unknown type and not the doctoring mechanism.
    """
    uid = await wk.new_user("T5 unknown type")
    jid = await wk.enqueue(uid)
    ran = []

    async def pipeline(user_id, db, job=None):
        ran.append(user_id)
        return await _profile_writer({uid: "should not happen"})(user_id, db, job)

    monkeypatch.setattr(worker_main, "run_profile_pipeline", pipeline)

    with _job_type_forced_on_load(jid, "bogus_type"):
        await worker_main.process_job(jid, "bogus_type", 0, 3, wk.factory)

    assert ran == [], "an unknown job type reached the profile pipeline"
    state = await wk.job_state(jid)
    assert state.status != "completed", (
        "a job of an unknown type was marked completed — the worker silently did "
        "nothing and reported success"
    )
    row = await wk.job(jid)
    assert "Unknown job type: bogus_type" in (row.last_error or ""), (
        f"the rejection was not recorded: {row.last_error!r}"
    )
    assert row.type == "generate_profile", (
        "the doctored type was written back to the database, which the enum should "
        "have made impossible"
    )
    assert await wk.profile_count(uid) == 0

    # CONTROL: the same doctoring mechanism with a legal type completes the job.
    uid2 = await wk.new_user("T5 unknown type control")
    jid2 = await wk.enqueue(uid2)
    monkeypatch.setattr(worker_main, "run_profile_pipeline", _profile_writer({uid2: "ok"}))
    with _job_type_forced_on_load(jid2, "generate_profile"):
        await worker_main.process_job(jid2, "generate_profile", 0, 3, wk.factory)
    assert (await wk.job_state(jid2)).status == "completed"
    assert await wk.profile_count(uid2) == 1

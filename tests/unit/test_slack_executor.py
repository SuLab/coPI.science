"""``run_slack_call`` runs on its own bounded pool, isolated from every other
``asyncio.to_thread`` user in the process (audit 2026-09-10 R-2).

O-1 (audit 2026-09-10): rewritten for the single process-wide
``slack_client.SHUTDOWN_REQUESTED`` event, replacing the per-pool
event/thread-local/pending-flag design (K-2/M-2/N-2) that regressed in three
consecutive review rounds. There is now exactly one sticky shutdown signal;
``shutdown_slack_executor()`` sets it and drops the pool reference (without
cancelling queued work); ``_get_executor()`` never touches the event.

Reuses the exception-identity pattern from
``tests/unit/test_private_channel_migration.py::TestSlackCallsRunOffTheEventLoop``:
proving a call ran on a worker thread and that its exception propagated
unchanged (not wrapped) is what distinguishes "actually moved off the loop"
from "looks async but isn't".
"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import src.services.slack_executor as slack_executor_module
from src.agent.slack_client import SHUTDOWN_REQUESTED, SlackShuttingDown, _sleep_interruptibly
from src.services.slack_executor import (
    SLACK_IO_MAX_WORKERS,
    _get_executor,
    run_slack_call,
    shutdown_slack_executor,
)


@pytest.fixture(autouse=True)
def _reset_shutdown_state():
    """Several tests in this file force a REAL pool into existence or set the
    process-wide shutdown event — reset both so tests stay order-independent
    within this file and do not leak state into other test files sharing the
    same pytest process.

    A ``ThreadPoolExecutor`` that is never ``.shutdown()``ed leaks up to
    ``SLACK_IO_MAX_WORKERS`` (16) live, non-daemon worker threads, so this
    also shuts down whatever pool a test leaves assigned to the module
    global (real or a throwaway substituted via ``monkeypatch``).
    """
    yield
    shutdown_slack_executor()
    SHUTDOWN_REQUESTED.clear()


def test_the_slack_pool_is_a_bounded_dedicated_executor():
    pool = _get_executor()
    assert isinstance(pool, ThreadPoolExecutor)
    assert pool._max_workers == SLACK_IO_MAX_WORKERS
    assert pool._thread_name_prefix == "slack-io"


def test_slack_io_max_workers_exceeds_one_reopen_flows_sequential_call_count():
    """Opus review follow-up, audit 2026-09-10: sized to comfortably exceed the
    ~8 sequential run_slack_call's one migrate_public_thread_to_private reopen
    makes by itself, so a second concurrent flow is not left queuing behind the
    first for every worker thread."""
    ONE_REOPEN_FLOWS_SEQUENTIAL_CALL_COUNT = 8
    assert SLACK_IO_MAX_WORKERS >= 2 * ONE_REOPEN_FLOWS_SEQUENTIAL_CALL_COUNT


async def test_run_slack_call_executes_off_the_event_loop_on_a_slack_io_thread():
    loop_thread = threading.get_ident()
    seen: dict = {}

    def fn():
        seen["ident"] = threading.get_ident()
        seen["name"] = threading.current_thread().name

    await run_slack_call(fn)
    assert seen["ident"] != loop_thread
    assert seen["name"].startswith("slack-io")


async def test_run_slack_call_forwards_args_and_kwargs_and_returns_the_result():
    def fn(a, b, *, c):
        return a + b + c

    assert await run_slack_call(fn, 1, 2, c=3) == 6


async def test_an_exception_from_the_call_propagates_unchanged():
    """Identity, not just type — a wrapper that re-raised a *new* exception of
    the same class would still pass a ``pytest.raises(_Boom)`` check."""

    class _Boom(Exception):
        pass

    marker = _Boom("boom")

    def fn():
        raise marker

    with pytest.raises(_Boom) as exc_info:
        await run_slack_call(fn)
    assert exc_info.value is marker


# --- shutdown (O-1, audit 2026-09-10) ---------------------------------------
#
# `_SLACK_EXECUTOR` is a process-wide singleton, so these tests substitute a
# throwaway executor via monkeypatch rather than shutting down the real one out
# from under every other test in this file/session.


def test_shutdown_slack_executor_shuts_down_without_waiting(monkeypatch):
    fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-io-test")
    monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)
    shutdown_slack_executor()
    assert fresh._shutdown, "shutdown_slack_executor() must actually shut the pool down"


def test_shutdown_slack_executor_sets_the_shared_shutdown_event(monkeypatch):
    fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-io-test")
    monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)
    assert not SHUTDOWN_REQUESTED.is_set()
    shutdown_slack_executor()
    assert SHUTDOWN_REQUESTED.is_set()


async def test_run_slack_call_after_shutdown_lazily_creates_a_fresh_pool(monkeypatch):
    """K-9 (audit 2026-09-10): a shut-down pool is lazily replaced with a
    fresh one on the next call rather than raising, so a process sharing one
    interpreter across more than one lifespan (most concretely the test
    suite) does not permanently kill this singleton for every later test."""
    fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-io-test")
    monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)
    shutdown_slack_executor()

    result = await run_slack_call(lambda: 1)

    assert result == 1
    new_pool = slack_executor_module._SLACK_EXECUTOR
    assert new_pool is not None and new_pool is not fresh
    assert not new_pool._shutdown


def test_a_new_pool_created_after_shutdown_does_not_clear_the_event(monkeypatch):
    """O-1 requirement (b): the shutdown event is sticky for the process's
    life — `_get_executor()` must not clear it just because it minted a
    fresh pool."""
    fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-io-test")
    monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)
    shutdown_slack_executor()
    assert SHUTDOWN_REQUESTED.is_set()

    new_pool = _get_executor()

    assert new_pool is not fresh
    assert SHUTDOWN_REQUESTED.is_set(), (
        "_get_executor() must not clear the sticky shutdown event when it "
        "creates a fresh pool"
    )


async def test_queued_run_slack_call_is_not_cancelled_by_shutdown(monkeypatch):
    """O-1 requirement (c): `shutdown_slack_executor()` must call
    `pool.shutdown(wait=False, cancel_futures=False)` — a queued
    `run_slack_call` still runs (and aborts quickly via the event) rather
    than raising `CancelledError` into whatever awaits it."""
    fresh = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slack-io-test")
    monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)

    blocker_started = threading.Event()
    release_blocker = threading.Event()

    def blocker():
        blocker_started.set()
        release_blocker.wait(timeout=5.0)

    # Occupy the pool's only worker so the second call is queued, not running.
    blocker_future = fresh.submit(blocker)
    blocker_started.wait(timeout=2.0)

    queued_task = asyncio.ensure_future(run_slack_call(lambda: "ran"))
    await asyncio.sleep(0.05)  # let the second call actually enqueue

    shutdown_slack_executor()  # cancel_futures=False: must not drop the queued call
    release_blocker.set()
    blocker_future.result(timeout=2.0)

    result = await asyncio.wait_for(queued_task, timeout=3.0)
    assert result == "ran"


async def test_a_sleeper_via_run_slack_call_aborts_within_a_second_of_shutdown():
    outcome: dict = {}

    def sleeper():
        try:
            _sleep_interruptibly(30.0)
        except Exception as exc:
            outcome["exc"] = exc
        else:
            outcome["completed"] = True

    task = asyncio.ensure_future(run_slack_call(sleeper))
    await asyncio.sleep(0.05)  # let the worker thread get into its first slice

    start = time.monotonic()
    shutdown_slack_executor()

    await asyncio.wait_for(task, timeout=3.0)
    elapsed = time.monotonic() - start

    assert isinstance(outcome.get("exc"), SlackShuttingDown)
    assert elapsed < 2.0, f"took {elapsed:.2f}s to abort"


def test_a_main_thread_sleeper_aborts_within_a_second_of_shutdown(monkeypatch):
    """A caller running `AgentSlackClient` directly on its own thread (never
    through `run_slack_call`'s pool) is bound to the same shared event and
    must abort just as promptly."""
    fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-io-test")
    monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)

    outcome: dict = {}

    def run():
        try:
            _sleep_interruptibly(30.0)
        except Exception as exc:
            outcome["exc"] = exc
        else:
            outcome["completed"] = True

    t = threading.Thread(target=run, daemon=True)
    start = time.monotonic()
    t.start()
    time.sleep(0.05)  # let the sleeper start

    shutdown_slack_executor()

    t.join(timeout=3.0)
    elapsed = time.monotonic() - start

    assert not t.is_alive(), "the main-thread sleeper did not abort promptly"
    assert elapsed < 2.0, f"took {elapsed:.2f}s to abort"
    assert isinstance(outcome.get("exc"), SlackShuttingDown)

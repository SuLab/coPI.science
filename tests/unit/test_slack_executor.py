"""``run_slack_call`` runs on its own bounded pool, isolated from every other
``asyncio.to_thread`` user in the process (audit 2026-09-10 R-2).

Reuses the exception-identity pattern from
``tests/unit/test_private_channel_migration.py::TestSlackCallsRunOffTheEventLoop``:
proving a call ran on a worker thread and that its exception propagated
unchanged (not wrapped) is what distinguishes "actually moved off the loop"
from "looks async but isn't".
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import src.services.slack_executor as slack_executor_module
from src.services.slack_executor import (
    SLACK_IO_MAX_WORKERS,
    _get_executor,
    run_slack_call,
    shutdown_slack_executor,
)


@pytest.fixture(autouse=True)
def _shut_down_whatever_pool_this_test_leaves_behind(monkeypatch):
    """Several tests in this file force a REAL pool into existence — either
    the process-wide singleton (via `run_slack_call`/`_get_executor`) or a
    throwaway ``fresh`` one substituted via ``monkeypatch.setattr`` — and a
    ``ThreadPoolExecutor`` that is never ``.shutdown()``ed leaks up to
    ``SLACK_IO_MAX_WORKERS`` (16) live, non-daemon worker threads (opus
    review, audit 2026-09-10). Relying on ``monkeypatch``'s own teardown to
    fix this does not work: it only restores the ``_SLACK_EXECUTOR``
    *attribute* to its pre-test value, which does nothing to the actual pool
    object the test created and orphans its threads.

    Requesting ``monkeypatch`` as a dependency (even though this fixture does
    not call it) makes pytest set it up before this fixture, so at teardown
    this fixture's ``yield`` resumes BEFORE monkeypatch reverts
    ``_SLACK_EXECUTOR`` — this shuts down whatever pool the test actually
    left assigned there, not whatever it was before the test ran.
    """
    yield
    shutdown_slack_executor()
    # L-1 (opus review, audit 2026-09-10): shutdown_slack_executor() now also
    # sets slack_client.SHUTTING_DOWN, the fallback event — every test in
    # this file calls it at least once (directly or via this fixture), so
    # without clearing it back here it leaks SET into every OTHER test file
    # in the same pytest process, aborting their retry sleeps instantly.
    from src.agent.slack_client import SHUTTING_DOWN

    SHUTTING_DOWN.clear()


def test_the_slack_pool_is_a_bounded_dedicated_executor():
    # K-9 (audit 2026-09-10): the pool is now created lazily (see
    # `_get_executor`'s docstring/module docstring), so `_SLACK_EXECUTOR`
    # itself may be `None` at import time — force creation the same way
    # `run_slack_call` does before asserting on its shape.
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


# --- shutdown (opus review follow-up, audit 2026-09-10) ---------------------------
#
# `_SLACK_EXECUTOR` is a process-wide singleton, so these tests substitute a
# throwaway executor via monkeypatch rather than shutting down the real one out
# from under every other test in this file/session.


def test_shutdown_slack_executor_shuts_down_without_waiting(monkeypatch):
    fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-io-test")
    monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)
    shutdown_slack_executor()
    assert fresh._shutdown, "shutdown_slack_executor() must actually shut the pool down"


def test_shutdown_slack_executor_sets_the_current_pools_shutdown_event(monkeypatch):
    """K-2 follow-up #3 (opus review, audit 2026-09-10): the abort signal is
    per-pool, not one shared `slack_client.SHUTTING_DOWN` — signals the
    CURRENT pool's own event before shutting the pool down, so an in-flight
    retry sleep bound to that event can abort rather than block interpreter
    exit."""
    import threading as threading_module

    fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-io-test")
    event = threading_module.Event()
    monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)
    monkeypatch.setattr(slack_executor_module, "_CURRENT_SHUTDOWN_EVENT", event)
    assert not event.is_set()
    shutdown_slack_executor()
    assert event.is_set()


async def test_run_slack_call_after_shutdown_lazily_creates_a_fresh_pool(monkeypatch):
    """K-9 (audit 2026-09-10): the old contract (see module docstring history)
    had `run_slack_call` raise RuntimeError after a shutdown. That broke any
    process sharing one interpreter across more than one lifespan — most
    concretely the test suite, where an unrelated integration test's
    `create_app()` ASGI lifespan shutdown permanently killed this singleton
    for every later test in the same pytest process. `run_slack_call` must now
    succeed on a freshly created pool instead."""
    fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-io-test")
    monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)
    shutdown_slack_executor()

    result = await run_slack_call(lambda: 1)

    assert result == 1
    new_pool = slack_executor_module._SLACK_EXECUTOR
    assert new_pool is not None and new_pool is not fresh
    assert not new_pool._shutdown


async def test_a_new_pools_calls_are_not_immediately_aborted(monkeypatch):
    """K-9/K-2 interaction: a freshly created pool must start with an UNSET
    event of its own, or every retry on the new pool would abort instantly —
    this is exactly why the abort signal had to become per-pool (K-2 follow-up
    #3) instead of one shared Event that shutdown set and re-creation cleared."""
    fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-io-test")
    monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)
    shutdown_slack_executor()

    from src.agent.slack_client import _current_shutdown_event

    def check_event():
        return _current_shutdown_event().is_set()

    assert await run_slack_call(check_event) is False


def test_shutdown_slack_executor_also_sets_the_fallback_shutdown_event(monkeypatch):
    """L-1 (opus review, audit 2026-09-10): a caller that never went through
    `run_slack_call`'s pool — e.g. `AgentSlackClient` calls made directly on
    the agent-run process's event-loop thread (src/agent/main.py,
    src/agent/simulation.py) — is bound to `slack_client.SHUTTING_DOWN`, the
    module-level fallback, not to any per-pool event. Setting only the
    current pool's own event on shutdown left that fallback caller with no
    way to abort a retry sleep at process shutdown at all.
    `shutdown_slack_executor()` must also flip the fallback so a
    `_call_with_retry` sleeping on the main thread aborts promptly, with NO
    monkeypatching of the event itself.
    """
    import time

    from slack_sdk.errors import SlackApiError

    from src.agent import slack_client as slack_client_module

    slack_client_module.SHUTTING_DOWN.clear()
    try:
        fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-io-test")
        monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)

        outcome: dict = {}

        def run():
            try:
                slack_client_module._sleep_interruptibly(30.0)
            except Exception as exc:
                outcome["exc"] = exc
            else:
                outcome["completed"] = True

        t = threading.Thread(target=run)
        start = time.monotonic()
        t.start()
        time.sleep(0.05)  # let the main-thread-bound sleeper start

        shutdown_slack_executor()

        t.join(timeout=3.0)
        elapsed = time.monotonic() - start

        assert not t.is_alive(), "the main-thread sleeper did not abort promptly"
        assert elapsed < 2.0, f"took {elapsed:.2f}s to abort"
        assert isinstance(outcome.get("exc"), SlackApiError)
    finally:
        slack_client_module.SHUTTING_DOWN.clear()


async def test_get_executor_clears_the_fallback_event_when_it_creates_a_new_pool(monkeypatch):
    """M-2 (opus review, audit 2026-09-10): SHUTTING_DOWN (the fallback event
    for off-pool callers, e.g. AgentSlackClient calls made directly on
    src/agent/main.py's event-loop thread) is never cleared anywhere in
    src/ -- only tests reach in and clear it directly. In a process that
    shuts the pool down and later lazily re-creates it without exiting the
    interpreter (K-9), every later off-pool retry sleep would be bound to a
    permanently-SET fallback and abort instantly forever after. `_get_executor`
    must clear it when it mints a fresh pool.
    """
    from src.agent import slack_client as slack_client_module

    fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-io-test")
    monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)
    shutdown_slack_executor()  # sets the fallback (L-1) as a side effect
    assert slack_client_module.SHUTTING_DOWN.is_set()

    # Force _get_executor() to mint a fresh pool (this is what run_slack_call
    # does lazily on any call after a shutdown) BEFORE starting the sleeper --
    # this test is about a sleeper that starts after re-creation, not one
    # already mid-sleep across it (that scenario is covered separately below).
    await run_slack_call(lambda: 1)
    assert not slack_client_module.SHUTTING_DOWN.is_set()

    outcome: dict = {}

    def sleeper():
        try:
            slack_client_module._sleep_interruptibly(0.2)
        except Exception as exc:
            outcome["exc"] = exc
        else:
            outcome["completed"] = True

    t = threading.Thread(target=sleeper, daemon=True)
    t.start()
    t.join(timeout=2.0)

    assert outcome.get("completed") is True, (
        "an off-pool sleeper started AFTER a new pool was created must not be "
        f"aborted by the stale fallback event, got {outcome!r}"
    )


async def test_an_old_pools_sleeper_still_aborts_after_a_new_pool_is_created():
    """K-2 follow-up #3 (opus review, audit 2026-09-10): the abort signal must
    be bound per pool, not shared. Reproduces the reviewer's exact scenario
    against the real singleton pool: a call is mid-sleep on the CURRENT pool,
    shutdown fires (setting that pool's own event), and a fresh pool is
    created immediately afterward via `run_slack_call` — the OLD pool's
    sleeper must still abort promptly. A single shared Event that shutdown set
    and pool-recreation cleared would silently lose this thread's signal.
    """
    from src.agent.slack_client import SlackShuttingDown, _sleep_interruptibly

    outcome: dict = {}

    def sleeper():
        try:
            _sleep_interruptibly(30.0)
        except Exception as exc:
            outcome["exc"] = exc
        else:
            outcome["completed"] = True

    sleeper_task = asyncio.ensure_future(run_slack_call(sleeper))
    await asyncio.sleep(0.05)  # let the worker thread get into its first slice

    shutdown_slack_executor()  # sets the OLD pool's event; drops the pool ref
    await run_slack_call(lambda: 1)  # forces _get_executor() to mint a NEW pool + event

    await asyncio.wait_for(sleeper_task, timeout=3.0)

    assert isinstance(outcome.get("exc"), SlackShuttingDown), (
        f"the old pool's sleeper must still abort promptly, got {outcome!r}"
    )

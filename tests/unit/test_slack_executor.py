"""``run_slack_call`` runs on its own bounded pool, isolated from every other
``asyncio.to_thread`` user in the process (audit 2026-09-10 R-2).

Reuses the exception-identity pattern from
``tests/unit/test_private_channel_migration.py::TestSlackCallsRunOffTheEventLoop``:
proving a call ran on a worker thread and that its exception propagated
unchanged (not wrapped) is what distinguishes "actually moved off the loop"
from "looks async but isn't".
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import src.services.slack_executor as slack_executor_module
from src.services.slack_executor import (
    _SLACK_EXECUTOR,
    SLACK_IO_MAX_WORKERS,
    run_slack_call,
    shutdown_slack_executor,
)


def test_the_slack_pool_is_a_bounded_dedicated_executor():
    assert isinstance(_SLACK_EXECUTOR, ThreadPoolExecutor)
    assert _SLACK_EXECUTOR._max_workers == SLACK_IO_MAX_WORKERS
    assert _SLACK_EXECUTOR._thread_name_prefix == "slack-io"


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


@pytest.fixture(autouse=True)
def _reset_shutting_down():
    """`shutdown_slack_executor()` now sets the process-wide
    `slack_client.SHUTTING_DOWN` event (K-2) — clear it after each test in
    this file so it does not leak into unrelated tests/processes sharing the
    same interpreter.
    """
    from src.agent.slack_client import SHUTTING_DOWN
    yield
    SHUTTING_DOWN.clear()


def test_shutdown_slack_executor_shuts_down_without_waiting(monkeypatch):
    fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-io-test")
    monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)
    shutdown_slack_executor()
    assert fresh._shutdown, "shutdown_slack_executor() must actually shut the pool down"


def test_shutdown_slack_executor_sets_the_shutting_down_event(monkeypatch):
    """K-2: signals slack_client's sleep loop before shutting the pool down, so
    an in-flight retry sleep can abort rather than block interpreter exit."""
    from src.agent.slack_client import SHUTTING_DOWN
    fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-io-test")
    monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)
    assert not SHUTTING_DOWN.is_set()
    shutdown_slack_executor()
    assert SHUTTING_DOWN.is_set()


async def test_run_slack_call_after_shutdown_raises_a_clear_runtimeerror(monkeypatch):
    """Documented contract (see module docstring): once shut down, `run_slack_call`
    raises rather than silently re-creating the pool or hanging."""
    fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="slack-io-test")
    monkeypatch.setattr(slack_executor_module, "_SLACK_EXECUTOR", fresh)
    shutdown_slack_executor()
    with pytest.raises(RuntimeError, match="shutdown"):
        await run_slack_call(lambda: 1)

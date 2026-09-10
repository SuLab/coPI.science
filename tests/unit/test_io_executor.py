"""``run_blocking`` runs synchronous SES-send calls off the event loop, on its
own bounded pool separate from Slack's (S-7, audit 2026-09-10).

Mirrors tests/unit/test_slack_executor.py's shape for the equivalent module.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import src.services.io_executor as io_executor_module
from src.services.io_executor import (
    IO_MAX_WORKERS,
    _get_executor,
    run_blocking,
    shutdown_io_executor,
)


@pytest.fixture(autouse=True)
def _reset_pool_state():
    """Shut down whatever pool a test leaves assigned to the module global
    (real or a throwaway substituted via monkeypatch), same rationale as
    test_slack_executor.py's fixture -- an un-shutdown ThreadPoolExecutor
    leaks up to IO_MAX_WORKERS live, non-daemon worker threads.
    """
    yield
    shutdown_io_executor()


def test_the_io_pool_is_a_bounded_dedicated_executor():
    pool = _get_executor()
    assert isinstance(pool, ThreadPoolExecutor)
    assert pool._max_workers == IO_MAX_WORKERS
    assert pool._thread_name_prefix == "io-blocking"


async def test_run_blocking_executes_off_the_event_loop_on_an_io_thread():
    loop_thread = threading.get_ident()
    seen: dict = {}

    def fn():
        seen["ident"] = threading.get_ident()
        seen["name"] = threading.current_thread().name

    await run_blocking(fn)
    assert seen["ident"] != loop_thread
    assert seen["name"].startswith("io-blocking")


async def test_run_blocking_forwards_args_and_kwargs_and_returns_the_result():
    def fn(a, b, *, c):
        return a + b + c

    assert await run_blocking(fn, 1, 2, c=3) == 6


async def test_an_exception_from_the_call_propagates_unchanged():
    """Identity, not just type -- a wrapper that re-raised a *new* exception of
    the same class would still pass a ``pytest.raises(_Boom)`` check."""

    class _Boom(Exception):
        pass

    marker = _Boom("boom")

    def fn():
        raise marker

    with pytest.raises(_Boom) as exc_info:
        await run_blocking(fn)
    assert exc_info.value is marker


def test_shutdown_io_executor_shuts_down_without_waiting(monkeypatch):
    fresh = ThreadPoolExecutor(max_workers=2, thread_name_prefix="io-blocking-test")
    monkeypatch.setattr(io_executor_module, "_IO_EXECUTOR", fresh)
    shutdown_io_executor()
    assert fresh._shutdown, "shutdown_io_executor() must actually shut the pool down"


async def test_run_blocking_is_re_entrant_after_a_shutdown():
    await run_blocking(lambda: None)
    shutdown_io_executor()
    # A fresh pool is lazily created on the next call rather than raising.
    assert await run_blocking(lambda: 42) == 42

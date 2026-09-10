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

from src.services.slack_executor import _SLACK_EXECUTOR, run_slack_call


def test_the_slack_pool_is_a_bounded_dedicated_executor():
    assert isinstance(_SLACK_EXECUTOR, ThreadPoolExecutor)
    assert _SLACK_EXECUTOR._max_workers == 8
    assert _SLACK_EXECUTOR._thread_name_prefix == "slack-io"


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

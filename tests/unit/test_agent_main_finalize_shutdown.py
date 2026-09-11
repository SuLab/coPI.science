"""The teardown path must set ``SHUTDOWN_REQUESTED`` unconditionally, not only
via the SIGTERM/SIGINT handler's ``loop.call_later`` timer.

If the first signal only scheduled the Slack-abort event via
``loop.call_later(SHUTDOWN_SLACK_ABORT_GRACE_SECONDS, signal_shutdown)`` and
``sim_engine.start()`` returned before that timer fired (the common case: a
``--max-runtime`` run finishing on schedule, a clean stop, or simply a run
that flushes faster than the 20s grace), the timer was dropped when the
event loop closed and the event was NEVER set — a ``run_slack_call`` queued
just before exit would run to completion (modulo the
``threading._register_atexit`` hook, which is a different pool) rather than
aborting promptly at the point the process has already decided to exit.

``_finalize_shutdown`` is called from ``_run_simulation``'s ``finally`` block,
AFTER ``sim_engine.stop()``'s DB flush, and unconditionally sets the event
(cancelling the pending timer handle first, since it is now moot).
"""

import asyncio
from types import SimpleNamespace

from src.agent import main as _main_module
from src.agent.slack_client import SlackShuttingDown, _sleep_interruptibly
from src.services.slack_executor import run_slack_call

# SHUTDOWN_REQUESTED is cleared before and after every test by
# tests/conftest.py's autouse
# `_clear_slack_shutdown_requested` fixture; no per-file fixture needed here.


async def test_finalize_shutdown_aborts_a_live_sleeper_even_with_no_signal_received():
    loop = asyncio.get_running_loop()
    fake_engine = SimpleNamespace(request_stop=lambda: None)
    shutdown = _main_module._make_shutdown_handler(loop, fake_engine)

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

    # No signal was ever received -- shutdown() was never called, so no
    # call_later timer is even pending. This simulates the teardown path
    # running because sim_engine.start() simply returned (max-runtime, clean
    # stop, exception) rather than because of a SIGTERM.
    _main_module._finalize_shutdown(shutdown)

    await asyncio.wait_for(task, timeout=3.0)

    assert isinstance(outcome.get("exc"), SlackShuttingDown), (
        f"the teardown path must unconditionally signal shutdown, got {outcome!r}"
    )


async def test_finalize_shutdown_cancels_the_pending_grace_timer():
    loop = asyncio.get_running_loop()
    fake_engine = SimpleNamespace(request_stop=lambda: None)
    shutdown = _main_module._make_shutdown_handler(loop, fake_engine)

    shutdown()  # first signal: schedules the grace-delay timer
    # The timer is installed via loop.call_soon_threadsafe (not directly
    # inside the handler, which may run as a true signal.signal callback) —
    # give the loop one tick to run that queued callback before reading the
    # handle back.
    await asyncio.sleep(0)
    timer_handle = shutdown.state["timer_handle"]
    assert timer_handle is not None
    assert not timer_handle.cancelled()

    _main_module._finalize_shutdown(shutdown)

    assert timer_handle.cancelled()

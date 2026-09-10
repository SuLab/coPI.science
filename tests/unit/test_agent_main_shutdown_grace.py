"""M-1 (opus review, audit 2026-09-10): the agent-run process's SIGTERM/SIGINT
handler used to call ``signal_shutdown()`` at t=0, aborting ANY in-flight
Slack retry sleep instantly -- including a typical ~10s Retry-After backoff
that would otherwise finish comfortably inside the runbook's `docker stop -t
30` grace window. That turned an ordinary throttle into a permanently
DB-only thread (`_post_message` records `slack_ts=None` when the post never
reaches Slack), breaking that thread's Slack mirror forever.

The fix delays the abort by ``SHUTDOWN_SLACK_ABORT_GRACE_SECONDS`` (20s,
comfortably under the 30s stop grace) on the FIRST signal, via
``loop.call_later``, so a short in-flight sleep can finish naturally. A
SECOND signal (operator impatience, or a slow shutdown) aborts immediately,
same as before.

``_make_shutdown_handler`` is factored out of ``_run_simulation`` precisely so
this can be pinned without a full DB session factory / agent roster (see
``test_agent_main_shutdown.py``'s docstring for why standing that up here
would be out of proportion).

O-1 (audit 2026-09-10): the handler now calls
``slack_client.signal_shutdown()`` directly (not
``shutdown_slack_executor()`` -- the agent-run process does not own that
pool's lifecycle). There is a single process-wide
``slack_client.SHUTDOWN_REQUESTED`` event shared by every caller regardless
of thread/pool, so a `run_slack_call`-bound sleeper aborts the same way a
main-thread one does.
"""

import asyncio
import threading
import time
from types import SimpleNamespace

from src.agent import main as _main_module
from src.agent.slack_client import SlackShuttingDown, _sleep_interruptibly
from src.services.slack_executor import run_slack_call

# P-4 (opus review, audit 2026-09-10): SHUTDOWN_REQUESTED is cleared before
# and after every test by tests/conftest.py's autouse
# `_clear_slack_shutdown_requested` fixture; no per-file fixture needed here.


async def test_a_single_signal_does_not_abort_a_sleep_shorter_than_the_grace_period(monkeypatch):
    monkeypatch.setattr(_main_module, "SHUTDOWN_SLACK_ABORT_GRACE_SECONDS", 0.3)
    loop = asyncio.get_running_loop()
    fake_engine = SimpleNamespace(request_stop=lambda: None)
    shutdown = _main_module._make_shutdown_handler(loop, fake_engine)

    outcome: dict = {}

    def sleeper():
        try:
            _sleep_interruptibly(0.1)
        except Exception as exc:
            outcome["exc"] = exc
        else:
            outcome["completed"] = True

    t = threading.Thread(target=sleeper, daemon=True)
    t.start()
    shutdown()  # first signal: schedules the abort at +0.3s, must not fire yet
    t.join(timeout=2.0)

    assert outcome.get("completed") is True, (
        "a single shutdown signal must not abort a retry sleep shorter than "
        f"the grace period, got {outcome!r}"
    )


async def test_a_second_signal_aborts_immediately_without_waiting_for_the_grace_period(monkeypatch):
    monkeypatch.setattr(_main_module, "SHUTDOWN_SLACK_ABORT_GRACE_SECONDS", 20)
    loop = asyncio.get_running_loop()
    fake_engine = SimpleNamespace(request_stop=lambda: None)
    shutdown = _main_module._make_shutdown_handler(loop, fake_engine)

    outcome: dict = {}

    def sleeper():
        try:
            _sleep_interruptibly(5.0)
        except Exception as exc:
            outcome["exc"] = exc
        else:
            outcome["completed"] = True

    t = threading.Thread(target=sleeper, daemon=True)
    t.start()
    time.sleep(0.05)  # let the sleeper get into its first slice

    shutdown()  # first signal: schedules the abort 20s out, does not fire yet
    shutdown()  # second signal: aborts immediately, not through call_later
    t.join(timeout=2.0)

    assert not t.is_alive(), "a second signal must abort the sleep promptly"
    assert isinstance(outcome.get("exc"), SlackShuttingDown)


def test_shutdown_slack_abort_grace_seconds_is_documented_under_the_stop_grace():
    # docker stop -t 30 (see docs/README.md / runbook) leaves 10s of headroom.
    assert _main_module.SHUTDOWN_SLACK_ABORT_GRACE_SECONDS == 20


async def test_a_pool_bound_slack_call_aborts_promptly_after_the_second_signal():
    """After M-8, this process's hottest per-tick Slack calls (_post_message,
    the channel/DM/proposal-thread pollers) run through
    src.services.slack_executor's dedicated pool, not directly on this
    event-loop thread. O-1 (audit 2026-09-10): there is a single process-wide
    shutdown event shared by every caller, so the handler's plain
    `signal_shutdown()` call aborts a pool-bound sleeper just as promptly as
    a main-thread one.
    """
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

    shutdown()  # first signal: schedules the abort 20s out, does not fire yet
    shutdown()  # second signal: aborts immediately

    await asyncio.wait_for(task, timeout=3.0)

    assert isinstance(outcome.get("exc"), SlackShuttingDown), (
        f"a pool-bound sleeper must abort promptly after the second signal, got {outcome!r}"
    )

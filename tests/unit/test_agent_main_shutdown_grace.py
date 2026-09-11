"""The agent-run process's SIGTERM/SIGINT handler must not call
``signal_shutdown()`` at t=0, since that would abort ANY in-flight Slack retry
sleep instantly -- including a typical ~10s Retry-After backoff that would
otherwise finish comfortably inside the runbook's `docker stop -t 30` grace
window. Aborting one turns an ordinary throttle into a permanently DB-only
thread (`_post_message` records `slack_ts=None` when the post never reaches
Slack), breaking that thread's Slack mirror forever.

Instead the abort is delayed by ``SHUTDOWN_SLACK_ABORT_GRACE_SECONDS`` (20s,
comfortably under the 30s stop grace) on the FIRST signal, via
``loop.call_later``, so a short in-flight sleep can finish naturally. A
SECOND signal (operator impatience, or a slow shutdown) aborts immediately.

``_make_shutdown_handler`` is factored out of ``_run_simulation`` precisely so
this can be pinned without a full DB session factory / agent roster (see
``test_agent_main_shutdown.py``'s docstring for why standing that up here
would be out of proportion).

The handler calls ``slack_client.signal_shutdown()`` directly (not
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
from src.agent.slack_client import SHUTDOWN_REQUESTED, SlackShuttingDown, _sleep_interruptibly
from src.services.slack_executor import run_slack_call

# SHUTDOWN_REQUESTED is cleared before and after every test by
# tests/conftest.py's autouse
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
    """This process's hottest per-tick Slack calls (_post_message, the
    channel/DM/proposal-thread pollers) run through
    src.services.slack_executor's dedicated pool, not directly on this
    event-loop thread. There is a single process-wide shutdown event shared
    by every caller, so the handler's plain
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


async def test_second_signal_sets_the_event_even_from_a_non_loop_thread():
    """The handler is installed via ``signal.signal`` rather than
    ``loop.add_signal_handler`` precisely so it
    still runs (and the second-signal branch still fires) even when the
    calling thread is not the loop's own thread -- the scenario that matters
    is the loop thread being blocked inside a synchronous Slack call, which
    is indistinguishable, from the handler's point of view, from being
    invoked off-thread. ``request_stop()`` and the second signal's
    ``signal_shutdown()`` call must not depend on the loop being free.
    """
    loop = asyncio.get_running_loop()
    stop_calls: list[None] = []
    fake_engine = SimpleNamespace(_running=True, request_stop=lambda: stop_calls.append(None))
    shutdown = _main_module._make_shutdown_handler(loop, fake_engine)

    def call_from_thread():
        shutdown()  # first signal
        shutdown()  # second signal: must set the event synchronously, here

    t = threading.Thread(target=call_from_thread)
    t.start()
    t.join(timeout=2.0)

    assert not t.is_alive()
    # The flag flip is synchronous (works even against a blocked loop); the
    # full request_stop() (which may touch the loop) is deferred to the
    # loop's own turn via call_soon_threadsafe.
    assert fake_engine._running is False
    await asyncio.sleep(0)
    assert len(stop_calls) >= 1, "request_stop() must run once the loop gets a turn"
    assert SHUTDOWN_REQUESTED.is_set(), (
        "a second signal must set SHUTDOWN_REQUESTED synchronously even when "
        "delivered from a thread other than the event loop's own"
    )


def test_a_signal_after_the_loop_closed_sets_the_event_and_does_not_raise(monkeypatch):
    """signal.signal handlers outlive asyncio.run(); a signal during post-loop
    teardown must not raise 'Event loop is closed' and must not be
    swallowed."""
    import asyncio
    import signal as _signal

    from src.agent import slack_client
    from src.agent.main import _make_shutdown_handler

    restored = []
    monkeypatch.setattr(_signal, "signal", lambda sig, h: restored.append((sig, h)))
    loop = asyncio.new_event_loop()
    loop.close()
    engine = type("E", (), {"_running": True, "request_stop": lambda self: None})()
    handler = _make_shutdown_handler(loop, engine)

    handler(_signal.SIGTERM, None)  # must not raise

    assert slack_client.SHUTDOWN_REQUESTED.is_set()
    assert engine._running is False
    assert (_signal.SIGTERM, _signal.SIG_DFL) in restored


def test_a_second_signal_restores_the_default_action_for_a_third(monkeypatch):
    """After the immediate abort on the second signal, a third signal must
    reach the default action (terminate / KeyboardInterrupt) rather than an
    inert handler against a wedged flush."""
    import asyncio
    import signal as _signal

    from src.agent.main import _make_shutdown_handler

    restored = []
    monkeypatch.setattr(_signal, "signal", lambda sig, h: restored.append((sig, h)))
    loop = asyncio.new_event_loop()
    try:
        engine = type("E", (), {"_running": True, "request_stop": lambda self: None})()
        handler = _make_shutdown_handler(loop, engine)
        handler(_signal.SIGINT, None)
        assert restored == []
        handler(_signal.SIGINT, None)
        assert (_signal.SIGINT, _signal.default_int_handler) in restored
    finally:
        loop.close()


def test_signal_defaults_are_restored_only_after_the_run_status_commit():
    """Restoring SIG_DFL before the SimulationRun status update would let a
    signal during teardown kill the process with the run row stuck at
    status='running'. The restore must be the last thing in the finally."""
    import inspect

    from src.agent.main import _run_simulation

    src = inspect.getsource(_run_simulation)
    assert src.index('run.status = "stopped"') < src.index("_restore_signal_default(sig)")
    assert src.index('"Summary: %s"') < src.index("_restore_signal_default(sig)")


def test_sigint_is_restored_to_the_raising_default_handler(monkeypatch):
    """SIG_DFL for SIGINT is the OS default (terminate, no unwinding); the
    Python-level default_int_handler raises KeyboardInterrupt instead."""
    import signal as _signal

    from src.agent.main import _restore_signal_default

    recorded = []
    monkeypatch.setattr(_signal, "signal", lambda sig, h: recorded.append((sig, h)))
    _restore_signal_default(_signal.SIGINT)
    _restore_signal_default(_signal.SIGTERM)
    assert recorded == [
        (_signal.SIGINT, _signal.default_int_handler),
        (_signal.SIGTERM, _signal.SIG_DFL),
    ]

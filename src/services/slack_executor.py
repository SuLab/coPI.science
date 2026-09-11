"""A dedicated thread pool for synchronous Slack Web API calls.

Each Slack call can block for a long time under a sustained rate-limit
throttle, so sharing the event loop's default ``to_thread`` executor would
let a run of Slack calls starve unrelated non-Slack work. ``run_slack_call``
gives Slack I/O its own bounded pool (sized well above one call flow's
sequential call count, so a second concurrent flow isn't left queuing behind
the first) so a Slack throttle can only ever starve other Slack calls.

Shutdown sets the process-wide ``slack_client.SHUTDOWN_REQUESTED`` event
first (checked before attempt 0 and again in the retry-sleep loop, so queued
or throttled calls abort quickly) and then shuts the pool down with
``wait=False, cancel_futures=False`` so already-queued work still runs to
completion rather than raising ``CancelledError`` mid-turn. It is registered
via ``threading._register_atexit`` (which runs before the interpreter joins
worker threads) with a plain ``atexit.register`` fallback, because a normal
``atexit`` callback would fire too late to interrupt an already-sleeping
worker. The agent-run process does not call this directly — it signals
``slack_client.signal_shutdown()`` itself since it owns no separate pool
lifecycle.
"""

import asyncio
import atexit
import functools
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from src.agent.slack_client import signal_shutdown

_T = TypeVar("_T")

# See "Sizing / queuing bound" above: sized to comfortably exceed the ~8
# sequential Slack calls one private-channel reopen flow makes by itself, so a
# second concurrent flow (another reopen, a GrantBot post, a delegate-name
# lookup) is not left queuing behind the first for a worker thread. Still
# small enough that a sustained throttle across the whole pool reads as a
# Slack-specific incident rather than a process-wide one.
SLACK_IO_MAX_WORKERS = 16

# The pool starts (and becomes, after a shutdown) `None`; `_get_executor()`
# lazily creates a fresh one on demand, guarded by `_POOL_LOCK` so two
# concurrent callers cannot each create and orphan one. See the module
# docstring's "Re-creation after shutdown" for why this does not touch the
# shutdown event in either direction.
_POOL_LOCK = threading.Lock()
_SLACK_EXECUTOR: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _SLACK_EXECUTOR
    with _POOL_LOCK:
        if _SLACK_EXECUTOR is None:
            _SLACK_EXECUTOR = ThreadPoolExecutor(
                max_workers=SLACK_IO_MAX_WORKERS, thread_name_prefix="slack-io",
            )
        return _SLACK_EXECUTOR


async def run_slack_call(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a synchronous Slack call on the dedicated Slack I/O executor.

    Drop-in replacement for ``asyncio.to_thread(fn, *args, **kwargs)`` at every
    Slack call site — same signature, same "runs in a worker thread and awaits
    the result" behaviour, same exception propagation. The only difference is
    *which* thread pool it runs on.

    This does NOT raise after a shutdown — a shut-down pool is lazily
    replaced with a fresh one on the next call, the same way a fresh
    ``asyncio.to_thread`` default executor would be. A real process's
    shutdown path (SIGTERM handling in main.py/worker/main.py) exits the
    interpreter shortly after calling ``shutdown_slack_executor()``
    regardless, so this only matters for a long-lived process that shuts the
    pool down without exiting (or a test process sharing one interpreter
    across many independent lifespans).
    """
    loop = asyncio.get_running_loop()
    executor = _get_executor()
    return await loop.run_in_executor(executor, functools.partial(fn, *args, **kwargs))


def shutdown_slack_executor() -> None:
    """Shut the Slack I/O pool down without blocking on in-flight calls.

    Sets the process-wide ``slack_client.SHUTDOWN_REQUESTED`` event FIRST:
    ``_call_with_retry`` checks it before attempt 0, so a queued-but-unstarted
    call aborts with no network round trip at all,
    and again inside the retry-sleep branch — ``_sleep_interruptibly`` sleeps
    a Retry-After backoff in <=1s slices and checks the event between slices,
    so an in-flight call that is mid-throttle aborts within about a second
    instead of finishing out whatever it had left of the (up to 180s) wait
    budget. This does NOT make an in-flight *HTTP request* interruptible —
    only the pre-attempt check and the retry sleep — so a call whose attempt
    is already in flight when the event is set still runs that one attempt to
    completion; only something not yet started or already sleeping on a 429
    is cut short.

    ``wait=False``: does not block the caller (a FastAPI lifespan or the
    worker's shutdown path) on however long an in-flight Slack call has left
    to run. ``cancel_futures=False`` (deliberately NOT True): a
    ``run_slack_call`` that is already queued but not yet started must still
    run and abort quickly via the event, rather than raise
    ``CancelledError`` into whatever in-flight turn is awaiting it.

    Drops the reference to the now-shut-down pool (setting the module global
    to ``None``) instead of leaving callers pointed at a permanently-dead
    singleton — ``run_slack_call`` lazily creates a fresh pool on its next
    invocation. Safe to call more than once (``ThreadPoolExecutor.shutdown``
    is idempotent; setting an already-set ``Event`` is a no-op; shutting down
    with no pool currently created is a no-op).
    """
    global _SLACK_EXECUTOR
    signal_shutdown()
    with _POOL_LOCK:
        executor, _SLACK_EXECUTOR = _SLACK_EXECUTOR, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=False)


# A plain `atexit.register` callback runs AFTER `threading._shutdown()` has
# already joined every non-daemon thread to completion -- including this
# pool's workers. A worker sleeping through `_sleep_interruptibly` at
# interpreter exit therefore runs out its FULL sleep (nothing has set
# SHUTDOWN_REQUESTED yet) before the atexit callback below ever gets a
# chance to fire.
#
# `threading._register_atexit` hooks run BEFORE that join, so registering
# `signal_shutdown` there sets the event in time for an in-flight sleeper to
# actually abort. It is a private/underscore CPython API (no public
# equivalent exists for "run before thread join"), so it is guarded with
# `hasattr` and falls back to the plain `atexit.register` below on any
# interpreter that lacks it -- on that fallback interpreter a sleeper still
# runs to completion, but the pool itself is at least shut down cleanly
# rather than leaking.
try:
    threading._register_atexit(signal_shutdown)  # type: ignore[attr-defined]
except (AttributeError, RuntimeError):  # pragma: no cover - non-CPython, or
    # imported during interpreter shutdown ("can't register atexit after
    # shutdown"); fall back rather than failing the import.
    atexit.register(signal_shutdown)

# Backstop for any exit path that does not run the FastAPI lifespan or the
# worker's own shutdown call (a crash, a one-off script that imports this
# module directly, a signal neither of those handles) — so this pool is never
# the reason interpreter shutdown hangs waiting to join its worker threads.
# This still runs (in normal atexit order, i.e. AFTER the thread join and
# AFTER the `_register_atexit` hook above), so it is what actually shuts the
# pool down/drops the module reference; the hook above only exists to set the
# shutdown event early enough for a sleeper to see it before that join.
atexit.register(shutdown_slack_executor)

"""A dedicated thread pool for synchronous Slack Web API calls.

Every ``asyncio.to_thread`` wrapper around a blocking ``AgentSlackClient``/
``httpx`` Slack call previously shared the event loop's *default* executor
(``min(32, os.cpu_count() + 4)`` threads) with every other ``to_thread`` user in
the process. Each Slack call can block for up to
``slack_client.RATE_LIMIT_WAIT_BUDGET_SECONDS`` (180s) under a sustained
throttle, so a run of Slack calls could occupy enough of that shared pool to
starve unrelated ``to_thread`` work (DB migrations off the loop, CPU-bound
helpers, etc.) that has nothing to do with Slack.

``run_slack_call`` gives Slack I/O its own bounded pool instead, so a Slack
throttle can only ever starve *other Slack calls*, never the rest of the
process. Use it everywhere a synchronous Slack Web API call (through
``AgentSlackClient`` or the ``slack_web``/``slack_provisioning`` httpx helpers)
is moved off the event loop; leave non-Slack ``to_thread`` call sites alone.

**Shutdown (O-1, audit 2026-09-10 — replaces the per-pool
event/thread-local/pending-flag design from K-2/M-2/N-2, which regressed in
three consecutive review rounds).** ``shutdown_slack_executor()`` sets the
single process-wide ``slack_client.SHUTDOWN_REQUESTED`` event (sticky for the
rest of the process's life — see that module) and then calls
``pool.shutdown(wait=False, cancel_futures=False)``: queued-but-not-yet-started
work still runs (it is NOT dropped — a queued ``run_slack_call`` must
execute and abort quickly via the event rather than raise ``CancelledError``
into an in-flight turn), and an in-flight call already sleeping through a
Retry-After backoff aborts within about a second because
``slack_client._sleep_interruptibly`` checks the event between <=1s slices.
A call that is NOT currently sleeping on a throttle (e.g. blocked on the
underlying HTTP request itself) is not interrupted by this at all; it still
runs to completion or times out on its own.

Call this from every process's shutdown path: the FastAPI app's lifespan
(``src/main.py``) and the worker's shutdown path (``src/worker/main.py``) call it
explicitly; ``atexit.register`` below is a backstop for anything that exits
without running either (a crash path, a script that imports this module
directly, etc.) so the pool is never the reason interpreter shutdown hangs.

**The agent-run process (src/agent/main.py) does NOT call this.** Its worker
threads exit with the process once their sleeps become interruptible via the
shared event; there is no separate pool lifecycle to tear down there. It
calls ``slack_client.signal_shutdown()`` directly instead (with a grace
delay on the first signal — see ``src/agent/main.py``).

**Re-creation after shutdown.** ``_get_executor()`` lazily creates a fresh
plain ``ThreadPoolExecutor`` when ``_SLACK_EXECUTOR`` is ``None`` — including
right after a ``shutdown_slack_executor()`` call, so ``run_slack_call`` is
re-entrant across a shutdown rather than raising. It does NOT touch
``SHUTDOWN_REQUESTED`` in either direction: because that event is sticky for
the process's life, a call queued on the fresh pool is expected to still
abort immediately via ``_sleep_interruptibly``. This matters mainly for a
test process sharing one interpreter across many independent
lifespans/pools — real production shutdown paths exit the interpreter
shortly after calling this anyway.

**Sizing / queuing bound (opus review follow-up, audit 2026-09-10).** A single
`private_channels.migrate_public_thread_to_private` call ("reopen a public
thread into a private channel") makes up to ~8 sequential `run_slack_call`s by
itself (two `_make_client`s, `create_private_channel`, two `invite_to_channel`s,
`_resolve_channel_id`, 2+ handover `post_message`s, a close-marker
`post_message`, and an other-PI invite + DM) — sequential, not concurrent,
because each `await`s the last. ``SLACK_IO_MAX_WORKERS`` therefore has to
comfortably exceed the call count of one flow, not just be "more than one": a
pool sized at exactly one flow's call count leaves zero headroom for a SECOND
concurrent reopen (or a GrantBot post, or a delegate-name lookup) to make
Slack calls at all — those would simply queue behind whichever calls are
already using every worker, indistinguishable from a Slack throttle from the
caller's point of view. 16 gives roughly 2x one flow's sequential call count,
so two whole reopens (or one reopen plus several smaller flows) can be
in-flight without one starving the other for a worker thread; anything queued
beyond that still eventually runs — the pool is a queue, not a hard cap on
concurrency — but waits for a free worker like any bounded pool.
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
    ``_call_with_retry``/``_sleep_interruptibly`` sleeps a Retry-After backoff
    in <=1s slices and checks this event between slices, so an in-flight call
    that is mid-throttle aborts within about a second instead of finishing
    out whatever it had left of the (up to 180s) wait budget. This does NOT
    make an in-flight *HTTP request* interruptible — only the retry sleep —
    so a call that is not currently throttled still runs to completion; only
    something already sleeping on a 429 is cut short.

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


# Backstop for any exit path that does not run the FastAPI lifespan or the
# worker's own shutdown call (a crash, a one-off script that imports this
# module directly, a signal neither of those handles) — so this pool is never
# the reason interpreter shutdown hangs waiting to join its worker threads.
atexit.register(shutdown_slack_executor)

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

**Shutdown (opus review follow-up, audit 2026-09-10).** A bare module-level
``ThreadPoolExecutor`` is never torn down on its own: the interpreter's default
shutdown behaviour joins every non-daemon thread pool at exit, so a single
in-flight Slack call blocked on a sustained throttle (up to
``RATE_LIMIT_WAIT_BUDGET_SECONDS`` — 180s) can hold the whole process open past
``docker stop -t 30``'s grace period, which then SIGKILLs it — losing whatever
that thread was doing mid-flight rather than letting it finish or fail cleanly.
``shutdown_slack_executor()`` sets the CURRENT pool's own shutdown event (a
per-pool ``threading.Event``, not a single shared one — see the K-2 follow-up
note below) and then calls ``.shutdown(wait=False, cancel_futures=True)``:
queued-but-not-yet-started work is dropped immediately. ``wait=False`` on its
own does NOT stop an already-running call — ``ThreadPoolExecutor`` worker
threads are ordinary (non-daemon) threads, and the interpreter's own atexit
machinery still joins them after this module's ``atexit`` backstop runs, so a
thread genuinely still blocked would hold up exit regardless of what this
function does (K-2, opus review, audit 2026-09-10, fixing an inaccurate claim
in an earlier version of this docstring). What actually makes shutdown prompt
is ``slack_client._call_with_retry`` sleeping a Retry-After backoff in <=1s
slices and checking its bound shutdown event between slices, aborting with a
``SlackApiError`` (``SlackShuttingDown``) as soon as it is set — so a call
that is mid-throttle gives up within about a second. A call that is NOT
currently sleeping on a throttle (e.g. blocked on the underlying HTTP request
itself) is not interrupted by this at all; it still runs to completion or
times out on its own.

**Per-pool shutdown event (K-2 follow-up, opus review, audit 2026-09-10).** A
single shared shutdown ``Event`` cannot survive a shutdown/re-create cycle
(K-9, below) correctly: clearing it the moment a fresh pool is created — so
that new pool's OWN retries do not abort instantly — would also silence the
signal for an OLD pool's worker thread still sleeping through a backoff right
now. Each pool generation gets its own ``threading.Event``
(``_CURRENT_SHUTDOWN_EVENT``), bound into every worker thread's thread-local
at thread start via ``ThreadPoolExecutor(initializer=bind_shutdown_event,
initargs=(event,))``, so a call always checks the event for the SPECIFIC pool
it is running on — a fresh pool's fresh event, or an old pool's already-set
one — never whatever pool happens to be "current" module-wide by the time the
check runs.

Call this from every process's shutdown path: the FastAPI app's lifespan
(``src/main.py``) and the worker's shutdown path (``src/worker/main.py``) call it
explicitly; ``atexit.register`` below is a backstop for anything that exits
without running either (a crash path, a script that imports this module
directly, etc.) so the pool is never the reason interpreter shutdown hangs.

**Contract after shutdown (revised by K-9, audit 2026-09-10)**: ``run_slack_call``
IS re-entrant across a shutdown — the pool is created lazily, so a call after
``shutdown_slack_executor()`` transparently creates a fresh pool rather than
raising. The pre-K-9 contract (raise ``RuntimeError``, never re-create) broke
any process that shares one interpreter across more than one lifespan, most
concretely the test suite: many unrelated integration tests spin up
``create_app()``'s ASGI lifespan via ``httpx.ASGITransport`` for reasons that
have nothing to do with Slack, and its shutdown path calls
``shutdown_slack_executor()`` -- permanently killing the process-wide
singleton for every OTHER test in the same pytest process. A real process's
shutdown path (SIGTERM in main.py/worker/main.py) still exits the interpreter
shortly after calling this, so the lazy re-create is mostly invisible there;
it exists for exactly the shared-interpreter case above.

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

from src.agent.slack_client import bind_shutdown_event

_T = TypeVar("_T")

# See "Sizing / queuing bound" above: sized to comfortably exceed the ~8
# sequential Slack calls one private-channel reopen flow makes by itself, so a
# second concurrent flow (another reopen, a GrantBot post, a delegate-name
# lookup) is not left queuing behind the first for a worker thread. Still
# small enough that a sustained throttle across the whole pool reads as a
# Slack-specific incident rather than a process-wide one.
SLACK_IO_MAX_WORKERS = 16

# K-9 (audit 2026-09-10): the pool used to be created once at import time and
# never re-created. `shutdown_slack_executor()` is called from every
# lifespan/worker shutdown path (src/main.py, src/worker/main.py) AND from
# this module's own atexit backstop, so any test process that spins up
# `create_app()`'s ASGI lifespan even once (most of the httpx-ASGITransport
# integration tests do, incidentally, for reasons unrelated to Slack)
# permanently shut this singleton down for the rest of that interpreter --
# every later `run_slack_call` anywhere else in the same pytest process then
# raised "cannot schedule new futures after shutdown", regardless of whether
# THAT test ever touched shutdown itself. `_SLACK_EXECUTOR` now starts (and
# becomes, after a shutdown) `None`; `_get_executor()` lazily creates a fresh
# pool on demand, guarded by `_POOL_LOCK` so two concurrent callers cannot
# each create and orphan one.
#
# K-2 follow-up #3 (opus review, audit 2026-09-10): a shared shutdown Event
# cannot survive a shutdown/re-create cycle correctly -- clearing it the
# moment a new pool is created (so the NEW pool's retries do not abort
# instantly) would also silence the abort signal for an OLD pool's worker
# thread still sleeping through a backoff right now, defeating K-2 for
# exactly the thread it exists to interrupt. `_CURRENT_SHUTDOWN_EVENT` is a
# fresh `threading.Event` per pool generation, bound into each worker
# thread's thread-local via `initializer=bind_shutdown_event` at thread
# start (see slack_client.py) -- so a call keeps checking the event for the
# SPECIFIC pool it started on, regardless of what "current" means by the
# time the check runs.
_POOL_LOCK = threading.Lock()
_SLACK_EXECUTOR: ThreadPoolExecutor | None = None
_CURRENT_SHUTDOWN_EVENT: threading.Event | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _SLACK_EXECUTOR, _CURRENT_SHUTDOWN_EVENT
    with _POOL_LOCK:
        if _SLACK_EXECUTOR is None:
            _CURRENT_SHUTDOWN_EVENT = threading.Event()
            _SLACK_EXECUTOR = ThreadPoolExecutor(
                max_workers=SLACK_IO_MAX_WORKERS, thread_name_prefix="slack-io",
                initializer=bind_shutdown_event, initargs=(_CURRENT_SHUTDOWN_EVENT,),
            )
        return _SLACK_EXECUTOR


async def run_slack_call(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a synchronous Slack call on the dedicated Slack I/O executor.

    Drop-in replacement for ``asyncio.to_thread(fn, *args, **kwargs)`` at every
    Slack call site — same signature, same "runs in a worker thread and awaits
    the result" behaviour, same exception propagation. The only difference is
    *which* thread pool it runs on.

    Contract after a shutdown (K-9, audit 2026-09-10): this does NOT raise —
    a shut-down pool is lazily replaced with a fresh one on the next call, the
    same way a fresh ``asyncio.to_thread`` default executor would be. A
    process's *real* shutdown path (SIGTERM handling in main.py/worker/main.py)
    exits the interpreter shortly after calling ``shutdown_slack_executor()``
    regardless, so this only matters for a long-lived process that shuts the
    pool down without exiting (or a test process sharing one interpreter
    across many independent lifespans).
    """
    loop = asyncio.get_running_loop()
    executor = _get_executor()
    return await loop.run_in_executor(executor, functools.partial(fn, *args, **kwargs))


def shutdown_slack_executor() -> None:
    """Shut the Slack I/O pool down without blocking on in-flight calls.

    Sets the CURRENT pool's own shutdown event FIRST (K-2, opus review, audit
    2026-09-10; made per-pool by a K-2 follow-up, same day):
    ``_call_with_retry`` sleeps a Retry-After backoff in <=1s slices and
    checks this thread's bound event between slices, so an in-flight call
    that is mid-throttle aborts within about a second instead of finishing
    out whatever it had left of the (up to 180s) wait budget. This does NOT
    make an in-flight *HTTP request* interruptible — only the retry sleep —
    so a call that is not currently throttled still runs to completion; only
    something already sleeping on a 429 is cut short. Setting THIS pool's own
    event (rather than a single shared one) means a subsequent
    ``_get_executor()`` call can safely start the next pool with a fresh,
    unset event of its own — the old pool's worker threads keep whatever
    event they were bound to at their own start, so this shutdown's signal
    reaches them regardless of what pool is "current" by the time they next
    check it.

    ``wait=False``: does not block the caller (a FastAPI lifespan or the
    worker's shutdown path) on however long an in-flight Slack call has left to
    run. ``cancel_futures=True``: drops anything still queued (not yet
    started) rather than starting it during shutdown.

    K-9 (audit 2026-09-10): drops the reference to the now-shut-down pool
    (setting the module global to ``None``) instead of leaving callers pointed
    at a permanently-dead singleton — ``run_slack_call`` lazily creates a
    fresh pool on its next invocation. Safe to call more than once
    (``ThreadPoolExecutor.shutdown`` is idempotent; setting an already-set
    ``Event`` is a no-op; shutting down with no pool currently created is a
    no-op).
    """
    global _SLACK_EXECUTOR, _CURRENT_SHUTDOWN_EVENT
    with _POOL_LOCK:
        executor, _SLACK_EXECUTOR = _SLACK_EXECUTOR, None
        event, _CURRENT_SHUTDOWN_EVENT = _CURRENT_SHUTDOWN_EVENT, None
    if event is not None:
        event.set()
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


# Backstop for any exit path that does not run the FastAPI lifespan or the
# worker's own shutdown call (a crash, a one-off script that imports this
# module directly, a signal neither of those handles) — so this pool is never
# the reason interpreter shutdown hangs waiting to join its worker threads.
atexit.register(shutdown_slack_executor)

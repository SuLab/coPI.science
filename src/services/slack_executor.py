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
``shutdown_slack_executor()`` sets ``slack_client.SHUTTING_DOWN`` and then calls
``.shutdown(wait=False, cancel_futures=True)``: queued-but-not-yet-started work is
dropped immediately. ``wait=False`` on its own does NOT stop an already-running
call — ``ThreadPoolExecutor`` worker threads are ordinary (non-daemon) threads,
and the interpreter's own atexit machinery still joins them after this module's
``atexit`` backstop runs, so a thread genuinely still blocked would hold up exit
regardless of what this function does (K-2, opus review, audit 2026-09-10, fixing
an inaccurate claim in an earlier version of this docstring). What actually makes
shutdown prompt is ``slack_client._call_with_retry`` sleeping a Retry-After
backoff in <=1s slices and checking ``SHUTTING_DOWN`` between slices, aborting
with a ``SlackApiError`` as soon as it is set — so a call that is mid-throttle
gives up within about a second. A call that is NOT currently sleeping on a
throttle (e.g. blocked on the underlying HTTP request itself) is not
interrupted by this at all; it still runs to completion or times out on its own.

Call this from every process's shutdown path: the FastAPI app's lifespan
(``src/main.py``) and the worker's shutdown path (``src/worker/main.py``) call it
explicitly; ``atexit.register`` below is a backstop for anything that exits
without running either (a crash path, a script that imports this module
directly, etc.) so the pool is never the reason interpreter shutdown hangs.

**Contract after shutdown**: ``run_slack_call`` is not re-entrant across a
shutdown — a call after ``shutdown_slack_executor()`` raises ``RuntimeError``
(``ThreadPoolExecutor``'s own "cannot schedule new futures after shutdown"),
not a silently re-created pool. Every caller here already treats an unhandled
exception from a Slack call as a real failure (see e.g. `private_channels.py`'s
try/except around the Slack side-effects), so this fails the same way a live
Slack error would, rather than needing a new failure mode of its own.

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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from src.agent.slack_client import SHUTTING_DOWN

_T = TypeVar("_T")

# See "Sizing / queuing bound" above: sized to comfortably exceed the ~8
# sequential Slack calls one private-channel reopen flow makes by itself, so a
# second concurrent flow (another reopen, a GrantBot post, a delegate-name
# lookup) is not left queuing behind the first for a worker thread. Still
# small enough that a sustained throttle across the whole pool reads as a
# Slack-specific incident rather than a process-wide one.
SLACK_IO_MAX_WORKERS = 16

_SLACK_EXECUTOR = ThreadPoolExecutor(
    max_workers=SLACK_IO_MAX_WORKERS, thread_name_prefix="slack-io"
)


async def run_slack_call(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a synchronous Slack call on the dedicated Slack I/O executor.

    Drop-in replacement for ``asyncio.to_thread(fn, *args, **kwargs)`` at every
    Slack call site — same signature, same "runs in a worker thread and awaits
    the result" behaviour, same exception propagation. The only difference is
    *which* thread pool it runs on.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_SLACK_EXECUTOR, functools.partial(fn, *args, **kwargs))


def shutdown_slack_executor() -> None:
    """Shut the Slack I/O pool down without blocking on in-flight calls.

    Sets ``slack_client.SHUTTING_DOWN`` FIRST (K-2, opus review, audit
    2026-09-10): ``_call_with_retry`` sleeps a Retry-After backoff in <=1s
    slices and checks this event between slices, so an in-flight call that is
    mid-throttle aborts within about a second instead of finishing out
    whatever it had left of the (up to 180s) wait budget. This does NOT make
    an in-flight *HTTP request* interruptible — only the retry sleep — so a
    call that is not currently throttled still runs to completion; only
    something already sleeping on a 429 is cut short.

    ``wait=False``: does not block the caller (a FastAPI lifespan or the
    worker's shutdown path) on however long an in-flight Slack call has left to
    run. ``cancel_futures=True``: drops anything still queued (not yet
    started) rather than starting it during shutdown. Safe to call more than
    once (``ThreadPoolExecutor.shutdown`` is idempotent; setting an already-set
    ``Event`` is a no-op).
    """
    SHUTTING_DOWN.set()
    _SLACK_EXECUTOR.shutdown(wait=False, cancel_futures=True)


# Backstop for any exit path that does not run the FastAPI lifespan or the
# worker's own shutdown call (a crash, a one-off script that imports this
# module directly, a signal neither of those handles) — so this pool is never
# the reason interpreter shutdown hangs waiting to join its worker threads.
atexit.register(shutdown_slack_executor)

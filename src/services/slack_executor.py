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
``shutdown_slack_executor()`` calls ``.shutdown(wait=False, cancel_futures=True)``:
queued-but-not-yet-started work is dropped immediately, already-running calls are
abandoned as daemon-adjacent background threads rather than blocking process exit
(``ThreadPoolExecutor`` worker threads are **not** daemon threads by default, but
``wait=False`` means *this call* does not block on them — the interpreter's own
atexit handling is what would otherwise wait, and this module's own ``atexit``
registration runs before that point).

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
"""

import asyncio
import atexit
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

_T = TypeVar("_T")

# 8 workers: comfortably more than the number of Slack calls any single turn or
# request makes concurrently, small enough that a sustained throttle across the
# whole pool is a Slack-specific incident rather than a process-wide one.
_SLACK_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="slack-io")


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

    ``wait=False``: does not block the caller (a FastAPI lifespan or the
    worker's shutdown path) on however long an in-flight Slack call has left to
    run — that call may still be sleeping through a Retry-After when this
    fires. ``cancel_futures=True``: drops anything still queued (not yet
    started) rather than starting it during shutdown. Safe to call more than
    once (``ThreadPoolExecutor.shutdown`` is idempotent).
    """
    _SLACK_EXECUTOR.shutdown(wait=False, cancel_futures=True)


# Backstop for any exit path that does not run the FastAPI lifespan or the
# worker's own shutdown call (a crash, a one-off script that imports this
# module directly, a signal neither of those handles) — so this pool is never
# the reason interpreter shutdown hangs waiting to join its worker threads.
atexit.register(shutdown_slack_executor)

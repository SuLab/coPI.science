"""A dedicated thread pool for synchronous, non-Slack blocking I/O (S-7, audit
2026-09-10) -- currently just outbound SES email sends.

``src/services/email_inbound.py``'s ``_send_*`` helpers and
``src/services/email_notifications.py``'s ``send_html_email_outcome`` /
``_send_html_email`` construct a ``boto3`` SES client and call
``send_email``/``send_raw_email`` synchronously. Every async call site that
invoked one of these directly ran that blocking network call ON the worker's
single event loop, stalling every other coroutine (Slack polling, DB writes,
other pending email sends) for however long SES takes to respond -- the same
class of defect ``src/services/slack_executor.py`` exists to close for Slack
Web API calls.

``run_blocking`` gives this kind of call its own bounded pool, separate from
Slack's: a slow/blocked SES send must not starve Slack I/O (or vice versa),
and neither should starve the loop's default executor used by unrelated
``asyncio.to_thread`` callers. The sync helper signatures in
``email_inbound``/``email_notifications`` are left exactly as they are; only
the ASYNC call sites change, from calling them directly to
``await run_blocking(fn, ...)``.

**Shutdown.** Mirrors ``slack_executor.shutdown_slack_executor()``'s
wiring: ``shutdown_io_executor()`` calls ``pool.shutdown(wait=False,
cancel_futures=False)`` so queued-but-unstarted work still runs (never
dropped) and the caller (a FastAPI lifespan / worker shutdown path) is never
blocked on however long an in-flight SES call has left. Unlike Slack's pool,
there is no shared "abort quickly" event to set first -- an SES
``send_email``/``send_raw_email`` call has no interruptible retry-sleep of
its own to short-circuit, so a call already in flight when this runs simply
finishes on its own (or times out via boto3's own connect/read timeouts).
Registered via ``threading._register_atexit`` (falling back to
``atexit.register`` on a non-CPython interpreter or one already mid-shutdown)
for the same reason as the Slack pool: a plain ``atexit.register`` callback
only runs AFTER ``threading._shutdown()`` has already joined every
non-daemon thread, including this pool's workers, to completion.

Call this from every process's shutdown path that also calls
``shutdown_slack_executor()`` -- the FastAPI app's lifespan
(``src/main.py``) and the worker's shutdown path (``src/worker/main.py``).
"""

import asyncio
import atexit
import functools
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

_T = TypeVar("_T")

# 8, not SLACK_IO_MAX_WORKERS' 16 -- there is no equivalent to a single
# private-channel reopen flow's ~8 sequential Slack calls here; every SES
# send this pool serves is a single, independent, non-chained network call.
# 8 comfortably covers several concurrent sends (a batch of
# proposal-notification emails, a status overview run, a couple of
# inbound-reply confirmations) without sizing this pool for a call pattern
# (one flow issuing many sequential calls) that does not exist for email.
IO_MAX_WORKERS = 8

_POOL_LOCK = threading.Lock()
_IO_EXECUTOR: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _IO_EXECUTOR
    with _POOL_LOCK:
        if _IO_EXECUTOR is None:
            _IO_EXECUTOR = ThreadPoolExecutor(
                max_workers=IO_MAX_WORKERS, thread_name_prefix="io-blocking",
            )
        return _IO_EXECUTOR


async def run_blocking(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Run a synchronous blocking call (currently: SES sends) on this
    module's dedicated pool.

    Drop-in replacement for ``asyncio.to_thread(fn, *args, **kwargs)`` at
    every such call site -- same signature, same "runs in a worker thread
    and awaits the result" behaviour, same exception propagation. The only
    difference is which thread pool it runs on.

    Does NOT raise after a shutdown: a shut-down pool is lazily replaced
    with a fresh one on the next call, the same way ``run_slack_call`` is
    re-entrant across ``shutdown_slack_executor()``.
    """
    loop = asyncio.get_running_loop()
    executor = _get_executor()
    return await loop.run_in_executor(executor, functools.partial(fn, *args, **kwargs))


def shutdown_io_executor() -> None:
    """Shut this pool down without blocking on in-flight calls.

    ``wait=False``: never blocks the caller (a FastAPI lifespan or the
    worker's shutdown path) on however long an in-flight SES call has left.
    ``cancel_futures=False``: a queued-but-not-yet-started call still runs
    to completion rather than raising ``CancelledError`` into whatever
    in-flight turn is awaiting it.

    Drops the reference to the now-shut-down pool so ``run_blocking`` lazily
    creates a fresh one on its next call. Safe to call more than once.
    """
    global _IO_EXECUTOR
    with _POOL_LOCK:
        executor, _IO_EXECUTOR = _IO_EXECUTOR, None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=False)


# Backstop for any exit path that does not run the FastAPI lifespan or the
# worker's own shutdown call (a crash, a one-off script importing this
# module directly) -- so this pool is never the reason interpreter shutdown
# hangs waiting to join its worker threads. `threading._register_atexit`
# hooks run BEFORE `threading._shutdown()`'s non-daemon-thread join; a plain
# `atexit.register` callback (the fallback here) only runs AFTER it, same
# ordering rationale as slack_executor.py's P-1.
try:
    threading._register_atexit(shutdown_io_executor)  # type: ignore[attr-defined]
except (AttributeError, RuntimeError):  # pragma: no cover - non-CPython, or
    # imported during interpreter shutdown ("can't register atexit after
    # shutdown"); fall back rather than failing the import.
    atexit.register(shutdown_io_executor)

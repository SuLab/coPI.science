"""Request pacing and bounded retries shared by the HTTP clients (DP-14).

One `Pacer` per upstream (NCBI, NIH RePORTER, USPTO ODP), at the interval each
client used before this module existed. The algorithm is the one the three copies
shared: space request STARTS at least `interval` apart, process-wide. The
read-modify-write of `_next_slot` has no await between read and write, so it is
atomic on the event loop — no asyncio lock (a module-level asyncio primitive binds
to the first event loop that touches it, which breaks under pytest's per-test loops).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})

# Module-level alias so tests can patch the sleep without patching the global.
_sleep = asyncio.sleep


class Pacer:
    def __init__(self, interval: float | Callable[[], float]) -> None:
        self._interval = interval
        self._next_slot = 0.0

    def _current_interval(self) -> float:
        return self._interval() if callable(self._interval) else self._interval

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        wait = self._next_slot - now
        self._next_slot = max(now, self._next_slot) + self._current_interval()
        if wait > 0:
            await asyncio.sleep(wait)

    def reset(self) -> None:
        """Tests only: forget the schedule (the old copies reset `_next_slot = 0.0`)."""
        self._next_slot = 0.0


async def with_retries(
    call: Callable[[], Awaitable[httpx.Response]],
    *,
    attempts: int,
    backoff: Callable[[int], float],
    retry_statuses: frozenset[int] = frozenset(),
    retry_exceptions: tuple[type[BaseException], ...] = (),
    pacer: Pacer | None = None,
    pace_each_attempt: bool = True,
) -> httpx.Response:
    """Run `call` up to `attempts` times. A listed status or exception on a non-final
    attempt sleeps `backoff(attempt)` and retries; the final attempt's response is
    returned whatever its status (the caller raises), and its exception propagates."""
    for attempt in range(attempts):
        last = attempt == attempts - 1
        if pacer is not None and (pace_each_attempt or attempt == 0):
            await pacer.wait()
        try:
            resp = await call()
        except retry_exceptions:
            if last:
                raise
            await _sleep(backoff(attempt))
            continue
        if resp.status_code in retry_statuses and not last:
            await _sleep(backoff(attempt))
            continue
        return resp
    raise RuntimeError("unreachable")

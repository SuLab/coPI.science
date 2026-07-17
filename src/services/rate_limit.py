"""Lightweight in-process rate limiting for public, unauthenticated endpoints.

This is a best-effort, per-worker sliding-window limiter — a defense-in-depth
layer that bounds abuse of anonymous write endpoints (proposal votes, waitlist
signups) even when the nginx edge limits (see ``nginx.conf``) are bypassed
(e.g. the app container reached directly at ``app:8000``).

It is deliberately *not* a substitute for the edge limits: the window state is
per-process and not shared across workers or restarts. It exists so a single
process still refuses obviously abusive bursts on its own.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import Request


def client_ip(request: Request) -> str:
    """Best-effort real client IP for a request behind our nginx reverse proxy.

    Our nginx sets ``X-Real-IP`` to ``$remote_addr`` and appends to
    ``X-Forwarded-For`` (see ``nginx.conf``), so those headers reflect the true
    peer in production. When the app is reached directly (dev / tests) the
    headers are absent and we fall back to the socket peer. This value is only
    used as a rate-limit bucket key, never for an authorization decision, so a
    forged header at worst lets an attacker share/split their own bucket.
    """
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class SlidingWindowRateLimiter:
    """A simple thread-safe fixed-memory sliding-window counter, keyed by string.

    ``allow(key)`` returns ``False`` once more than ``max_events`` calls for that
    key have arrived within the trailing ``window_seconds``. Expired keys are
    swept opportunistically so memory stays bounded by the number of *active*
    keys rather than growing forever.
    """

    def __init__(self, max_events: int, window_seconds: float):
        self.max_events = max_events
        self.window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()
        self._last_sweep = 0.0

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._sweep(now)
            dq = self._events[key]
            cutoff = now - self.window
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= self.max_events:
                return False
            dq.append(now)
            return True

    def _sweep(self, now: float) -> None:
        """Drop fully-expired buckets so idle keys don't accumulate forever."""
        if now - self._last_sweep < self.window:
            return
        self._last_sweep = now
        cutoff = now - self.window
        for k in list(self._events):
            dq = self._events[k]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if not dq:
                del self._events[k]

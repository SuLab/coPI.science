"""Per-key asyncio locks with deadlock-free multi-acquire.

Two call sites mutate more than one agent's state: _close_thread writes the
other agent's active_threads, and _evict_dead_thread writes every agent's. Two
of those running in opposite orders deadlock, so multi-key acquisition is
always done in sorted key order — via acquire_all, which is the only sanctioned
way to hold more than one lock at a time.
"""

import asyncio
from contextlib import asynccontextmanager


class LockRegistry:
    """Lazily-created asyncio.Lock per key. Loop-only; not thread-safe.

    Same invariant as MessageLog: safe to call from coroutines sharing one
    event-loop thread, not safe from a worker thread (e.g. inside
    asyncio.to_thread) — ``get``'s check-then-create on ``self._locks`` is
    unguarded and would race across threads.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @asynccontextmanager
    async def acquire_all(self, *keys: str):
        """Acquire every key, in sorted order, releasing in reverse.

        Sorting establishes one global acquisition order across every caller,
        so two callers requesting the same set of keys in opposite order can
        never form a circular wait. Releases everything it acquired, in
        reverse order, even if a later acquisition fails or the body raises.
        """
        ordered = sorted(set(keys))
        acquired: list[asyncio.Lock] = []
        try:
            for key in ordered:
                lock = self.get(key)
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

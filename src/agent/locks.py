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
    """Lazily-created asyncio.Lock per key, refcount-evicted. Loop-only.

    Same loop-only invariant as MessageLog: safe to call from coroutines
    sharing one event-loop thread, not safe from a worker thread (e.g. inside
    asyncio.to_thread) — the check-then-create in ``get`` and the refcount
    read-modify-write in ``acquire_all`` are both unguarded and would race
    across threads.

    Eviction happens ONLY at refcount zero: every ``acquire_all`` registers
    its intent for all its keys SYNCHRONOUSLY, before its first await, so
    "refcount zero" means no holder, no waiter, and no task between
    registration and acquisition. Evicting any earlier splits mutual
    exclusion: between a holder's ``release()`` and a waiter's wakeup the lock
    reports unlocked while the waiter still references the old object, and a
    fresh Lock for the same key would let two tasks into one critical section.
    Pinned by tests/unit/test_lock_registry.py.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def __len__(self) -> int:
        return len(self._locks)

    @asynccontextmanager
    async def acquire_all(self, *keys: str):
        """Acquire every key, in sorted order, releasing in reverse.

        Sorting establishes one global acquisition order across every caller,
        so two callers requesting the same set of keys in opposite order can
        never form a circular wait. Releases everything it acquired, in
        reverse order, even if a later acquisition fails or the body raises.
        """
        ordered = sorted(set(keys))
        for key in ordered:  # register intent BEFORE any await
            self._refs[key] = self._refs.get(key, 0) + 1
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
            for key in ordered:
                self._refs[key] -= 1
                if self._refs[key] <= 0:
                    del self._refs[key]
                    self._locks.pop(key, None)

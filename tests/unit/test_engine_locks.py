"""_close_thread mutates the OTHER agent's state, so two closes in opposite
directions would deadlock on per-agent locks. Sorted acquisition is the fix,
and it must be the only sanctioned way to take more than one."""
import asyncio

import pytest

from src.agent.locks import LockRegistry


def test_get_returns_the_same_lock_for_a_key():
    r = LockRegistry()
    assert r.get("a") is r.get("a")
    assert r.get("a") is not r.get("b")


@pytest.mark.asyncio
async def test_acquire_all_is_order_independent():
    r = LockRegistry()

    async def forward():
        async with r.acquire_all("wang", "blackbird"):
            await asyncio.sleep(0.01)

    async def backward():
        async with r.acquire_all("blackbird", "wang"):
            await asyncio.sleep(0.01)

    # Deadlocks without sorted acquisition; wait_for turns that into a failure
    # instead of a hung test run.
    await asyncio.wait_for(asyncio.gather(forward(), backward()), timeout=2.0)


@pytest.mark.asyncio
async def test_acquire_all_is_mutually_exclusive():
    r = LockRegistry()
    live = 0
    peak = 0

    async def worker():
        nonlocal live, peak
        async with r.acquire_all("x"):
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1

    await asyncio.gather(*(worker() for _ in range(5)))
    assert peak == 1

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
async def test_acquire_all_does_not_deadlock_under_genuine_contention():
    """test_acquire_all_is_order_independent above can pass even against an
    UNSORTED acquire_all: whichever coroutine asyncio.gather schedules first
    races through both of its uncontended acquires before the second one gets
    a turn (asyncio.Lock.acquire() has a no-yield fast path when uncontended),
    so the two opposite-order callers never actually contend for anything.
    Confirmed empirically: a naive unsorted implementation passes that test
    5/5 runs. That test alone does not guard the property this module exists
    for.

    This test manufactures real contention instead: a third party holds
    "wang" up front, forcing BOTH forward and backward to actually suspend and
    interleave. Under sorted acquisition both converge on the same key order
    and this completes cleanly. Verified against a naive unsorted variant kept
    outside this repo (never committed): under this exact construction it
    deadlocks and asyncio.wait_for turns that into a TimeoutError instead of a
    hung run.
    """
    r = LockRegistry()
    wang = r.get("wang")
    await wang.acquire()  # third party holds "wang" so both callers must block

    async def forward():
        async with r.acquire_all("wang", "blackbird"):
            await asyncio.sleep(0.01)

    async def backward():
        async with r.acquire_all("blackbird", "wang"):
            await asyncio.sleep(0.01)

    forward_task = asyncio.ensure_future(forward())
    await asyncio.sleep(0)  # let forward register as a waiter on "wang"
    backward_task = asyncio.ensure_future(backward())
    await asyncio.sleep(0)  # let backward take "blackbird", then block on "wang"

    wang.release()  # hand "wang" to whichever queued first

    await asyncio.wait_for(asyncio.gather(forward_task, backward_task), timeout=2.0)


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

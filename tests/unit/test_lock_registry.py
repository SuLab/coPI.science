"""LockRegistry eviction: it must bound its key space without ever splitting
mutual exclusion.

The registry used to be insert-only, so a long run accumulated one Lock per
thread id and per agent id forever (audit finding 5). Eviction is the fix, but
the *obvious* eviction — "drop any lock that is not `.locked()`" — is a
correctness bug, not just a shortcut: `asyncio.Lock.release()` clears `_locked`
and only *schedules* the next waiter's wakeup, so there is a window in which the
lock reports unlocked while a waiter still references it. Dropping it there
hands the next arrival a FRESH Lock for the same key, and two tasks end up
inside one critical section. See docs/audits/2026-08-21-perf-memory-race
(finding 5 / §F5).
"""

import asyncio

import pytest

from src.agent.locks import LockRegistry


@pytest.mark.asyncio
async def test_registry_evicts_keys_that_no_task_holds_or_wants():
    reg = LockRegistry()
    async with reg.acquire_all("t1"):
        assert len(reg) == 1
    assert len(reg) == 0, "an idle key must not live forever"


@pytest.mark.asyncio
async def test_eviction_never_splits_mutual_exclusion():
    """Three tasks contend one key across an eviction boundary; at no point
    may two of them hold the critical section at once. This is the exact
    failure mode of evict-when-unlocked: between T1's release and T2's
    wakeup the lock reports unlocked, and a naive sweep would hand T3 a
    FRESH Lock object while T2 still waits on the old one."""
    reg = LockRegistry()
    inside = 0
    max_inside = 0

    async def worker():
        nonlocal inside, max_inside
        async with reg.acquire_all("k"):
            inside += 1
            max_inside = max(max_inside, inside)
            await asyncio.sleep(0.02)
            inside -= 1

    await asyncio.gather(*(worker() for _ in range(3)))
    assert max_inside == 1
    assert len(reg) == 0


@pytest.mark.asyncio
async def test_eviction_never_splits_mutual_exclusion_for_a_late_arrival():
    """The same property, but with the arrival ORDER that actually reproduces
    the evict-when-unlocked bug.

    ``test_eviction_never_splits_mutual_exclusion`` above starts all three
    tasks at once, so T2 and T3 both park on the *same* Lock object before
    anyone releases; the old object goes on serialising them even if the
    registry drops its reference, and a naive sweep passes that test.
    (Verified empirically against a naive `if not lock.locked(): pop(key)`
    variant kept outside this repo: it passes the three-at-once construction
    5/5 runs.)

    The bug needs a task that calls ``acquire_all`` AFTER the eviction while
    an earlier waiter is still parked on the evicted object:

        T1 holds "k";  T2 arrives and parks on L1
        T1 releases    -> L1 reports unlocked (T2's wakeup is only *scheduled*)
        naive sweep    -> L1 dropped from the registry
        T2 wakes       -> enters the critical section holding L1
        T3 arrives     -> registry has no "k", so it mints L2 and walks
                          straight in.  Two tasks, one key.

    Refcounting closes it: T2's intent is registered synchronously before its
    first await, so T1's exit cannot take the refcount to zero and L1 survives
    for T3 to contend on.
    """
    reg = LockRegistry()
    inside = 0
    max_inside = 0
    holder_has_it = asyncio.Event()
    waiter_parked = asyncio.Event()
    let_holder_go = asyncio.Event()
    holder_left = asyncio.Event()

    async def critical_section():
        nonlocal inside, max_inside
        inside += 1
        max_inside = max(max_inside, inside)
        await asyncio.sleep(0.02)
        inside -= 1

    async def holder():
        async with reg.acquire_all("k"):
            holder_has_it.set()
            await let_holder_go.wait()
            await critical_section()
        # Runs the instant the context manager's finally completes, i.e. after
        # release() and after any eviction the registry chooses to do.
        holder_left.set()

    async def waiter():
        await holder_has_it.wait()
        waiter_parked.set()
        async with reg.acquire_all("k"):
            await critical_section()

    async def late_arrival():
        # Enters only once the holder has released AND evicted, which is the
        # window where a naive registry has already forgotten the key while
        # the waiter above is still queued on the old Lock object.
        await holder_left.wait()
        async with reg.acquire_all("k"):
            await critical_section()

    tasks = [
        asyncio.create_task(holder()),
        asyncio.create_task(waiter()),
        asyncio.create_task(late_arrival()),
    ]
    await waiter_parked.wait()
    await asyncio.sleep(0.01)  # let the waiter genuinely park on the lock
    let_holder_go.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

    assert max_inside == 1, (
        "two tasks held one key at once — the registry handed a late arrival a "
        "fresh Lock while an earlier waiter still held the evicted one"
    )
    assert len(reg) == 0


@pytest.mark.asyncio
async def test_waiting_task_keeps_the_key_alive():
    reg = LockRegistry()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with reg.acquire_all("k"):
            entered.set()
            await release.wait()

    async def waiter():
        await entered.wait()
        async with reg.acquire_all("k"):
            pass

    h = asyncio.create_task(holder())
    w = asyncio.create_task(waiter())
    await entered.wait()
    await asyncio.sleep(0.01)  # let the waiter genuinely park on the lock
    assert len(reg) == 1
    release.set()
    await asyncio.gather(h, w)
    assert len(reg) == 0

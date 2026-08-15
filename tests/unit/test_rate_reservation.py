"""A selection-time check cannot bound concurrent spend. Reserve before the
call, not after it."""
import asyncio
import time

import pytest

from src.agent.agent import Agent


def test_reserve_admits_up_to_the_allowance_then_refuses():
    a = Agent("wang", "WangBot", "Wang")
    assert [a.try_reserve(3, 600, now=1000.0) for _ in range(4)] == [
        True, True, True, False,
    ]


def test_a_released_reservation_is_reusable():
    a = Agent("wang", "WangBot", "Wang")
    assert a.try_reserve(1, 600, now=1000.0) is True
    assert a.try_reserve(1, 600, now=1000.0) is False
    a.release_reservation()
    assert a.try_reserve(1, 600, now=1000.0) is True


def test_reservations_age_out_of_the_window():
    a = Agent("wang", "WangBot", "Wang")
    assert a.try_reserve(1, 600, now=1000.0) is True
    assert a.try_reserve(1, 600, now=1000.0) is False
    assert a.try_reserve(1, 600, now=1700.0) is True   # window slid


def test_try_reserve_defaults_to_wall_clock():
    """Coverage for the omitted-``now`` path, moved here from
    ``test_hub_budget_scheduler.py``'s old ``record_api_call`` wall-clock test
    now that the ledger append lives in ``try_reserve``, not ``record_api_call``."""
    a = Agent("wang", "WangBot", "Wang")
    before = time.time()
    assert a.try_reserve(1, 600) is True
    after = time.time()
    assert len(a.state.call_times) == 1
    assert before <= a.state.call_times[0] <= after


@pytest.mark.asyncio
async def test_allowance_holds_under_concurrent_callers():
    """The property the old entry gate could not provide."""
    a = Agent("wang", "WangBot", "Wang")
    granted = 0

    async def caller():
        nonlocal granted
        if a.try_reserve(5, 600, now=1000.0):
            granted += 1
            await asyncio.sleep(0)

    await asyncio.gather(*(caller() for _ in range(50)))
    assert granted == 5, f"allowance 5 exceeded under concurrency: {granted}"


# --- Fix round 1 (Ruling R5): record_api_call's already_reserved split -----
#
# Round 1's regression: making record_api_call NEVER append to call_times
# (to stop it double-booking the two sites that call try_reserve) silently
# took SIX other call sites off the window entirely — specialist consults,
# both truncation-retry hooks, the memory update, and its own retry hook.
# None of those six separately reserves a slot, so record_api_call is the
# ONLY place they get booked at all. record_api_call now takes
# ``already_reserved`` to distinguish "try_reserve just booked this exact
# call" (True, skip the append) from "this call was never reserved" (the
# default, False — append here or it is invisible to the window).

def test_a_reserved_call_does_not_double_book_the_ledger():
    """The two call sites that reserve (``_reply_to_thread``,
    ``_phase5_new_post``) must pass ``already_reserved=True``, or the one real
    call they make shows up twice in call_times and the effective allowance
    is halved."""
    a = Agent("wang", "WangBot", "Wang")
    assert a.try_reserve(5, 600, now=1000.0) is True
    a.record_api_call(now=1000.0, already_reserved=True)
    assert a.api_call_count == 1
    assert list(a.state.call_times) == [1000.0]


def test_an_unreserved_call_still_books_the_ledger_by_default():
    """Every call site that was never separately reserved (consults,
    retries, the memory update) relies on record_api_call's DEFAULT to put it
    in the window at all."""
    a = Agent("wang", "WangBot", "Wang")
    a.record_api_call(now=1000.0)
    assert a.api_call_count == 1
    assert list(a.state.call_times) == [1000.0]

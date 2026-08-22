"""Where API calls run, and how many of them run at once.

`_acreate` used `asyncio.to_thread`, which submits to the loop's DEFAULT
executor — sized `min(32, cpu_count + 4)`, i.e. **6** on the 2-vCPU host this
deployment runs on (verified in the deployed container). Two consequences,
neither of them visible as an error:

  * a concluding hub turn can request **8** specialist consults in one gathered
    round, so two of them always waited for a free thread;
  * `src/agent/slack_client.py` routes every Slack call through `to_thread`
    too, and so does every other `to_thread` in the process — so 8 gathered
    300-second consults could starve the Slack pollers and the DB persist path
    out of the same 6 threads.

The fix is a dedicated pool, which removes an accidental throttle: with the
default executor gone, "sized from the intended fan-out" is
`reply_lane_max_in_flight=4` x 8 specialists = 32 concurrent 300-second API
calls against an endpoint nothing in this module rate-limits (the SDK retries a
429 twice and gives up). So the pool comes with an explicit semaphore, and it
is the semaphore — not a thread count nobody chose — that decides how much real
API concurrency this process is allowed.
"""

import asyncio
import os
import threading
import time

import pytest

from src.services import llm
from tests.fakes import FakeAnthropic

# What `asyncio.to_thread` would have had to share. Computed rather than
# hard-coded at 6, because the box CI runs on is not necessarily the box
# production runs on and the property under test is "not the default pool",
# not "not six threads".
_DEFAULT_POOL_WORKERS = min(32, (os.cpu_count() or 1) + 4)

# Long enough that a queued call is unmistakable next to a free one, short
# enough to keep the file fast.
_BLOCK_S = 0.30


class _RecordingAnthropic(FakeAnthropic):
    """Records which thread each request ran on, and how many overlapped."""

    def __init__(self, *, block_s: float = 0.0) -> None:
        super().__init__(default_text="ok")
        self.block_s = block_s
        self.thread_names: list[str] = []
        self.peak_concurrency = 0
        self._in_flight = 0
        self._lock = threading.Lock()

    def _next(self, kwargs: dict):
        with self._lock:
            self._in_flight += 1
            self.peak_concurrency = max(self.peak_concurrency, self._in_flight)
            self.thread_names.append(threading.current_thread().name)
        try:
            if self.block_s:
                time.sleep(self.block_s)
        finally:
            with self._lock:
                self._in_flight -= 1
        return super()._next(kwargs)


async def _api_call(fake):
    return await llm._acreate(
        fake, model="m", max_tokens=100, system="sys", messages=[]
    )


@pytest.mark.asyncio
async def test_api_calls_do_not_share_the_default_thread_pool(monkeypatch):
    """The property: a co-scheduled `to_thread` must not queue behind them.

    Every other blocking call in this process — every Slack API call, the DB
    persist flush's sync work — reaches a thread through `asyncio.to_thread`.
    If the LLM calls take the same pool, filling it is enough to stall all of
    them for the length of an API request, which at this module's 300 s read
    timeout is not a hiccup.
    """
    fake = _RecordingAnthropic(block_s=_BLOCK_S)

    async def _probe() -> float:
        # After every API call has had its turn to reach a thread, ask the
        # DEFAULT pool for one — the way Slack does.
        await asyncio.sleep(0.02)
        t0 = time.monotonic()
        await asyncio.to_thread(lambda: None)
        return time.monotonic() - t0

    results = await asyncio.gather(
        *(_api_call(fake) for _ in range(_DEFAULT_POOL_WORKERS)), _probe()
    )
    probe_seconds = results[-1]

    assert len(fake.thread_names) == _DEFAULT_POOL_WORKERS
    assert probe_seconds < _BLOCK_S / 2, (
        f"a to_thread call waited {probe_seconds:.3f}s behind the API calls — "
        "they are still sharing the default executor"
    )
    # ...and the direct statement of the same thing, so a failure names the
    # cause rather than only the symptom.
    assert all(
        name.startswith(llm._API_THREAD_NAME_PREFIX) for name in fake.thread_names
    ), fake.thread_names


@pytest.mark.asyncio
async def test_concurrent_api_calls_are_bounded_by_the_semaphore(monkeypatch):
    """The bound has to be a number someone chose.

    Before the dedicated pool, real API concurrency was capped by the default
    executor's `cpu_count + 4` — an accident that happened to be load-bearing,
    since nothing here paces requests and the SDK gives up after two 429s. The
    semaphore replaces it with an explicit ceiling that does not move when the
    host does.
    """
    assert llm._API_EXECUTOR_MAX_WORKERS > llm._API_MAX_CONCURRENCY, (
        "the pool must be wider than the semaphore, or this test would be "
        "measuring the pool and the semaphore would be decorative"
    )
    fake = _RecordingAnthropic(block_s=0.10)
    attempts = llm._API_MAX_CONCURRENCY + 6

    await asyncio.gather(*(_api_call(fake) for _ in range(attempts)))

    assert len(fake.thread_names) == attempts, "every call still gets made"
    assert fake.peak_concurrency <= llm._API_MAX_CONCURRENCY, (
        f"{fake.peak_concurrency} requests were in flight at once against a "
        f"declared ceiling of {llm._API_MAX_CONCURRENCY}"
    )
    # The floor is deliberately loose — the exact peak depends on thread
    # start-up timing — but it has to prove the calls still overlap, because a
    # semaphore of 1 would satisfy the assertion above and re-serialize the
    # 2,344 s of consults the gather was introduced to recover.
    assert fake.peak_concurrency >= 4


def test_the_semaphore_survives_a_second_event_loop():
    """`asyncio.Semaphore` binds itself to a loop and raises
    `RuntimeError: ... is bound to a different event loop` on any other.

    One module-level instance would therefore work in production (one process,
    one loop) and fail from the second test onwards in a suite that gives every
    test its own loop — and, worse, in any future process that runs
    `asyncio.run` twice. It is kept per-loop for that reason.

    The calls must CONTEND, or this test cannot fail: `Semaphore.acquire()`
    only reaches `_get_loop()` when the semaphore is already locked, so an
    uncontended acquire never binds `_loop` at all and a module-level singleton
    sails through. Saturating it on each loop is what makes the second
    `asyncio.run` the real assertion.
    """
    fake = _RecordingAnthropic(block_s=0.02)
    attempts = llm._API_MAX_CONCURRENCY + 2

    async def _saturate():
        await asyncio.gather(*(_api_call(fake) for _ in range(attempts)))

    asyncio.run(_saturate())
    asyncio.run(_saturate())

    assert len(fake.thread_names) == 2 * attempts

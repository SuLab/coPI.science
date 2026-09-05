"""get_with_retry / post_with_retry: shared retry/backoff for the three outbound HTTP clients
(orcid.py, pubmed.py, grants.py) that had none (issue #23 COR-29a).

No assertion in this file is a wall-clock threshold. Other agents run pytest concurrently in this
checkout, so "it finished within N seconds" would be flaky under load; the budget tests below drive
a hand-advanced clock (``_FakeClock``, installed over ``http_retry``'s own ``time`` name only) and
assert on attempt COUNTS, and the slot test asserts on enter/exit counts and a peak-concurrency
watermark rather than on elapsed time.
"""

import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from src.services import http_retry
from src.services.http_retry import get_with_retry, post_with_retry


class _FakeClock:
    """Stands in for the ``time`` module inside ``http_retry`` only, plus its ``asyncio.sleep``.

    Rebinding the module-global NAME means nothing outside ``http_retry`` (httpx, the event loop)
    sees a doctored clock, and a test can make an attempt "cost" 60 s without sleeping for 60 s.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def _install_fake_clock(monkeypatch) -> _FakeClock:
    clock = _FakeClock()
    # raising=False on purpose: `http_retry` grew its `import time` as part of the very change
    # these tests cover, so without it they would be red against the pre-fix module for a missing
    # module global instead of for the missing `deadline` parameter they are actually about.
    monkeypatch.setattr(http_retry, "time", clock, raising=False)
    monkeypatch.setattr(http_retry.asyncio, "sleep", clock.sleep)
    return clock


async def test_succeeds_first_try():
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await get_with_retry(client, "https://x.test/a", retries=3, backoff=0)
        assert resp.status_code == 200


async def test_retries_on_429_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await get_with_retry(client, "https://x.test/a", retries=3, backoff=0)
        assert resp.status_code == 200
        assert calls["n"] == 3


async def test_exhausts_retries_and_raises():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await get_with_retry(client, "https://x.test/a", retries=2, backoff=0)
        assert calls["n"] == 3  # initial + 2 retries


async def test_non_retryable_status_raises_immediately():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, text="nope")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await get_with_retry(client, "https://x.test/a", retries=3, backoff=0)
        assert calls["n"] == 1


async def test_transport_error_is_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await get_with_retry(client, "https://x.test/a", retries=3, backoff=0)
        assert resp.status_code == 200
        assert calls["n"] == 2


async def test_before_request_hook_runs_before_every_attempt_including_the_first():
    """I1: a retry must re-enter the caller's pacing gate. `before_request` is that gate's hook
    — it must fire once per attempt, including the very first, or a paced caller's first request
    would be unpaced."""
    hook_calls = {"n": 0}

    async def hook():
        hook_calls["n"] += 1

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await get_with_retry(
            client, "https://x.test/a", retries=3, backoff=0, before_request=hook
        )
        assert resp.status_code == 200
    assert hook_calls["n"] == 3


async def test_no_hook_default_path_is_unchanged():
    """The default (no `before_request`) path must behave exactly as before — no attribute
    error, no extra call, nothing paced."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await get_with_retry(client, "https://x.test/a", retries=3, backoff=0)
        assert resp.status_code == 200
        assert calls["n"] == 1


async def test_retry_after_header_extends_the_planned_delay(monkeypatch):
    """I1: `get_with_retry` ignored `Retry-After` entirely — the header is how NCBI/ORCID/
    Grants.gov tell you the backoff that would keep you unblocked. A `Retry-After: 5` on a 429
    must plan a delay of at least 5s even though `backoff=0` would otherwise plan 0."""
    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(http_retry.asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "5"}, text="slow down")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await get_with_retry(client, "https://x.test/a", retries=3, backoff=0)
        assert resp.status_code == 200
    assert delays and delays[0] >= 5.0


async def test_post_retries_on_503_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503, text="down")
        return httpx.Response(200, json={"data": {"oppHits": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await post_with_retry(
            client, "https://x.test/search2", json={"a": 1}, retries=3, backoff=0
        )
        assert resp.status_code == 200
        assert calls["n"] == 2


async def test_a_total_deadline_stops_the_retry_loop_before_the_attempt_budget(monkeypatch):
    """over-impl R3: the loop shipped with 4 attempts and NO total ceiling.

    With pubmed's 60 s client timeout and ``Retry-After`` honoured up to 60 s, one logical call
    could burn 4x60 s of request + 3x60 s of sleep = 420 s, and ``convert_dois_to_pmids`` issues one
    such call per unresolved DOI in a sequential loop while the worker awaits one job at a time.
    A ``deadline`` must stop the NEXT attempt starting once the budget is spent — and must still
    surface the same ``HTTPStatusError`` a caller's ``except Exception`` already handles, not a
    ``CancelledError`` (which is a ``BaseException`` and would escape every one of those handlers).
    """
    clock = _install_fake_clock(monkeypatch)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        clock.t += 60.0  # every attempt burns a full 60 s client timeout
        return httpx.Response(503, text="down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await get_with_retry(
                client, "https://x.test/a", retries=3, backoff=0.5, deadline=120.0
            )
        # t=0 attempt 1 -> t=60 (+0.5s backoff) -> t=60.5 attempt 2 -> t=120.5; a third attempt
        # would start at 121.5 > 120, so the budget stops it and raise_for_status() raises.
        assert calls["n"] == 2

        calls["n"] = 0
        clock.t = 0.0
        with pytest.raises(httpx.HTTPStatusError):
            await get_with_retry(
                client, "https://x.test/a", retries=3, backoff=0.5, deadline=None
            )
        # The control: deadline=None is the pre-fix behaviour, unchanged, for orcid.py and
        # grants.py, which do not opt in.
        assert calls["n"] == 4


async def test_a_total_deadline_bounds_transport_error_retries_too(monkeypatch):
    """The 503 path is not the only unbounded one: a hung host that resets the connection retried
    on the same 4-attempt budget. The budget must bound that path too, and must re-raise the
    ORIGINAL ``TransportError`` — the diagnosable failure every caller already catches."""
    clock = _install_fake_clock(monkeypatch)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        clock.t += 60.0
        raise httpx.ConnectError("boom", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ConnectError):
            await get_with_retry(
                client, "https://x.test/a", retries=3, backoff=0.5, deadline=120.0
            )
    assert calls["n"] == 2


async def test_post_honours_the_same_total_deadline(monkeypatch):
    """`post_with_retry` carries the identical loop, so it must carry the identical bound —
    otherwise grants.py could opt in and silently get the unbounded version."""
    clock = _install_fake_clock(monkeypatch)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        clock.t += 60.0
        return httpx.Response(503, text="down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await post_with_retry(
                client, "https://x.test/s", json={"a": 1}, retries=3, backoff=0.5, deadline=120.0
            )
    assert calls["n"] == 2


async def test_the_attempt_slot_is_taken_per_attempt_and_still_caps_concurrency():
    """over-impl R4: `_ncbi_get` held its NCBI semaphore slot across all four attempts AND the
    backoff sleeps between them, so a 429 storm cut effective concurrency to a fraction of the
    slot count for as long as the storm lasted — a caller that is sleeping is not in flight and
    must not occupy a slot.

    Two properties, and they are enforced by different things (the distinction the module comment
    in pubmed.py warns about conflating):
      * one acquire per ATTEMPT, not one per call — 5 calls x 3 attempts = 15 enters and 15 exits;
      * the released slot never lets in-flight exceed the slot count — that is the semaphore's own
        guarantee, since a retry has to re-acquire before it may send anything.
    Both are counts, not durations, so this cannot flake under a loaded machine.
    """
    sem = asyncio.Semaphore(2)
    live = {"now": 0, "peak": 0}
    events: list[str] = []

    @asynccontextmanager
    async def slot():
        await sem.acquire()
        live["now"] += 1
        live["peak"] = max(live["peak"], live["now"])
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")
            live["now"] -= 1
            sem.release()

    per_url: dict[str, int] = {}

    async def handler(request):
        key = request.url.path
        per_url[key] = per_url.get(key, 0) + 1
        # Yield while the slot is held so the other callers really do contend for it.
        await asyncio.sleep(0)
        if per_url[key] < 3:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await asyncio.gather(*(
            get_with_retry(
                client, f"https://x.test/{i}", retries=3, backoff=0, attempt_context=slot
            )
            for i in range(5)
        ))

    assert sum(per_url.values()) == 15
    assert events.count("enter") == 15
    assert events.count("exit") == 15
    assert live["peak"] <= 2
    assert live["now"] == 0

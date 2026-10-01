"""DP-14: one Pacer per upstream and bounded retries."""
import asyncio

import httpx
import pytest
import respx

from src.services import http_pacing, nih_reporter, openalex, orcid
from src.services.http_pacing import TRANSIENT_STATUSES, Pacer, with_retries

pytestmark = pytest.mark.asyncio


async def _no_sleep(_s):
    return None


async def test_pacer_spaces_starts():
    p = Pacer(0.05)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.gather(*(p.wait() for _ in range(5)))
    assert loop.time() - t0 >= 0.19


async def test_pacer_reads_a_callable_interval_each_time():
    box = {"v": 0.0}
    p = Pacer(lambda: box["v"])
    await p.wait()
    box["v"] = 0.05
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await p.wait()
    await p.wait()
    assert loop.time() - t0 >= 0.04


async def test_with_retries_retries_listed_statuses_then_returns_last(monkeypatch):
    monkeypatch.setattr(http_pacing, "_sleep", _no_sleep)
    calls = []

    async def call():
        calls.append(1)
        return httpx.Response(503, request=httpx.Request("GET", "http://x"))

    resp = await with_retries(call, attempts=3, backoff=lambda a: 0, retry_statuses=frozenset({503}))
    assert resp.status_code == 503 and len(calls) == 3


async def test_pace_once_mode_paces_only_the_first_attempt(monkeypatch):
    monkeypatch.setattr(http_pacing, "_sleep", _no_sleep)
    waits = []

    class Spy(Pacer):
        async def wait(self):
            waits.append(1)

    async def call():
        return httpx.Response(429, request=httpx.Request("POST", "http://x"))

    await with_retries(call, attempts=3, backoff=lambda a: 0, retry_statuses=frozenset({429}),
                       pacer=Spy(0), pace_each_attempt=False)
    assert waits == [1]


@respx.mock
async def test_orcid_works_retries_a_503_then_succeeds(monkeypatch):
    monkeypatch.setattr(http_pacing, "_sleep", _no_sleep)
    route = respx.get(f"{orcid.ORCID_API_BASE}/0000-0001-0000-0001/works").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"group": []})]
    )
    assert await orcid.fetch_orcid_works("0000-0001-0000-0001", strict=True) == []
    assert route.call_count == 2


@respx.mock
async def test_orcid_record_state_404_is_not_retried():
    route = respx.get(f"{orcid.ORCID_API_BASE}/0000-0001-0000-0002/works").mock(return_value=httpx.Response(404))
    assert await orcid.fetch_orcid_works("0000-0001-0000-0002", strict=True) == []
    assert route.call_count == 1


@respx.mock
async def test_openalex_retries_a_transport_error(monkeypatch):
    monkeypatch.setattr(http_pacing, "_sleep", _no_sleep)
    route = respx.get(openalex.OPENALEX_WORKS_URL).mock(
        side_effect=[httpx.ConnectError("reset"), httpx.Response(200, json={"results": [], "meta": {}})]
    )
    assert await openalex.fetch_works_by_orcid("0000-0001-0000-0003") == []
    assert route.call_count == 2


def test_transient_statuses():
    assert TRANSIENT_STATUSES == frozenset({429, 500, 502, 503, 504})


def test_nih_reporter_has_one_pacer():
    assert isinstance(nih_reporter._PACER, Pacer)

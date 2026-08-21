import asyncio
import time

import httpx
import pytest

from src.services import pubmed


@pytest.mark.asyncio
async def test_pace_spaces_request_starts(monkeypatch):
    monkeypatch.setattr(pubmed, "_pace_interval", lambda: 0.05)
    pubmed._next_slot = 0.0
    t0 = time.monotonic()
    await asyncio.gather(*(pubmed._pace() for _ in range(10)))
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.05 * 9, (
        f"10 paced starts finished in {elapsed:.3f}s — pacing is not "
        f"bounding the rate"
    )


@pytest.mark.asyncio
async def test_ncbi_get_retries_a_429_and_paces_before_raising(monkeypatch):
    calls = []

    def handler(request):
        calls.append(time.monotonic())
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        pubmed, "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(pubmed, "_pace_interval", lambda: 0.01)
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        await real_sleep(min(seconds, 0.01))

    monkeypatch.setattr(pubmed.asyncio, "sleep", fast_sleep)
    resp = await pubmed._ncbi_get("https://x.test/e", {})
    assert resp.status_code == 200
    assert len(calls) == 2  # one 429, one retry — not an immediate raise


def test_pace_interval_tracks_the_api_key(monkeypatch):
    class _S:
        ncbi_api_key = ""

    monkeypatch.setattr(pubmed, "get_settings", lambda: _S())
    assert pubmed._pace_interval() == 0.34

    _S.ncbi_api_key = "some-key"
    assert pubmed._pace_interval() == 0.11

"""get_with_retry / post_with_retry: shared retry/backoff for the three outbound HTTP clients
(orcid.py, pubmed.py, grants.py) that had none (issue #23 COR-29a)."""

import httpx
import pytest

from src.services import http_retry
from src.services.http_retry import get_with_retry, post_with_retry


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

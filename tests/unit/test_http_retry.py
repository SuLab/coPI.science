"""get_with_retry / post_with_retry: shared retry/backoff for the three outbound HTTP clients
(orcid.py, pubmed.py, grants.py) that had none (issue #23 COR-29a)."""

import httpx
import pytest

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

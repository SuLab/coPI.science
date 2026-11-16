"""The evidence and normalization clients of spec §6.3 (plan Task 4.1).

Each module wraps one public source. Every request is paced by a process-wide `Pacer` and
retried with exponential backoff on 429 and transient 5xx statuses. The clients never touch
the database: callers wrap a lookup in `cache.cached` with the source's current release
(`cache.current_release`), so a result is fetched once per (source, release, query) (§11.2).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from src.services.http_pacing import TRANSIENT_STATUSES, Pacer, with_retries

#: Attempts per request; the last attempt's error status raises.
ATTEMPTS = 4
#: Transport failures that mean the request did not complete, so retrying is safe.
RETRYABLE_TRANSPORT = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


def backoff(attempt: int) -> float:
    """Seconds to sleep after failed attempt ``attempt`` (0-based): 2, 4, 8."""
    return 2.0 * (2 ** attempt)


def make_client(timeout: float = 60.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True)


async def request_json(
    client_factory: Callable[[], httpx.AsyncClient],
    url: str,
    *,
    pacer: Pacer,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    none_on_404: bool = False,
) -> Any:
    """GET ``url`` (POST when ``json_body`` is given), paced and retried, and return the
    decoded JSON. A 404 returns None when ``none_on_404``; any other error status after the
    retries raises ``httpx.HTTPStatusError``."""
    async with client_factory() as client:
        if json_body is not None:
            def call():
                return client.post(url, json=json_body, headers=headers)
        else:
            def call():
                return client.get(url, params=params, headers=headers)
        resp = await with_retries(
            call, attempts=ATTEMPTS, backoff=backoff, retry_statuses=TRANSIENT_STATUSES,
            retry_exceptions=RETRYABLE_TRANSPORT, pacer=pacer,
        )
    if resp.status_code == 404 and none_on_404:
        return None
    resp.raise_for_status()
    return resp.json()

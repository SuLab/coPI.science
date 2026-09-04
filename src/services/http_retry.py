"""Shared retry/backoff wrapper for outbound HTTP requests.

None of src/services/orcid.py, pubmed.py or grants.py retried a transient failure before issue #23
(COR-29a): every call was a one-shot httpx request + `raise_for_status()`, so a single 429/502/503 from
ORCID, NCBI or Grants.gov failed the whole fetch — exactly the case a client-side backoff exists to
absorb. One pair of helpers, used by all three, instead of three near-identical retry loops.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

from src.agent.retry_after import parse_retry_after

logger = logging.getLogger(__name__)

DEFAULT_RETRY_ON = (429, 500, 502, 503, 504)


async def get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    retries: int = 3,
    backoff: float = 0.5,
    retry_on: tuple[int, ...] = DEFAULT_RETRY_ON,
    before_request: Callable[[], Awaitable[None]] | None = None,
) -> httpx.Response:
    """GET ``url`` through ``client``, retrying a transient failure.

    Retries on any status in ``retry_on`` (defaults to 429 plus the 5xx family) and on
    ``httpx.TransportError`` (connection reset, timeout — the failure modes retrying can actually fix).
    Backoff is exponential: ``backoff * 2 ** attempt``, and is raised to honour a ``Retry-After``
    header on the retryable-status branch, if present. On the final attempt, ``raise_for_status()`` is
    allowed to raise (or the transport error propagates), so a caller's existing ``except Exception``
    still sees a failure — this only buys the retries in between.

    ``before_request``, if given, is awaited at the top of EVERY loop iteration — including the
    first — before the request is sent. This lets a caller re-enter its own pacing gate on a retry
    (issue #23 I1): without it, a caller that paces its first attempt (e.g. ``pubmed._pace_ncbi``)
    had no way to pace the retries this loop issues on its behalf, letting a burst of 429s escape
    the rate ceiling the caller thought it was enforcing.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        if before_request is not None:
            await before_request()
        try:
            resp = await client.get(url, params=params, headers=headers)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt == retries:
                raise
            delay = backoff * (2 ** attempt)
            logger.warning(
                "GET %s failed (%s); retrying in %.1fs (attempt %d/%d)",
                url, exc, delay, attempt + 1, retries,
            )
            await asyncio.sleep(delay)
            continue
        if resp.status_code in retry_on and attempt < retries:
            delay = backoff * (2 ** attempt)
            delay = max(delay, parse_retry_after(resp.headers.get("Retry-After"), default=delay, cap=60.0))
            logger.warning(
                "GET %s got %d; retrying in %.1fs (attempt %d/%d)",
                url, resp.status_code, delay, attempt + 1, retries,
            )
            await asyncio.sleep(delay)
            continue
        resp.raise_for_status()
        return resp
    # Unreachable: every iteration of the loop above either `return`s (success or the final
    # attempt's `raise_for_status()` raising) or `raise`s (a `TransportError` on the final
    # attempt), so `last_exc` is only ever still `None` here if the loop body never ran at all
    # (`retries < 0`) — a caller misuse, not a runtime failure this function retries. `raise
    # last_exc` with `last_exc: Exception | None` is a mypy error (`Exception must be derived
    # from BaseException [misc]`); it also degraded to `TypeError: exceptions must derive from
    # BaseException` at runtime for that same misuse, a worse diagnostic than this (#23 I3).
    raise AssertionError(  # pragma: no cover
        f"unreachable: get_with_retry's loop always returns or raises (last_exc={last_exc!r})"
    )


async def post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    json: dict | None = None,
    headers: dict | None = None,
    retries: int = 3,
    backoff: float = 0.5,
    retry_on: tuple[int, ...] = DEFAULT_RETRY_ON,
    before_request: Callable[[], Awaitable[None]] | None = None,
) -> httpx.Response:
    """POST ``url`` through ``client``, retrying a transient failure. Same shape as ``get_with_retry``,
    including the ``before_request`` pacing hook and ``Retry-After`` handling."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        if before_request is not None:
            await before_request()
        try:
            resp = await client.post(url, json=json, headers=headers)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt == retries:
                raise
            delay = backoff * (2 ** attempt)
            logger.warning(
                "POST %s failed (%s); retrying in %.1fs (attempt %d/%d)",
                url, exc, delay, attempt + 1, retries,
            )
            await asyncio.sleep(delay)
            continue
        if resp.status_code in retry_on and attempt < retries:
            delay = backoff * (2 ** attempt)
            delay = max(delay, parse_retry_after(resp.headers.get("Retry-After"), default=delay, cap=60.0))
            logger.warning(
                "POST %s got %d; retrying in %.1fs (attempt %d/%d)",
                url, resp.status_code, delay, attempt + 1, retries,
            )
            await asyncio.sleep(delay)
            continue
        resp.raise_for_status()
        return resp
    # Unreachable — same reasoning as get_with_retry's identical tail (#23 I3).
    raise AssertionError(  # pragma: no cover
        f"unreachable: post_with_retry's loop always returns or raises (last_exc={last_exc!r})"
    )

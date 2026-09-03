"""Shared retry/backoff wrapper for outbound HTTP requests.

None of src/services/orcid.py, pubmed.py or grants.py retried a transient failure before issue #23
(COR-29a): every call was a one-shot httpx request + `raise_for_status()`, so a single 429/502/503 from
ORCID, NCBI or Grants.gov failed the whole fetch — exactly the case a client-side backoff exists to
absorb. One pair of helpers, used by all three, instead of three near-identical retry loops.
"""

import asyncio
import logging

import httpx

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
) -> httpx.Response:
    """GET ``url`` through ``client``, retrying a transient failure.

    Retries on any status in ``retry_on`` (defaults to 429 plus the 5xx family) and on
    ``httpx.TransportError`` (connection reset, timeout — the failure modes retrying can actually fix).
    Backoff is exponential: ``backoff * 2 ** attempt``. On the final attempt, ``raise_for_status()`` is
    allowed to raise (or the transport error propagates), so a caller's existing ``except Exception``
    still sees a failure — this only buys the retries in between.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
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
            logger.warning(
                "GET %s got %d; retrying in %.1fs (attempt %d/%d)",
                url, resp.status_code, delay, attempt + 1, retries,
            )
            await asyncio.sleep(delay)
            continue
        resp.raise_for_status()
        return resp
    raise last_exc  # pragma: no cover — loop above always returns or raises


async def post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    json: dict | None = None,
    headers: dict | None = None,
    retries: int = 3,
    backoff: float = 0.5,
    retry_on: tuple[int, ...] = DEFAULT_RETRY_ON,
) -> httpx.Response:
    """POST ``url`` through ``client``, retrying a transient failure. Same shape as ``get_with_retry``."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
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
            logger.warning(
                "POST %s got %d; retrying in %.1fs (attempt %d/%d)",
                url, resp.status_code, delay, attempt + 1, retries,
            )
            await asyncio.sleep(delay)
            continue
        resp.raise_for_status()
        return resp
    raise last_exc  # pragma: no cover

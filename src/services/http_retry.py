"""Shared retry/backoff wrapper for outbound HTTP requests.

None of src/services/orcid.py, pubmed.py or grants.py retried a transient failure before issue #23
(COR-29a): every call was a one-shot httpx request + `raise_for_status()`, so a single 429/502/503 from
ORCID, NCBI or Grants.gov failed the whole fetch — exactly the case a client-side backoff exists to
absorb. One pair of helpers, used by all three, instead of three near-identical retry loops.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any

import httpx

from src.agent.retry_after import parse_retry_after

logger = logging.getLogger(__name__)

DEFAULT_RETRY_ON = (429, 500, 502, 503, 504)


def _out_of_budget(started: float, delay: float, deadline: float | None) -> bool:
    """True when sleeping ``delay`` and re-attempting would run past the total ``deadline``.

    The deadline bounds the RETRY BUDGET, not a single request: it stops the next attempt from
    starting and never cancels one already in flight (bounding a single request is the client
    timeout's job, and cancelling mid-response would trade a diagnosable ``HTTPStatusError`` /
    ``TransportError`` for a ``CancelledError`` every caller here would mis-handle). The
    guarantee is therefore "no attempt begins later than ``deadline`` seconds in", so the worst
    case for one logical call is ``deadline`` plus one client timeout — not ``deadline`` flat.

    Why it exists (over-implementation review R3): the retry loop below shipped with 4 attempts
    (``retries=3``) and no total ceiling of any kind. With ``pubmed``'s 60 s client timeout
    (``pubmed.py``'s ``httpx.AsyncClient(timeout=60)``; httpx bounds each I/O operation, so 60 s
    is the nominal per-attempt cost, not a hard one) and ``Retry-After`` honoured up to the 60 s
    ``cap`` below, one logical ``_ncbi_get`` could burn 4x60 s of request plus 3x60 s of sleep =
    420 s — and ``convert_dois_to_pmids`` issues one such call per unresolved DOI in a sequential
    loop (``pubmed.py``'s phase-2 ESearch), while ``worker/main.py`` awaits one job at a time, so
    a hung NCBI multiplied that by the DOI count and head-of-line-blocked every other queued
    profile job. ``deadline=None`` (the default) keeps the old unbounded behaviour for callers
    that have not opted in: only ``pubmed._ncbi_get`` opts in today, because it is the only one of
    the three clients whose per-item loop multiplies the worst case.
    """
    if deadline is None:
        return False
    return (time.monotonic() - started) + delay > deadline


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
    attempt_context: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
    deadline: float | None = None,
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

    ``attempt_context``, if given, is entered per ATTEMPT and wraps ``before_request`` plus the one
    request, so a caller's concurrency slot is held only while an attempt is actually in flight and
    is released across the backoff sleep (issue #23 over-impl R4: ``_ncbi_get`` used to hold an
    NCBI semaphore slot across all four attempts *and* the sleeps between them, so a single 429
    storm cut effective concurrency to a fraction of the slot count for minutes). It wraps
    ``before_request`` rather than just the request so that a pacing gate still reserves its start
    slot *after* admission — reserving before admission would let a caller blocked on the slot
    drift past the start time it had already booked.

    ``deadline`` bounds the total retry budget in seconds — see ``_out_of_budget``. ``None``
    (the default) means unbounded, exactly as before.
    """
    started = time.monotonic()
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        slot: AbstractAsyncContextManager[Any] = (
            nullcontext() if attempt_context is None else attempt_context()
        )
        try:
            async with slot:
                if before_request is not None:
                    await before_request()
                resp = await client.get(url, params=params, headers=headers)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt == retries:
                raise
            delay = backoff * (2 ** attempt)
            if _out_of_budget(started, delay, deadline):
                logger.warning(
                    "GET %s failed (%s); retry budget of %.1fs spent after %d attempt(s), giving up",
                    url, exc, deadline, attempt + 1,
                )
                raise
            logger.warning(
                "GET %s failed (%s); retrying in %.1fs (attempt %d/%d)",
                url, exc, delay, attempt + 1, retries,
            )
            await asyncio.sleep(delay)
            continue
        if resp.status_code in retry_on and attempt < retries:
            delay = backoff * (2 ** attempt)
            delay = max(delay, parse_retry_after(resp.headers.get("Retry-After"), default=delay, cap=60.0))
            if _out_of_budget(started, delay, deadline):
                logger.warning(
                    "GET %s got %d; retry budget of %.1fs spent after %d attempt(s), giving up",
                    url, resp.status_code, deadline, attempt + 1,
                )
            else:
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
    attempt_context: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
    deadline: float | None = None,
) -> httpx.Response:
    """POST ``url`` through ``client``, retrying a transient failure. Same shape as ``get_with_retry``,
    including the ``before_request`` pacing hook, the per-attempt ``attempt_context`` slot, the total
    ``deadline`` retry budget and ``Retry-After`` handling."""
    started = time.monotonic()
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        slot: AbstractAsyncContextManager[Any] = (
            nullcontext() if attempt_context is None else attempt_context()
        )
        try:
            async with slot:
                if before_request is not None:
                    await before_request()
                resp = await client.post(url, json=json, headers=headers)
        except httpx.TransportError as exc:
            last_exc = exc
            if attempt == retries:
                raise
            delay = backoff * (2 ** attempt)
            if _out_of_budget(started, delay, deadline):
                logger.warning(
                    "POST %s failed (%s); retry budget of %.1fs spent after %d attempt(s), giving up",
                    url, exc, deadline, attempt + 1,
                )
                raise
            logger.warning(
                "POST %s failed (%s); retrying in %.1fs (attempt %d/%d)",
                url, exc, delay, attempt + 1, retries,
            )
            await asyncio.sleep(delay)
            continue
        if resp.status_code in retry_on and attempt < retries:
            delay = backoff * (2 ** attempt)
            delay = max(delay, parse_retry_after(resp.headers.get("Retry-After"), default=delay, cap=60.0))
            if _out_of_budget(started, delay, deadline):
                logger.warning(
                    "POST %s got %d; retry budget of %.1fs spent after %d attempt(s), giving up",
                    url, resp.status_code, deadline, attempt + 1,
                )
            else:
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

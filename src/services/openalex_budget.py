"""Adaptive pacing of bulk OpenAlex work on the free tier (spec 2026-10-05 D67).

OpenAlex meters keyless use per IP and per UTC day. Every response carries the meter
(measured 2026-10-06): `x-ratelimit-limit`
(1000 credits), `x-ratelimit-remaining` and `x-ratelimit-reset` (seconds to the next
00:00 UTC). A list request costs one credit; a singleton lookup costs none
(`x-ratelimit-cost-usd: 0`), so reading the meter is free.

Before the worker runs a BULK-priority job of a type in `JOB_CREDITS`, `defer_if_low`
reads the meter with one singleton lookup and defers the job (`job_queue.JobDeferred`)
to just after the reset when the job's estimate exceeds the available free credits.
The owner removed the shared reserve on
2026-10-06: Blackbird may use the whole free daily budget, but never prepaid credits.
Every charged request and retry in bulk/repair work checks the meter too: a fixed
job estimate cannot bound industry lookups over an uncapped publication corpus.
Interactive jobs never come here. When the meter cannot be read (OpenAlex unreachable,
headers missing or renamed) BULK jobs fall back to the fixed D53 pace instead of running
back to back: one job per `fallback_interval`, tracked in this process (the worker is a
single process; a restart lets at most one extra job through).
"""
import logging
import math
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from src.config import get_settings
from src.services.job_queue import JobDeferred

logger = logging.getLogger(__name__)

#: A singleton lookup (free) whose response carries the meter.
PROBE_URL = "https://api.openalex.org/works/W2741809807"
RESERVE_FRACTION = 0.0
#: Coarse admission estimates, not upper bounds for an uncapped corpus:
#: generate_profile's corpus fetch is at most `openalex._MAX_PAGES` list pages;
#: industry_evidence is one /works request per 100 PMIDs plus /funders and
#: /institutions per 50 funders. Per-request checks enforce the actual free budget
#: even when an uncapped corpus exceeds the admission estimate.
JOB_CREDITS: dict[str, int] = {"generate_profile": 5, "industry_evidence": 8}
#: How long after the reported reset a deferred job wakes (clock skew).
WAKE_MARGIN = timedelta(minutes=2)
#: The longest reset honoured: OpenAlex's window is one UTC day, so a larger value is
#: a garbled header, not a reason to park bulk work for longer.
MAX_RESET_SECONDS = 86400
#: The fixed pace's share of the keyless 1000 credits a day, and its retry headroom
#: (scripts/_bulk_enqueue.py DEFAULT_BUDGET_SHARE, RETRY_HEADROOM).
FALLBACK_DAILY_CREDITS = 500
FALLBACK_RETRY_HEADROOM = 2

#: A bare 429 (no headers) may be a per-second limit, not a spent day: BULK jobs back off
#: this long (or to 00:00 UTC, if sooner) and probe again.
BARE_429_BACKOFF = timedelta(minutes=30)

#: While the meter is unreadable, when the next BULK job may run (process-local).
_unmetered_next: datetime | None = None
_guard_requests: ContextVar[bool] = ContextVar('openalex_bulk_requests', default=False)


@contextmanager
def bulk_requests(enabled: bool = True) -> Iterator[None]:
    """Enforce the free daily budget before each charged bulk/repair request.

    Scope is task-local and restored on failure/cancellation; interactive work
    retains its existing policy. Estimates alone cannot bound an uncapped corpus.
    """
    token = _guard_requests.set(enabled)
    try:
        yield
    finally:
        _guard_requests.reset(token)


async def check_request_budget() -> None:
    """Check immediately before each list-request attempt, including retries."""
    if not _guard_requests.get():
        return
    meter = await read_meter()
    if meter is None:
        return  # Admission already applied the owner's fixed-rate fallback.
    wake = wake_time(meter, 1, datetime.now(UTC))
    if wake is not None:
        raise JobDeferred(wake, f"OpenAlex free budget: {meter.remaining} of {meter.limit} "
                          "credits left; waiting for the daily reset")


async def check_inline_corpus_budget() -> None:
    """Admit inline repair/audit work only with a readable, sufficient free meter.

    Unlike worker jobs, scripts cannot persist a deferred job for a later slot;
    their caller reports incomplete work and the operator can rerun after reset.
    """
    meter = await read_meter()
    now = datetime.now(UTC)
    if meter is None:
        raise JobDeferred(now + BARE_429_BACKOFF,
                          "OpenAlex free budget meter unreadable; inline corpus work unverified")
    wake = wake_time(meter, JOB_CREDITS["generate_profile"], now)
    if wake is not None:
        raise JobDeferred(wake, f"OpenAlex free budget: {meter.remaining} of {meter.limit} "
                          "credits left; waiting for the daily reset")


@dataclass(frozen=True)
class Meter:
    limit: int
    remaining: int
    reset_seconds: int


def parse_meter(headers: Mapping[str, str]) -> Meter | None:
    """The meter from one OpenAlex response's headers; None when any field is absent
    or not an integer."""
    try:
        return Meter(int(headers["x-ratelimit-limit"]), int(headers["x-ratelimit-remaining"]),
                     int(headers["x-ratelimit-reset"]))
    except (KeyError, TypeError, ValueError):
        return None


def wake_time(meter: Meter, credits: int, now: datetime) -> datetime | None:
    """None when the free budget covers ``credits``; otherwise when it may run:
    `WAKE_MARGIN` after the meter's reset."""
    reserve = math.ceil(meter.limit * RESERVE_FRACTION)
    if meter.remaining - credits >= reserve:
        return None
    reset = min(max(meter.reset_seconds, 0), MAX_RESET_SECONDS)
    return now + timedelta(seconds=reset) + WAKE_MARGIN


def fallback_interval(credits: int) -> timedelta:
    """The D53 slot of a job spending ``credits``, used while the meter is unreadable."""
    return timedelta(seconds=86400 * credits * FALLBACK_RETRY_HEADROOM / FALLBACK_DAILY_CREDITS)


def _make_client() -> httpx.AsyncClient:
    """Client factory: a seam so tests can inject httpx.MockTransport. Redirects are
    followed, so a merged probe work still answers with the meter."""
    return httpx.AsyncClient(timeout=15, follow_redirects=True)


def _seconds_to_utc_midnight(now: datetime) -> int:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((tomorrow - now).total_seconds())


async def read_meter() -> Meter | None:
    """The meter as of now, from one free singleton lookup; None when unreadable. A 429
    without the headers reads as nothing left until BARE_429_BACKOFF from now, or 00:00
    UTC (OpenAlex's reset) if sooner."""
    contact = getattr(get_settings(), "ncbi_contact_email", None)
    params = {"select": "id", **({"mailto": contact} if contact else {})}
    try:
        async with _make_client() as client:
            resp = await client.get(PROBE_URL, params=params)
    except httpx.HTTPError as exc:
        logger.warning("OpenAlex meter unreadable (%s)", exc)
        return None
    meter = parse_meter(resp.headers)
    if meter is None and resp.status_code == 429:
        logger.warning("OpenAlex answered the meter probe 429 without headers; backing off")
        backoff = int(BARE_429_BACKOFF.total_seconds())
        return Meter(0, 0, min(backoff, _seconds_to_utc_midnight(datetime.now(UTC))))
    if meter is None:
        logger.warning("OpenAlex meter absent from an HTTP %s response",
                       resp.status_code)
    return meter


async def defer_if_low(job_type: str) -> None:
    """Raise `JobDeferred` to just after the reset when a ``job_type`` job exceeds
    the free budget, or, with the meter unreadable, until the fixed pace's next slot; return
    otherwise (also for a type that spends no credits)."""
    global _unmetered_next
    credits = JOB_CREDITS.get(job_type)
    if not credits:
        return
    meter = await read_meter()
    now = datetime.now(UTC)
    if meter is None:
        if _unmetered_next is not None and now < _unmetered_next:
            raise JobDeferred(_unmetered_next, "OpenAlex meter unreadable: fixed pace")
        _unmetered_next = now + fallback_interval(credits)
        return
    _unmetered_next = None
    wake = wake_time(meter, credits, now)
    if wake is not None:
        raise JobDeferred(wake, f"OpenAlex free budget: {meter.remaining} of {meter.limit} "
                                "credits left; waiting for the daily reset")

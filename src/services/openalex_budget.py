"""Adaptive pacing of bulk OpenAlex work on the free tier (spec 2026-10-05 D67).

OpenAlex meters keyless use per IP and per UTC day, and this host's IP is shared with
org1. Every response carries the meter (measured 2026-10-06): `x-ratelimit-limit`
(1000 credits), `x-ratelimit-remaining` and `x-ratelimit-reset` (seconds to the next
00:00 UTC). A list request costs one credit; a singleton lookup costs none
(`x-ratelimit-cost-usd: 0`), so reading the meter is free.

Before the worker runs a BULK-priority job of a type in `JOB_CREDITS`, `defer_if_low`
reads the meter with one singleton lookup and defers the job (`job_queue.JobDeferred`)
to just after the reset when the job's estimate would leave less than
`RESERVE_FRACTION` of the day's limit, the share kept for org1 and interactive work.
Interactive jobs never come here. When the meter cannot be read (OpenAlex unreachable,
headers missing or renamed) BULK jobs fall back to the fixed D53 pace instead of running
back to back: one job per `fallback_interval`, tracked in this process (the worker is a
single process; a restart lets at most one extra job through).
"""
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from src.config import get_settings
from src.services.job_queue import JobDeferred

logger = logging.getLogger(__name__)

#: A singleton lookup (free) whose response carries the meter.
PROBE_URL = "https://api.openalex.org/works/W2741809807"
RESERVE_FRACTION = 0.10
#: Most OpenAlex credits one job of the type spends, from its call sites:
#: generate_profile's corpus fetch is at most `openalex._MAX_PAGES` list pages;
#: industry_evidence is one /works request per 100 PMIDs plus /funders and
#: /institutions per 50 funders. The reserve absorbs an overrun.
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
    """None when a job spending ``credits`` still leaves the reserve; otherwise when it
    may run: `WAKE_MARGIN` after the meter's reset."""
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
        logger.warning("OpenAlex meter unreadable (%s); the job runs unpaced", exc)
        return None
    meter = parse_meter(resp.headers)
    if meter is None and resp.status_code == 429:
        logger.warning("OpenAlex answered the meter probe 429 without headers; backing off")
        backoff = int(BARE_429_BACKOFF.total_seconds())
        return Meter(0, 0, min(backoff, _seconds_to_utc_midnight(datetime.now(UTC))))
    if meter is None:
        logger.warning("OpenAlex meter absent from an HTTP %s response; the job runs unpaced",
                       resp.status_code)
    return meter


async def defer_if_low(job_type: str) -> None:
    """Raise `JobDeferred` to just after the reset when a ``job_type`` job would eat into
    the reserve, or, with the meter unreadable, until the fixed pace's next slot; return
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
                                f"credits left, {math.ceil(meter.limit * RESERVE_FRACTION)} kept in reserve")

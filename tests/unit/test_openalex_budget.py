"""src/services/openalex_budget.py: adaptive pacing on OpenAlex's free daily meter (spec 2026-10-05 D67)."""
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.services import openalex_budget as ob
from src.services.job_queue import JobDeferred

NOW = datetime(2026, 10, 6, 13, 17, 39, tzinfo=UTC)
#: The meter as the host saw it on 2026-10-06 (a singleton lookup, cost 0).
HEADERS = {"x-ratelimit-limit": "1000", "x-ratelimit-remaining": "996", "x-ratelimit-reset": "38541",
           "x-ratelimit-cost-usd": "0"}


def test_parse_meter_reads_the_three_fields_and_refuses_partial_or_garbled_ones():
    assert ob.parse_meter(HEADERS) == ob.Meter(1000, 996, 38541)
    assert ob.parse_meter({k: v for k, v in HEADERS.items() if k != "x-ratelimit-reset"}) is None
    assert ob.parse_meter({**HEADERS, "x-ratelimit-remaining": "lots"}) is None


def test_wake_time_uses_the_whole_free_daily_budget_without_a_shared_reserve():
    assert ob.RESERVE_FRACTION == 0
    assert ob.wake_time(ob.Meter(1000, 996, 38541), 5, NOW) is None
    assert ob.wake_time(ob.Meter(1000, 5, 38541), 5, NOW) is None  # uses the last free credit
    wake = ob.wake_time(ob.Meter(1000, 4, 38541), 5, NOW)
    assert wake == NOW + timedelta(seconds=38541) + ob.WAKE_MARGIN
    assert wake.replace(microsecond=0) == datetime(2026, 10, 7, 0, 2, tzinfo=UTC)


def test_wake_time_never_goes_backwards_on_a_negative_reset():
    assert ob.wake_time(ob.Meter(1000, 0, -5), 5, NOW) == NOW + ob.WAKE_MARGIN


def test_wake_time_caps_a_garbled_reset_at_one_day():
    assert ob.wake_time(ob.Meter(1000, 0, 10**9), 5, NOW) == NOW + timedelta(days=1) + ob.WAKE_MARGIN


def _client_with(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_read_meter_uses_one_free_singleton_lookup(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, headers=HEADERS, json={"id": "W2741809807"})

    monkeypatch.setattr(ob, "_make_client", _client_with(handler))
    assert await ob.read_meter() == ob.Meter(1000, 996, 38541)
    assert len(seen) == 1 and str(seen[0].url).startswith(ob.PROBE_URL)
    assert seen[0].url.params["select"] == "id"


async def test_read_meter_is_none_when_openalex_is_unreachable_or_sends_no_meter(monkeypatch):
    def down(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(ob, "_make_client", _client_with(down))
    assert await ob.read_meter() is None
    monkeypatch.setattr(ob, "_make_client", _client_with(lambda r: httpx.Response(503)))
    assert await ob.read_meter() is None


async def test_defer_if_low_defers_to_the_reset_and_skips_types_that_spend_nothing(monkeypatch):
    async def low():
        return ob.Meter(1000, 7, 600)

    monkeypatch.setattr(ob, "read_meter", low)
    with pytest.raises(JobDeferred) as exc:
        await ob.defer_if_low("industry_evidence")
    assert exc.value.not_before > datetime.now(UTC) + timedelta(seconds=600)
    assert "7 of 1000" in exc.value.reason

    async def never():
        raise AssertionError("a zero-credit job type must not read the meter")

    monkeypatch.setattr(ob, "read_meter", never)
    await ob.defer_if_low("enrich_grants")
    await ob.defer_if_low("company_discovery")


async def test_an_unreadable_meter_falls_back_to_the_fixed_pace(monkeypatch):
    async def unreadable():
        return None

    monkeypatch.setattr(ob, "_unmetered_next", None)
    monkeypatch.setattr(ob, "read_meter", unreadable)
    before = datetime.now(UTC)
    await ob.defer_if_low("generate_profile")  # the first job runs
    with pytest.raises(JobDeferred) as exc:
        await ob.defer_if_low("industry_evidence")  # the next waits for the slot
    assert exc.value.not_before >= before + ob.fallback_interval(5)
    assert "unreadable" in exc.value.reason
    assert ob.fallback_interval(5) == timedelta(seconds=1728)  # 5 credits x 2 / 500 a day


async def test_an_ample_meter_runs_the_job_and_clears_the_fallback_slot(monkeypatch):
    async def ample():
        return ob.Meter(1000, 900, 600)

    monkeypatch.setattr(ob, "_unmetered_next", datetime(2099, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(ob, "read_meter", ample)
    await ob.defer_if_low("generate_profile")
    assert ob._unmetered_next is None


async def test_a_bare_429_probe_backs_off_half_an_hour(monkeypatch):
    monkeypatch.setattr(ob, "_make_client", _client_with(lambda r: httpx.Response(429)))
    meter = await ob.read_meter()
    assert meter is not None and meter.remaining == 0
    assert 0 < meter.reset_seconds <= ob.BARE_429_BACKOFF.total_seconds()
    assert ob.wake_time(meter, 5, NOW) is not None


def test_seconds_to_utc_midnight():
    assert ob._seconds_to_utc_midnight(NOW) == 38541  # 13:17:39 -> 00:00:00

"""The COI budget's pure parts (spec 2026-10-05 §6.2, D36): when a deferral wakes, what a
settled call cost, the ledger key, and the stored-claims round trip."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.services.company_discovery_budget import (
    COI_RESERVE_USD,
    DEFER_MARGIN,
    WINDOW,
    call_cost,
    claims_from_json,
    claims_to_json,
    ledger_key,
    release_time,
)
from src.services.company_sources import coi_llm, pi_name
from src.services.company_sources.coi_founders import FounderClaim
from src.services.llm_pricing import cost_for_tokens

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
VEV = {"last": "Velculescu", "fore": "Victor E", "initials": "VE", "collective": None}


def test_release_time_frees_room_for_every_slot():
    """Spend $19.90 in three rows; four slots need $1.20, so the spend must fall to $18.80:
    the first row ($0.50) is not enough, the second ($18.00) is."""
    window = [(NOW - timedelta(hours=20), Decimal("0.50")), (NOW - timedelta(hours=10), Decimal("18.00")),
              (NOW - timedelta(hours=1), Decimal("1.40"))]
    at = release_time(window, spend=Decimal("19.90"), limit=Decimal("20"),
                      needed=COI_RESERVE_USD * 4, now=NOW)
    assert at == NOW - timedelta(hours=10) + WINDOW + DEFER_MARGIN


def test_settled_siblings_bring_the_wake_time_forward():
    """Three calls were in flight (reserved at $0.30 each) when a fourth reservation was
    refused; they settle at $0.01 each. Read at the refusal, the wake-up waits for the
    $18.40 row (t+14h); read after the settles (`CoiBudget.wake_time`), the $0.50 row
    leaving is enough (t+4h)."""
    old_rows = [(NOW - timedelta(hours=20), Decimal("0.50")), (NOW - timedelta(hours=10), Decimal("18.40"))]
    in_flight = [(NOW - timedelta(minutes=1), COI_RESERVE_USD)] * 3
    settled = [(NOW - timedelta(minutes=1), Decimal("0.01"))] * 3
    kwargs = {"limit": Decimal("20"), "needed": COI_RESERVE_USD * 4, "now": NOW}
    at_refusal = release_time(old_rows + in_flight, spend=Decimal("19.80"), **kwargs)
    after_settles = release_time(old_rows + settled, spend=Decimal("18.93"), **kwargs)
    assert at_refusal == NOW + timedelta(hours=14) + DEFER_MARGIN
    assert after_settles == NOW + timedelta(hours=4) + DEFER_MARGIN


def test_release_time_when_no_window_could_hold_the_slots():
    at = release_time([], spend=Decimal("0"), limit=Decimal("1"), needed=Decimal("1.20"), now=NOW)
    assert at == NOW + WINDOW + DEFER_MARGIN


def test_release_time_when_there_is_room_already():
    at = release_time([], spend=Decimal("1"), limit=Decimal("20"), needed=Decimal("1.20"), now=NOW)
    assert at == NOW + DEFER_MARGIN


USAGE = {"input_tokens": 900, "output_tokens": 100, "cache_read_input_tokens": 60,
         "cache_creation_input_tokens": 40}


def test_call_cost_prices_entries_usage_and_unknowns():
    priced = cost_for_tokens("claude-opus-5-5", input_tokens=900, output_tokens=100,
                             cache_read=60, cache_creation=40)
    entries = [{"model": "claude-opus-5-5", "billed": True, **USAGE},
               {"model": "claude-opus-5-5", "billed": False, **USAGE}]
    r = COI_RESERVE_USD
    assert call_cost(coi_llm.CoiOutcome("ok", [], usage=USAGE, entries=entries), model="x", reserved=r) == priced
    assert call_cost(coi_llm.CoiOutcome("ok", [], usage=USAGE), model="claude-opus-5-5", reserved=r) == priced
    assert call_cost(coi_llm.CoiOutcome("unavailable", [], "api_status_429", entries=[]), model="x", reserved=r) == 0
    assert call_cost(coi_llm.CoiOutcome("skipped", [], "pi_not_located"), model="x", reserved=r) == 0
    assert call_cost(coi_llm.CoiOutcome("unavailable", [], "api_timeout"), model="x", reserved=r) == r
    unpriced = [{"model": "claude-unpriced", "billed": True, **USAGE}]
    assert call_cost(coi_llm.CoiOutcome("ok", [], entries=unpriced), model="x", reserved=r) == r


def test_the_ledger_key_follows_the_statement_and_the_name_forms():
    record = {"pmid": "7", "authors": [VEV], "coi_statement": "V.E.V. is a founder of Acme Bio."}
    first = ledger_key(record, pi_name("Victor Velculescu"))
    assert first == ledger_key(dict(record), pi_name("Victor Velculescu"))
    assert first != ledger_key(record, pi_name("Victor E. Velculescu"))      # the PI block changed
    assert first != ledger_key(record | {"coi_statement": "V.E.V. founded Beta Bio."}, pi_name("Victor Velculescu"))
    assert ledger_key(record | {"authors": []}, pi_name("Victor Velculescu")) is None


def test_stored_claims_round_trip_and_skip_garbage():
    claim = FounderClaim("Acme Bio", "founder", "7", 2020, "V.E.V. is a founder of Acme Bio.", False)
    assert claims_from_json(claims_to_json([claim])) == [claim]
    assert claims_from_json([{"company_name": "x"}, "junk", None]) == []
    assert claims_from_json(None) == []


def test_a_reservation_grows_with_the_prompt_and_never_falls_below_the_floor():
    from src.services.company_discovery_budget import reservation_usd
    assert reservation_usd("claude-opus-5-5", None) == COI_RESERVE_USD
    assert reservation_usd("claude-opus-5-5", 6_000) == COI_RESERVE_USD   # a normal prompt
    assert reservation_usd("unpriced-model", 10**7) == COI_RESERVE_USD
    big = reservation_usd("claude-opus-5-5", 300_000)                     # 100k tokens
    assert big > COI_RESERVE_USD
    assert big == ((100_000 * Decimal(5) + 4000 * Decimal(20)) / Decimal(1_000_000) * 2).quantize(Decimal("0.0001"))


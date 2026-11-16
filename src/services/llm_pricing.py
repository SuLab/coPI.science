"""Versioned Anthropic price table + cost math for llm_call_logs rows and for the
assessment chat's usage ledger (assessment_chat_usage).

Prices are $/MTok from platform.claude.com/docs/en/about-claude/pricing.
The simulation uses the 5-MINUTE cache TTL exclusively (src/services/llm.py
:147-148 — deliberate), so cache_creation tokens bill at the 1.25x write rate; the
assessment chat uses the same 5-minute TTL.
Batch pricing applies only through cost_for_request (BATCH_MULTIPLIER, the landscaping
pipeline's Message Batches); cost_for_tokens bills standard rates. Haiku 5.5 has a
long-prompt tier, picked per request by cost_for_request. No fast-mode/inference_geo
modifiers apply (none are used in src/). Unknown models are None-priced: the caller renders
"unpriced" and surfaces the model name; a silent $0 is the one forbidden
failure mode. Rows written before migration 0036 have NULL cache columns —
treat NULL as 0 and label those aggregates as floors ("≥"), which
the read side (simulation_stats.CostSummary.is_floor) carries; cost_for_tokens itself
takes plain token counts and has no flag parameter.
"""
from dataclasses import dataclass
from decimal import Decimal

AS_OF = "2026-10-30"

@dataclass(frozen=True)
class ModelPrice:
    input: Decimal          # $/MTok
    output: Decimal
    cache_write_5m: Decimal
    cache_read: Decimal

PRICES: dict[str, ModelPrice] = {
    "claude-opus-5":      ModelPrice(Decimal("5"), Decimal("25"), Decimal("6.25"), Decimal("0.50")),
    "claude-opus-4-6":    ModelPrice(Decimal("5"), Decimal("25"), Decimal("6.25"), Decimal("0.50")),
    # Pricing page, fetched 2026-09-24. claude-opus-5-5 is the assessment chat's model;
    # claude-opus-4-8 is one of its server-side fallback targets.
    "claude-opus-5-5":    ModelPrice(Decimal("4"), Decimal("20"), Decimal("5"), Decimal("0.20")),
    "claude-opus-4-8":    ModelPrice(Decimal("5"), Decimal("25"), Decimal("6.25"), Decimal("0.50")),
    "claude-sonnet-5":    ModelPrice(Decimal("2"), Decimal("10"), Decimal("2.50"), Decimal("0.20")),
    "claude-sonnet-4-6":  ModelPrice(Decimal("3"), Decimal("15"), Decimal("3.75"), Decimal("0.30")),
    "claude-haiku-4-5":   ModelPrice(Decimal("1"), Decimal("5"),  Decimal("1.25"), Decimal("0.10")),
    "claude-sonnet-5-5":  ModelPrice(Decimal("2"), Decimal("10"), Decimal("2.50"), Decimal("0.10")),
    "claude-haiku-5-5":   ModelPrice(Decimal("0.10"), Decimal("0.50"), Decimal("0.125"), Decimal("0.01")),
    "claude-fable-5":     ModelPrice(Decimal("10"), Decimal("50"), Decimal("12.50"), Decimal("1")),
}

#: Haiku 5.5 rates for a request whose own prompt exceeds HAIKU_5_5_LONG_THRESHOLD tokens.
HAIKU_5_5_LONG = ModelPrice(Decimal("0.50"), Decimal("2.50"), Decimal("0.625"), Decimal("0.05"))
HAIKU_5_5_LONG_THRESHOLD = 100_000
#: Message Batches bill at half the standard rate.
BATCH_MULTIPLIER = Decimal("0.5")

_MTOK = Decimal(1_000_000)

def cost_for_tokens(model: str, *, input_tokens: int, output_tokens: int,
                    cache_read: int, cache_creation: int) -> Decimal | None:
    """Dollar cost of one aggregate; None when the model is unpriced."""
    p = PRICES.get(model)
    if p is None:
        return None
    return (
        Decimal(input_tokens) * p.input
        + Decimal(output_tokens) * p.output
        + Decimal(cache_read) * p.cache_read
        + Decimal(cache_creation) * p.cache_write_5m
    ) / _MTOK


def cost_for_request(model: str, *, input_tokens: int, output_tokens: int,
                     cache_read: int, cache_creation: int, batch: bool) -> Decimal | None:
    """Dollar cost of one request; None when the model is unpriced.

    Haiku 5.5's tier follows this request's own prompt length (input + cache read +
    cache creation above 100,000 tokens is the long tier); ``batch`` then halves the
    result."""
    p = PRICES.get(model)
    if p is None:
        return None
    if model == "claude-haiku-5-5" and (
        input_tokens + cache_read + cache_creation > HAIKU_5_5_LONG_THRESHOLD
    ):
        p = HAIKU_5_5_LONG
    cost = (
        Decimal(input_tokens) * p.input
        + Decimal(output_tokens) * p.output
        + Decimal(cache_read) * p.cache_read
        + Decimal(cache_creation) * p.cache_write_5m
    ) / _MTOK
    return cost * BATCH_MULTIPLIER if batch else cost

"""Versioned Anthropic price table + cost math for llm_call_logs rows.

Prices are $/MTok from platform.claude.com/docs/en/about-claude/pricing.
The simulation uses the 5-MINUTE cache TTL exclusively (src/services/llm.py
:147-148 — deliberate), so cache_creation tokens bill at the 1.25x write rate.
No batch/fast-mode/inference_geo modifiers apply (none are used in src/ —
verified 2026-08-30). Unknown models are None-priced: the caller renders
"unpriced" and surfaces the model name; a silent $0 is the one forbidden
failure mode. Rows written before migration 0036 have NULL cache columns —
treat NULL as 0 and label those aggregates as floors ("≥"), which
the read side (Task 9's CostSummary.is_floor) carries; cost_for_tokens itself
takes plain token counts and has no flag parameter.
"""
from dataclasses import dataclass
from decimal import Decimal

AS_OF = "2026-08-29"

@dataclass(frozen=True)
class ModelPrice:
    input: Decimal          # $/MTok
    output: Decimal
    cache_write_5m: Decimal
    cache_read: Decimal

PRICES: dict[str, ModelPrice] = {
    "claude-opus-5":      ModelPrice(Decimal("5"), Decimal("25"), Decimal("6.25"), Decimal("0.50")),
    "claude-opus-4-6":    ModelPrice(Decimal("5"), Decimal("25"), Decimal("6.25"), Decimal("0.50")),
    "claude-sonnet-5":    ModelPrice(Decimal("2"), Decimal("10"), Decimal("2.50"), Decimal("0.20")),
    "claude-sonnet-4-6":  ModelPrice(Decimal("3"), Decimal("15"), Decimal("3.75"), Decimal("0.30")),
    "claude-haiku-4-5":   ModelPrice(Decimal("1"), Decimal("5"),  Decimal("1.25"), Decimal("0.10")),
    "claude-fable-5":     ModelPrice(Decimal("10"), Decimal("50"), Decimal("12.50"), Decimal("1")),
}

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

"""Tests for llm_pricing module."""
import re
from decimal import Decimal

from src.services.llm_pricing import (
    AS_OF,
    BATCH_MULTIPLIER,
    PRICES,
    cost_for_request,
    cost_for_tokens,
)


def test_cost_for_tokens_hand_computed_case():
    """Opus-5 with input 1M / output 100K / cache_read 500K / cache_creation 200K."""
    cost = cost_for_tokens(
        "claude-opus-5",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read=500_000,
        cache_creation=200_000,
    )
    assert cost == Decimal("9.00")


def test_cost_for_tokens_unknown_model_returns_none():
    """Unknown model returns None."""
    cost = cost_for_tokens(
        "claude-unknown-99",
        input_tokens=1000,
        output_tokens=100,
        cache_read=0,
        cache_creation=0,
    )
    assert cost is None


def test_four_production_models_are_priced():
    """Drift alarm: the four production models are all in PRICES."""
    production_models = [
        "claude-opus-5",
        "claude-opus-4-6",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
    ]
    for model in production_models:
        assert model in PRICES, f"Drift: {model} not in PRICES"
        # Also verify cost_for_tokens returns a value (not None)
        cost = cost_for_tokens(model, input_tokens=1, output_tokens=1, cache_read=0, cache_creation=0)
        assert cost is not None, f"Drift: {model} not priced"


def test_cost_for_tokens_zero_tokens_returns_zero():
    """Zero tokens across all types returns Decimal 0."""
    cost = cost_for_tokens(
        "claude-opus-5",
        input_tokens=0,
        output_tokens=0,
        cache_read=0,
        cache_creation=0,
    )
    assert cost == Decimal("0")


def test_as_of_date_format():
    """AS_OF matches YYYY-MM-DD format."""
    pattern = r"^\d{4}-\d{2}-\d{2}$"
    assert re.match(pattern, AS_OF), f"AS_OF '{AS_OF}' does not match format {pattern}"


def test_the_assessment_chat_model_and_its_fallback_targets_are_priced():
    """claude-opus-5-5 is the chat model; claude-opus-4-8 and claude-opus-5 are its
    server-side fallback targets (Models API allowed_fallback_models, 2026-09-24).
    An unpriced one would make the chat's dollar ceilings blind."""
    for model in ("claude-opus-5-5", "claude-opus-4-8", "claude-opus-5"):
        assert model in PRICES, model
        assert cost_for_tokens(model, input_tokens=1, output_tokens=1, cache_read=0, cache_creation=0)


def test_opus_5_5_hand_computed_case():
    """$4 in, $20 out, $0.20 cache read, $5 5-minute cache write (per MTok):
    4 + 2 + 0.20 + 5 = 11.20."""
    cost = cost_for_tokens(
        "claude-opus-5-5",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read=1_000_000,
        cache_creation=1_000_000,
    )
    assert cost == Decimal("11.20")


def test_opus_4_8_is_priced_like_opus_5():
    args = dict(input_tokens=123_456, output_tokens=7_890, cache_read=50_000, cache_creation=20_000)
    assert cost_for_tokens("claude-opus-4-8", **args) == cost_for_tokens("claude-opus-5", **args)


_KW = dict(output_tokens=0, cache_read=0, cache_creation=0)


def test_cost_for_request_haiku_tiers():
    short = cost_for_request("claude-haiku-5-5", input_tokens=100_000, batch=False, **_KW)
    long = cost_for_request("claude-haiku-5-5", input_tokens=100_001, batch=False, **_KW)
    assert short == Decimal("0.01")
    assert long == Decimal(100_001) * Decimal("0.50") / Decimal(1_000_000)
    # The tier counts cache tokens toward the prompt length.
    mixed = cost_for_request(
        "claude-haiku-5-5", input_tokens=50_000, output_tokens=0,
        cache_read=40_000, cache_creation=10_001, batch=False,
    )
    assert mixed == (
        Decimal(50_000) * Decimal("0.50") + Decimal(40_000) * Decimal("0.05")
        + Decimal(10_001) * Decimal("0.625")
    ) / Decimal(1_000_000)


def test_cost_for_request_batch_halves():
    kw = dict(input_tokens=1_000, output_tokens=500, cache_read=200, cache_creation=100)
    full = cost_for_request("claude-sonnet-5-5", batch=False, **kw)
    assert full is not None
    assert cost_for_request("claude-sonnet-5-5", batch=True, **kw) == full * BATCH_MULTIPLIER
    assert BATCH_MULTIPLIER == Decimal("0.5")


def test_unpriced_model_returns_none():
    assert cost_for_request("claude-nonexistent", input_tokens=1, batch=True, **{
        "output_tokens": 1, "cache_read": 0, "cache_creation": 0}) is None


def test_cost_for_tokens_unchanged():
    # 1M input + 1M output on claude-opus-5 at $5 / $25.
    assert cost_for_tokens(
        "claude-opus-5", input_tokens=1_000_000, output_tokens=1_000_000,
        cache_read=0, cache_creation=0,
    ) == Decimal("30")
    assert cost_for_request(
        "claude-opus-5", input_tokens=1_000_000, output_tokens=1_000_000,
        cache_read=0, cache_creation=0, batch=False,
    ) == Decimal("30")

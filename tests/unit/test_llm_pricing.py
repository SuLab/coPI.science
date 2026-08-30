"""Tests for llm_pricing module."""
import re
from decimal import Decimal

from src.services.llm_pricing import AS_OF, PRICES, cost_for_tokens


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

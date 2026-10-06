"""COMPANY_DISCOVERY_DAILY_USD_LIMIT (spec 2026-10-05 §6.2, D36): $20 by default; a
non-positive value falls back with a WARNING; nan or inf refuses to start."""
import logging
import math

import pytest

from src.config import Settings


def test_the_default_is_twenty_dollars():
    assert Settings(_env_file=None).company_discovery_daily_usd_limit == 20.0


@pytest.mark.parametrize("value", [0, -5.0])
def test_a_non_positive_limit_falls_back(value, caplog):
    with caplog.at_level(logging.WARNING, logger="src.config"):
        s = Settings(_env_file=None, company_discovery_daily_usd_limit=value)
    assert s.company_discovery_daily_usd_limit == 20.0
    assert "COMPANY_DISCOVERY_DAILY_USD_LIMIT" in caplog.text


@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_a_non_finite_limit_refuses_to_start(value):
    with pytest.raises(ValueError, match="COMPANY_DISCOVERY_DAILY_USD_LIMIT"):
        Settings(_env_file=None, company_discovery_daily_usd_limit=value)

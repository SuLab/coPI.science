"""Tests for grantbot's minimum-lead-time filter."""

from datetime import datetime, timezone

from src.agent.grantbot import (
    MIN_LEAD_DAYS,
    _has_sufficient_lead_time,
    _parse_close_date,
)


NOW = datetime(2026, 5, 3, tzinfo=timezone.utc)


def test_parse_close_date_mdy():
    assert _parse_close_date("05/04/2026") == datetime(2026, 5, 4, tzinfo=timezone.utc)


def test_parse_close_date_iso():
    assert _parse_close_date("2026-05-04") == datetime(2026, 5, 4, tzinfo=timezone.utc)


def test_parse_close_date_empty_returns_none():
    assert _parse_close_date("") is None
    assert _parse_close_date("Not specified") is None


def test_short_lead_time_rejected():
    # 1-day lead — the original incident
    assert not _has_sufficient_lead_time("05/04/2026", NOW, MIN_LEAD_DAYS)


def test_exactly_at_threshold_accepted():
    # Exactly 21 days out → accepted
    assert _has_sufficient_lead_time("05/24/2026", NOW, MIN_LEAD_DAYS)


def test_long_lead_time_accepted():
    assert _has_sufficient_lead_time("12/01/2026", NOW, MIN_LEAD_DAYS)


def test_unparseable_close_date_accepted():
    # Rolling/standing FOAs have no deadline — keep them
    assert _has_sufficient_lead_time("", NOW, MIN_LEAD_DAYS)
    assert _has_sufficient_lead_time("Not specified", NOW, MIN_LEAD_DAYS)


def test_already_past_close_date_rejected():
    assert not _has_sufficient_lead_time("01/01/2026", NOW, MIN_LEAD_DAYS)

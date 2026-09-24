"""Assessment-chat settings (spec §10.1): the defaults, and the warn-and-fall-back
guard that keeps a typo'd .env value from refusing every question."""

import logging
from typing import get_args

import pytest

from src.config import ASSESSMENT_CHAT_EFFORTS, Settings


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_the_effort_type_and_the_effort_list_agree():
    annotation = Settings.model_fields["assessment_chat_effort"].annotation
    assert get_args(annotation) == ASSESSMENT_CHAT_EFFORTS


def test_defaults_are_the_specified_values():
    s = _settings()
    assert s.llm_assessment_chat_model == "claude-opus-5-5"
    assert s.assessment_chat_enabled is True
    assert s.assessment_chat_effort == "medium"
    assert s.assessment_chat_daily_question_limit == 100
    assert s.assessment_chat_daily_user_usd_limit == 20.0
    assert s.assessment_chat_daily_total_usd_limit == 100.0
    assert s.assessment_chat_max_question_chars == 4000
    assert s.assessment_chat_max_turns == 50


@pytest.mark.parametrize("effort", ASSESSMENT_CHAT_EFFORTS)
def test_every_supported_effort_is_kept(effort):
    assert _settings(assessment_chat_effort=effort).assessment_chat_effort == effort


def test_an_unknown_effort_falls_back_to_medium_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="src.config"):
        s = _settings(assessment_chat_effort="extreme")
    assert s.assessment_chat_effort == "medium"
    assert "ASSESSMENT_CHAT_EFFORT" in caplog.text


@pytest.mark.parametrize(
    "name,bad,default",
    [
        ("assessment_chat_daily_question_limit", 0, 100),
        ("assessment_chat_daily_user_usd_limit", -1.0, 20.0),
        ("assessment_chat_daily_total_usd_limit", 0.0, 100.0),
        ("assessment_chat_max_question_chars", 0, 4000),
        ("assessment_chat_max_turns", -5, 50),
    ],
)
def test_a_non_positive_limit_falls_back_with_a_warning(caplog, name, bad, default):
    with caplog.at_level(logging.WARNING, logger="src.config"):
        s = _settings(**{name: bad})
    assert getattr(s, name) == default
    assert name.upper() in caplog.text


@pytest.mark.parametrize(
    "name",
    ["assessment_chat_daily_user_usd_limit", "assessment_chat_daily_total_usd_limit"],
)
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_usd_limit_raises_instead_of_falling_back(name, bad):
    """`value <= 0` is False for both `nan` and `inf`, so the warn-and-fall-back guard
    above would let both slip through: a NaN ceiling 500s every ask in
    `decimal.Decimal`, and `inf` silently disables the ceiling it exists to enforce.
    Both fail startup instead (SB-7)."""
    with pytest.raises(Exception, match=name.upper()):
        _settings(**{name: bad})

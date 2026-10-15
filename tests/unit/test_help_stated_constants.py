"""The only code constants the help pages state (spec 2026-10-14 §6.3 rule 4): the 1-5
overall rating and the review comment limit. Each is pinned to the validator that
enforces it and to the help text that states it, so moving either value fails here until
templates/help/pages/assessments.html is updated."""

import re
from pathlib import Path

import pytest

from src.services.assessment_reviews import _MAX_COMMENT_CHARS, _validate, _validate_comment

ROOT = Path(__file__).resolve().parents[2]
HELP_PAGE = ROOT / "templates/help/pages/assessments.html"


def _reviewing_section() -> str:
    """Template source from the `reviewing` section's heading to the next <h2."""
    source = HELP_PAGE.read_text(encoding="utf-8")
    start = source.index('id="reviewing"')
    end = source.find("<h2", start)
    return source[start:] if end == -1 else source[start:end]


@pytest.mark.parametrize(("score", "valid"), [(0, False), (1, True), (5, True), (6, False)])
def test_overall_rating_bounds_are_one_to_five(score, valid):
    # No dimension scores, so _validate never reads the rubric document.
    if valid:
        _validate(score, "learn")
    else:
        with pytest.raises(ValueError):
            _validate(score, "learn")


def test_comment_limit_is_ten_thousand_characters():
    assert _MAX_COMMENT_CHARS == 10_000
    _validate_comment("x" * 10_000)
    with pytest.raises(ValueError):
        _validate_comment("x" * 10_001)


def test_reviewing_section_states_the_same_values():
    section = _reviewing_section()
    assert re.search(r"\b1\b\D{0,40}\b5\b", section), "the 1-5 rating range is not stated"
    assert f"{_MAX_COMMENT_CHARS:,}" in section, "the comment limit is not stated"

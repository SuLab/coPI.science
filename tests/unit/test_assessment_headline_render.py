"""The `#assessments-summary` headline renders five fields and no more (D12)."""

import pytest

from src.services.assessment_headline import render_assessment_headline


def test_a_scored_verdict_renders_all_five_fields():
    text = render_assessment_headline(
        pi_label="Jeffrey Rothstein",
        project="CHMP7 / ESCRT-III–nuclear-pore-injury axis in ALS",
        recommendation="conditional",
        scores={"a": 3, "b": 3},
        permalink="https://slack.example/p1",
    )
    assert text.startswith(":mag: Jeffrey Rothstein — ")
    assert "CHMP7" in text
    assert "*conditional*" in text
    assert "band:" in text and "score:" in text
    assert "<https://slack.example/p1|View interview>" in text


def test_pass_is_displayed_as_decline():
    text = render_assessment_headline(
        pi_label="Wang", project="X", recommendation="pass",
        scores={"a": 1}, permalink=None,
    )
    assert "*decline*" in text
    assert "*pass*" not in text


def test_no_scores_omits_band_and_score_entirely():
    """An empty scores map is 'we don't know', not a 0.00 that bands as a
    decline nobody made — the same reason `_persist_assessment` leaves those
    columns NULL."""
    text = render_assessment_headline(
        pi_label="Wang", project="X", recommendation="conditional",
        scores={}, permalink=None,
    )
    assert "band:" not in text
    assert "score:" not in text


def test_a_missing_permalink_degrades_rather_than_dropping_the_post():
    text = render_assessment_headline(
        pi_label="Wang", project="X", recommendation="conditional",
        scores={}, permalink=None,
    )
    assert "(link unavailable)" in text


@pytest.mark.parametrize("bad", [None, 42, {"nested": "object"}, ""])
def test_a_non_string_project_degrades_to_untitled(bad):
    """A model that answers `company_or_project` with an object must not get a
    Python repr posted to a public channel."""
    text = render_assessment_headline(
        pi_label="Wang", project=bad, recommendation="conditional",
        scores={}, permalink=None,
    )
    assert "(untitled)" in text
    assert "nested" not in text


def test_an_overlong_project_is_clipped_to_a_headline():
    text = render_assessment_headline(
        pi_label="Wang", project="z" * 500, recommendation="conditional",
        scores={}, permalink=None,
    )
    assert "z" * 120 in text
    assert "z" * 121 not in text

"""The `#assessments-summary` headline renders five fields and no more (D12).

Also covers the `score`/`band` override added in fix round 1 (2026-08-29): a
repaired headline must say exactly what the stored row already said —
`opportunity_assessments.weighted_score`/`.band`, computed once at write time
— rather than recomputing from `scores` against whatever rubric happens to be
live when `scripts/backfill_assessment_headlines.py` runs. See that module's
docstring and `docs/audits/2026-08-29-lost-assessment-headlines/README.md`
for the loss this guards against.

The engine's own call site (`SimulationEngine._post_assessment_summary`)
passes neither kwarg and stays on the compute-from-`scores` path; it is
covered, untouched, by `tests/unit/test_assessments_summary_post.py`.
"""

import pytest

from src.services.assessment_headline import render_assessment_headline
from src.services.blackbird_rubric import band as rubric_band
from src.services.blackbird_rubric import weighted_score as rubric_weighted_score


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


# ---------------------------------------------------------------------------
# Fix round 1 — the score/band override
# ---------------------------------------------------------------------------

# All six live rubric dimensions maxed out: whatever this computes to is a
# HIGH score that bands "advance" — deliberately far from the override used
# below, so a test that accidentally fell through to the compute path is
# caught by a mismatched NUMBER, not merely a present one.
HIGH_SCORES = {
    "differentiation_unmet_need": 5, "scientific_credibility": 5,
    "translational_path": 5, "fundable_experiment": 5,
    "venture_potential": 5, "team_executability": 5,
}


def test_both_overrides_supplied_are_used_verbatim_not_recomputed():
    computed_score = rubric_weighted_score(HIGH_SCORES)
    computed_band = rubric_band(computed_score)
    assert computed_band == "advance"  # sanity: the fixture really is "high"

    text = render_assessment_headline(
        pi_label="Lee", project="Widget", recommendation="conditional",
        scores=HIGH_SCORES, permalink=None,
        score=1.23, band="pass",
    )

    assert "score: 1.2" in text
    assert "band: pass" in text
    assert f"score: {computed_score:.1f}" not in text
    assert "band: advance" not in text


def test_only_score_supplied_falls_back_to_computing_from_scores():
    computed_score = rubric_weighted_score(HIGH_SCORES)
    computed_band = rubric_band(computed_score)

    text = render_assessment_headline(
        pi_label="Lee", project="Widget", recommendation="conditional",
        scores=HIGH_SCORES, permalink=None,
        score=1.23, band=None,
    )

    assert f"score: {computed_score:.1f}" in text
    assert f"band: {computed_band}" in text
    assert "score: 1.2" not in text


def test_only_band_supplied_falls_back_to_computing_from_scores():
    computed_score = rubric_weighted_score(HIGH_SCORES)
    computed_band = rubric_band(computed_score)

    text = render_assessment_headline(
        pi_label="Lee", project="Widget", recommendation="conditional",
        scores=HIGH_SCORES, permalink=None,
        score=None, band="pass",
    )

    assert f"score: {computed_score:.1f}" in text
    assert f"band: {computed_band}" in text
    assert "band: pass" not in text


def test_a_null_stored_score_omits_the_band_segment_entirely():
    """The stored-row override path for a verdict whose `weighted_score` is
    NULL — `_persist_assessment` leaves both `weighted_score` and `band` NULL
    together when a verdict carries no dimension scores, and the repair
    script passes those straight through (`score=row.weighted_score,
    band=row.band`). The headline must omit the segment, not print `None`.
    """
    text = render_assessment_headline(
        pi_label="Lee", project="Widget", recommendation="conditional",
        scores=None, permalink=None,
        score=None, band=None,
    )

    assert "band" not in text.lower()
    assert "score" not in text.lower()
    assert "None" not in text


# ---------------------------------------------------------------------------
# Elevator pitch (2026-09-09) — a deliberate widening of D12 from five fields
# to six. See the module docstring.
# ---------------------------------------------------------------------------


def test_the_pitch_renders_as_a_second_line():
    text = render_assessment_headline(
        pi_label="Yarchoan lab",
        project="Th17/IL-8 plasma cytokine score",
        recommendation="conditional",
        scores={},
        permalink=None,
        elevator_pitch="Hopkins has 39-plex cytokine data on 124 ICI patients.",
    )
    assert "Hopkins has 39-plex cytokine data" in text
    assert text.count("\n") == 1


def test_an_absent_pitch_is_byte_identical_to_the_old_output():
    """A6/A3. Every row in production today has elevator_pitch IS NULL, and the
    repair script shares this renderer — so the widening must be invisible for
    a row that carries no pitch, or re-running the backfill would change what
    it posts for rows it has already handled."""
    common = dict(
        pi_label="Yarchoan lab",
        project="Th17/IL-8 plasma cytokine score",
        recommendation="conditional",
        scores={},
        permalink=None,
    )
    assert render_assessment_headline(**common) == render_assessment_headline(
        **common, elevator_pitch=None
    )
    assert "\n" not in render_assessment_headline(**common)


def test_a_non_string_pitch_is_dropped_not_repr_posted():
    """`_clip` drops a non-string outright: a model that answers with an object
    must not have a Python repr posted to a workspace-visible channel."""
    text = render_assessment_headline(
        pi_label="L", project="P", recommendation="pass", scores={}, permalink=None,
        elevator_pitch={"not": "a string"},
    )
    assert "not" not in text
    assert "\n" not in text


def test_an_overlong_pitch_is_clipped():
    from src.services.assessment_headline import PITCH_DISPLAY_CHARS

    text = render_assessment_headline(
        pi_label="L", project="P", recommendation="pass", scores={}, permalink=None,
        elevator_pitch="x" * (PITCH_DISPLAY_CHARS + 500),
    )
    assert "x" * PITCH_DISPLAY_CHARS in text
    assert "x" * (PITCH_DISPLAY_CHARS + 1) not in text

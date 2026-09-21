"""The `#assessments-summary` headline renders six fields and no more (D12,
widened once). PI/lab name, project, recommendation, band/score and permalink
were the original five; since 2026-09-09 a sixth, the sidecar's
`elevator_pitch`, renders on a second line — see the "Elevator pitch" section
below and the module docstring of `src/services/assessment_headline.py`. The
widening rests on the operator's assertion that PIs cannot join the Slack
workspace, which no code enforces, so it is a deliberately accepted risk, not
a free extension of the existing five-field policy.

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


# ---------------------------------------------------------------------------
# `_clip_at_sentence` (P1 fix, 2026-09-14) — the pitch is clipped at a
# sentence boundary rather than mid-word. `test_an_overlong_pitch_is_clipped`
# above is left unchanged, per the task: it pins the no-boundary/no-whitespace
# case, which `_clip_at_sentence` must still satisfy.
# ---------------------------------------------------------------------------


def test_a_short_pitch_is_byte_identical_to_the_old_clip():
    from src.services.assessment_headline import PITCH_DISPLAY_CHARS, _clip, _clip_at_sentence

    short = "Hopkins has 39-plex cytokine data on 124 ICI patients."
    assert _clip_at_sentence(short, PITCH_DISPLAY_CHARS) == _clip(
        short, PITCH_DISPLAY_CHARS
    )


def test_a_pitch_of_exactly_the_cap_is_unchanged():
    from src.services.assessment_headline import PITCH_DISPLAY_CHARS, _clip_at_sentence

    exact = "x" * PITCH_DISPLAY_CHARS
    assert _clip_at_sentence(exact, PITCH_DISPLAY_CHARS) == exact


def test_a_long_pitch_is_clipped_at_a_sentence_boundary_and_marked():
    """REVERSED from "...with_no_ellipsis" (2026-09-14 audit). The marker is
    unconditional on a real truncation: the scan takes the HIGHEST qualifying
    boundary and the pitch contract requires the citation sentence to END
    within approximately the first 550 characters (sidecar item 8, scout_hub
    1.7.0), so that boundary can be an abbreviation ("et al. ", "e.g. ")
    rather than a sentence end. A reader would have no way to tell the
    published fragment from the whole pitch. See `_clip_at_sentence`."""
    from src.services.assessment_headline import PITCH_DISPLAY_CHARS, _clip_at_sentence

    # A sentence terminator sits well past the half-budget mark (300 of 600),
    # with no whitespace anywhere else in the window, so the boundary path is
    # exercised unambiguously.
    prefix = "y" * 310
    sentence_end = "Sentence ends here. "
    padding = "y" * 400
    value = prefix + sentence_end + padding
    assert len(value) > PITCH_DISPLAY_CHARS

    result = _clip_at_sentence(value, PITCH_DISPLAY_CHARS)

    assert result == prefix + "Sentence ends here." + " …"


def test_a_terminator_at_the_cap_is_rejected_unless_a_space_follows_it():
    """The end-of-window candidate is `max_len`, which is by construction the
    LARGEST, so it used to beat every real boundary. A pitch whose 600th
    character is the "." of "2.5-fold" or of "et al." then published
    "... at 2." to a channel the post cannot be retracted from."""
    from src.services.assessment_headline import PITCH_DISPLAY_CHARS, _clip_at_sentence

    head = "Some words here and more words follow along nicely. "
    value = head + "y" * (598 - len(head)) + "2." + "5-fold more potent."
    assert value[PITCH_DISPLAY_CHARS] == "5"  # the cap lands mid-number

    result = _clip_at_sentence(value, PITCH_DISPLAY_CHARS)

    assert not result.rstrip(" …").endswith("2.")
    assert result == "Some words here and more words follow along nicely." + " …"


def test_a_true_sentence_end_at_the_cap_is_accepted():
    """The other side of the guard: a terminator at the cap whose next
    character IS whitespace is a real sentence end and must be used."""
    from src.services.assessment_headline import PITCH_DISPLAY_CHARS, _clip_at_sentence

    head = "z" * (PITCH_DISPLAY_CHARS - len(" ends right here.")) + " ends right here."
    value = head + " And more follows."
    # head FILLS the window, so window[-1] is its "." and the next character
    # (the first of " And more follows.") is the space that makes it a real
    # sentence end.
    assert len(head) == PITCH_DISPLAY_CHARS and value[PITCH_DISPLAY_CHARS] == " "

    result = _clip_at_sentence(value, PITCH_DISPLAY_CHARS)

    assert result == head + " …"


def test_a_window_whose_only_whitespace_is_a_newline_still_backs_off():
    """A markdown pitch separates paragraphs with newlines, and the back-off
    used to search for a literal space only — so such a window fell through to
    the mid-word cut this function exists to remove."""
    from src.services.assessment_headline import PITCH_DISPLAY_CHARS, _clip_at_sentence

    value = "w" * 300 + "\n" + "q" * 400

    result = _clip_at_sentence(value, PITCH_DISPLAY_CHARS)

    assert result == "w" * 300 + " …"


def test_a_long_pitch_with_whitespace_but_no_boundary_clips_at_a_word():
    from src.services.assessment_headline import PITCH_DISPLAY_CHARS, _clip_at_sentence

    # No sentence terminators at all, but plenty of word breaks.
    value = " ".join(["word"] * (PITCH_DISPLAY_CHARS // 5 + 50))
    assert len(value) > PITCH_DISPLAY_CHARS

    result = _clip_at_sentence(value, PITCH_DISPLAY_CHARS)

    assert result.endswith(" …")
    window = value[:PITCH_DISPLAY_CHARS]
    assert result == window[: window.rfind(" ")] + " …"
    assert len(result) <= PITCH_DISPLAY_CHARS + 2


def test_a_non_string_pitch_is_dropped_by_clip_at_sentence():
    from src.services.assessment_headline import _clip_at_sentence

    assert _clip_at_sentence({"not": "a string"}, 600) is None
    assert _clip_at_sentence(None, 600) is None
    assert _clip_at_sentence("", 600) is None


def test_a_late_citation_sentence_is_dropped_whole_not_clipped():
    """Evidence for the 550-character budget: _clip_at_sentence cuts at the last
    terminator INSIDE value[:600], so a citation sentence that starts before 600
    but ends after it is removed entirely, not truncated."""
    from src.services.assessment_headline import _clip_at_sentence

    s1_3 = ("A. " * 4) + "B" * 388 + ". "   # ends at ~400
    s4 = "The work builds on " + "c" * 190 + "."   # 210 chars, ends past 600
    pitch = s1_3 + s4 + " Tail sentence."
    out = _clip_at_sentence(pitch, 600)
    assert "The work builds on" not in out, (
        "a citation sentence ending past 600 must be dropped whole — this is why "
        "the contract bounds where sentence 4 ENDS, not where it begins"
    )


def test_a_sentence_ending_at_a_paragraph_break_is_a_real_boundary():
    """Regression, scout_hub 1.7.0: `elevator_pitch` is contractually markdown
    with "short paragraphs separated by a blank line", and item 8 puts the
    provenance citation in sentence FOUR, bounded to end within ~550 chars so
    it lands inside the 600 that post publicly.

    `_clip_at_sentence` used to scan for the literal pairs ". ", "! " and "? ",
    so a sentence ending at a paragraph break (".\n\n") was invisible to it.
    Measured against the shape the contract now asks for, a citation sentence
    ending at offset 545 was dropped whole and the excerpt cut at 391 — the
    550-char budget is the entire mechanism protecting the published
    citation, and a terminator the scan cannot see silently defeats it.
    """
    from src.services.assessment_headline import _clip_at_sentence

    elements_1_2 = "A" * 248 + ". "
    element_3 = "B" * 138 + ". "
    citation = "The work builds on https://doi.org/10.7554/eLife.94488" + "x" * 100 + "."
    pitch = elements_1_2 + element_3 + citation + "\n\n" + "C" * 300
    assert len(elements_1_2 + element_3 + citation) == 545, "the shape under test"

    out = _clip_at_sentence(pitch, 600)
    assert "The work builds on" in out, (
        "a citation sentence ending at 545 must survive the 600-char window "
        "even when a markdown paragraph break follows its full stop"
    )
    assert out.endswith(" …"), "still a real truncation, so still marked"

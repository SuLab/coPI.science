import pytest

from src.services.blackbird_rubric import RUBRIC_WEIGHTS, band, weighted_score


def test_weights_are_the_six_dimensions_and_sum_to_one_hundred():
    assert RUBRIC_WEIGHTS == {
        "differentiation_unmet_need": 25,
        "scientific_credibility": 20,
        "translational_path": 15,
        "fundable_experiment": 15,
        "venture_potential": 15,
        "team_executability": 10,
    }
    assert sum(RUBRIC_WEIGHTS.values()) == 100


def test_science_carries_thirty_five_percent():
    """BBL rejects on science, so the score must be able to move on science
    alone — the two scientific dimensions jointly carry 35 points."""
    science = {"scientific_credibility", "translational_path"}
    assert sum(RUBRIC_WEIGHTS[k] for k in science) == 35


def test_all_fives_is_five_and_all_ones_is_one():
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, 5)) == 5.0
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, 1)) == 1.0


def test_a_real_verdict_scores_as_hand_computed():
    scores = {
        "differentiation_unmet_need": 4, "scientific_credibility": 3,
        "translational_path": 3, "fundable_experiment": 4,
        "venture_potential": 2, "team_executability": 4,
    }
    # 100 + 60 + 45 + 60 + 30 + 40 = 335 / 100
    assert weighted_score(scores) == 3.35


def test_missing_and_unscorable_dimensions_count_as_zero():
    # differentiation_unmet_need's weight is 25 of 100: a lone perfect score on
    # it alone is 25 * 5 / 100 = 1.25 — no single dimension can carry the whole
    # scale by itself.
    assert weighted_score({"differentiation_unmet_need": 5}) == 1.25
    assert weighted_score({"differentiation_unmet_need": "high"}) == 0.0
    assert weighted_score({}) == 0.0
    assert weighted_score(None) == 0.0


def test_out_of_range_scores_are_clamped_to_one_through_five():
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, 9)) == 5.0
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, -3)) == 1.0


@pytest.mark.parametrize("score,expected", [
    (3.4, "advance"), (4.7, "advance"),
    (3.39, "conditional"), (2.8, "conditional"),
    (2.79, "pass"), (0.0, "pass"),
])
def test_banding_matches_the_document(score, expected):
    assert band(score) == expected


def test_non_finite_scores_count_as_zero_not_a_perfect_five():
    nan = float("nan")
    inf = float("inf")
    ninf = float("-inf")
    # All six dimensions unscorable via NaN/inf/-inf: must be the "no usable
    # scores" 0.0, never a clamped 5.0 (inf) or 1.0 (-inf), and never NaN's
    # perfect 5.0 that min()/max()'s NaN-is-always-False comparisons produce.
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, nan)) == 0.0
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, inf)) == 0.0
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, ninf)) == 0.0
    # A single non-finite dimension among otherwise-perfect scores must still
    # drag the total down by its full weight, not be dropped from the
    # denominator or clamped into range: (100 - 10) * 5 / 100 = 4.5.
    scores_with_nan = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_nan["team_executability"] = nan
    assert weighted_score(scores_with_nan) == 4.5
    scores_with_inf = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_inf["team_executability"] = inf
    assert weighted_score(scores_with_inf) == 4.5
    scores_with_ninf = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_ninf["team_executability"] = ninf
    assert weighted_score(scores_with_ninf) == 4.5


def test_display_rounding_cannot_flip_the_band_across_a_threshold():
    # Five dimensions at 3.4, team_executability (weight 10) at 3.35: true mean
    # is exactly 3.395, which is < 3.4 and must band as "conditional". Naive
    # round(3.395, 2) == 3.4, which would wrongly band as "advance" — a
    # proposal promoted by a rounding artefact instead of its true score.
    scores = dict.fromkeys(RUBRIC_WEIGHTS, 3.4)
    scores["team_executability"] = 3.35
    score = weighted_score(scores)
    assert score == 3.39
    assert band(score) == "conditional"


def test_case_and_whitespace_variant_keys_still_match_their_dimension():
    # Finding A5: a key differing only in case (or padded with stray
    # whitespace) must still hit its rubric weight, not be silently treated
    # as missing (and thus scored 0) purely because of spelling.
    scores = {
        "Differentiation_Unmet_Need": 4, "SCIENTIFIC_CREDIBILITY": 3,
        " translational_path ": 3, "Fundable_Experiment": 4,
        "venture_potential": 2, " Team_Executability ": 4,
    }
    # Same hand-computed total as test_a_real_verdict_scores_as_hand_computed.
    assert weighted_score(scores) == 3.35


def test_unrecognized_key_is_logged_and_still_scores_as_zero(caplog):
    # A typo'd or made-up key must count as 0 (it can never match a rubric
    # dimension) but should be diagnosable — not just a mysteriously low
    # score with nothing pointing at why.
    scores = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores["venture_potental"] = 1  # typo — shadows nothing, the real key is still 5
    score = weighted_score(scores)
    assert score == 5.0  # the typo'd key never touched a real dimension
    assert "venture_potental" in caplog.text
    assert "not in the six" in caplog.text


def test_out_of_scale_scores_are_logged_when_clamped(caplog):
    # An explicit 0 is out of contract (the scale is 1-5) and clamps UP to 1,
    # so the stored score no longer equals the score that was counted. Run
    # ee419dd3 stored three such 0s and nothing said so — the clamp must be
    # diagnosable, same policy as the unrecognized-key warning above.
    scores = dict.fromkeys(RUBRIC_WEIGHTS, 3)
    scores["venture_potential"] = 0
    weighted_score(scores)
    assert "venture_potential" in caplog.text
    assert "clamped" in caplog.text


def test_in_range_and_non_numeric_scores_do_not_log_a_clamp(caplog):
    # In-range values need no diagnosis, and a non-numeric value takes the
    # unscorable path (counts as 0 without ever reaching the clamp) — warning
    # "clamped" there would misdescribe what happened.
    weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, 3))
    weighted_score({"differentiation_unmet_need": "high"})
    weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, float("nan")))
    assert "clamped" not in caplog.text


def test_bool_dimension_values_count_as_zero_not_as_one_or_zero():
    # isinstance(True, int) is True in Python, so without an explicit bool
    # guard a True score would be silently accepted as 1 (and False as 0,
    # itself a valid-looking but wrong score) instead of being treated as the
    # unscorable, non-numeric value it actually is.
    assert weighted_score({"differentiation_unmet_need": True}) == 0.0
    assert weighted_score({"differentiation_unmet_need": False}) == 0.0
    scores_with_true = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_true["team_executability"] = True
    assert weighted_score(scores_with_true) == 4.5
    scores_with_false = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_false["team_executability"] = False
    assert weighted_score(scores_with_false) == 4.5

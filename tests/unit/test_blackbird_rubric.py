import pytest

from src.services.blackbird_rubric import RUBRIC_WEIGHTS, band, weighted_score


def test_weights_match_the_pdf_and_sum_to_one_hundred():
    assert RUBRIC_WEIGHTS == {
        "differentiation": 20,
        "market_unmet_need": 15,
        "team": 15,
        "external_signals": 15,
        "ip_fto": 10,
        "platform": 8,
        "dev_regulatory_feasibility": 7,
        "workplan_capital_efficiency": 5,
        "exit_thesis": 5,
    }
    assert sum(RUBRIC_WEIGHTS.values()) == 100


def test_all_fives_is_five_and_all_ones_is_one():
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, 5)) == 5.0
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, 1)) == 1.0


def test_a_real_verdict_scores_as_hand_computed():
    scores = {
        "differentiation": 4, "market_unmet_need": 4, "team": 4, "external_signals": 1,
        "ip_fto": 2, "platform": 3, "dev_regulatory_feasibility": 3,
        "workplan_capital_efficiency": 3, "exit_thesis": 2,
    }
    # 80 + 60 + 60 + 15 + 20 + 24 + 21 + 15 + 10 = 305 / 100
    assert weighted_score(scores) == 3.05


def test_missing_and_unscorable_dimensions_count_as_zero():
    assert weighted_score({"differentiation": 5}) == 1.0
    assert weighted_score({"differentiation": "high"}) == 0.0
    assert weighted_score({}) == 0.0
    assert weighted_score(None) == 0.0


def test_out_of_range_scores_are_clamped_to_one_through_five():
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, 9)) == 5.0
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, -3)) == 1.0


@pytest.mark.parametrize("score,expected", [
    (4.0, "advance"), (4.7, "advance"),
    (3.9, "conditional"), (3.0, "conditional"),
    (2.99, "pass"), (0.0, "pass"),
])
def test_banding_matches_the_pdf(score, expected):
    assert band(score) == expected


def test_non_finite_scores_count_as_zero_not_a_perfect_five():
    nan = float("nan")
    inf = float("inf")
    ninf = float("-inf")
    # All nine dimensions unscorable via NaN/inf/-inf: must be the "no usable
    # scores" 0.0, never a clamped 5.0 (inf) or 1.0 (-inf), and never NaN's
    # perfect 5.0 that min()/max()'s NaN-is-always-False comparisons produce.
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, nan)) == 0.0
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, inf)) == 0.0
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, ninf)) == 0.0
    # A single non-finite dimension among otherwise-perfect scores must still
    # drag the total down by its full weight, not be dropped from the
    # denominator or clamped into range: (100 - 5) * 5 / 100 = 4.75.
    scores_with_nan = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_nan["exit_thesis"] = nan
    assert weighted_score(scores_with_nan) == 4.75
    scores_with_inf = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_inf["exit_thesis"] = inf
    assert weighted_score(scores_with_inf) == 4.75
    scores_with_ninf = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_ninf["exit_thesis"] = ninf
    assert weighted_score(scores_with_ninf) == 4.75


def test_display_rounding_cannot_flip_the_band_across_a_threshold():
    # Eight dimensions at 4, exit_thesis (weight 5) at 3.9: true mean is
    # exactly 3.995, which is < 4.0 and must band as "conditional". Naive
    # round(3.995, 2) == 4.0, which would wrongly band as "advance" — a
    # proposal promoted by a rounding artefact instead of its true score.
    scores = dict.fromkeys(RUBRIC_WEIGHTS, 4)
    scores["exit_thesis"] = 3.9
    score = weighted_score(scores)
    assert score == 3.99
    assert band(score) == "conditional"


def test_bool_dimension_values_count_as_zero_not_as_one_or_zero():
    # isinstance(True, int) is True in Python, so without an explicit bool
    # guard a True score would be silently accepted as 1 (and False as 0,
    # itself a valid-looking but wrong score) instead of being treated as the
    # unscorable, non-numeric value it actually is.
    assert weighted_score({"differentiation": True}) == 0.0
    assert weighted_score({"differentiation": False}) == 0.0
    scores_with_true = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_true["exit_thesis"] = True
    assert weighted_score(scores_with_true) == 4.75
    scores_with_false = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_false["exit_thesis"] = False
    assert weighted_score(scores_with_false) == 4.75

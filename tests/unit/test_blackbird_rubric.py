import pytest

from src.services.blackbird_rubric import RUBRIC_WEIGHTS, band, weighted_score


def test_weights_are_the_thirteen_dimensions_and_sum_to_one_hundred():
    assert RUBRIC_WEIGHTS == {
        "differentiation": 15,
        "mechanism_validation": 12,
        "market_unmet_need": 12,
        "experimental_rigor": 10,
        "toxicity_selectivity": 10,
        "team": 10,
        "chemistry_dc_path": 8,
        "external_signals": 8,
        "ip_fto": 6,
        "platform": 4,
        "dev_regulatory_feasibility": 3,
        "workplan_capital_efficiency": 1,
        "exit_thesis": 1,
    }
    assert sum(RUBRIC_WEIGHTS.values()) == 100


def test_science_carries_forty_percent():
    """BBL rejects on science. Counted over the 15 documented rejections:
    mechanism 5, toxicity 4, chemistry-to-DC 4. Before this split, a purely
    scientific objection could move at most 7 points."""
    science = {
        "mechanism_validation", "experimental_rigor",
        "toxicity_selectivity", "chemistry_dc_path",
    }
    assert sum(RUBRIC_WEIGHTS[k] for k in science) == 40


def test_all_fives_is_five_and_all_ones_is_one():
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, 5)) == 5.0
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, 1)) == 1.0


def test_a_real_verdict_scores_as_hand_computed():
    scores = {
        "differentiation": 4, "market_unmet_need": 4, "team": 4, "external_signals": 1,
        "ip_fto": 2, "platform": 3, "dev_regulatory_feasibility": 3,
        "workplan_capital_efficiency": 3, "exit_thesis": 2,
        "mechanism_validation": 4, "toxicity_selectivity": 3, "experimental_rigor": 4,
        "chemistry_dc_path": 2,
    }
    # 60 + 48 + 40 + 8 + 12 + 12 + 9 + 3 + 2 + 48 + 30 + 40 + 16 = 328 / 100
    assert weighted_score(scores) == 3.28


def test_missing_and_unscorable_dimensions_count_as_zero():
    # differentiation's weight is 15 of 100: a lone perfect score on it alone
    # is 15 * 5 / 100 = 0.75, not a full 1.0 — no single dimension can carry
    # the whole scale by itself.
    assert weighted_score({"differentiation": 5}) == 0.75
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
    # All thirteen dimensions unscorable via NaN/inf/-inf: must be the "no
    # usable scores" 0.0, never a clamped 5.0 (inf) or 1.0 (-inf), and never
    # NaN's perfect 5.0 that min()/max()'s NaN-is-always-False comparisons
    # produce.
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, nan)) == 0.0
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, inf)) == 0.0
    assert weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, ninf)) == 0.0
    # A single non-finite dimension among otherwise-perfect scores must still
    # drag the total down by its full weight, not be dropped from the
    # denominator or clamped into range: (100 - 1) * 5 / 100 = 4.95.
    scores_with_nan = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_nan["exit_thesis"] = nan
    assert weighted_score(scores_with_nan) == 4.95
    scores_with_inf = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_inf["exit_thesis"] = inf
    assert weighted_score(scores_with_inf) == 4.95
    scores_with_ninf = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_ninf["exit_thesis"] = ninf
    assert weighted_score(scores_with_ninf) == 4.95


def test_display_rounding_cannot_flip_the_band_across_a_threshold():
    # Twelve dimensions at 4, exit_thesis (weight 1) at 3.9: true mean is
    # exactly 3.999, which is < 4.0 and must band as "conditional". Naive
    # round(3.999, 2) == 4.0, which would wrongly band as "advance" — a
    # proposal promoted by a rounding artefact instead of its true score.
    scores = dict.fromkeys(RUBRIC_WEIGHTS, 4)
    scores["exit_thesis"] = 3.9
    score = weighted_score(scores)
    assert score == 3.99
    assert band(score) == "conditional"


def test_case_and_whitespace_variant_keys_still_match_their_dimension():
    # Finding A5: a key differing only in case (or padded with stray
    # whitespace) must still hit its rubric weight, not be silently treated
    # as missing (and thus scored 0) purely because of spelling.
    scores = {
        "Differentiation": 4, "market_unmet_need": 4, "TEAM": 4,
        " external_signals ": 1, "Ip_Fto": 2, "platform": 3,
        "dev_regulatory_feasibility": 3, "workplan_capital_efficiency": 3,
        "exit_thesis": 2, "Mechanism_Validation": 4, " toxicity_selectivity ": 3,
        "EXPERIMENTAL_RIGOR": 4, "Chemistry_Dc_Path": 2,
    }
    # Same hand-computed total as test_a_real_verdict_scores_as_hand_computed.
    assert weighted_score(scores) == 3.28


def test_unrecognized_key_is_logged_and_still_scores_as_zero(caplog):
    # A typo'd or made-up key must count as 0 (it can never match a rubric
    # dimension) but should be diagnosable — not just a mysteriously low
    # score with nothing pointing at why.
    scores = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores["differentiaton"] = 1  # typo — shadows nothing, "differentiation" is still 5
    score = weighted_score(scores)
    assert score == 5.0  # the typo'd key never touched a real dimension
    assert "differentiaton" in caplog.text
    assert "not in the thirteen" in caplog.text


def test_out_of_scale_scores_are_logged_when_clamped(caplog):
    # An explicit 0 is out of contract (the scale is 1-5) and clamps UP to 1,
    # so the stored score no longer equals the score that was counted. Run
    # ee419dd3 stored three such 0s and nothing said so — the clamp must be
    # diagnosable, same policy as the unrecognized-key warning above.
    scores = dict.fromkeys(RUBRIC_WEIGHTS, 3)
    scores["chemistry_dc_path"] = 0
    weighted_score(scores)
    assert "chemistry_dc_path" in caplog.text
    assert "clamped" in caplog.text


def test_in_range_and_non_numeric_scores_do_not_log_a_clamp(caplog):
    # In-range values need no diagnosis, and a non-numeric value takes the
    # unscorable path (counts as 0 without ever reaching the clamp) — warning
    # "clamped" there would misdescribe what happened.
    weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, 3))
    weighted_score({"differentiation": "high"})
    weighted_score(dict.fromkeys(RUBRIC_WEIGHTS, float("nan")))
    assert "clamped" not in caplog.text


def test_bool_dimension_values_count_as_zero_not_as_one_or_zero():
    # isinstance(True, int) is True in Python, so without an explicit bool
    # guard a True score would be silently accepted as 1 (and False as 0,
    # itself a valid-looking but wrong score) instead of being treated as the
    # unscorable, non-numeric value it actually is.
    assert weighted_score({"differentiation": True}) == 0.0
    assert weighted_score({"differentiation": False}) == 0.0
    scores_with_true = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_true["exit_thesis"] = True
    assert weighted_score(scores_with_true) == 4.95
    scores_with_false = dict.fromkeys(RUBRIC_WEIGHTS, 5)
    scores_with_false["exit_thesis"] = False
    assert weighted_score(scores_with_false) == 4.95

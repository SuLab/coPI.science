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

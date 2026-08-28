"""Construct sensitivity and invariance — the pair from arXiv:2608.24419.

They are INDEPENDENT quantities and no scalar summarises both, which is why
these are two functions returning two ratios rather than one score.
"""
import pytest

from src.agent.specialists import construct_sensitivity, invariance


def test_sensitivity_counts_domains_whose_verdict_moved_between_tiers():
    """R = P(verdict changed | the input's real quality changed)."""
    obs = {
        ("WEAK", "legal"): "caution", ("STRONG", "legal"): "caution",
        ("WEAK", "budget"): "blocking", ("STRONG", "budget"): "clear",
    }
    assert construct_sensitivity(obs) == (1, 2)


def test_sensitivity_is_zero_when_nothing_moves():
    obs = {
        ("WEAK", "legal"): "caution", ("STRONG", "legal"): "caution",
    }
    assert construct_sensitivity(obs) == (0, 1)


def test_invariance_counts_domains_that_held_under_a_wording_change():
    """S = P(verdict unchanged | an edit that does not change the construct)."""
    obs = {
        ("PROD", "legal"): "caution", ("NEUTRAL", "legal"): "caution",
        ("PROD", "scientific"): "clear", ("NEUTRAL", "scientific"): "caution",
    }
    assert invariance(obs) == (1, 2)


def test_both_ignore_domains_present_in_only_one_condition():
    """A domain with no pair cannot be compared and must not silently count as
    agreement — that would inflate S and deflate R."""
    obs = {("WEAK", "legal"): "caution", ("STRONG", "budget"): "clear"}
    assert construct_sensitivity(obs) == (0, 0)
    assert invariance(obs) == (0, 0)


@pytest.mark.parametrize("fn", [construct_sensitivity, invariance])
def test_neither_divides_by_zero_on_empty_input(fn):
    assert fn({}) == (0, 0)


@pytest.mark.parametrize("fn", [construct_sensitivity, invariance])
def test_three_conditions_for_one_domain_raise_rather_than_measuring_nothing(fn):
    """A domain under THREE conditions has no pair to pick, and the old
    behaviour was to drop it through the same branch that drops a domain seen
    ONCE. So a caller handing in all three quality tiers at once — the obvious
    mistake — got `(0, 0)` back and the report printed `n/a`: a total
    measurement loss dressed as a formatting quirk. Loud instead.
    """
    obs = {
        ("WEAK", "legal"): "blocking",
        ("MEDIUM", "legal"): "gap",
        ("STRONG", "legal"): "adequate",
    }
    with pytest.raises(ValueError, match="at most two conditions per domain"):
        fn(obs)


@pytest.mark.parametrize("fn", [construct_sensitivity, invariance])
def test_one_over_full_domain_does_not_take_the_comparable_ones_with_it(fn):
    """The raise is about the CALL, not about salvage: it fires even when other
    domains in the same observation set are perfectly comparable, because a
    partial answer computed over a filtered subset the caller did not choose is
    worse than an error."""
    obs = {
        ("WEAK", "legal"): "blocking",
        ("MEDIUM", "legal"): "gap",
        ("STRONG", "legal"): "adequate",
        ("WEAK", "budget"): "blocking",
        ("STRONG", "budget"): "adequate",
    }
    with pytest.raises(ValueError, match="'legal' was observed under 3"):
        fn(obs)

# tests/unit/test_calibration_ladder_fixtures.py
"""The ladder's FIXTURES are testable without spending an API call.

The harness itself makes real Opus calls and is deliberately not in ci.sh; what
is pinned here is that its grid is complete and that every persona is exercised
— a ladder missing a domain would silently exempt exactly the domain most
likely to be broken.
"""
from scripts.panel_calibration_ladder import CONTEXTS, FRAMINGS, build_cells
from src.agent.specialists import SPECIALIST_DOMAINS


def test_every_specialist_domain_is_exercised():
    domains = {domain for _, _, domain in build_cells()}
    assert domains == set(SPECIALIST_DOMAINS)


def test_the_grid_is_the_full_cross_product():
    assert len(build_cells()) == len(CONTEXTS) * len(FRAMINGS) * len(SPECIALIST_DOMAINS)


def test_every_framing_supplies_a_question_for_every_domain():
    """A missing question would be silently skipped, which is how a domain
    disappears from a ladder run without anyone noticing."""
    for name, questions in FRAMINGS.items():
        missing = set(SPECIALIST_DOMAINS) - set(questions)
        assert not missing, f"framing {name!r} has no question for {sorted(missing)}"


def test_tiers_are_ordered_weak_to_strong():
    """`construct_sensitivity` compares ADJACENT tiers, so order is load-bearing."""
    assert list(CONTEXTS) == ["WEAK", "MEDIUM", "STRONG"]

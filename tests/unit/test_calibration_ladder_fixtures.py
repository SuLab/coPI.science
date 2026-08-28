# tests/unit/test_calibration_ladder_fixtures.py
"""The ladder's FIXTURES are testable without spending an API call.

The harness itself makes real Opus calls and is deliberately not in ci.sh; what
is pinned here is that its grid is complete and that every persona is exercised
— a ladder missing a domain would silently exempt exactly the domain most
likely to be broken.
"""
from scripts.panel_calibration_ladder import CONTEXTS, FRAMINGS, build_cells, report
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


def _r_and_s_lines(text: str) -> list[str]:
    """The `  R ...` / `  S ...` lines `report()` prints, stripped of leading
    whitespace so two runs are comparable regardless of surrounding output."""
    return [
        line.strip() for line in text.splitlines()
        if line.strip().startswith(("R ", "S "))
    ]


def test_error_and_unparsed_cells_are_excluded_from_r_and_s(capsys):
    """An ERROR/UNPARSED sentinel carries no real opinion. Counting it as a
    signal change (against a real signal, it always differs) inflates R;
    counting it as agreement (it can never equal a real signal) deflates S.
    Either way the computed R/S must come out IDENTICAL to a run where that
    cell was never recorded at all."""
    base = [
        {"tier": "WEAK", "framing": "PROD", "domain": "scientific", "signal": "caution"},
        {"tier": "MEDIUM", "framing": "PROD", "domain": "scientific", "signal": "blocking"},
        {"tier": "WEAK", "framing": "PROD", "domain": "chemistry", "signal": "clear"},
        {"tier": "MEDIUM", "framing": "PROD", "domain": "chemistry", "signal": "clear"},
    ]
    with_sentinels = base + [
        {"tier": "STRONG", "framing": "PROD", "domain": "scientific",
         "signal": "ERROR:TimeoutError", "detail": "boom"},
        {"tier": "STRONG", "framing": "PROD", "domain": "chemistry",
         "signal": "UNPARSED", "raw": "no signal: line in this reply"},
    ]

    report(with_sentinels)
    with_sentinels_lines = _r_and_s_lines(capsys.readouterr().out)

    report(base)
    base_lines = _r_and_s_lines(capsys.readouterr().out)

    assert with_sentinels_lines == base_lines


def test_excluded_cells_are_reported(capsys):
    """A metric quietly computed over fewer cells than the grid is its own
    trap -- the drop must be visible in the printed report, not just correct
    in the arithmetic."""
    results = [
        {"tier": "WEAK", "framing": "PROD", "domain": "scientific", "signal": "caution"},
        {"tier": "MEDIUM", "framing": "PROD", "domain": "scientific",
         "signal": "ERROR:TimeoutError", "detail": "boom"},
    ]
    report(results)
    out = capsys.readouterr().out
    assert "excluded" in out.lower()
    assert "1 of 2" in out

    # A run with no sentinels at all must NOT claim anything was excluded.
    report([results[0]])
    clean_out = capsys.readouterr().out
    assert "excluded" not in clean_out.lower()

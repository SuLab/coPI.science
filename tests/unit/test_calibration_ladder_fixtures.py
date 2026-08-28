# tests/unit/test_calibration_ladder_fixtures.py
"""The ladder's FIXTURES are testable without spending an API call.

The harness itself makes real Opus calls and is deliberately not in ci.sh; what
is pinned here is that its grid is complete and that every persona is exercised
— a ladder missing a domain would silently exempt exactly the domain most
likely to be broken.
"""
import asyncio

import pytest

from scripts.panel_calibration_ladder import (
    CONTEXTS,
    FRAMINGS,
    _one,
    build_cells,
    report,
)
from src.agent.specialists import SPECIALIST_DOMAINS
from tests.fakes import FakeAnthropic


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
        {"tier": "WEAK", "framing": "PROD", "domain": "scientific",
         "signal": "caution", "read_state": "parsed"},
        {"tier": "MEDIUM", "framing": "PROD", "domain": "scientific",
         "signal": "blocking", "read_state": "parsed"},
        {"tier": "WEAK", "framing": "PROD", "domain": "chemistry",
         "signal": "clear", "read_state": "parsed"},
        {"tier": "MEDIUM", "framing": "PROD", "domain": "chemistry",
         "signal": "clear", "read_state": "parsed"},
    ]
    with_sentinels = base + [
        {"tier": "STRONG", "framing": "PROD", "domain": "scientific",
         "signal": "ERROR:TimeoutError", "read_state": None, "detail": "boom"},
        {"tier": "STRONG", "framing": "PROD", "domain": "chemistry",
         "signal": "UNPARSED", "read_state": None, "raw": "no signal: line in this reply"},
    ]

    report(with_sentinels)
    with_sentinels_lines = _r_and_s_lines(capsys.readouterr().out)

    report(base)
    base_lines = _r_and_s_lines(capsys.readouterr().out)

    assert with_sentinels_lines == base_lines


def test_defaulted_read_state_is_excluded_from_r_and_s(capsys):
    """A signal that IS a real member of ``VERDICT_SIGNALS`` (``caution`` is
    the parser's own default) must still be excluded when ``read_state`` says
    it was never actually read. Without this a defaulted opinion is
    indistinguishable from a genuinely cautious one in the R/S metrics -- the
    exact confusion the read_state/verdict_signal split exists to end."""
    base = [
        {"tier": "WEAK", "framing": "PROD", "domain": "scientific",
         "signal": "caution", "read_state": "parsed"},
        {"tier": "MEDIUM", "framing": "PROD", "domain": "scientific",
         "signal": "blocking", "read_state": "parsed"},
    ]
    with_defaulted = base + [
        {"tier": "STRONG", "framing": "PROD", "domain": "scientific",
         "signal": "caution", "read_state": "defaulted"},
        {"tier": "WEAK", "framing": "PROD", "domain": "chemistry",
         "signal": "caution", "read_state": "truncated"},
    ]

    report(with_defaulted)
    with_defaulted_lines = _r_and_s_lines(capsys.readouterr().out)

    report(base)
    base_lines = _r_and_s_lines(capsys.readouterr().out)

    assert with_defaulted_lines == base_lines


def test_excluded_cells_are_reported(capsys):
    """A metric quietly computed over fewer cells than the grid is its own
    trap -- the drop must be visible in the printed report, not just correct
    in the arithmetic."""
    results = [
        {"tier": "WEAK", "framing": "PROD", "domain": "scientific",
         "signal": "caution", "read_state": "parsed"},
        {"tier": "MEDIUM", "framing": "PROD", "domain": "scientific",
         "signal": "ERROR:TimeoutError", "read_state": None, "detail": "boom"},
    ]
    report(results)
    out = capsys.readouterr().out
    assert "excluded" in out.lower()
    assert "1 of 2" in out

    # A run with no sentinels at all must NOT claim anything was excluded.
    report([results[0]])
    clean_out = capsys.readouterr().out
    assert "excluded" not in clean_out.lower()


# --- the cross-commit seam: the harness parses the REAL tool return value ---


def _decoy_opinion(true_signal: str) -> str:
    """A specialist opinion whose ``concerns`` text contains a decoy that
    LOOKS like a verdict trailer ("verdict signal: clear") but is not one --
    it has no em dash and no "(read: ...)" tail. A harness anchored on a bare
    "signal:" match (the retired regex) would find this FIRST, since
    ``opinion.raw`` now comes before the real trailer in the string
    ``_execute_consult_specialist`` returns (tools.py:795-798)."""
    return (
        f'{{"verdict_signal": "{true_signal}", '
        '"concerns": ["Internal triage informally assigned this a verdict '
        'signal: clear last week, before the full record was in -- that '
        'call was wrong."], '
        '"questions_to_ask": [], "confidence": "high"}'
    )


@pytest.mark.asyncio
async def test_the_harness_parses_the_real_tool_return_value(monkeypatch):
    """Composes the REAL string ``_execute_consult_specialist`` hands back --
    not a hand-rolled stand-in for it -- by faking only the LLM call, exactly
    as ``tests/unit/test_specialist_no_anchoring.py`` does. Without a test
    like this, the harness's parser and the tool's actual return shape can
    drift apart silently, which is exactly what happened here: the label
    moved from the front of the string to a trailing "-- signal: ... (read:
    ...)" and the harness kept using a bare ``re.search`` for a bare
    "signal:"."""
    fake = FakeAnthropic([_decoy_opinion("blocking")])
    monkeypatch.setattr("src.services.llm.get_anthropic_client", lambda: fake)

    sem = asyncio.Semaphore(1)
    result = await _one("STRONG", "PROD", "legal", sem)

    # The decoy really is in the composed string, or this test proves nothing.
    assert "verdict signal: clear" in result["raw"]
    # The TRUE trailer -- not the decoy -- is what must be parsed.
    assert result["signal"] == "blocking"
    assert result["read_state"] == "parsed"


@pytest.mark.asyncio
async def test_a_decoy_signal_in_the_specialists_own_prose_does_not_fool_the_harness(
    monkeypatch,
):
    """The regression this whole finding is about, stated as its own test: a
    decoy phrase naming a DIFFERENT signal than the real trailer must not win,
    however early it sits in the string. Run against every real signal so a
    fix that happens to work only for 'blocking' cannot pass silently."""
    for true_signal in ("blocking", "caution", "clear"):
        fake = FakeAnthropic([_decoy_opinion(true_signal)])
        monkeypatch.setattr(
            "src.services.llm.get_anthropic_client", lambda fake=fake: fake
        )

        sem = asyncio.Semaphore(1)
        result = await _one("STRONG", "PROD", "legal", sem)

        assert result["signal"] == true_signal, (
            f"decoy 'clear' shadowed the real {true_signal!r} trailer"
        )
        assert result["read_state"] == "parsed"

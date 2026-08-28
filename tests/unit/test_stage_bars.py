"""Stage bars are EXTRACTED from the rubric, not authored.

Each bar records the `source` clause it condenses so a reviewer can check the
condensation against the original, and so this test can assert the source
exists. Before this existed, every persona judged against a standard the
document explicitly disclaims — `legal` most sharply, which says three separate
times that unresolved FTO is the normal starting condition and not a
disqualifier, and which has never cleared once in 91 consults.
"""
from pathlib import Path

import pytest

from src.agent.specialists import SPECIALIST_DOMAINS
from src.services.blackbird_rubric import (
    RubricError,
    load_rubric,
    parse_rubric,
    render_stage_bar_markdown,
)


def test_every_specialist_domain_has_a_stage_bar():
    missing = set(SPECIALIST_DOMAINS) - set(load_rubric().stage_bars)
    assert not missing, f"no stage bar for {sorted(missing)}"


def test_no_stage_bar_names_a_domain_that_does_not_exist():
    extra = set(load_rubric().stage_bars) - set(SPECIALIST_DOMAINS)
    assert not extra, f"stage bar for unknown domain {sorted(extra)}"


def _mutated_rubric(tmp_path, old: str, new: str, name: str = "mutated.toml"):
    """The real document with one line rewritten, written to ``tmp_path``.

    Every negative test here mutates the REAL rubric rather than building a
    minimal one: a hand-built document would have to satisfy the whole
    validator (six dimensions, weights summing to 100, every prose section
    present) to reach the check under test, and any drift in those unrelated
    requirements would then look like the failure this file is about.

    A MUTATION rather than an APPENDED second table, which is how the task
    brief originally had it: the document now defines every `[stage_bar.*]`
    table, so appending another is duplicate-key TOML, and tomllib's own error
    message happens to contain the string "stage_bar" — the brief's
    `match="stage_bar"` would have passed with no validator present at all.
    """
    text = Path("prompts/rubric/blackbird-rubric.toml").read_text(encoding="utf-8")
    assert old in text, f"mutation anchor not found: {old!r}"
    path = tmp_path / name
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return path


@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_the_validator_rejects_an_unknown_source_for_any_domains_bar(
    domain, tmp_path,
):
    """`source` is what makes the condensation auditable, and the VALIDATOR is
    what keeps it honest — so this exercises `parse_rubric` on a mutated
    document rather than asserting over the live one.

    It used to do the latter: read `load_rubric().stage_bars[domain].source` and
    assert each name was valid. That test could no longer FAIL. The identical
    check now runs at import time (`blackbird_rubric.py`'s `valid_sources`
    loop), so a bad `source` raises while this module is being imported and the
    whole file errors at COLLECTION — never at the assert, and never naming the
    domain. Third brief-supplied negative test in this plan to pass or fail for
    the wrong reason; the shape that works is to drive the validator directly.
    """
    bar = load_rubric().stage_bars[domain]
    path = _mutated_rubric(
        tmp_path,
        f'source = "{bar.source}"\ntext = "{bar.text}"',
        f'source = "no_such_dimension"\ntext = "{bar.text}"',
        name=f"{domain}.toml",
    )
    with pytest.raises(
        RubricError, match=rf"stage_bar\.{domain} names unknown source"
    ):
        parse_rubric(path)


def test_the_live_documents_bar_sources_all_name_something_real(tmp_path):
    """The positive half of the above, kept as a statement of the invariant
    even though the import-time validator is what enforces it. Deliberately
    NOT the enforcement: if this is the only test and the validator is
    deleted, nothing fails until someone edits a `source`.
    """
    r = load_rubric()
    valid = (
        {d.key for d in r.dimensions}
        | set(r.gating)
        | {"red_flags", "scoring_preamble"}
    )
    for domain, bar in r.stage_bars.items():
        for named in bar.source.split(","):
            assert named.strip() in valid, f"{domain}: unknown source {named!r}"
    for named in r.stage_bar_global.source.split(","):
        assert named.strip() in valid, f"global bar: unknown source {named!r}"


@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_the_rendered_bar_carries_the_global_incubation_sentence(domain):
    """The one-sentence global bar goes to all eight, unchanged: 'never the
    replicated data, filed IP, or identified syndicate a later-stage deal would
    show.'"""
    out = render_stage_bar_markdown(domain)
    assert "incubation grain" in out
    assert load_rubric().stage_bars[domain].text in out


def test_the_global_bar_is_the_scoring_preambles_first_sentence_and_nothing_more():
    """`[stage_bar_global]` is an EXTRACTION, and its extent is the claim.

    Byte-for-byte the preamble's first sentence: verbatim, so a reviewer
    editing `[scoring].preamble` can see immediately that the specialists'
    global bar has to move with it, and STOPPING THERE, so the four
    hub-facing paragraphs that follow it cannot arrive at a specialist by
    accident. Whitespace is normalised on the preamble side only because the
    TOML wraps it across lines; nothing else is allowed to differ.
    """
    r = load_rubric()
    preamble = " ".join(r.scoring_preamble.split())
    first_sentence = preamble.split(". ")[0] + "."
    assert r.stage_bar_global.text == first_sentence, (
        "the global stage bar is no longer the scoring preamble's first "
        "sentence — either the preamble was reworded without moving the bar, "
        "or the bar has grown past the one sentence it is allowed to carry"
    )


#: Strings that belong to the HUB's scoring machinery and must never reach a
#: specialist. Each is drawn from a paragraph of `[scoring].preamble` AFTER the
#: first sentence — the part `render_stage_bar_markdown` deliberately does not
#: inject.
_HUB_ONLY_MACHINERY = (
    ("rationale", "`rationale` is a field on the HUB's assessment sidecar; a "
                  "specialist has no schema slot for it and asking for one "
                  "invites prose the consult contract cannot carry"),
    ("35%", "the science/commercial weight split is how the HUB combines six "
            "dimensions into one score"),
    ("65%", "the science/commercial weight split is how the HUB combines six "
            "dimensions into one score"),
    ("score market size", "a specialist judges its own domain against a bar; "
                          "it does not score rubric dimensions"),
    ("every dimension applies to every proposal", "dimension coverage is the "
                                                  "HUB's problem, not a "
                                                  "specialist's"),
)


@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_no_rendered_bar_carries_the_hubs_scoring_machinery(domain):
    """The narrowing in `render_stage_bar_markdown` is a decision, so it gets a
    test.

    The function injects `[stage_bar_global].text` — one extracted sentence —
    and NOT the whole four-paragraph `[scoring].preamble`. Reverting that
    narrowing (a one-token change: `_RUBRIC.stage_bar_global.text` back to
    `_RUBRIC.scoring_preamble`) passed the entire suite when it was measured,
    which is the wrong way round for a change that decides what eight LLM
    personas read.

    It matters beyond tidiness. A specialist is asked ONE question about ONE
    domain and answers in a fixed four-field schema; the rest of that preamble
    instructs its reader to write `rationale`, to score six dimensions, and it
    discloses the 35/65 weighting. Handing that to a specialist (a) anchors it
    on the hub's own aggregation rather than on its domain, (b) asks for output
    the contract has nowhere to put — `parse_opinion` reads four keys and
    defaults the signal for anything else, and a defaulted signal is an unread
    consult — and (c) leaks the internal weightings into eight more prompts
    than need them. The specialist's job is the bar, not the arithmetic.
    """
    out = render_stage_bar_markdown(domain).lower()
    for needle, why in _HUB_ONLY_MACHINERY:
        assert needle.lower() not in out, (
            f"{domain}'s rendered stage bar carries {needle!r}, which belongs "
            f"to the hub's scoring machinery and not to a specialist: {why}. "
            f"Only `[stage_bar_global].text` plus this domain's own bar may be "
            f"injected — check render_stage_bar_markdown has not been widened "
            f"back to the whole `[scoring].preamble`."
        )


def test_render_raises_rather_than_rendering_a_bar_less_section():
    """A silently bar-less persona is the defect this whole change exists to
    end, so an unknown domain is loud. The alternative — returning "" — would
    reintroduce it for any domain someone forgot to add a bar for."""
    with pytest.raises(RubricError, match="no stage_bar for domain"):
        render_stage_bar_markdown("no_such_domain")

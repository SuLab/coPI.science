"""The eight personas share one answer contract. These pin the two properties
that make the contract work, across all eight files at once — a per-file edit
that misses one is the failure mode this catches.
"""
import re

import pytest

from src.agent.specialists import SPECIALIST_DOMAINS, persona_path


@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_the_verdict_field_comes_first(domain):
    """``verdict_signal`` is the FIRST key — a measured decision that REVERSED
    this plan's original design. Do not "fix" it back.

    The contract shipped on 2026-08-28 with the verdict LAST, on an
    evidence-before-rating rationale. Two things then undermined that. The
    warrant was misapplied: the "+6 to +11 accuracy points" figure is k=6
    ensembling on PAIRWISE judging, not a k=1 reorder of one key in one schema,
    and the same review grades the opposite (chain-of-thought narrowing the
    criterion) as STRONG. And a seven-run, 336-consult quality-ladder series
    measured the reorder doing real harm:

        verdict LAST   pooled R 0.281-0.312, top label 0-10 of 48
        verdict FIRST  pooled R 0.594,       top label 20 of 48
        (baseline before any change: 0.625, 7 of 48)

    With the verdict first, all eight domains can reach the top label; with it
    last, three to five could, and for two runs none could at all.

    Mechanism, as far as it is understood: ``concerns`` is a REQUIRED
    negative-valence array, so a verdict written after it is chosen with a
    freshly-authored list of problems adjacent in context, and self-consistency
    pressure compresses the scale toward the middle. Ordering the rating after
    the evidence ordered it after NEGATIVE evidence.

    Evidence: docs/audits/2026-08-27-consult-persona-calibration/
    05-isolation-series-design.md (pre-registered before the arms were run).
    Restoring the old order needs a ladder run that beats the numbers above.
    """
    text = persona_path(domain).read_text(encoding="utf-8")
    assert text.index('"verdict_signal"') < text.index('"established"')
    assert text.index('"verdict_signal"') < text.index('"concerns"')
    assert text.index('"verdict_signal"') < text.index('"questions_to_ask"')


@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_every_persona_asks_for_positive_evidence(domain):
    text = persona_path(domain).read_text(encoding="utf-8")
    assert '"established"' in text, (
        "without a positive field, specialists file positives inside `concerns`"
    )


@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_no_persona_declares_a_label_too_long_for_the_column(domain):
    """`verdict_signal` is String(10). A label that does not fit is a silent
    truncation or a write failure at runtime."""
    text = persona_path(domain).read_text(encoding="utf-8")
    m = re.search(r'"verdict_signal":\s*"([^"]+)"', text)
    assert m
    for label in (part.strip() for part in m.group(1).split("|")):
        assert len(label) <= 10, f"{label!r} exceeds String(10)"

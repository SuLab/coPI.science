"""The eight personas share one answer contract. These pin the two properties
that make the contract work, across all eight files at once — a per-file edit
that misses one is the failure mode this catches.
"""
import re

import pytest

from src.agent.specialists import SPECIALIST_DOMAINS, persona_path


@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_the_verdict_field_comes_after_the_evidence_fields(domain):
    """A model generates left to right, so a schema that names the verdict first
    commits to a label before writing any evidence. Evidence-before-rating is
    worth +6 to +11 accuracy points (arXiv:2305.17926)."""
    text = persona_path(domain).read_text(encoding="utf-8")
    assert text.index('"established"') < text.index('"verdict_signal"')
    assert text.index('"concerns"') < text.index('"verdict_signal"')
    assert text.index('"questions_to_ask"') < text.index('"verdict_signal"')


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

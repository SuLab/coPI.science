"""The headline contract's two mandatory elements (spec 2026-09-21 §2).

A prompt-TEXT assertion, deliberately: the suite drives tests/fakes.py's
FakeAnthropic and never reaches a real model, so nothing here can observe what
the hub actually writes. What it can do is stop the requirement being deleted
or softened without a decision.

Note the "two `Write:` examples" assertion. The unmodified file already had one
`Write:` and one `Not:`, so an "at least two worked examples" check would have
passed against it and detected nothing.
"""

from __future__ import annotations

import pathlib
import re

PROMPT = pathlib.Path("prompts/roles/scout_hub/phase4-thread-reply.md")


def _item_six() -> str:
    """Item 6, verbatim — for the assertions that care about layout."""
    text = PROMPT.read_text()
    start = text.index("6. **Headline.**")
    end = text.index("7. **Key points.**", start)
    return text[start:end]


def _item_six_flat() -> str:
    """Item 6 with every run of whitespace collapsed to one space, lowercased.

    The prompt is hard-wrapped at ~76 columns, so a required phrase can and
    does straddle a line break — "patient\\n      population" is the live
    case. A raw substring check against the wrapped text fails for a phrase
    that is present, which is a test defect rather than a finding about the
    prompt. Layout-sensitive assertions use `_item_six()` instead.
    """
    return re.sub(r"\s+", " ", _item_six()).lower()


def test_the_headline_item_requires_a_named_disease_area():
    item = _item_six_flat()
    assert "disease area" in item
    assert "patient population" in item


def test_the_headline_item_requires_what_the_intervention_does():
    item = _item_six_flat()
    assert "what it does" in item or "the action" in item
    assert "modality" in item, (
        "the rule must say a modality noun alone does not satisfy it"
    )


def test_the_headline_item_is_modality_neutral():
    """The corpus is mostly diagnostics and platforms, not drugs."""
    item = _item_six()
    assert not re.search(r"\bthe drug\b", item, re.I)


def test_the_headline_item_carries_two_positive_examples():
    assert _item_six().count("Write:") == 2


def test_the_headline_cap_is_unchanged():
    assert "at most 140 characters" in _item_six()


def test_the_prompt_set_version_was_bumped():
    """A prompt-set edit without a `role.toml` bump is an unrecorded edit:
    `prompt_set_stamp` hashes the set and stamps every run with version +
    hash, so a changed hash under an unchanged version is undetectable from
    the run record. `role.toml` is not embedded in the synced doc, so nothing
    else pins it."""
    toml = (PROMPT.parent / "role.toml").read_text()
    assert 'version = "1.6.0"' in toml

"""The elevator pitch's ordering contract (spec 2026-09-21 §3.2).

A prompt-TEXT assertion, deliberately: the suite drives tests/fakes.py's
FakeAnthropic and never reaches a real model, so nothing here can observe what
the hub writes. What it can do is stop the requirement being deleted or
softened without a decision.
"""

from __future__ import annotations

import pathlib
import re

PROMPT = pathlib.Path("prompts/roles/scout_hub/phase4-thread-reply.md")


def _item_eight() -> str:
    text = PROMPT.read_text()
    start = text.index("8. **Elevator pitch.**")
    return text[start:text.index("9. **Project label.**", start)]


def _flat() -> str:
    return re.sub(r"\s+", " ", _item_eight()).lower()


def test_the_pitch_opens_on_the_problem_not_the_asset():
    item = _flat()
    assert "the problem" in item
    assert "not with the asset" in item


def test_the_pitch_closes_on_what_a_read_out_would_enable():
    assert "would enable" in _flat()


def test_the_pitch_is_bounded_at_250_words():
    """Raised from "four to six sentences … at most 900 characters" on
    2026-09-28. The bound is mirrored by `_PITCH_WORD_LIMIT` on the write path,
    so the prose and the drift alarm cannot part company."""
    from src.agent.simulation import _PITCH_WORD_LIMIT

    item = _flat()
    assert "at most 250 words" in item
    assert _PITCH_WORD_LIMIT == 250
    assert "four to six sentences" not in item
    assert "900 characters" not in item


def test_the_citation_budget_bounds_where_sentence_four_ENDS():
    """_clip_at_sentence publishes only COMPLETE sentences, so a budget on
    where sentence 4 begins does not keep the citation in the public excerpt."""
    item = _flat()
    assert "end within approximately 550 characters" in item

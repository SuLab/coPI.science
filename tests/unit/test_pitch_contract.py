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


#: The 544-character opening approved for item 8's `Write:` example
#: (spec 2026-10-02 §6.2).
CANAVAN_WRITE = (
    "Canavan disease is an inherited brain disease of infancy that affects a few "
    "thousand children worldwide, who receive only supportive care because no "
    "therapy is approved. In this disease, a missing enzyme lets the brain chemical "
    "N-acetylaspartate build up to toxic levels and destroy myelin. This pill blocks "
    "NAT8L, the enzyme that makes that chemical, and unlike gene therapies in early "
    "trials it can be dosed daily and stopped. It comes from the lab's published "
    "screen of over 36,000 compounds (https://doi.org/10.1021/acsmedchemlett.5c00623)."
)
#: The real 1.9.0 opening of row 37b9c43b (slusher, 2026-09-29; spec F5),
#: item 8's `Not:` example.
CANAVAN_NOT = (
    "Canavan disease is an inherited brain disease of infancy — a few thousand "
    "children worldwide, no approved therapy, supportive care only — in which a "
    "missing enzyme lets the brain chemical N-acetylaspartate build up to toxic "
    "levels and destroy myelin."
)


def test_element_one_gives_the_patients_their_own_sentence():
    """Spec 2026-10-02 §6.2 (O3): the problem gets one or two sentences of its
    own, the population size names its measure and comes only from the record,
    and the population, its size and today's care are never an aside."""
    item = _flat()
    assert "the problem**, in one or two sentences of its own" in item
    assert (
        "say whether the number is how many people have the disease or how many "
        "are diagnosed each year"
    ) in item
    assert "give it only as the record states it" in item
    assert "never compress the population, its size or today's care into an aside" in item
    assert "elements 2 and 3 may share one sentence" in item


def test_item_eight_carries_the_canavan_not_and_write_pair():
    """`Not:` is the real 1.9.0 opening (a dash aside; that pitch's elements
    1-4 ended at 606); `Write:` is the 544-character rewrite."""
    flat = re.sub(r"\s+", " ", _item_eight())
    assert f'Not: "{CANAVAN_NOT}"' in flat
    assert f'Write: "{CANAVAN_WRITE}"' in flat
    assert len(CANAVAN_WRITE) == 544


def test_the_citation_budget_bounds_where_element_four_ENDS():
    """_clip_at_sentence publishes only COMPLETE sentences, so a budget on
    where element 4 begins does not keep the citation in the public excerpt.
    Since scout_hub 1.10.0 the budget counts elements, not sentences: element
    1 may take two sentences and elements 2 and 3 may share one."""
    item = _flat()
    assert "end within approximately 550 characters" in item
    assert "elements 1-4 together must" in item
    assert "sentences 1-4" not in item


#: Element 5 of row 37b9c43b's pitch, verbatim: app-only text after the cut.
_CANAVAN_NEXT = (
    "The supporting genetics are unusually clean: switching off the NAA-making "
    "enzyme rescues Canavan mice, so reducing the substrate is the most directly "
    "validated therapeutic hypothesis in the disease."
)
#: Elements 2-4 of the same 1.9.0 pitch, which follow CANAVAN_NOT.
_CANAVAN_1_9_0_REST = (
    " This program is a small molecule taken by mouth that blocks NAT8L, the single "
    "enzyme that makes that chemical, cutting it off at the source. Unlike the gene "
    "therapies now in early trials, it can be dosed daily and stopped. The chemical "
    "series comes from the lab's published screen of over 36,000 compounds "
    "(https://doi.org/10.1021/acsmedchemlett.5c00623)."
)
_CANAVAN_DOI = "https://doi.org/10.1021/acsmedchemlett.5c00623"


def test_the_canavan_write_opening_publishes_whole_with_its_doi():
    """Spec §8: the 544-character `Write:` opening, through the real clipper at
    PITCH_DISPLAY_CHARS, is published whole and keeps its DOI, alone or
    followed by the rest of a pitch. The control is the 1.9.0 original, whose
    elements 1-4 end at 606: the same cut stops after character 473 and loses
    the citation (spec F5)."""
    from src.services.assessment_headline import PITCH_DISPLAY_CHARS, _clip_at_sentence

    assert _clip_at_sentence(CANAVAN_WRITE, PITCH_DISPLAY_CHARS) == CANAVAN_WRITE
    excerpt = _clip_at_sentence(f"{CANAVAN_WRITE}\n\n{_CANAVAN_NEXT}", PITCH_DISPLAY_CHARS)
    assert excerpt == f"{CANAVAN_WRITE} …"
    assert _CANAVAN_DOI in excerpt

    original = f"{CANAVAN_NOT}{_CANAVAN_1_9_0_REST}"
    assert len(original) == 606
    cut = _clip_at_sentence(f"{original}\n\n{_CANAVAN_NEXT}", PITCH_DISPLAY_CHARS)
    assert _CANAVAN_DOI not in cut
    assert len(cut) == 473 + len(" …")

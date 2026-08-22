"""The tolerant JSON extractor, and the drift alarm between its two callers.

`src/services/json_extract.py` exists because two modules need the same
algorithm and one of them may not import the other: `src/agent/specialists.py`
is deliberately dependency-free on the engine (its module docstring), so it
cannot reach into `src/services/llm.py` for the extractor that module has
carried since profile synthesis was written.

The measured reason it was extracted at all: run 8b64a0e0 laundered 6 of 168
specialist consults into `caution`/`low`/no-concerns, one of them inverting a
`blocking`/`high` opinion that was then published to the PI's own thread as
`⚠️ caution`. Running `llm.py::_extract_json` verbatim over those six recovers
all 3 `end_turn` cases and none of the 3 `refusal` cases — a clean split along
"complete object plus trailing prose" vs "object cut mid-array". See
docs/audits/2026-08-22-run-8b64a0e0/rca-and-corrections.md, H5.
"""
import json

import pytest

from src.services.json_extract import extract_json

# The three shapes measured in the run, plus the two the profile-synthesis
# caller has always relied on. Kept as one table because the last test in this
# file replays it against llm.py's own copy.
_CORPUS = (
    # A bare object, the happy path.
    '{"verdict_signal": "clear", "confidence": "high"}',
    # end_turn: a complete object followed by the model's own commentary. This
    # is the shape that cost chute/scientific its `blocking` signal.
    '{"verdict_signal": "blocking", "confidence": "high"}\n\n'
    "Note: I have marked this blocking because a single-antibody ICC cannot "
    "establish nuclear translocation.",
    # end_turn: fenced, then commentary after the closing fence.
    '```json\n{"verdict_signal": "blocking", "confidence": "high"}\n```\n\n'
    "Happy to go deeper on the selectivity question if useful.",
    # A fence with no language tag.
    '```\n{"verdict_signal": "caution", "confidence": "low"}\n```',
    # Leading commentary, then the object.
    'Here is my read.\n\n{"verdict_signal": "caution", "confidence": "moderate"}',
)


@pytest.mark.parametrize("raw", _CORPUS)
def test_every_measured_recoverable_shape_yields_a_dict(raw):
    assert isinstance(extract_json(raw), dict)


def test_trailing_prose_does_not_cost_the_object():
    got = extract_json(_CORPUS[1])
    assert got == {"verdict_signal": "blocking", "confidence": "high"}


def test_a_fence_followed_by_prose_is_still_unwrapped():
    """`_strip_fence` in specialists.py anchors the closing fence at the END of
    the string, so a fence with anything after it is not a fence to it. That is
    exactly one of the six laundered consults."""
    assert extract_json(_CORPUS[2])["verdict_signal"] == "blocking"


def test_an_object_cut_mid_array_raises():
    """The `refusal` half of the split: 3 of 3 unrecoverable. There is no
    closing brace to find, and inventing one would invent content."""
    with pytest.raises(ValueError):
        extract_json('{"verdict_signal": "blocking", "concerns": ["The dose-response')


def test_no_object_at_all_raises():
    for raw in ("", "   ", "I cannot answer that.", "[1, 2, 3]", "null"):
        with pytest.raises(ValueError):
            extract_json(raw)


def test_a_body_missing_its_opening_brace_inside_a_fence_is_wrapped():
    """Claude drops the opening brace inside a fence often enough that the
    original extractor grew a branch for it. Pinned so the extraction does not
    quietly lose it."""
    assert extract_json('```json\n"verdict_signal": "clear"\n```') == {
        "verdict_signal": "clear"
    }


def test_the_error_names_what_it_could_not_parse():
    """`parse_opinion` swallows this exception, so the message is the only
    trace a reader gets. It must carry a prefix of the text."""
    with pytest.raises(ValueError, match="unmistakable-marker"):
        extract_json("unmistakable-marker and no object at all")


@pytest.mark.parametrize("raw", _CORPUS)
def test_the_shared_extractor_agrees_with_llms_own_copy(raw):
    """The drift alarm.

    `llm.py::_extract_json` is the original and still has its own callers.
    Until it is reduced to a thin delegator (it belongs to a different
    workstream's files), the duplication is real, and a divergence would mean
    a specialist opinion and a synthesized profile disagree about what the same
    bytes say. This test is what makes that impossible to ship quietly — and it
    keeps passing, unchanged, once that module delegates here.
    """
    from src.services.llm import _extract_json

    assert extract_json(raw) == _extract_json(raw)
    # And json.loads is NOT a substitute for either: 4 of the 5 shapes above
    # are exactly the ones it rejects.
    if raw != _CORPUS[0]:
        with pytest.raises(ValueError):
            json.loads(raw)

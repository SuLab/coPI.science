"""Rubric-document <-> prompts <-> specialists sync.

The rubric CONTENT lives in one place — ``prompts/rubric/blackbird-rubric.toml``,
loaded by ``src/services/blackbird_rubric.py`` — and reaches the scouting hub by
being rendered into the ``{rubric}`` placeholder of
``prompts/roles/scout_hub/agent-system.md``. Three things outside that document
still restate parts of it and can therefore drift out of sync with it silently:

* the ``<assessment_json>`` skeleton in ``prompts/roles/scout_hub/
  phase4-thread-reply.md`` — the *keys* the model is told to emit. A key the
  skeleton omits scores zero (``weighted_score``), so a dimension added to the
  document but not to the skeleton would drag every future assessment down
  invisibly; a key the skeleton invents is logged as unmatched and scored as
  unset.
* ``src/agent/specialists.py`` — each specialist's ``maps_to_dimension``, which
  is where a blocking specialist signal lands. A dimension renamed in the
  document leaves that mapping pointing at nothing.
* the prose percentages in the phase-4 prompt and in the document's own scoring
  preamble ("the four scientific dimensions are 40% ... and 34% on the
  incubation scale"), which are hand-written restatements of the weights — now
  of BOTH weight sets, so a re-cut of either one forces the prose update.

These tests are the drift alarm for all three, plus the wiring itself: that
agent-system.md carries the placeholder rather than a stale copy of the table,
and that composing the hub's system prompt actually yields the rendered rubric.
"""
import json
import re
from pathlib import Path

import pytest

from src.agent.agent import Agent
from src.agent.specialists import SPECIALIST_DOMAINS
from src.services.blackbird_rubric import (
    BANDING,
    BANDING_INCUBATION,
    RUBRIC_WEIGHTS,
    RUBRIC_WEIGHTS_INCUBATION,
    load_rubric,
    render_rubric_markdown,
)

ROOT = Path(__file__).resolve().parents[2]
HUB_PROMPTS = ROOT / "prompts/roles/scout_hub"
SYSTEM_PROMPT = HUB_PROMPTS / "agent-system.md"
PHASE4_PROMPT = HUB_PROMPTS / "phase4-thread-reply.md"

# The four scientific dimensions added by the 2026-08 weight re-cut. Named here
# rather than derived, because "which dimensions are the scientific ones" is
# exactly the fact the phase-4 prose asserts a percentage about.
SCIENCE_DIMENSIONS = (
    "mechanism_validation",
    "toxicity_selectivity",
    "experimental_rigor",
    "chemistry_dc_path",
)


def _norm(text: str) -> str:
    """Collapse line-wraps so an assertion about prose survives a reflow."""
    return " ".join(text.split())


def _phase4_text() -> str:
    return PHASE4_PROMPT.read_text(encoding="utf-8")


def _skeleton() -> dict:
    """The `<assessment_json>` skeleton, parsed. It is bare JSON (deliberately
    unfenced — a fenced block would be mistaken for the action JSON), so it
    parses directly once the tags are stripped."""
    body = _phase4_text()
    m = re.search(r"<assessment_json>\s*(\{.*?\})\s*</assessment_json>", body, re.DOTALL)
    assert m, "phase4-thread-reply.md no longer carries an <assessment_json> skeleton"
    return json.loads(m.group(1))


# ---------------------------------------------------------------------------
# The sidecar skeleton vs. the document
# ---------------------------------------------------------------------------

def test_skeleton_scores_keys_are_exactly_the_rubric_dimensions():
    """A skeleton key that is not a rubric dimension is scored as unset; a
    rubric dimension missing from the skeleton scores zero and silently drags
    weighted_score down. Both are set-equality failures here."""
    scores = _skeleton()["scores"]
    assert set(scores) == set(RUBRIC_WEIGHTS), (
        "phase4-thread-reply.md's <assessment_json> scores keys have drifted from "
        "prompts/rubric/blackbird-rubric.toml: "
        f"only in skeleton={sorted(set(scores) - set(RUBRIC_WEIGHTS))}, "
        f"only in document={sorted(set(RUBRIC_WEIGHTS) - set(scores))}"
    )


def test_skeleton_gating_keys_are_exactly_the_documents_gating_criteria():
    """The gating keys are structural — the same three in the skeleton, in the
    document, and in the opportunity_assessments.gating column."""
    assert set(_skeleton()["gating"]) == set(load_rubric().gating)


def test_phase4_states_the_dimension_count_the_document_defines():
    """"Every one of the thirteen keys is required" is a hand-written count of
    the document's dimensions. Adding or removing one must update it."""
    words = {13: "thirteen", 12: "twelve", 14: "fourteen"}
    count = len(RUBRIC_WEIGHTS)
    assert count in words, f"add the number word for {count} to this test"
    assert f"every one of the {words[count]} keys is required" in _norm(
        _phase4_text()
    ).lower(), (
        "phase4-thread-reply.md's required-key count no longer matches the "
        f"{count} dimensions in the rubric document"
    )


# ---------------------------------------------------------------------------
# specialists.py vs. the document
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_specialist_maps_to_a_real_rubric_dimension(domain):
    """A specialist's concerns land in ``maps_to_dimension``. If the document
    renames or drops that dimension, the mapping points at nothing and a
    blocking signal has nowhere to go. (None is allowed — some specialists
    inform judgement without owning a dimension.)"""
    mapped = SPECIALIST_DOMAINS[domain].maps_to_dimension
    if mapped is None:
        return
    assert mapped in RUBRIC_WEIGHTS, (
        f"specialists.py maps {domain!r} to dimension {mapped!r}, which is not in "
        "prompts/rubric/blackbird-rubric.toml"
    )


def test_document_specialist_fields_name_real_specialist_domains():
    """The reverse direction: a dimension's ``specialist`` field must name a
    domain the panel actually has."""
    for dim in load_rubric().dimensions:
        if dim.specialist is not None:
            assert dim.specialist in SPECIALIST_DOMAINS, (
                f"dimension {dim.key!r} names specialist {dim.specialist!r}, which is "
                f"not a panel domain ({sorted(SPECIALIST_DOMAINS)})"
            )


# ---------------------------------------------------------------------------
# The prose percentages vs. the weights
# ---------------------------------------------------------------------------

def test_science_weights_sum_to_forty_and_the_prose_says_so():
    """The commercial/scientific split is asserted in prose in two places — the
    phase-4 prompt and the document's own scoring preamble — and computed from
    the weights here, so a weight change forces the prose update.

    Both scales, because the claim is now stage-qualified: the share is 60/40 on
    the investment scale and 66/34 on the incubation one. A single unqualified
    "40%" would be wrong for the stage almost every real verdict is at, which is
    what this assertion exists to prevent recurring.
    """
    science_total = sum(RUBRIC_WEIGHTS[k] for k in SCIENCE_DIMENSIONS)
    commercial_total = sum(
        w for k, w in RUBRIC_WEIGHTS.items() if k not in SCIENCE_DIMENSIONS
    )
    science_incubation = sum(
        RUBRIC_WEIGHTS_INCUBATION[k] for k in SCIENCE_DIMENSIONS
    )
    commercial_incubation = sum(
        w for k, w in RUBRIC_WEIGHTS_INCUBATION.items() if k not in SCIENCE_DIMENSIONS
    )
    assert science_total == 40
    assert commercial_total == 60
    assert science_incubation == 34
    assert commercial_incubation == 66

    phase4 = _norm(_phase4_text())
    # Both numbers, derived — and the phrase that ties them to the field the
    # model actually sets, so the prose cannot state two percentages without
    # saying which one applies when.
    assert (
        f"the four scientific dimensions are {science_total}% of the total on the "
        f"investment scale (pre-seed and later) and {science_incubation}% on the "
        "incubation scale"
    ) in phase4, (
        "phase4-thread-reply.md's scientific-share claim no longer matches the "
        f"document's weights ({science_total}% investment / "
        f"{science_incubation}% incubation)"
    )
    assert "follows from the `funnel_stage` you" in phase4

    preamble = _norm(load_rubric().scoring_preamble)
    assert f"Commercial dimensions carry {commercial_total}% of the total" in preamble
    assert f"the four scientific dimensions below carry {science_total}%" in preamble
    assert (
        f"On the incubation scale that split is {commercial_incubation}% / "
        f"{science_incubation}%"
    ) in preamble


# ---------------------------------------------------------------------------
# The placeholder wiring
# ---------------------------------------------------------------------------

def test_scout_hub_system_prompt_carries_the_placeholder_not_a_copy():
    """agent-system.md must delegate to the document, not restate it — a second
    copy of the table is exactly the drift this extraction removes."""
    body = SYSTEM_PROMPT.read_text(encoding="utf-8")
    assert "{rubric}" in body, "scout_hub agent-system.md lost the {rubric} placeholder"
    assert "| 1 | Commercialization" not in body, (
        "scout_hub agent-system.md has a literal dimension table again — the rubric "
        "belongs in prompts/rubric/blackbird-rubric.toml"
    )
    assert "**Banding:**" not in body
    assert "## Blackbird's Screening Rubric" not in body


def test_composed_hub_prompt_contains_the_whole_rendered_rubric(tmp_path, monkeypatch):
    """End-to-end: the hub's real system prompt, composed the way a turn
    composes it, carries every dimension (title + weight), both band
    thresholds, and the decision heuristic.

    PROFILES_DIR is redirected at tmp_path so the assertions do not depend on
    whatever a real run last wrote into profiles/ (same reason as the
    characterization suite's _hermetic_profiles fixture)."""
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    prompt = Agent(
        agent_id="blackbird", bot_name="BlackbirdBot", pi_name="Blackbird Labs",
        role="scout_hub",
    ).build_thread_reply_system_prompt()

    assert "{rubric}" not in prompt, "the placeholder was not substituted"
    assert render_rubric_markdown() in prompt

    rubric = load_rubric()
    for i, dim in enumerate(rubric.dimensions, start=1):
        # Title, both anchor sets and both weights asserted on the SAME row: a
        # table that lists every dimension and every weight but pairs them up
        # wrongly — including pairing an investment anchor with an incubation
        # weight — would pass any set of independent substring checks.
        assert (
            f"| {i} | {dim.title} | {dim.anchors} | {dim.weight}% | "
            f"{dim.anchors_incubation} | {dim.weight_incubation}% |"
        ) in prompt, (
            f"dimension {dim.key} is missing its row (title/anchors/weights, both "
            "scales) in the composed prompt"
        )
        assert dim.weight_incubation == RUBRIC_WEIGHTS_INCUBATION[dim.key]

    # One Banding line per scale, each carrying that scale's own two thresholds
    # and NOT the other's — a single line quoting all four numbers would be
    # unreadable and unfalsifiable.
    for label, banding in (
        ("investment", BANDING), ("incubation", BANDING_INCUBATION),
    ):
        prefix = f"**Banding ({label} scale):**"
        banding_line = next(
            line for line in prompt.splitlines() if line.startswith(prefix)
        )
        for threshold in (banding["advance_min"], banding["conditional_min"]):
            assert f"{threshold:.1f}" in banding_line, (
                f"band threshold {threshold} is not stated in the prompt's "
                f"{label} Banding line"
            )
        other = BANDING_INCUBATION if banding is BANDING else BANDING
        assert f"{other['advance_min']:.1f}" not in banding_line, (
            f"the {label} Banding line also quotes the other scale's advance line"
        )

    assert _norm(rubric.heuristic) in _norm(prompt)
    assert _norm(rubric.checklist[0]) in _norm(prompt)
    assert _norm(rubric.red_flags[0]) in _norm(prompt)
    for gate in rubric.gating.values():
        assert gate["title"] in prompt


def test_pi_lab_prompt_never_carries_the_rubric(tmp_path, monkeypatch):
    """The substitution is scoped by the placeholder: the global (pi_lab)
    agent-system.md has none, so a lab agent's prompt is untouched — the
    property the pi_lab golden masters pin byte-for-byte."""
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    prompt = Agent(agent_id="su", bot_name="SuBot", pi_name="Andrew Su").build_system_prompt()
    assert "{rubric}" not in prompt
    assert "Blackbird's Screening Rubric" not in prompt
    assert "| 1 | Commercialization" not in prompt

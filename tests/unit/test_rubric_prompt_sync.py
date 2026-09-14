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
* ``src/agent/specialists.py`` — each specialist's ``maps_to_dimensions``, which
  is where a blocking specialist signal lands. A dimension renamed in the
  document leaves that mapping pointing at nothing.
* the prose percentages in the document's own scoring preamble ("carry 35% of
  the total"), which are hand-written restatements of the weights.

These tests are the drift alarm for all three, plus the wiring itself: that
agent-system.md carries the placeholder rather than a stale copy of the table,
and that composing the hub's system prompt actually yields the rendered rubric.

Since rubric 3.3.0 the eight specialist personas are on the same wiring —
``{stage_bar}``, filled by ``render_stage_bar_markdown`` in
``src/agent/tools.py`` — so a persona that loses the placeholder is a fourth way
to drift out of the document, and is checked here too. What each bar SAYS, and
that its ``source`` still names something the document contains, is
tests/unit/test_stage_bars.py.
"""
import json
import re
import tomllib
from pathlib import Path

import pytest

from src.agent.agent import Agent
from src.agent.specialists import SPECIALIST_DOMAINS, persona_path
from src.services.blackbird_rubric import (
    BANDING,
    RUBRIC_WEIGHTS,
    load_rubric,
    render_rubric_markdown,
)

ROOT = Path(__file__).resolve().parents[2]
HUB_PROMPTS = ROOT / "prompts/roles/scout_hub"
SYSTEM_PROMPT = HUB_PROMPTS / "agent-system.md"
PHASE4_PROMPT = HUB_PROMPTS / "phase4-thread-reply.md"

# The two scientific dimensions. Named here rather than derived, because
# "which dimensions are the scientific ones" is exactly the fact the scoring
# preamble asserts a percentage about.
SCIENCE_DIMENSIONS = (
    "scientific_credibility",
    "translational_path",
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
    """"Every one of the six keys is required" is a hand-written count of
    the document's dimensions. Adding or removing one must update it."""
    words = {5: "five", 6: "six", 7: "seven"}
    count = len(RUBRIC_WEIGHTS)
    assert count in words, f"add the number word for {count} to this test"
    assert f"every one of the {words[count]} keys is required" in _norm(
        _phase4_text()
    ).lower(), (
        "phase4-thread-reply.md's required-key count no longer matches the "
        f"{count} dimensions in the rubric document"
    )


def test_phase4_says_the_skeleton_zeros_are_placeholders_not_scores():
    """The skeleton pre-fills every dimension score with 0 while the scale is
    1-5, and "a key you omit scores zero" frames 0 as the no-evidence value —
    so run ee419dd3 stored three explicit 0s, which weighted_score silently
    clamped UP to 1 (a present-0 outscores an omitted key). The prompt must
    close the ambiguity it created: the 0s are placeholders, never a
    submittable score."""
    body = _norm(_phase4_text())
    assert "placeholders, not scores" in body
    assert "never submit a 0" in body


def test_phase4_states_the_single_scale_and_has_no_funnel_stage():
    """The prompt must tell the model what the scorer does — one scale, one
    evidence bar — and the funnel-stage classification removed in v3.1.0 must
    stay removed: not in the skeleton, not in the prose."""
    body = _norm(_phase4_text()).lower()
    assert "there is one scale" in body
    assert "funnel" not in body
    assert "funnel_stage" not in _skeleton()


# ---------------------------------------------------------------------------
# specialists.py vs. the document
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_specialist_maps_to_a_real_rubric_dimension(domain):
    """A specialist's concerns land in ``maps_to_dimensions``. If the document
    renames or drops one of those dimensions, the mapping points at nothing and
    a blocking signal has nowhere to go. (An EMPTY tuple is allowed — some
    specialists inform judgement without owning a dimension.)"""
    for mapped in SPECIALIST_DOMAINS[domain].maps_to_dimensions:
        assert mapped in RUBRIC_WEIGHTS, (
            f"specialists.py maps {domain!r} to dimension {mapped!r}, which is not in "
            "prompts/rubric/blackbird-rubric.toml"
        )


@pytest.mark.parametrize("domain", sorted(SPECIALIST_DOMAINS))
def test_every_persona_carries_the_stage_bar_placeholder(domain):
    """A persona without the placeholder silently keeps its bar-less behaviour —
    which is the defect this whole change exists to fix, reintroduced by a
    forgotten file."""
    assert "{stage_bar}" in persona_path(domain).read_text(encoding="utf-8")


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

def test_science_weights_sum_to_thirty_five_and_the_prose_says_so():
    """The commercial/scientific split is asserted in prose in the document's
    scoring preamble and computed from the weights here, so a weight change
    forces the prose update."""
    science_total = sum(RUBRIC_WEIGHTS[k] for k in SCIENCE_DIMENSIONS)
    commercial_total = sum(
        w for k, w in RUBRIC_WEIGHTS.items() if k not in SCIENCE_DIMENSIONS
    )
    assert science_total == 35
    assert commercial_total == 65

    preamble = _norm(load_rubric().scoring_preamble)
    assert f"carry {science_total}% of the total" in preamble
    assert f"the four commercial dimensions carry {commercial_total}%" in preamble


# ---------------------------------------------------------------------------
# The placeholder wiring
# ---------------------------------------------------------------------------

def test_scout_hub_system_prompt_carries_the_placeholder_not_a_copy():
    """agent-system.md must delegate to the document, not restate it — a second
    copy of the table is exactly the drift this extraction removes."""
    body = SYSTEM_PROMPT.read_text(encoding="utf-8")
    assert "{rubric}" in body, "scout_hub agent-system.md lost the {rubric} placeholder"
    assert "| 1 | Differentiation" not in body, (
        "scout_hub agent-system.md has a literal dimension table again — the rubric "
        "belongs in prompts/rubric/blackbird-rubric.toml"
    )
    assert "**Banding:**" not in body
    assert "## Blackbird's Screening Rubric" not in body


def test_composed_hub_prompt_contains_the_whole_rendered_rubric(tmp_path, monkeypatch):
    """End-to-end: the hub's real system prompt, composed the way a turn
    composes it, carries every dimension (title + anchor + weight on one row),
    both band thresholds, the evidence lists, and the decision heuristic.

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
        # Title, anchor and weight asserted on the SAME row: a table that lists
        # every dimension and every weight but pairs them up wrongly would pass
        # any set of independent substring checks.
        assert f"| {i} | {dim.title} | {dim.anchors} | {dim.weight}% |" in prompt, (
            f"dimension {dim.key} is missing its row (title/anchors/weight) in the "
            "composed prompt"
        )

    # The one Banding line carries both thresholds.
    banding_line = next(
        line for line in prompt.splitlines() if line.startswith("**Banding:**")
    )
    for threshold in (BANDING["advance_min"], BANDING["conditional_min"]):
        assert f"{threshold:.1f}" in banding_line, (
            f"band threshold {threshold} is not stated in the prompt's Banding line"
        )

    assert _norm(rubric.heuristic) in _norm(prompt)
    # The evidence lists render in full — first item of each as a spot check,
    # with the full coverage asserted in test_rubric_document's renderer test.
    for dim in rubric.dimensions:
        if dim.evidence:
            assert _norm(dim.evidence[0]) in _norm(prompt)
    assert _norm(rubric.red_flags[0]) in _norm(prompt)
    assert _norm(rubric.red_flags_intro) in _norm(prompt)
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
    assert "| 1 | Differentiation" not in prompt


def test_skeleton_carries_the_narrative_fields():
    """The reviewer-facing narrative contract (design §3.2). These are NOT
    scored and are deliberately absent from the rubric document — the sidecar
    skeleton is their only definition, so this is where drift is caught.

    ``key_points`` is asserted as an ORDERED equality against the five groups
    of the shared ``KEY_POINT_GROUPS`` contract: the read path renders the
    groups in that order, so a key renamed or a group dropped from the prompt
    silently empties a section of the detail page."""
    skeleton = _skeleton()
    for key in (
        "headline", "key_points", "elevator_pitch", "score_rationale",
        "strengths", "risks",
    ):
        assert key in skeleton, f"phase4-thread-reply.md dropped {key!r}"
    assert skeleton["key_points"] == {
        "significance": [],
        "innovation": [],
        "clinical_actionability": [],
        "key_questions": [],
        "commercial_potential": [],
    }
    assert list(skeleton["key_points"]) == [
        "significance",
        "innovation",
        "clinical_actionability",
        "key_questions",
        "commercial_potential",
    ]
    assert skeleton["headline"] == ""
    assert skeleton["elevator_pitch"] == ""
    assert skeleton["score_rationale"] == ""
    assert skeleton["strengths"] == []
    assert skeleton["risks"] == []


def test_the_scout_hub_prompt_set_version_is_1_5_0_or_later():
    manifest = tomllib.loads(Path("prompts/roles/scout_hub/role.toml").read_text())
    assert tuple(int(x) for x in manifest["version"].split(".")) >= (1, 5, 0)


def test_phase4_bounds_the_headline_and_the_project_label():
    """The two length bounds are stated as numbers in the prompt and mirrored
    by ``_HEADLINE_SOFT_LIMIT`` / ``_PROJECT_SOFT_LIMIT`` on the write path, so
    the prose and the drift alarms cannot part company."""
    body = _norm(_phase4_text())
    assert "at most 140 characters" in body, (
        "phase4-thread-reply.md no longer bounds the headline at 140 characters"
    )
    assert "at most 70 characters" in body, (
        "phase4-thread-reply.md no longer bounds company_or_project at 70 characters"
    )


def test_phase4_forbids_the_bare_approximation_tilde():
    """Slack reads a PAIR of bare tildes as strikethrough, so a pitch with two
    "~5 patients"-style approximations publishes everything between them struck
    out. The hub-side counterpart of the renderer's own neutralisation."""
    body = _norm(_phase4_text())
    assert "bare `~`" in body
    assert "strikethrough" in body


def test_phase4_marks_the_score_rationale_staff_only():
    """D3 kept score reasoning OUT of the elevator pitch, which IS published,
    and gave it its own staff-only field. This assertion is the only thing
    standing between that decision and a future edit that re-merges the two."""
    body = _norm(_phase4_text())
    assert "score_rationale" in body
    assert "never posted to Slack" in body


def test_phase4_marks_strengths_and_risks_staff_only():
    """Same staff-only rule as the score rationale (items 11/12, 0049), pinned
    on a SLICE of the two items rather than the whole file — a whole-file
    check would pass vacuously off item 10's own "never posted to Slack"."""
    text = _phase4_text()
    start = text.index("11. **")
    end = text.index("Never write a bare", start)
    slice_ = _norm(text[start:end])
    assert "strengths" in slice_
    assert "risks" in slice_
    assert slice_.count("never posted to Slack") >= 2
    assert slice_.count("Never state a number for the weighted score or the band") >= 2


def test_phase4_binds_the_rationale_to_a_bolded_summary_sentence():
    """N6: a short run-in label AND a bolded one-sentence summary, so reading
    only the bold text gives the whole argument."""
    body = _norm(_phase4_text())
    assert "bolded one-sentence summary" in body
    assert "two to four words" in body


def test_phase4_binds_the_next_experiment_to_a_bolded_one_line_ask():
    """The detail page's "The ask" line is the FIRST line of
    recommended_next_experiment. Parsing cost and duration out of prose would
    be fragile in exactly the way that produces a confidently wrong number, so
    the prompt is asked for it instead (design §3.2)."""
    body = _norm(_phase4_text())
    assert "bolded one-line ask" in body


def test_the_scout_hub_prompt_set_version_moved_with_the_contract():
    """`prompt_set_stamp` records version + content hash in every run-start
    announcement, so a content change without a version bump is by definition
    an unrecorded edit (A17)."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    manifest = tomllib.loads(
        (root / "prompts/roles/scout_hub/role.toml").read_text(encoding="utf-8")
    )
    assert manifest["version"] != "1.0.0", (
        "phase4-thread-reply.md changed; bump prompts/roles/scout_hub/role.toml"
    )

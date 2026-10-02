"""Gate explanations, service side (hub 1.10.0,
docs/specs/2026-10-02-hub-1-10-summary-risks-gates-design.md §5.1-§5.3):
`normalize_gating_rationales`, the stored-reason read map, and the gate entries
`derive_strengths_and_risks` builds for a live row with and without reasons, archived
3.4.0 / 3.2.0 / 3.3.0 rows, an unknown and an unstamped row, and a malformed stored map."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.models import OpportunityAssessment
from src.services.assessment_detail import (
    derive_strengths_and_risks,
    gating_rationale_map,
    normalize_gating_rationales,
)
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION, load_rubric
from src.services.rubric_revisions import (
    PROVENANCE_ARCHIVED,
    PROVENANCE_LIVE,
    PROVENANCE_UNKNOWN,
    PROVENANCE_UNSTAMPED,
    resolve_revision,
)

GATING = {
    "life_sciences_domain": "met",
    "credible_science": "not_met",
    "translational_potential": "unconfirmed",
}
REASONS = {
    "life_sciences_domain": "A blood test that guides colorectal cancer treatment.",
    "credible_science": "The lab could not say how the screen hits were re-tested.",
    "translational_potential": "Whether the assay could leave the lab was never asked.",
}
#: The `detail` each gate's state renders as, which is how the entries are told apart.
DETAILS = {
    "life_sciences_domain": "met",
    "credible_science": "not met",
    "translational_potential": "never asked",
}
LIVE = (RUBRIC_VERSION, RUBRIC_CONTENT_HASH)
V340 = ("3.4.0", "b7b0a1d6a4a5")
V330 = ("3.3.0", "f13be750ac8d")
V320 = ("3.2.0", "42aec0479ac6")
UNKNOWN = ("9.9.9", "deadbeef0000")
UNSTAMPED = (None, None)


def _derive(assessment, stamp):
    revision, provenance = resolve_revision(*stamp)
    result = derive_strengths_and_risks(
        assessment, dimensions=[], consults=[], revision=revision,
        revision_provenance=provenance,
    )
    return result, provenance


def _gates(result) -> dict[str, dict]:
    """Every gate entry, keyed by its `detail`."""
    return {
        entry["detail"]: entry
        for bucket in ("strengths", "risks", "unestablished")
        for entry in result[bucket]
        if entry["source"] == "gating"
    }


@pytest.mark.parametrize("value, expected", [
    ({"credible_science": " A reason. "}, {"credible_science": "A reason."}),
    ({" Credible_Science ": "r"}, {"credible_science": "r"}),
    ({"credible_science": "r", "life_sciences_domain": ""}, {"credible_science": "r"}),
    ({"credible_science": "r", "life_sciences_domain": None}, {"credible_science": "r"}),
    ({"credible_science": "  ", "life_sciences_domain": None}, None),
    ({}, None),
    (None, None),
    ("credible_science: r", None),
    (["r"], None),
    ({"credible_science": 5}, None),
    ({5: "r"}, None),
    ({"  ": "r"}, None),
    ({"k" * 51: "r"}, None),
    ({f"gate_{i}": "r" for i in range(11)}, None),
], ids=[
    "strips", "normalizes-key", "drops-blank", "drops-null", "all-blank", "empty", "none",
    "string", "list", "non-string-value", "non-string-key", "blank-key", "long-key", "too-many",
])
def test_normalize_gating_rationales(value, expected):
    assert normalize_gating_rationales(value) == expected


def test_the_bounds_are_inclusive():
    assert normalize_gating_rationales({"k" * 50: "r"}) == {"k" * 50: "r"}
    ten = {f"gate_{i}": "r" for i in range(10)}
    assert normalize_gating_rationales(ten) == ten


def test_an_over_long_reason_is_stored_whole():
    """The 200-character sentence bound is a prompt contract the engine warns about
    (spec §5.1); dropping or clipping the text here would lose the reason."""
    long = "x" * 450
    assert normalize_gating_rationales({"credible_science": long}) == {"credible_science": long}


def test_the_read_map_renormalizes_and_skips_what_it_cannot_render():
    row = SimpleNamespace(gating_rationales={" Credible_Science ": " r ", "x": "  ", "y": 5, 7: "z"})
    assert gating_rationale_map(row) == {"credible_science": "r"}
    assert gating_rationale_map(SimpleNamespace()) == {}
    assert gating_rationale_map(SimpleNamespace(gating_rationales=["r"])) == {}


def test_a_live_row_with_reasons_titles_every_gate_and_carries_its_reason():
    result, provenance = _derive(
        OpportunityAssessment(gating=GATING, gating_rationales=REASONS), LIVE
    )
    assert provenance == PROVENANCE_LIVE
    live = load_rubric().gating
    gates = _gates(result)
    for key, detail in DETAILS.items():
        assert gates[detail]["label"] == live[key]["title"], key
        assert gates[detail]["body"] == [live[key]["description"]], key
        assert gates[detail]["rationale"] == REASONS[key], key
    # The unconfirmed gate is still "never asked", in the not-established bucket.
    assert [e["source"] for e in result["unestablished"]] == ["gating"]


def test_a_live_row_without_reasons_carries_every_definition_and_no_reason():
    result, _provenance = _derive(OpportunityAssessment(gating=GATING), LIVE)
    live = load_rubric().gating
    gates = _gates(result)
    for key, detail in DETAILS.items():
        assert gates[detail]["label"] == live[key]["title"], key
        assert gates[detail]["body"] == [live[key]["description"]], key
        assert gates[detail]["rationale"] is None, key


@pytest.mark.parametrize("stamp", [V340, V320], ids=["3.4.0", "3.2.0"])
def test_an_archived_row_with_gate_tables_uses_the_registry_text(stamp):
    result, provenance = _derive(
        OpportunityAssessment(
            gating=GATING, gating_rationales={"credible_science": REASONS["credible_science"]}
        ),
        stamp,
    )
    assert provenance == PROVENANCE_ARCHIVED
    registry = resolve_revision(*stamp)[0].gating
    assert registry, "the registry entry must carry gate tables (Task B1)"
    gates = _gates(result)
    for key, detail in DETAILS.items():
        assert gates[detail]["label"] == registry[key]["title"], key
        assert gates[detail]["body"] == [registry[key]["description"]], key
    assert gates["not met"]["rationale"] == REASONS["credible_science"]
    assert gates["met"]["rationale"] is None


@pytest.mark.parametrize("stamp, expected_provenance", [
    (V330, PROVENANCE_ARCHIVED),
    (UNKNOWN, PROVENANCE_UNKNOWN),
    (UNSTAMPED, PROVENANCE_UNSTAMPED),
], ids=["3.3.0-no-tables", "unknown", "unstamped"])
def test_a_row_with_no_resolvable_definition_keeps_bare_labels_and_its_reasons(
    stamp, expected_provenance
):
    result, provenance = _derive(
        OpportunityAssessment(gating=GATING, gating_rationales=REASONS), stamp
    )
    assert provenance == expected_provenance
    gates = _gates(result)
    for key, detail in DETAILS.items():
        assert gates[detail]["label"] == key.replace("_", " "), key
        assert gates[detail]["body"] == [], key
        assert gates[detail]["rationale"] == REASONS[key], key


def test_an_unrecognised_gate_value_still_gets_its_title_definition_and_reason():
    result, _provenance = _derive(
        OpportunityAssessment(
            gating={"credible_science": "maybe"},
            gating_rationales={"credible_science": "Asked twice, answered neither way."},
        ),
        LIVE,
    )
    [entry] = result["unestablished"]
    live = load_rubric().gating["credible_science"]
    assert entry["label"] == live["title"]
    assert entry["detail"] == "unrecognised gating value"
    assert entry["body"] == [live["description"]]
    assert entry["rationale"] == "Asked twice, answered neither way."


@pytest.mark.parametrize("stored", [
    ["a reason"],
    "a reason",
    {"credible_science": 5},
    {"credible_science": "   "},
    {7: "x"},
    {"credible_science": None},
], ids=["list", "string", "non-string-value", "blank-value", "non-string-key", "null-value"])
def test_a_malformed_stored_reason_map_gives_no_reason_and_never_raises(stored):
    result, _provenance = _derive(
        OpportunityAssessment(gating=GATING, gating_rationales=stored), LIVE
    )
    gates = _gates(result)
    assert len(gates) == 3
    assert all(entry["rationale"] is None for entry in gates.values())


def test_a_row_without_the_attribute_derives_without_reasons():
    row = SimpleNamespace(gating=GATING, red_flags=None, scores=None)
    result, _provenance = _derive(row, LIVE)
    gates = _gates(result)
    assert len(gates) == 3
    assert all(entry["rationale"] is None for entry in gates.values())

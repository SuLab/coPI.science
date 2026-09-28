"""`normalize_dimension_rationales` — the write-time shape check for sidecar
item 2's companion field (migration 0052) — and the read path that attaches a
stored reason to its dimension in `build_assessment_detail`."""
import pytest

from src.models import OpportunityAssessment
from src.services.assessment_detail import (
    build_assessment_detail,
    normalize_dimension_rationales,
)
from tests import factories


def test_a_well_formed_map_is_kept_and_stripped():
    assert normalize_dimension_rationales(
        {"scientific_credibility": "  Genetics and rescue data are believable.  "}
    ) == {"scientific_credibility": "Genetics and rescue data are believable."}


def test_keys_are_lowercased_and_stripped_like_the_scores_map():
    """`build_assessment_detail` normalizes `scores` keys with
    `.strip().lower()`; a rationale keyed `Scientific_Credibility` would
    otherwise score fine and silently render nothing."""
    assert normalize_dimension_rationales(
        {" Scientific_Credibility ": "why"}
    ) == {"scientific_credibility": "why"}


def test_blank_values_and_non_strings_reject_the_whole_field():
    """A map with nothing but blanks is None; a non-string value is a type
    violation and rejects the whole map."""
    for bad in ({"a": ""}, {"a": "   "}, {"a": None}, {"a": ["x"]}, {"a": 3}):
        assert normalize_dimension_rationales(bad) is None
    assert normalize_dimension_rationales({"a": "why", "b": 3}) is None


def test_one_blank_reason_among_six_keeps_the_other_five():
    """The skeleton pre-fills every key with "". A hub that explains five
    dimensions and leaves one placeholder must store the five, not lose all six
    to raw_verdict (spec §5.3: warn, never drop)."""
    six = {
        "differentiation_unmet_need": "Unmet need is well documented.",
        "scientific_credibility": "Rescue data in two models.",
        "translational_path": "",
        "fundable_experiment": "A 9-month decisive assay.",
        "venture_potential": None,
        "team_executability": "  Lab has run this assay before.  ",
    }
    assert normalize_dimension_rationales(six) == {
        "differentiation_unmet_need": "Unmet need is well documented.",
        "scientific_credibility": "Rescue data in two models.",
        "fundable_experiment": "A 9-month decisive assay.",
        "team_executability": "Lab has run this assay before.",
    }


def test_non_dicts_and_empties_are_none():
    for bad in (None, [], {}, "text", 3, ["a"]):
        assert normalize_dimension_rationales(bad) is None


def test_an_oversized_map_or_key_is_rejected():
    assert normalize_dimension_rationales({str(i): "why" for i in range(21)}) is None
    assert normalize_dimension_rationales({"k" * 51: "why"}) is None
    # The bounds themselves are inclusive.
    assert normalize_dimension_rationales({str(i): "why" for i in range(20)}) is not None
    assert normalize_dimension_rationales({"k" * 50: "why"}) == {"k" * 50: "why"}


def test_a_blank_or_non_string_key_rejects_the_whole_field():
    assert normalize_dimension_rationales({"   ": "why"}) is None
    assert normalize_dimension_rationales({3: "why"}) is None


# ---------------------------------------------------------------------------
# Read path: `build_assessment_detail(...)["dimensions"][i]["rationale"]`
# ---------------------------------------------------------------------------

#: Five of the live revision's six dimensions (the row is unstamped, so it is
#: read against the live document) plus one key the revision does not name,
#: which renders through the unnamed-key fallback loop.
_SCORES = {
    "differentiation_unmet_need": 4,
    "scientific_credibility": 5,
    "translational_path": 3,
    "fundable_experiment": 2,
    "venture_potential": 3,
    "off_rubric_key": 3,
}


async def _seed(db_session, **kwargs):
    run = await factories.make_simulation_run(db_session)
    a = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        recommendation="conditional", scores=_SCORES, **kwargs,
    )
    db_session.add(a)
    await db_session.flush()
    return a


@pytest.mark.integration
async def test_each_dimension_carries_its_stored_reason_in_both_loops(db_session):
    a = await _seed(db_session, dimension_rationales={
        "translational_path": "No route to clinic named.",
        "off_rubric_key": "Reason for a key the revision does not name.",
        "not_a_dimension": "orphan",
    })
    detail = await build_assessment_detail(db_session, a.id, admin_view=True)
    by_key = {d["key"]: d for d in detail["dimensions"]}
    assert by_key["translational_path"]["rationale"] == "No route to clinic named."
    assert by_key["off_rubric_key"]["rationale"] == (
        "Reason for a key the revision does not name."
    )
    # Scored-without-a-reason and never-scored dimensions carry None, and the
    # orphan key attaches to nothing.
    assert by_key["scientific_credibility"]["rationale"] is None
    assert by_key["team_executability"]["rationale"] is None
    assert "not_a_dimension" not in by_key
    assert all(d["rationale"] != "orphan" for d in detail["dimensions"])
    # The same reason reaches the derived brief's mid-scale entry.
    mid = detail["verdict_signals"]["mid_scale"]
    assert "No route to clinic named." in {e["rationale"] for e in mid}


@pytest.mark.integration
async def test_a_null_column_gives_every_dimension_and_every_entry_a_null_rationale(db_session):
    """Every pre-0052 row: the key is present everywhere and is None, so the
    page renders exactly as it did before the column existed."""
    a = await _seed(
        db_session, dimension_rationales=None,
        gating={"life_sciences_domain": "met"}, red_flags=["No IP position"],
    )
    detail = await build_assessment_detail(db_session, a.id, admin_view=True)
    assert detail["dimensions"]
    assert all(
        "rationale" in d and d["rationale"] is None for d in detail["dimensions"]
    )
    vs = detail["verdict_signals"]
    entries = [
        e for bucket in ("strengths", "risks", "unestablished", "mid_scale")
        for e in vs[bucket]
    ]
    assert vs["mid_scale"]
    assert entries
    assert all(e["rationale"] is None for e in entries)

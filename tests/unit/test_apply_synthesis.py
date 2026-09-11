"""Unit tests for the shared write-gate, including the "four scripts bypass
the gate" case."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from src.services.profile_pipeline import apply_synthesis

_LIST_FIELDS = (
    "techniques",
    "experimental_models",
    "disease_areas",
    "key_targets",
    "keywords",
)


def _profile(**overrides):
    """A stand-in for a ResearcherProfile row.

    Every synthesized column is present and None, which is what a freshly
    created row really holds (models/profile.py: each is `nullable=True` with no
    default). Tests that need a *curated* stored value pass it explicitly -- the
    distinction between "this column has never been written" and "this column
    holds something a PI or an earlier run put there" is the whole subject of
    the absent-vs-empty cases below.
    """
    defaults = dict(
        profile_version=0,
        research_summary=None,
        synthesis_validated=None,
        techniques=None,
        experimental_models=None,
        disease_areas=None,
        key_targets=None,
        keywords=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_first_ever_synthesis_is_stored_even_if_unvalidated():
    p = _profile()
    applied = apply_synthesis(p, {"research_summary": "bad"}, validated=False)
    assert applied is True
    assert p.research_summary == "bad"
    assert p.synthesis_validated is False


def test_a_validated_stored_profile_is_not_overwritten_by_an_unvalidated_one():
    p = _profile(profile_version=2, research_summary="GOOD OLD", synthesis_validated=True)
    applied = apply_synthesis(p, {"research_summary": "bad new"}, validated=False)
    assert applied is False
    assert p.research_summary == "GOOD OLD"


def test_a_validated_stored_profile_is_overwritten_by_a_validated_one():
    p = _profile(profile_version=2, research_summary="GOOD OLD", synthesis_validated=True)
    applied = apply_synthesis(p, {"research_summary": "good new"}, validated=True)
    assert applied is True
    assert p.research_summary == "good new"


def test_a_previously_unvalidated_stored_profile_is_not_worth_protecting():
    p = _profile(profile_version=2, research_summary="OLD DRAFT", synthesis_validated=False)
    applied = apply_synthesis(p, {"research_summary": "new draft"}, validated=False)
    assert applied is True
    assert p.research_summary == "new draft"


def test_empty_synthesized_is_never_applied():
    p = _profile(profile_version=0)
    assert apply_synthesis(p, {}, validated=False) is False
    assert apply_synthesis(p, None, validated=False) is False


@pytest.mark.parametrize("wrong", ["cancer", 5, {"a": 1}])
@pytest.mark.parametrize("field", _LIST_FIELDS)
def test_a_non_list_value_is_rejected_and_the_stored_list_survives(field, wrong):
    """A model returning a bare string for a list column must not be iterated
    character-by-character onto the column (`"cancer"` ->
    `['c','a','n','c','e','r']`), and a non-iterable must not raise
    `StatementError` at flush and fail the whole job. Coercing every non-list
    to `[]` is also wrong, because `[]` would then be written over whatever
    was curated there. A value of
    the wrong type says nothing about the stored one, so it is rejected and
    the stored value is left alone.

    Three mutants die here: storing the raw value (per-character corruption),
    coercing it to `[]` (blanking), and raising on a non-iterable.
    """
    curated = ["curated-a", "curated-b"]
    p = _profile(**{field: list(curated)})
    applied = apply_synthesis(
        p, {"research_summary": "x", field: wrong}, validated=False
    )
    assert applied is True
    assert getattr(p, field) == curated


@pytest.mark.parametrize("field", _LIST_FIELDS)
def test_an_omitted_list_field_leaves_the_stored_list_alone(field):
    """A response that PASSES validation while omitting
    `keywords`/`key_targets`/`experimental_models` must not write `[]` over
    curated values. An omitted key is not an instruction to empty a column --
    it is the absence of one.
    """
    curated = ["curated-a", "curated-b"]
    p = _profile(profile_version=4, research_summary="OLD", synthesis_validated=True,
                 **{field: list(curated)})
    applied = apply_synthesis(
        p, {"research_summary": "a new summary"}, validated=True
    )
    assert applied is True
    assert p.research_summary == "a new summary"
    assert getattr(p, field) == curated


@pytest.mark.parametrize("field", _LIST_FIELDS)
def test_an_explicitly_empty_list_really_does_empty_the_stored_list(field):
    """The other half of the distinction: `[]` present in the response IS an
    instruction to empty the column, and stays one. Without this, "keep what
    you have" would be indistinguishable from "never shrink a list".
    """
    p = _profile(profile_version=4, research_summary="OLD", synthesis_validated=True,
                 **{field: ["curated-a"]})
    applied = apply_synthesis(
        p, {"research_summary": "a new summary", field: []}, validated=True
    )
    assert applied is True
    assert getattr(p, field) == []


def test_a_synthesis_that_omits_every_known_field_changes_nothing():
    """A response whose keys are all misspelled (or which is a JSON object of
    something else entirely) parses, is truthy, and used to blank the summary
    and all five list columns in one go. There is nothing in it to apply, so
    nothing is applied -- including `synthesis_validated` and
    `profile_generated_at`, which describe a synthesis that never landed.
    """
    p = _profile(profile_version=4, research_summary="OLD", synthesis_validated=True,
                 techniques=["t"], experimental_models=["em"], disease_areas=["d"],
                 key_targets=["kt"], keywords=["k"])
    applied = apply_synthesis(
        p, {"summary": "misspelled", "keyword": ["also misspelled"]}, validated=True
    )
    assert applied is False
    assert p.research_summary == "OLD"
    assert p.techniques == ["t"]
    assert p.experimental_models == ["em"]
    assert p.disease_areas == ["d"]
    assert p.key_targets == ["kt"]
    assert p.keywords == ["k"]
    assert p.synthesis_validated is True
    assert not hasattr(p, "profile_generated_at")


def test_an_unvalidated_stored_profile_is_not_blanked_by_an_incomplete_synthesis():
    """The second, unfiled shape. `_stored_is_worth_keeping` returns False for
    `synthesis_validated=False`, so the keep-what-you-have gate above does not
    protect such a profile at all -- an incomplete synthesis was applied to it
    in full, blanking every curated list. The gate decides whether the new
    synthesis may be applied; it does not decide what "the new synthesis" even
    contains, which is why the per-field rule has to hold on this path too.
    """
    p = _profile(profile_version=3, research_summary="OLD DRAFT",
                 synthesis_validated=False, keywords=["ferroptosis", "autophagy"],
                 key_targets=["GPX4"], experimental_models=["HEK293"])
    applied = apply_synthesis(
        p,
        {"research_summary": "new draft", "techniques": ["a", "b", "c"],
         "disease_areas": ["cancer"]},
        validated=False,
    )
    assert applied is True
    assert p.research_summary == "new draft"
    assert p.keywords == ["ferroptosis", "autophagy"]
    assert p.key_targets == ["GPX4"]
    assert p.experimental_models == ["HEK293"]


def test_a_non_string_research_summary_is_rejected_and_the_stored_summary_survives():
    """`research_summary` had no type guard at all: whatever the response held
    was assigned straight to a Text column, so a `{"research_summary": {...}}`
    reached the flush as a dict (StatementError, job failure) and a
    `"research_summary": null` blanked the stored summary. Same rule as the
    list fields -- a value of the wrong type is not an instruction.
    """
    p = _profile(profile_version=2, research_summary="OLD", synthesis_validated=False)
    applied = apply_synthesis(
        p,
        {"research_summary": {"text": "nested"}, "techniques": ["a", "b", "c"]},
        validated=False,
    )
    assert applied is True
    assert p.research_summary == "OLD"
    assert p.techniques == ["a", "b", "c"]


def test_an_explicitly_empty_research_summary_is_applied():
    """`""` is a string, so it is an instruction, and it is honoured -- the
    guard is `isinstance(..., str)`, not truthiness."""
    p = _profile(profile_version=2, research_summary="OLD", synthesis_validated=False)
    applied = apply_synthesis(p, {"research_summary": ""}, validated=False)
    assert applied is True
    assert p.research_summary == ""


def test_sets_profile_generated_at_when_applied():
    p = _profile()
    apply_synthesis(p, {"research_summary": "x"}, validated=True)
    assert isinstance(p.profile_generated_at, datetime)


def test_does_not_touch_evidence_counts():
    """apply_synthesis is deliberately silent on evidence_pmid_count/evidence_pub_count
    — only run_profile_pipeline computes those; scripts have no equivalent."""
    p = _profile(evidence_pub_count=7, evidence_pmid_count=9)
    apply_synthesis(p, {"research_summary": "x"}, validated=True)
    assert p.evidence_pub_count == 7
    assert p.evidence_pmid_count == 9


def test_a_versioned_profile_with_no_summary_is_not_worth_protecting():
    """_stored_is_worth_keeping (the shared predicate extracted out of
    run_profile_pipeline/apply_synthesis) requires
    `bool(profile.research_summary)`; a versioned, previously-validated
    profile whose summary is the empty string (not None) is not worth
    protecting either — mutation-kill for that leg of the predicate, not just
    `profile_version > 0` or `synthesis_validated is not False`."""
    p = _profile(profile_version=3, research_summary="", synthesis_validated=True)
    applied = apply_synthesis(p, {"research_summary": "new"}, validated=False)
    assert applied is True
    assert p.research_summary == "new"


def test_a_non_dict_synthesized_result_is_never_applied():
    """extract_json's type hint promises dict[str, Any], but it is a bare
    `json.loads` under the hood: a fenced ```json block containing a JSON
    *array* (or any other non-object top-level value) parses fine and comes
    back as, e.g., a `list` — not a dict. `run_profile_pipeline` normalizes
    both its own synthesize_profile call sites for exactly this, but the four
    scripts/ callers (regen_profile_from_cv.py, vet_publications.py,
    resynth_from_current_pubs.py, regen_profiles_from_web.py) hand
    apply_synthesis their own extract_json result directly with no such
    guard — so apply_synthesis itself must reject a non-dict `synthesized`
    before ever calling a dict method on it.
    """
    p = _profile(profile_version=0, research_summary=None, synthesis_validated=None)
    applied = apply_synthesis(p, [1, 2, 3], validated=True)
    assert applied is False
    assert p.research_summary is None
    assert p.synthesis_validated is None
    assert not hasattr(p, "profile_generated_at")


def test_an_omitted_list_field_on_a_never_written_column_stays_none():
    """The counterpart of the curated case, and the reason
    `test_missing_keywords_key_defaults_to_empty_list` was wrong to pin `[]`:
    "leave the stored value alone" on a column nothing has ever written leaves
    it NULL, which is exactly what it was. The columns are nullable
    (models/profile.py) and every reader already handles NULL -- what no reader
    can recover from is a curated list replaced by `[]`.
    """
    p = _profile()
    applied = apply_synthesis(
        p, {"research_summary": "x", "techniques": ["a", "b", "c"]}, validated=True
    )
    assert applied is True
    assert p.keywords is None
    assert p.techniques == ["a", "b", "c"]

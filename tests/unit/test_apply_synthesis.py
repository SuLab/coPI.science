"""Unit tests for the shared write-gate (issue #22 COR-22 residual / red-team
"four scripts bypass the gate")."""

from datetime import datetime
from types import SimpleNamespace

from src.services.profile_pipeline import apply_synthesis


def _profile(**overrides):
    defaults = dict(profile_version=0, research_summary=None, synthesis_validated=None)
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


def test_non_list_techniques_is_coerced_to_empty_list_not_stored_as_a_string():
    p = _profile()
    apply_synthesis(p, {"research_summary": "x", "techniques": "PCR"}, validated=False)
    assert p.techniques == []


def test_non_iterable_techniques_does_not_raise_and_is_coerced_to_empty_list():
    p = _profile()
    apply_synthesis(p, {"research_summary": "x", "techniques": 5}, validated=False)
    assert p.techniques == []


def test_non_list_disease_areas_is_coerced_to_empty_list_not_stored_as_a_string():
    """issue #22 I1: apply_synthesis used to type-guard only `techniques`; a
    string for `disease_areas` iterated character-by-character onto the
    column (`"cancer"` -> `['c','a','n','c','e','r']`)."""
    p = _profile()
    apply_synthesis(p, {"research_summary": "x", "disease_areas": "cancer"}, validated=False)
    assert p.disease_areas == []


def test_non_iterable_disease_areas_does_not_raise_and_is_coerced_to_empty_list():
    p = _profile()
    apply_synthesis(p, {"research_summary": "x", "disease_areas": 5}, validated=False)
    assert p.disease_areas == []


def test_non_list_keywords_is_coerced_to_empty_list_not_stored_as_a_string():
    p = _profile()
    apply_synthesis(
        p, {"research_summary": "x", "keywords": "ferroptosis"}, validated=False
    )
    assert p.keywords == []


def test_non_iterable_keywords_does_not_raise_and_is_coerced_to_empty_list():
    p = _profile()
    apply_synthesis(p, {"research_summary": "x", "keywords": 5}, validated=False)
    assert p.keywords == []


def test_non_list_key_targets_is_coerced_to_empty_list_not_stored_as_a_string():
    p = _profile()
    apply_synthesis(p, {"research_summary": "x", "key_targets": "TP53"}, validated=False)
    assert p.key_targets == []


def test_non_iterable_key_targets_does_not_raise_and_is_coerced_to_empty_list():
    p = _profile()
    apply_synthesis(p, {"research_summary": "x", "key_targets": 5}, validated=False)
    assert p.key_targets == []


def test_non_list_experimental_models_is_coerced_to_empty_list_not_stored_as_a_string():
    p = _profile()
    apply_synthesis(
        p, {"research_summary": "x", "experimental_models": "mouse"}, validated=False
    )
    assert p.experimental_models == []


def test_non_iterable_experimental_models_does_not_raise_and_is_coerced_to_empty_list():
    p = _profile()
    apply_synthesis(p, {"research_summary": "x", "experimental_models": 5}, validated=False)
    assert p.experimental_models == []


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
    """_stored_is_worth_keeping (issue #22 COR-22 fix round: the shared
    predicate extracted out of run_profile_pipeline/apply_synthesis) requires
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
    before ever calling a dict method on it (issue #22 COR-22/COR-23 residual).
    """
    p = _profile(profile_version=0, research_summary=None, synthesis_validated=None)
    applied = apply_synthesis(p, [1, 2, 3], validated=True)
    assert applied is False
    assert p.research_summary is None
    assert p.synthesis_validated is None
    assert not hasattr(p, "profile_generated_at")


def test_missing_keywords_key_defaults_to_empty_list():
    """A validated synthesis dict that simply omits the `keywords` key (as
    opposed to supplying a non-list value, already covered for `techniques`)
    must still leave p.keywords == [] — pins the missing-key convention
    apply_synthesis uses for every optional field."""
    p = _profile()
    apply_synthesis(
        p, {"research_summary": "x", "techniques": ["a", "b", "c"]}, validated=True
    )
    assert p.keywords == []

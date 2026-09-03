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

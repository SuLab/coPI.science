"""The registry-driven _verdict_doc renders identical blocks and targets to the
frozen original for fixtures that exercise every branch, for both tiers. Its output
is part of the assessment-chat model input (C27)."""
import itertools
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.models.assessment_chat import CHAT_TIER_REVIEWER, CHAT_TIER_STAFF
from src.services import assessment_chat_record as rec
from src.services.rubric_revisions import PROVENANCE_ARCHIVED, PROVENANCE_LIVE, PROVENANCE_UNKNOWN
from tests.unit import _frozen_verdict_doc as frozen


def _assessment(**over):
    base = dict(
        company_or_project="Proj", headline="Head", confidence="[high]", elevator_pitch="Pitch",
        key_points={"proposal": ["kp1"], "lab_background": ["kp2"]}, score_rationale="Because",
        strengths=["s1", "", None, " s2 "], risks=["r1"], competitive_landscape=["c1"], evidence_maturity=["e1"],
        recommended_next_experiment="Para one.\n\nPara two.", subject_agent_id="lab1", agent_id="hub",
        created_at=datetime(2026, 9, 1, 12, 34, tzinfo=UTC), channel_name="interview-1",
        recommendation="pass", weighted_score=2.5, band="pass", rubric_version="3.5.0",
        rubric_content_hash="abc123def456", missing_domains=["legal"], gating={"ip_clear": "met", "odd": True},
        red_flags=["f1", "f2"], rationale="R1\n\nR2",
    )
    base.update(over)
    return SimpleNamespace(**base)


_REVISION = SimpleNamespace(advance_min=4.0, conditional_min=3.4, pass_label="decline", scale_min=1, scale_max=5)
_ASSESSMENT_VARIANTS = [
    {}, {"recommendation": "advance", "band": "advance"}, {"recommendation": None},
    {"weighted_score": None}, {"rubric_version": None}, {"rubric_content_hash": None},
    {"gating": None}, {"gating": {}}, {"red_flags": None}, {"red_flags": []}, {"red_flags": "not a list"},
    {"company_or_project": None, "headline": "", "confidence": None, "elevator_pitch": None,
     "score_rationale": None, "subject_agent_id": None, "created_at": "not a datetime"},
    {"key_points": None}, {"strengths": "x", "risks": None}, {"recommended_next_experiment": None, "rationale": None},
]
_DETAIL_VARIANTS = [
    dict(banding={"pass_label": "decline"}, revision=_REVISION, revision_provenance=PROVENANCE_LIVE,
         rubric_version="3.5.0", panel_state="gap", messages_available=True,
         verdict_signals={"strengths": [{"source": "consult", "body": ["b1"], "label": "legal", "detail": "3", "note": "latest of 2"},
                                        {"source": "dimension", "body": ["x"]}, "junk"]},
         gating_descriptions={"ip_clear": {"description": "IP is clear"}},
         dimensions=[{"title": "Team", "weight_note": "25%", "score": 4, "rationale": "good"},
                     {"key": "novelty", "score": None}, "junk"]),
    dict(banding={}, revision=None, revision_provenance=PROVENANCE_ARCHIVED, rubric_version="3.5.0",
         panel_state="not_owed", messages_available=False,
         verdict_signals={"strengths": [{"source": "consult", "body": ["b"], "label": "x", "detail": "1", "note": "n"}]},
         gating_descriptions={}, dimensions=[{"key": "a", "score": None}]),
    dict(banding={"pass_label": None},
         revision=SimpleNamespace(advance_min=None, scale_min=1, scale_max=5),
         revision_provenance=PROVENANCE_UNKNOWN,
         rubric_version=None, panel_state="unverified", messages_available=True, verdict_signals=None,
         gating_descriptions=None, dimensions=[]),
    dict(banding=None, revision=None, revision_provenance="weird", rubric_version="3.4.0",
         panel_state="verified", dimensions=None),
    dict(banding={}, revision=None, revision_provenance=None, rubric_version="x", panel_state=None),
]


def _render(fn, a, d, tier):
    doc = fn(dict(d, assessment=a), tier)
    return doc.key, doc.blocks, doc.targets


@pytest.mark.parametrize("a_over,d,tier", list(itertools.product(
    _ASSESSMENT_VARIANTS, _DETAIL_VARIANTS, [CHAT_TIER_STAFF, CHAT_TIER_REVIEWER])))
def test_identical_bytes(a_over, d, tier):
    a = _assessment(**a_over)
    assert _render(rec._verdict_doc, a, d, tier) == _render(frozen._verdict_doc, a, d, tier)


def test_staff_only_constants_are_unchanged():
    assert rec.STAFF_ONLY_VERDICT_FIELDS == frozen.STAFF_ONLY_VERDICT_FIELDS
    assert rec._STAFF_ONLY_LABELS == frozen._STAFF_ONLY_LABELS


def test_every_labelled_registry_field_reaches_the_record():
    """Registry -> chat record: a seeded value of every labelled field shows up under
    its label (staff tier sees staff-only ones, reviewer tier does not)."""
    from src.services.verdict_fields import VERDICT_FIELDS

    detail = dict(_DETAIL_VARIANTS[0])
    text = "\n".join(_render(rec._verdict_doc, _assessment(), detail, CHAT_TIER_STAFF)[1])
    for f in VERDICT_FIELDS:
        if f.label:
            assert f.label in text, f.key
    reviewer = "\n".join(_render(rec._verdict_doc, _assessment(), detail, CHAT_TIER_REVIEWER)[1])
    for f in VERDICT_FIELDS:
        if f.label and f.staff_only:
            assert f.label not in reviewer, f.key

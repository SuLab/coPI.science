"""S2-05: one registry of verdict fields. Storage equality: the registry-driven
kwargs equal today's _persist_assessment treatments for a corpus of sidecars,
wrong types included (Review Focus 4). Both-ways coverage: skeleton keys vs the
registry, registry vs table columns."""
import re
from pathlib import Path

import pytest

from src.models import OpportunityAssessment
from src.services.verdict_fields import (
    RAW_ONLY_SIDECAR_KEYS,
    VERDICT_FIELD,
    VERDICT_FIELDS,
    sidecar_column_kwargs,
)

_SKELETON = Path(__file__).resolve().parents[2] / "prompts/roles/scout_hub/phase4-thread-reply.md"

_CORPUS = [
    {"company_or_project": "Acme", "headline": "A headline", "elevator_pitch": "One. Two.",
     "score_rationale": "Because.", "strengths": ["a", "b"], "risks": ["r1", "r2"],
     "competitive_landscape": ["c1", "c2"], "evidence_maturity": ["e1", "e2"],
     "funnel_stage": "seed", "recommendation": "advance", "confidence": "High",
     "rationale": "Why.", "recommended_next_experiment": "Do X."},
    {"headline": "  padded  ", "strengths": ["", "  ", "kept "], "recommendation": "x" * 90,
     "funnel_stage": "y" * 60, "confidence": "z" * 60},
]

_WRONG_TYPES = [
    {"headline": 7, "strengths": {"a": 1}, "score_rationale": ["x"], "risks": "one string",
     "competitive_landscape": None, "funnel_stage": 12345678901234567890123, "recommendation": ["advance"],
     "confidence": {"level": "high"}, "company_or_project": "", "elevator_pitch": b"bytes"},
    {"rationale": "  ", "recommended_next_experiment": "Do X.", "evidence_maturity": ["a", "", None, " b "]},
    {},
]


def _skeleton_keys() -> tuple[str, ...]:
    text = _SKELETON.read_text(encoding="utf-8")
    block = re.search(r"<assessment_json>\n(.*?)\n</assessment_json>", text, re.S).group(1)
    # The skeleton's values are placeholders, not JSON ("a | b"), so read only the
    # top-level keys: lines indented by exactly two spaces.
    return tuple(re.findall(r'^  "([a-z_]+)":', block, re.M))


def _frozen_kwargs(verdict):
    """Today's treatments, verbatim from the pre-registry `_assessment_kwargs` and
    `_persist_assessment` at the Phase 3 base."""
    from src.agent.engine.sidecar import _bounded_str, _str_or_none
    from src.services.assessment_detail import normalize_bullets
    return dict(
        company_or_project=_str_or_none(verdict.get("company_or_project")),
        headline=_str_or_none(verdict.get("headline")),
        elevator_pitch=_str_or_none(verdict.get("elevator_pitch")),
        score_rationale=_str_or_none(verdict.get("score_rationale")),
        strengths=normalize_bullets(verdict.get("strengths")),
        risks=normalize_bullets(verdict.get("risks")),
        competitive_landscape=normalize_bullets(verdict.get("competitive_landscape")),
        evidence_maturity=normalize_bullets(verdict.get("evidence_maturity")),
        funnel_stage=_bounded_str(verdict.get("funnel_stage"), 20),
        recommendation=_bounded_str(verdict.get("recommendation"), 30),
        confidence=_bounded_str(verdict.get("confidence"), 20),
        rationale=_str_or_none(verdict.get("rationale")),
        recommended_next_experiment=_str_or_none(verdict.get("recommended_next_experiment")),
    )


@pytest.mark.parametrize("verdict", _CORPUS + _WRONG_TYPES)
def test_storage_equality(verdict):
    assert sidecar_column_kwargs(verdict) == _frozen_kwargs(verdict)


@pytest.mark.parametrize("verdict", _WRONG_TYPES)
def test_storage_equality_wrong_types(verdict):
    assert sidecar_column_kwargs(verdict) == _frozen_kwargs(verdict)


def test_the_registry_stores_exactly_the_thirteen_plain_columns():
    assert set(sidecar_column_kwargs({})) == set(_frozen_kwargs({}))


def test_every_registry_column_exists_on_the_table():
    cols = set(OpportunityAssessment.__table__.c.keys())
    assert {f.column for f in VERDICT_FIELDS if f.column} <= cols


def test_every_sidecar_skeleton_key_is_registered_or_raw_only():
    keys = _skeleton_keys()
    assert "headline" in keys and "subject_agent_id" in keys  # the parse is not vacuous
    registered = {f.sidecar_key for f in VERDICT_FIELDS if f.sidecar_key}
    assert set(keys) <= registered | RAW_ONLY_SIDECAR_KEYS
    assert not (registered & RAW_ONLY_SIDECAR_KEYS)


def test_soft_bounds_are_todays():
    assert VERDICT_FIELD["headline"].soft_bound == 110
    assert VERDICT_FIELD["company_or_project"].soft_bound == 70
    assert VERDICT_FIELD["elevator_pitch"].soft_bound == 250  # words
    for k in ("strengths", "risks", "competitive_landscape", "evidence_maturity"):
        assert VERDICT_FIELD[k].soft_bound == 200


def test_engine_constants_read_the_registry():
    from src.agent.engine import sidecar
    assert sidecar._HEADLINE_SOFT_LIMIT == VERDICT_FIELD["headline"].soft_bound
    assert sidecar._PROJECT_SOFT_LIMIT == VERDICT_FIELD["company_or_project"].soft_bound
    assert sidecar._PITCH_WORD_LIMIT == VERDICT_FIELD["elevator_pitch"].soft_bound
    assert sidecar._HUB_BULLET_CHARS == VERDICT_FIELD["strengths"].soft_bound


def test_staff_only_order_is_todays():
    assert tuple(f.key for f in VERDICT_FIELDS if f.staff_only) == (
        "strengths", "risks", "competitive_landscape", "evidence_maturity")

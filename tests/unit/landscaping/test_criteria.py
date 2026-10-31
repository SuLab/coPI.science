"""The landscaping criteria file and its loader (spec §8.1, D61, L49, L62)."""
from pathlib import Path

import pytest

from src.landscaping.criteria import CRITERIA_PATH, CriteriaError, load_criteria
from tests.unit.test_doc_prompt_sync import _FORBIDDEN


def _variant(tmp_path: Path, old: str, new: str) -> Path:
    text = CRITERIA_PATH.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    path = tmp_path / f"criteria-{abs(hash((old, new)))}.toml"
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


def test_criteria_judging_hash_ignores_meta_and_reason_tags(tmp_path):
    base = load_criteria()
    unchanged = [
        ('"Correlation only",', '"Correlation only, renamed",'),
        ("approved = false", "approved = true"),
        ('version = "1.0.0"', 'version = "1.0.1"'),
        ('"1.0.0 (2026-10-30): first version', '"1.0.0 (2026-10-31): first version'),
    ]
    for old, new in unchanged:
        edited = load_criteria(_variant(tmp_path, old, new))
        assert edited.judging_hash == base.judging_hash, old
        assert edited.content_hash != base.content_hash, old

    edited = load_criteria(_variant(tmp_path, '"deliver_to", ', '"deliver_into", '))
    assert edited.judging_hash != base.judging_hash


def test_criteria_vocabularies():
    c = load_criteria()
    assert set(c.directions) == {
        "inhibit", "activate", "degrade", "replace", "edit", "deliver_to", "modulate", "unknown",
    }
    assert c.evidence_grades == (
        "correlation", "in_vitro", "in_vivo", "patient_material", "human_genetics", "clinical",
    )
    assert c.dimension_share == 0.8
    assert c.idea_types == ("biology", "chemistry")
    assert {d.test for d in c.deal_breakers} == {"data", "llm"}


def test_reason_tags_approval_flag_parsed(tmp_path):
    c = load_criteria()
    assert c.reason_tags_approved is False
    assert c.reason_tags
    approved = load_criteria(_variant(tmp_path, "approved = false", "approved = true"))
    assert approved.reason_tags_approved is True


def test_criteria_has_no_forbidden_phrases():
    text = CRITERIA_PATH.read_text(encoding="utf-8").lower()
    scope = load_criteria().scope_text.lower()
    hits = [p for p in _FORBIDDEN if p.lower() in text or p.lower() in scope]
    assert hits == []


@pytest.mark.parametrize(("old", "new"), [
    ("dimension_share = 0.8", "dimension_share = 1.5"),
    ("approved = false", 'approved = "no"'),
    ('test = "data"\ndescription = "Open Targets marks', 'test = "maybe"\ndescription = "Open Targets marks'),
])
def test_invalid_criteria_raise(tmp_path, old, new):
    with pytest.raises(CriteriaError):
        load_criteria(_variant(tmp_path, old, new))

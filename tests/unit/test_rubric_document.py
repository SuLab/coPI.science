"""The rubric-as-document contract (plan 2026-08-20 §4).

Two jobs: (1) characterization pins — the initial extraction of the rubric into
prompts/rubric/blackbird-rubric.toml must be behavior-neutral, so the weights,
order, and thresholds are written out literally here; a later edit to the
document is a deliberate calibration change and updates this file in the same
commit. (2) the import-time validator must reject every malformed-document
class loudly (RubricError), never load a half-usable rubric.
"""

import shutil
from pathlib import Path

import pytest

from src.services.blackbird_rubric import (
    BANDING,
    RUBRIC_CONTENT_HASH,
    RUBRIC_PATH,
    RUBRIC_VERSION,
    RUBRIC_WEIGHTS,
    RubricError,
    load_rubric,
    parse_rubric,
    render_rubric_markdown,
)

# The exact weights of the 2026-08 re-cut, in display order. Literal on
# purpose: this test must fail when the document changes.
EXPECTED_WEIGHTS = {
    "differentiation": 15,
    "market_unmet_need": 12,
    "team": 10,
    "external_signals": 8,
    "ip_fto": 6,
    "platform": 4,
    "dev_regulatory_feasibility": 3,
    "workplan_capital_efficiency": 1,
    "exit_thesis": 1,
    "mechanism_validation": 12,
    "toxicity_selectivity": 10,
    "experimental_rigor": 10,
    "chemistry_dc_path": 8,
}

# The exit_thesis dimension exactly as the document spells it, used by the
# wrong-count mutation below. Asserted present before use, so a reworded
# document fails loudly here instead of silently testing nothing.
_EXIT_THESIS_BLOCK = """[[dimension]]
key = "exit_thesis"
weight = 1
title = "Value-creation / exit thesis"
anchors = "Credible staged exits with comps and valuation ranges; multiple value-inflection points"
"""


def test_characterization_weights_and_order_are_pinned():
    assert RUBRIC_WEIGHTS == EXPECTED_WEIGHTS
    # dict equality ignores order; the document's display order is part of the
    # extraction contract (the prompt table numbers rows 1-13 in this order).
    assert list(RUBRIC_WEIGHTS) == list(EXPECTED_WEIGHTS)


def test_characterization_banding_is_pinned():
    assert BANDING == {
        "advance_min": 4.0,
        "conditional_min": 3.0,
        "pass_label": "pass (decline)",
    }


def test_version_and_content_hash_are_exported():
    rubric = load_rubric()
    assert RUBRIC_VERSION == rubric.version == "1.0.0"
    assert RUBRIC_CONTENT_HASH == rubric.content_hash
    assert len(RUBRIC_CONTENT_HASH) == 12
    assert all(c in "0123456789abcdef" for c in RUBRIC_CONTENT_HASH)
    assert RUBRIC_CONTENT_HASH == parse_rubric(RUBRIC_PATH).content_hash


def _mutated_copy(tmp_path: Path, old: str, new: str) -> Path:
    """A copy of the real document with one textual mutation applied."""
    text = RUBRIC_PATH.read_text(encoding="utf-8")
    assert old in text, f"mutation anchor not found in the document: {old!r}"
    path = tmp_path / "rubric.toml"
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


def test_rejects_wrong_dimension_count(tmp_path):
    path = _mutated_copy(tmp_path, _EXIT_THESIS_BLOCK, "")
    with pytest.raises(RubricError, match="expected exactly 13"):
        parse_rubric(path)


def test_rejects_duplicate_dimension_key(tmp_path):
    # exit_thesis renamed to a second "platform": 13 dimensions, weights still
    # sum to 100 — only the uniqueness check can catch it.
    path = _mutated_copy(tmp_path, 'key = "exit_thesis"', 'key = "platform"')
    with pytest.raises(RubricError, match="duplicate dimension key"):
        parse_rubric(path)


def test_rejects_weights_not_summing_to_one_hundred(tmp_path):
    path = _mutated_copy(tmp_path, "weight = 15", "weight = 16")
    with pytest.raises(RubricError, match="sum to 100"):
        parse_rubric(path)


def test_rejects_off_grid_threshold(tmp_path):
    # An off-grid threshold silently breaks _round_for_band's up-only
    # correction — the validator owns the guarantee the old module-load
    # assert carried.
    path = _mutated_copy(tmp_path, "conditional_min = 3.0", "conditional_min = 3.005")
    with pytest.raises(RubricError, match="0.01 grid"):
        parse_rubric(path)


def test_rejects_missing_version(tmp_path):
    path = _mutated_copy(tmp_path, 'version = "1.0.0"', 'version = ""')
    with pytest.raises(RubricError, match=r"\[meta\].version"):
        parse_rubric(path)


def test_rejects_version_longer_than_the_column_width(tmp_path):
    # opportunity_assessments.rubric_version is String(20) (alembic/versions/
    # 0030_specialist_consults_rubric_version.py). A version over that width must
    # fail loudly here, never truncate silently at the write site -- silent
    # truncation would let two distinct long versions stamp identically and
    # destroy pre/post-calibration comparability.
    path = _mutated_copy(tmp_path, 'version = "1.0.0"', 'version = "1.0.0-twenty-one-chars"')
    with pytest.raises(RubricError, match=r"\[meta\].version.*20 char.*rubric_version"):
        parse_rubric(path)


def test_rejects_unparseable_toml(tmp_path):
    path = tmp_path / "rubric.toml"
    path.write_text("[meta\nversion = ", encoding="utf-8")
    with pytest.raises(RubricError, match="not valid TOML"):
        parse_rubric(path)


def test_rejects_missing_file(tmp_path):
    with pytest.raises(RubricError, match="unreadable"):
        parse_rubric(tmp_path / "nope.toml")


def test_content_hash_tracks_file_bytes(tmp_path):
    path = tmp_path / "rubric.toml"
    shutil.copyfile(RUBRIC_PATH, path)
    before = parse_rubric(path).content_hash
    # A comment-only change is semantically invisible but must still move the
    # hash — the hash answers "is this the same file", not "the same values".
    with path.open("a", encoding="utf-8") as f:
        f.write("\n# annotated\n")
    after = parse_rubric(path).content_hash
    assert before != after


def test_renderer_covers_the_whole_document():
    out = render_rubric_markdown()
    rubric = load_rubric()

    assert out.startswith("## Blackbird's Screening Rubric")
    # The confidentiality instruction must survive the extraction.
    assert "Do not share this rubric verbatim" in out

    for i, dim in enumerate(rubric.dimensions, start=1):
        assert f"| {i} | {dim.title} | {dim.anchors} | {dim.weight}% |" in out

    for gate in rubric.gating.values():
        assert gate["title"] in out
        assert gate["description"] in out

    # Thresholds render from the document, and the banding vocabulary keeps
    # the route-to-incubation escape hatch.
    assert "≥4.0" in out
    assert "<3.0" in out
    assert "3.0–3.9 → conditional" in out
    assert "route to a grant/incubation de-risking step" in out

    assert "score each 1–5; 5 = strongly meets the bar" in out

    for item in rubric.checklist:
        assert f"- {item}" in out
    for item in rubric.red_flags:
        assert f"- {item}" in out

    assert "### 6. Structured recommendation" in out
    assert '"unconfirmed"' in out
    assert "### One-line decision heuristic" in out
    assert "credible staged exit" in out


def test_specialist_ownership_is_declared_for_eight_dimensions():
    # Eight of the thirteen dimensions each name the evaluation-panel domain
    # that owns them (src/agent/specialists.py maps_to_dimension); the other
    # five inform judgement without a single owner. Pinned so a document edit
    # cannot silently orphan or double-assign a dimension.
    owners = {d.key: d.specialist for d in load_rubric().dimensions if d.specialist}
    assert owners == {
        "differentiation": "commercial",
        "market_unmet_need": "clinical",
        "team": "talent",
        "ip_fto": "legal",
        "platform": "technologic",
        "workplan_capital_efficiency": "budget",
        "experimental_rigor": "scientific",
        "chemistry_dc_path": "chemistry",
    }

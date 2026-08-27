"""The rubric-as-document contract (plan 2026-08-20 §4; regime 3.0.0 per
docs/plans/2026-08-27-rubric-v3-consolidation.md).

Two jobs: (1) characterization pins — the weights, order, thresholds, gating
keys and evidence lists are written out literally here; a later edit to the
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

# The exact weights of the 2026-08-27 consolidation, in display order. Literal
# on purpose: this test must fail when the document changes.
EXPECTED_WEIGHTS = {
    "differentiation_unmet_need": 25,
    "scientific_credibility": 20,
    "translational_path": 15,
    "fundable_experiment": 15,
    "venture_potential": 15,
    "team_executability": 10,
}

# The evidence lists (BBL's Target Rubric, folded into the dimension each item
# scores). Pinned in full: the items are stakeholder content, and a document
# edit that drops or rewords one is a deliberate change, not drift.
EXPECTED_EVIDENCE = {
    "scientific_credibility": (
        "Clinical genetic evidence linking target to disease",
        "Animal model evidence (phenotype + rescue on modulation)",
        "Mechanistic connection: pathway membership, expression, pathological localization",
        "Mechanistic connection: in vitro functional data (knockdown/probes; therapeutic index)",
        "Proof of mechanism established (confidence the mechanism impacts disease)",
    ),
    "translational_path": (
        "Tissue distribution / on-target liability profile (KO/OE phenotypes; delivery route)",
        "Ability to execute: biochemical/biophysical/cell-based assays and tool reagents",
        "Target structural information (cross-species, family members)",
        "Pharmacologic tools: ligands/antibodies/probes for orthogonal validation",
        "Is selective pharmacological modulation achievable (and by what modality)?",
        "Defined target product profile",
    ),
}

# The team_executability dimension exactly as the document spells it, used by
# the wrong-count mutation below. Asserted present before use, so a reworded
# document fails loudly here instead of silently testing nothing.
_TEAM_BLOCK = """[[dimension]]
key = "team_executability"
weight = 10
title = "Team & executability"
anchors = "PI credibility and lab capability to execute the de-risking plan in 12–24 months; complementary expertise identified, not necessarily hired."
specialist = "talent"
"""


def test_characterization_weights_and_order_are_pinned():
    assert RUBRIC_WEIGHTS == EXPECTED_WEIGHTS
    # dict equality ignores order; the document's display order is part of the
    # contract (the prompt table numbers rows 1-6 in this order).
    assert list(RUBRIC_WEIGHTS) == list(EXPECTED_WEIGHTS)


def test_characterization_banding_is_pinned():
    # 3.4/2.8, derived by back-test over 51 production verdicts and provisional
    # pending >=20 verdicts stamped 3.0.0 (plan §D3). A deliberate recalibration
    # updates this pin in the same commit.
    assert BANDING == {
        "advance_min": 3.4,
        "conditional_min": 2.8,
        "pass_label": "pass (decline)",
    }


def test_characterization_gating_keys_are_pinned():
    """The three gating keys are structural — they are the sidecar's JSON keys
    and the opportunity_assessments.gating keys. The drift alarm in
    test_rubric_prompt_sync holds the skeleton and this set together."""
    assert set(load_rubric().gating) == {
        "life_sciences_domain",
        "credible_science",
        "translational_potential",
    }


def test_characterization_evidence_lists_are_pinned():
    evidence = {d.key: d.evidence for d in load_rubric().dimensions if d.evidence}
    assert evidence == EXPECTED_EVIDENCE


def test_characterization_science_block_is_35_points():
    # The two scientific dimensions carry 35% — the stake the scoring preamble
    # states in prose (test_rubric_prompt_sync recomputes the prose claim).
    science = ("scientific_credibility", "translational_path")
    assert sum(RUBRIC_WEIGHTS[k] for k in science) == 35


def test_version_and_content_hash_are_exported():
    rubric = load_rubric()
    # "3.2.0" removed the four ceremonial elements the content audit named
    # (skeleton weighted_score, the milestones array, pass_note,
    # vocabulary_note); no weight/threshold/gating change. The stamp is what
    # keeps pre-/post-calibration rows separable in
    # opportunity_assessments.rubric_version, so it is pinned, not derived.
    assert RUBRIC_VERSION == rubric.version == "3.2.0"
    assert RUBRIC_CONTENT_HASH == rubric.content_hash
    assert len(RUBRIC_CONTENT_HASH) == 12
    assert all(c in "0123456789abcdef" for c in RUBRIC_CONTENT_HASH)
    assert RUBRIC_CONTENT_HASH == parse_rubric(RUBRIC_PATH).content_hash


def test_specialist_ownership_is_declared_per_dimension():
    # Every dimension names the evaluation-panel domain that owns it;
    # `commercial` owns two. Pinned so a document edit cannot silently orphan
    # or reassign a dimension. test_rubric_prompt_sync enforces that these
    # names are real specialist domains and that the code's maps_to_dimensions
    # names real dimensions.
    owners = {d.key: d.specialist for d in load_rubric().dimensions if d.specialist}
    assert owners == {
        "differentiation_unmet_need": "commercial",
        "scientific_credibility": "scientific",
        "translational_path": "chemistry",
        "fundable_experiment": "budget",
        "venture_potential": "commercial",
        "team_executability": "talent",
    }


def _mutated_copy(tmp_path: Path, old: str, new: str) -> Path:
    """A copy of the real document with one textual mutation applied."""
    text = RUBRIC_PATH.read_text(encoding="utf-8")
    assert old in text, f"mutation anchor not found in the document: {old!r}"
    path = tmp_path / "rubric.toml"
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


def test_rejects_wrong_dimension_count(tmp_path):
    path = _mutated_copy(tmp_path, _TEAM_BLOCK, "")
    with pytest.raises(RubricError, match="expected exactly 6"):
        parse_rubric(path)


def test_rejects_duplicate_dimension_key(tmp_path):
    # team_executability renamed to a second "venture_potential": 6 dimensions
    # — only the uniqueness check can catch it before the sum check confuses
    # the diagnosis.
    path = _mutated_copy(
        tmp_path, 'key = "team_executability"', 'key = "venture_potential"'
    )
    with pytest.raises(RubricError, match="duplicate dimension key"):
        parse_rubric(path)


def test_rejects_weights_not_summing_to_one_hundred(tmp_path):
    path = _mutated_copy(tmp_path, "weight = 25", "weight = 26")
    with pytest.raises(RubricError, match="sum to 100"):
        parse_rubric(path)


def test_rejects_a_dimension_with_an_empty_anchor(tmp_path):
    # The anchors ARE the scale: a dimension with a weight and no anchor would
    # be scored against nothing, silently.
    path = _mutated_copy(
        tmp_path,
        'anchors = "PI credibility and lab capability to execute the de-risking plan in 12–24 months; complementary expertise identified, not necessarily hired."',
        'anchors = ""',
    )
    with pytest.raises(RubricError, match="anchors"):
        parse_rubric(path)


def test_rejects_an_empty_evidence_item(tmp_path):
    # `evidence` is optional, but a PRESENT list must be non-empty strings —
    # an empty item is a deleted checklist entry wearing the key.
    path = _mutated_copy(
        tmp_path,
        '"Clinical genetic evidence linking target to disease",',
        '"",',
    )
    with pytest.raises(RubricError, match="evidence"):
        parse_rubric(path)


def test_rejects_a_fourth_gating_key(tmp_path):
    # The three gating keys are the sidecar's JSON keys; a fourth cannot be
    # added by editing the document.
    path = _mutated_copy(
        tmp_path,
        "[gating.translational_potential]",
        '[gating.baltimore_commitment]\ntitle = "x"\ndescription = "y"\n\n'
        "[gating.translational_potential]",
    )
    with pytest.raises(RubricError, match="unknown gating key"):
        parse_rubric(path)


def test_rejects_off_grid_threshold(tmp_path):
    # An off-grid threshold silently breaks _round_for_band's up-only
    # correction — the validator owns that guarantee.
    path = _mutated_copy(tmp_path, "conditional_min = 2.8", "conditional_min = 2.805")
    with pytest.raises(RubricError, match="0.01 grid"):
        parse_rubric(path)


def test_rejects_inverted_band_lines(tmp_path):
    path = _mutated_copy(tmp_path, "advance_min = 3.4", "advance_min = 2.5")
    with pytest.raises(RubricError, match=r"\[banding\].advance_min must be >"):
        parse_rubric(path)


def test_rejects_missing_version(tmp_path):
    path = _mutated_copy(tmp_path, 'version = "3.2.0"', 'version = ""')
    with pytest.raises(RubricError, match=r"\[meta\].version"):
        parse_rubric(path)


def test_rejects_a_missing_banding_advisory_note(tmp_path):
    # Renaming the key leaves valid TOML behind, so only the required-field
    # check can catch it — without it the advisory paragraph would silently
    # vanish from the rendered rubric while version and hash kept stamping.
    path = _mutated_copy(tmp_path, "advisory_note = ", "advisory_note_off = ")
    with pytest.raises(RubricError, match=r"\[banding\].advisory_note"):
        parse_rubric(path)


def test_rejects_a_missing_red_flags_intro(tmp_path):
    # The intro carries the re-scope rule (disqualifier-grade only, max three);
    # without it the items render as an unframed list.
    path = _mutated_copy(
        tmp_path, 'intro = "Disqualifier-grade only', 'intro_off = "Disqualifier-grade only'
    )
    with pytest.raises(RubricError, match=r"\[red_flags\].intro"):
        parse_rubric(path)


def test_rejects_version_longer_than_the_column_width(tmp_path):
    # opportunity_assessments.rubric_version is String(20) (alembic/versions/
    # 0030_specialist_consults_rubric_version.py). A version over that width must
    # fail loudly here, never truncate silently at the write site -- silent
    # truncation would let two distinct long versions stamp identically and
    # destroy pre/post-calibration comparability.
    path = _mutated_copy(tmp_path, 'version = "3.2.0"', 'version = "3.2.0-twenty-one-chars"')
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
    # The confidentiality instruction must survive.
    assert "Do not share this rubric verbatim" in out

    for i, dim in enumerate(rubric.dimensions, start=1):
        # The whole row on ONE line — a table carrying every title, anchor and
        # weight but pairing them up wrongly would pass independent checks.
        assert f"| {i} | {dim.title} | {dim.anchors} | {dim.weight}% |" in out

    # Evidence lists render under their dimension, every item.
    for dim in rubric.dimensions:
        if dim.evidence:
            assert f"**Evidence to look for — {dim.title}**" in out
            for item in dim.evidence:
                assert f"- {item}" in out

    for gate in rubric.gating.values():
        assert gate["title"] in out
        assert gate["description"] in out

    # Thresholds render from the document, and the banding vocabulary keeps
    # the route-to-incubation escape hatch and the action each band commits
    # someone to.
    assert "≥3.4" in out
    assert "2.8–3.3 → conditional" in out
    assert "<2.8" in out
    assert "staff opens grant diligence now" in out

    # The band's authority is advisory over the recommendation — the whole
    # paragraph must render, not just exist in the document.
    assert rubric.banding_advisory_note in out
    assert "Treat the computed band as advisory, not binding" in out

    assert "score each 1–5; 5 = strongly meets the bar" in out

    # Red flags: the re-scope intro plus every item.
    assert rubric.red_flags_intro in out
    for item in rubric.red_flags:
        assert f"- {item}" in out

    assert "### 4. Structured recommendation" in out
    # The funnel-stage classification is gone (v3.1.0) and must stay gone.
    assert "funnel" not in out.lower()
    assert '"unconfirmed"' in out
    assert "### One-line decision heuristic" in out
    assert "the grant is what buys the result" in out

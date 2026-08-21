"""The rubric-as-document contract (plan 2026-08-20 §4).

Two jobs: (1) characterization pins — the initial extraction of the rubric into
prompts/rubric/blackbird-rubric.toml must be behavior-neutral, so the weights,
order, and thresholds are written out literally here; a later edit to the
document is a deliberate calibration change and updates this file in the same
commit. (2) the import-time validator must reject every malformed-document
class loudly (RubricError), never load a half-usable rubric.

Since v2.0.0 the document carries TWO scales (docs/specs/2026-08-20-rubric-v2-
incubation-rebaseline-proposal.md), so both are pinned. The investment pins are
byte-identical to the v1 ones on purpose: that re-baseline added a scale, it did
not touch the existing one, and this file is where that claim is checked.
"""

import shutil
from pathlib import Path

import pytest

from src.services.blackbird_rubric import (
    BANDING,
    BANDING_INCUBATION,
    RUBRIC_CONTENT_HASH,
    RUBRIC_PATH,
    RUBRIC_VERSION,
    RUBRIC_WEIGHTS,
    RUBRIC_WEIGHTS_INCUBATION,
    RubricError,
    display_scale_for,
    load_rubric,
    parse_rubric,
    render_rubric_markdown,
    scored_stage_aware,
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

# The incubation scale's W-C weights (proposal §4), literal for the same reason.
# The four that moved most are the whole point of the re-baseline:
# workplan_capital_efficiency 1 -> 8 (the best discriminator in the back-test),
# external_signals 8 -> 2, experimental_rigor 10 -> 8, ip_fto 6 -> 4.
EXPECTED_WEIGHTS_INCUBATION = {
    "differentiation": 16,
    "market_unmet_need": 14,
    "team": 12,
    "external_signals": 2,
    "ip_fto": 4,
    "platform": 5,
    "dev_regulatory_feasibility": 3,
    "workplan_capital_efficiency": 8,
    "exit_thesis": 2,
    "mechanism_validation": 10,
    "toxicity_selectivity": 8,
    "experimental_rigor": 8,
    "chemistry_dc_path": 8,
}

# The exit_thesis dimension exactly as the document spells it, used by the
# wrong-count mutation below. Asserted present before use, so a reworded
# document fails loudly here instead of silently testing nothing.
_EXIT_THESIS_BLOCK = """[[dimension]]
key = "exit_thesis"
weight = 1
weight_incubation = 2
title = "Value-creation / exit thesis"
anchors = "Credible staged exits with comps and valuation ranges; multiple value-inflection points"
anchors_incubation = "Venture-scale potential in one sentence: if the science works, is there a company or license a VC or pharma would want? Comps optional. Grant-only science with no commercial endpoint = 1."
"""


def test_characterization_weights_and_order_are_pinned():
    assert RUBRIC_WEIGHTS == EXPECTED_WEIGHTS
    # dict equality ignores order; the document's display order is part of the
    # extraction contract (the prompt table numbers rows 1-13 in this order).
    assert list(RUBRIC_WEIGHTS) == list(EXPECTED_WEIGHTS)


def test_characterization_incubation_weights_and_order_are_pinned():
    assert RUBRIC_WEIGHTS_INCUBATION == EXPECTED_WEIGHTS_INCUBATION
    # Same KEY ORDER as the investment set, not merely the same keys: the
    # assessments page renders one or the other per row and the prompt renders
    # both as columns of one table, so a divergent order would silently reorder
    # the chips on incubation rows and misalign the prompt's two anchor columns.
    assert list(RUBRIC_WEIGHTS_INCUBATION) == list(EXPECTED_WEIGHTS_INCUBATION)
    assert list(RUBRIC_WEIGHTS_INCUBATION) == list(RUBRIC_WEIGHTS)


def test_characterization_banding_is_pinned():
    assert BANDING == {
        "advance_min": 4.0,
        "conditional_min": 3.0,
        "pass_label": "pass (decline)",
    }


def test_characterization_incubation_banding_is_pinned():
    # Proposal §5. Same three keys as BANDING — the band NAMES are shared
    # between the scales, so pass_label is too and nothing downstream branches.
    assert BANDING_INCUBATION == {
        "advance_min": 3.4,
        "conditional_min": 2.7,
        "pass_label": "pass (decline)",
    }
    assert BANDING_INCUBATION.keys() == BANDING.keys()
    # And the incubation lines really are the lower pair — the re-baseline's
    # whole purpose. A transcription that swapped the two scales would satisfy
    # every "is it a float on the grid" check.
    assert BANDING_INCUBATION["advance_min"] < BANDING["advance_min"]
    assert BANDING_INCUBATION["conditional_min"] < BANDING["conditional_min"]


def test_version_and_content_hash_are_exported():
    rubric = load_rubric()
    # Bumped from "1.0.0" for the v2.0.0 incubation re-baseline. The stamp is
    # what keeps pre-/post-calibration rows separable in
    # opportunity_assessments.rubric_version, so it is pinned, not derived.
    assert RUBRIC_VERSION == rubric.version == "2.0.0"
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


def test_rejects_incubation_weights_not_summing_to_one_hundred(tmp_path):
    # The second scale needs its own check: this mutation leaves the INVESTMENT
    # weights summing to 100, so only a per-scale sum can catch it. A denominator
    # that is not 100 still computes a number — it is just no longer on the 1-5
    # scale the band lines are expressed in, so it would band against thresholds
    # that no longer mean anything.
    path = _mutated_copy(
        tmp_path, "weight_incubation = 16", "weight_incubation = 17"
    )
    with pytest.raises(
        RubricError, match="weight_incubation values must sum to 100"
    ):
        parse_rubric(path)


def test_rejects_a_dimension_with_no_incubation_anchor(tmp_path):
    # The anchors ARE the scale. A dimension carrying an incubation weight but
    # no incubation anchor would be scored against the investment bar the
    # re-baseline exists to move it off, at the incubation weight — the worst of
    # both, and silent.
    path = _mutated_copy(
        tmp_path,
        'anchors_incubation = "Reusable platform generating a pipeline vs one shot on goal."',
        'anchors_incubation = ""',
    )
    with pytest.raises(RubricError, match="anchors_incubation"):
        parse_rubric(path)


def test_rejects_off_grid_threshold(tmp_path):
    # An off-grid threshold silently breaks _round_for_band's up-only
    # correction — the validator owns the guarantee the old module-load
    # assert carried.
    path = _mutated_copy(tmp_path, "conditional_min = 3.0", "conditional_min = 3.005")
    with pytest.raises(RubricError, match="0.01 grid"):
        parse_rubric(path)


def test_rejects_off_grid_incubation_threshold(tmp_path):
    # weighted_score() rounds against whichever scale's lines its `stage`
    # selected, so the grid guarantee has to hold on BOTH pairs. An off-grid
    # incubation line would only misband incubation-stage verdicts — i.e. every
    # verdict this instance actually produces.
    path = _mutated_copy(tmp_path, "conditional_min = 2.7", "conditional_min = 2.705")
    with pytest.raises(RubricError, match=r"banding\.incubation.*0.01 grid"):
        parse_rubric(path)


def test_rejects_inverted_incubation_band_lines(tmp_path):
    path = _mutated_copy(tmp_path, "advance_min = 3.4", "advance_min = 2.5")
    with pytest.raises(
        RubricError, match=r"\[banding\.incubation\].advance_min must be >"
    ):
        parse_rubric(path)


def test_rejects_a_missing_incubation_banding_table(tmp_path):
    # Renamed, not deleted: the [banding] keys above it survive, so only the
    # explicit sub-table check can catch it. Without that check the incubation
    # lines would fall back to nothing at all.
    path = _mutated_copy(tmp_path, "[banding.incubation]", "[banding.incubation_off]")
    with pytest.raises(RubricError, match=r"missing \[banding.incubation\] table"):
        parse_rubric(path)


def test_rejects_missing_version(tmp_path):
    path = _mutated_copy(tmp_path, 'version = "2.0.0"', 'version = ""')
    with pytest.raises(RubricError, match=r"\[meta\].version"):
        parse_rubric(path)


def test_rejects_version_longer_than_the_column_width(tmp_path):
    # opportunity_assessments.rubric_version is String(20) (alembic/versions/
    # 0030_specialist_consults_rubric_version.py). A version over that width must
    # fail loudly here, never truncate silently at the write site -- silent
    # truncation would let two distinct long versions stamp identically and
    # destroy pre/post-calibration comparability.
    path = _mutated_copy(tmp_path, 'version = "2.0.0"', 'version = "2.0.0-twenty-one-chars"')
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
        # The whole row, both scales, on ONE line. Asserted as one string rather
        # than four substring checks: a table that carried every title, anchor
        # and weight but paired them up wrongly across the two scales would pass
        # any set of independent checks and be actively misleading.
        assert (
            f"| {i} | {dim.title} | {dim.anchors} | {dim.weight}% | "
            f"{dim.anchors_incubation} | {dim.weight_incubation}% |"
        ) in out
        # The investment prefix still holds on its own — the v1 pin, unchanged,
        # so "we only added columns to the right" is checked rather than assumed.
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

    # Both scales are stated, each labelled, and the incubation one carries the
    # action each band commits someone to (proposal §5 — bands map to actions).
    assert "**Banding (investment scale):**" in out
    assert "**Banding (incubation scale):**" in out
    assert "≥3.4" in out
    assert "2.7–3.3 → conditional" in out
    assert "<2.7" in out
    assert "staff opens grant diligence now" in out

    # And the model is told which anchor column to use, which is the only thing
    # that makes a two-scale table usable rather than confusing.
    assert "Score against the anchor column for the funnel stage you assigned" in out

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


# ---------------------------------------------------------------------------
# scored_stage_aware / display_scale_for (provenance gate, F1/F2/F3)
#
# 34 rows in production predate stage-aware scoring (29 NULL rubric_version, 5
# "1.0.0") and were scored on the investment weights regardless of what their
# funnel_stage says. A page rendering a STORED row has to reconstruct that
# fact from rubric_version, not assume the current (stage-aware) document
# scored every row it is now displaying.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "rubric_version, expected",
    [
        (None, False),
        ("1.0.0", False),
        ("2.0.0", True),
        ("2.1.3", True),
        ("10.0.0", True),
        ("garbage", False),
    ],
)
def test_scored_stage_aware(rubric_version, expected):
    # "10.0.0" is the case a lexicographic string compare gets wrong:
    # "10.0.0" < "2.0.0" as strings, but major version 10 is >= 2.
    assert scored_stage_aware(rubric_version) is expected


def test_display_scale_for_a_legacy_row_stays_on_the_investment_scale():
    # rubric_version=None, funnel_stage="incubation": exactly the 29-row
    # legacy shape. Must NOT flip to incubation just because the stage says
    # so — that scale was never applied when this row was scored.
    scale = display_scale_for(None, "incubation")
    assert scale.incubation is False
    assert scale.weights == RUBRIC_WEIGHTS
    assert scale.banding == BANDING
    assert scale.label == "investment scale"

    # Same for the 5 "1.0.0" rows.
    scale = display_scale_for("1.0.0", "incubation")
    assert scale.incubation is False
    assert scale.weights == RUBRIC_WEIGHTS


def test_display_scale_for_a_v2_incubation_row_uses_incubation_weights():
    scale = display_scale_for("2.0.0", "incubation")
    assert scale.incubation is True
    assert scale.weights == RUBRIC_WEIGHTS_INCUBATION
    assert scale.banding == BANDING_INCUBATION
    assert scale.label == "incubation scale (rubric v2)"


def test_display_scale_for_a_v2_investment_row_stays_investment():
    # A v2-scored row whose funnel_stage is NOT incubation (pre-seed and
    # later) is the other half of the AND: stage-aware alone is not enough.
    scale = display_scale_for("2.0.0", "pre-seed")
    assert scale.incubation is False
    assert scale.weights == RUBRIC_WEIGHTS
    assert scale.banding == BANDING

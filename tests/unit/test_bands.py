import re
from pathlib import Path

from src.agent import specialists
from src.services import blackbird_rubric as br
from src.services.bands import BANDS, band_class, band_label

REPO = Path(__file__).resolve().parents[2]


def _frozen_band(score):
    if score >= br._RUBRIC.advance_min:
        return "advance"
    if score >= br._RUBRIC.conditional_min:
        return "conditional"
    return "pass"


def test_band_equals_today_on_the_grid():
    for i in range(0, 501):
        s = i / 100
        assert br.band(s) == _frozen_band(s), s


def test_band_thresholds_are_todays():
    assert br._BAND_THRESHOLDS == (br._RUBRIC.conditional_min, br._RUBRIC.advance_min)


def test_panel_required_for_is_todays():
    assert specialists.PANEL_REQUIRED_FOR == frozenset({"advance", "conditional"})


# `weighted_score` at the phase base (9b66cc85), computed by THAT code and frozen
# here: one row per grid value v, entry k is the score with the first k+1
# rubric dimensions (in RUBRIC_WEIGHTS order) set to v and the rest to 3.
# Base rule: a missing, non-number, bool, NaN or infinite value counts 0; an
# out-of-scale number is clamped into the scale (0 -> 1, 6 -> 5); the mean is
# rounded by `_round_for_band` (3.395 everywhere -> 3.39, not 3.4).
_FROZEN_WEIGHTED_GRID = (
    (1, [2.5, 2.0, 1.5, 1.2, 1.1, 1.0]),
    (2, [2.75, 2.5, 2.25, 2.1, 2.05, 2.0]),
    (2.5, [2.88, 2.75, 2.62, 2.55, 2.52, 2.5]),
    (3, [3.0, 3.0, 3.0, 3.0, 3.0, 3.0]),
    (3.4, [3.1, 3.2, 3.3, 3.36, 3.38, 3.4]),
    (3.395, [3.1, 3.2, 3.3, 3.36, 3.38, 3.39]),
    (4, [3.25, 3.5, 3.75, 3.9, 3.95, 4.0]),
    (5, [3.5, 4.0, 4.5, 4.8, 4.9, 5.0]),
    (None, [2.25, 1.5, 0.75, 0.3, 0.15, 0.0]),
    ("x", [2.25, 1.5, 0.75, 0.3, 0.15, 0.0]),
    (float("nan"), [2.25, 1.5, 0.75, 0.3, 0.15, 0.0]),
    (float("inf"), [2.25, 1.5, 0.75, 0.3, 0.15, 0.0]),
    (float("-inf"), [2.25, 1.5, 0.75, 0.3, 0.15, 0.0]),
    (0, [2.5, 2.0, 1.5, 1.2, 1.1, 1.0]),
    (6, [3.5, 4.0, 4.5, 4.8, 4.9, 5.0]),
    (True, [2.25, 1.5, 0.75, 0.3, 0.15, 0.0]),
)
# The six dimension keys the frozen values were computed over, in order.
_FROZEN_WEIGHT_KEYS = [
    "differentiation_unmet_need", "scientific_credibility", "translational_path",
    "fundable_experiment", "venture_potential", "team_executability",
]


def _frozen_weighted_grid(keys):
    out = []
    for v, wants in _FROZEN_WEIGHTED_GRID:
        for k, want in enumerate(wants):
            scores = {kk: (v if j <= k else 3) for j, kk in enumerate(keys)}
            out.append((scores, want))
    return out


def test_weighted_score_is_todays_on_a_grid():
    keys = list(br.RUBRIC_WEIGHTS)
    assert keys == _FROZEN_WEIGHT_KEYS
    for scores, want in _frozen_weighted_grid(keys):
        assert br.weighted_score(scores) == want, scores
    # Keys match case- and whitespace-insensitively; empty input is 0.
    assert br.weighted_score({f" {k.upper()} ": 4 for k in keys}) == 4.0
    assert br.weighted_score(None) == 0.0
    assert br.weighted_score({}) == 0.0


def test_band_class_reproduces_both_template_palettes():
    assert [band_class(b, 700, 600) for b in ("advance", "conditional", "pass", None)] == [
        "text-green-700", "text-amber-700", "text-gray-600", "text-gray-600"]
    assert [band_class(b, 600, 400) for b in ("advance", "conditional", "pass", None)] == [
        "text-green-600", "text-amber-600", "text-gray-400", "text-gray-400"]


def test_band_label():
    assert band_label("pass", "decline") == "decline"
    assert band_label("advance", "decline") == "advance"
    assert band_label(None, "decline") is None


def test_no_band_comparisons_in_templates():
    offenders = [str(p.relative_to(REPO)) for p in (REPO / "templates").rglob("*.html")
                 if re.search(r"\bband\s*==", p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_registry_shape():
    assert [b.name for b in BANDS] == ["advance", "conditional", "pass"]
    assert [b.threshold_key for b in BANDS] == ["advance_min", "conditional_min", None]

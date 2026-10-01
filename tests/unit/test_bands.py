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

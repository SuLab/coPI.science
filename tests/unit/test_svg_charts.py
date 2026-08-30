"""Dependency-free SVG/HTML chart primitives (`src/services/svg_charts.py`).

Every renderer returns a plain `str` of already-escaped markup: a single
wrapping element (so the whole return value parses as one XML document via
`xml.etree.ElementTree`), an `<svg>` for the chart itself (except `stat_tile`,
which is plain HTML with no chart to draw), and a `<details>` "table view"
fallback for every function that renders a data series (all but `stat_tile`).

Baked-in rules under test: labels are escaped (a `<script>` label must never
appear as a live tag), bar widths are normalized to the row set's max value
so a doubled value yields an exactly doubled width, `meter` clamps its
fraction to `[0, 1]`, `sparkline` never divides by zero on a single point,
and `gantt` clamps every row's span to `[t0, t1]`.
"""

import xml.etree.ElementTree as ET

from src.services.svg_charts import (
    diverging_hbar,
    gantt,
    hbar_list,
    meter,
    sparkline,
    stacked_hbar,
    stat_tile,
)

_EVIL_LABEL = "<script>alert(1)</script>"


def _parse(markup: str) -> ET.Element:
    return ET.fromstring(markup)


def _find_svg(root: ET.Element) -> ET.Element:
    svg = root.find(".//{http://www.w3.org/2000/svg}svg")
    if svg is None:
        # No SVG namespace declared -- fall back to a bare tag lookup, which
        # still works because ElementTree tags un-namespaced markup as-is.
        svg = root.find(".//svg")
    assert svg is not None, markup_debug(root)
    return svg


def markup_debug(root: ET.Element) -> str:
    return ET.tostring(root, encoding="unicode")


# --------------------------------------------------------------------------
# stat_tile
# --------------------------------------------------------------------------


def test_stat_tile_is_well_formed():
    root = _parse(stat_tile("Active PIs", "12"))
    assert root.tag == "div"


def test_stat_tile_escapes_a_script_label():
    markup = stat_tile(_EVIL_LABEL, "12")
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup
    # And still parses as inert text, not a live element.
    root = _parse(markup)
    assert root.find(".//script") is None


def test_stat_tile_warn_flag_is_reflected_in_class():
    plain = stat_tile("Errors", "0")
    warn = stat_tile("Errors", "3", warn=True)
    assert "warn" not in plain
    assert "warn" in warn


def test_stat_tile_omits_note_when_absent():
    markup = stat_tile("Active PIs", "12")
    assert "sc-tile-note" not in markup


def test_stat_tile_includes_note_when_present():
    markup = stat_tile("Active PIs", "12", "up 2 since Monday")
    assert "up 2 since Monday" in markup


# --------------------------------------------------------------------------
# meter
# --------------------------------------------------------------------------


def test_meter_is_well_formed_svg():
    root = _parse(meter("Budget used", 0.4, "40 of 100 calls"))
    _find_svg(root)


def test_meter_escapes_a_script_label():
    markup = meter(_EVIL_LABEL, 0.5, "detail")
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


def test_meter_clamps_over_one_to_full():
    over = _parse(meter("x", 1.5, "detail"))
    full = _parse(meter("x", 1.0, "detail"))
    over_fill = _find_svg(over).find(".//rect[@class='sc-meter-fill']")
    full_fill = _find_svg(full).find(".//rect[@class='sc-meter-fill']")
    assert float(over_fill.get("width")) == float(full_fill.get("width"))


def test_meter_clamps_under_zero_to_empty():
    under = _parse(meter("x", -0.5, "detail"))
    fill = _find_svg(under).find(".//rect[@class='sc-meter-fill']")
    assert float(fill.get("width")) == 0.0


def test_meter_has_a_table_fallback():
    markup = meter("Budget used", 0.4, "40 of 100 calls")
    assert "<details" in markup
    assert "table view" in markup


# --------------------------------------------------------------------------
# hbar_list
# --------------------------------------------------------------------------


def test_hbar_list_is_well_formed_svg():
    root = _parse(hbar_list([("a", 10.0, "10"), ("b", 20.0, "20")]))
    _find_svg(root)


def test_hbar_list_escapes_a_script_label():
    markup = hbar_list([(_EVIL_LABEL, 10.0, "10")])
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


def test_hbar_list_normalizes_widths_to_the_max_value():
    root = _parse(hbar_list([("a", 10.0, "10"), ("b", 20.0, "20")]))
    rects = _find_svg(root).findall(".//rect")
    assert len(rects) == 2
    w0 = float(rects[0].get("width"))
    w1 = float(rects[1].get("width"))
    assert w0 > 0
    assert w1 == w0 * 2


def test_hbar_list_handles_all_zero_values_without_dividing_by_zero():
    root = _parse(hbar_list([("a", 0.0, "0"), ("b", 0.0, "0")]))
    rects = _find_svg(root).findall(".//rect")
    assert all(float(r.get("width")) == 0.0 for r in rects)


def test_hbar_list_has_a_title_per_mark():
    root = _parse(hbar_list([("a", 10.0, "ten")]))
    titles = _find_svg(root).findall(".//title")
    assert len(titles) == 1
    assert "ten" in titles[0].text


def test_hbar_list_has_a_table_fallback_with_every_row():
    markup = hbar_list([("alpha", 10.0, "10"), ("beta", 20.0, "20")])
    assert "<details" in markup
    assert "alpha" in markup
    assert "beta" in markup


def test_hbar_list_accepts_a_custom_color():
    root = _parse(hbar_list([("a", 10.0, "10")], color="#1baf7a"))
    rect = _find_svg(root).find(".//rect")
    assert rect.get("fill") == "#1baf7a"


# --------------------------------------------------------------------------
# stacked_hbar
# --------------------------------------------------------------------------


def test_stacked_hbar_is_well_formed_svg():
    root = _parse(
        stacked_hbar("Domains", [("met", 3.0, "#2a78d6"), ("unmet", 1.0, "#e34948")])
    )
    _find_svg(root)


def test_stacked_hbar_escapes_a_script_label():
    markup = stacked_hbar(_EVIL_LABEL, [(_EVIL_LABEL, 1.0, "#2a78d6")])
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


def test_stacked_hbar_leaves_two_pixel_gaps_between_segments():
    root = _parse(
        stacked_hbar("Domains", [("a", 1.0, "#2a78d6"), ("b", 1.0, "#eb6834")])
    )
    rects = _find_svg(root).findall(".//rect")
    assert len(rects) == 2
    first_end = float(rects[0].get("x")) + float(rects[0].get("width"))
    second_start = float(rects[1].get("x"))
    assert round(second_start - first_end, 2) == 2.0


def test_stacked_hbar_has_a_title_per_segment():
    root = _parse(
        stacked_hbar("Domains", [("met", 3.0, "#2a78d6"), ("unmet", 1.0, "#e34948")])
    )
    titles = _find_svg(root).findall(".//title")
    assert len(titles) == 2


def test_stacked_hbar_has_a_table_fallback():
    markup = stacked_hbar("Domains", [("met", 3.0, "#2a78d6")])
    assert "<details" in markup
    assert "met" in markup


# --------------------------------------------------------------------------
# diverging_hbar
# --------------------------------------------------------------------------


def test_diverging_hbar_is_well_formed_svg():
    root = _parse(diverging_hbar("Panel", 1.0, 2.0, 4.0, ("blocking", "gap", "adequate")))
    _find_svg(root)


def test_diverging_hbar_uses_the_pinned_palette():
    root = _parse(diverging_hbar("Panel", 1.0, 2.0, 4.0, ("blocking", "gap", "adequate")))
    rects = _find_svg(root).findall(".//rect")
    fills = [r.get("fill") for r in rects]
    assert fills == ["#e34948", "#f0efec", "#2a78d6"]


def test_diverging_hbar_escapes_a_script_label():
    markup = diverging_hbar(_EVIL_LABEL, 1.0, 1.0, 1.0, (_EVIL_LABEL, "gap", "adequate"))
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


def test_diverging_hbar_has_a_table_fallback():
    markup = diverging_hbar("Panel", 1.0, 2.0, 4.0, ("blocking", "gap", "adequate"))
    assert "<details" in markup
    assert "blocking" in markup


# --------------------------------------------------------------------------
# sparkline
# --------------------------------------------------------------------------


def test_sparkline_is_well_formed_svg():
    root = _parse(sparkline([1.0, 2.0, 3.0, 2.0]))
    _find_svg(root)


def test_sparkline_single_point_does_not_divide_by_zero():
    # Must not raise, and must still produce a well-formed, non-empty chart.
    markup = sparkline([5.0])
    root = _parse(markup)
    svg = _find_svg(root)
    assert len(svg.findall(".//circle")) == 1


def test_sparkline_flat_series_does_not_divide_by_zero():
    markup = sparkline([3.0, 3.0, 3.0])
    root = _parse(markup)
    _find_svg(root)


def test_sparkline_empty_series_does_not_raise():
    markup = sparkline([])
    root = _parse(markup)
    _find_svg(root)


def test_sparkline_respects_custom_dimensions():
    root = _parse(sparkline([1.0, 2.0], width=100, height=30))
    svg = _find_svg(root)
    assert svg.get("width") == "100"
    assert svg.get("height") == "30"


def test_sparkline_has_a_title_per_mark():
    root = _parse(sparkline([1.0, 2.0, 3.0]))
    titles = _find_svg(root).findall(".//title")
    assert len(titles) == 3


def test_sparkline_has_a_table_fallback():
    markup = sparkline([1.0, 2.0, 3.0])
    assert "<details" in markup
    assert "table view" in markup


# --------------------------------------------------------------------------
# gantt
# --------------------------------------------------------------------------


def test_gantt_is_well_formed_svg():
    root = _parse(gantt([("phase1", 0.0, 5.0, "#2a78d6", "Phase 1")], 0.0, 10.0))
    _find_svg(root)


def test_gantt_escapes_a_script_label():
    markup = gantt([(_EVIL_LABEL, 0.0, 5.0, "#2a78d6", _EVIL_LABEL)], 0.0, 10.0)
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


def test_gantt_clamps_spans_to_t0_t1():
    # A row that starts before t0 and ends after t1 must be clamped to the
    # full [t0, t1] width, not overflow it.
    root = _parse(gantt([("row", -5.0, 50.0, "#2a78d6", "t")], 0.0, 10.0))
    rect = _find_svg(root).find(".//rect")
    x = float(rect.get("x"))
    w = float(rect.get("width"))
    assert x >= 0.0
    # The svg's own viewBox width is the scale's full extent.
    full_width = float(_find_svg(root).get("width"))
    assert round(x + w, 2) == round(full_width, 2)


def test_gantt_a_row_entirely_before_t0_collapses_to_zero_width():
    root = _parse(gantt([("row", -20.0, -10.0, "#2a78d6", "t")], 0.0, 10.0))
    rect = _find_svg(root).find(".//rect")
    assert float(rect.get("x")) == 0.0
    assert float(rect.get("width")) == 0.0


def test_gantt_has_a_title_per_row():
    root = _parse(
        gantt(
            [
                ("row1", 0.0, 5.0, "#2a78d6", "one"),
                ("row2", 2.0, 8.0, "#eb6834", "two"),
            ],
            0.0,
            10.0,
        )
    )
    titles = _find_svg(root).findall(".//title")
    assert len(titles) == 2


def test_gantt_has_a_table_fallback():
    markup = gantt([("phase1", 0.0, 5.0, "#2a78d6", "Phase 1")], 0.0, 10.0)
    assert "<details" in markup
    assert "phase1" in markup

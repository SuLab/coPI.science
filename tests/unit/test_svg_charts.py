"""Chart primitives (`src/services/svg_charts.py`).

Contract: every renderer returns ONE well-formed fragment; every value is
visible as text (never tooltip-only); every bar chart has an axis with a 0
tick, a midpoint tick and a max tick plus a unit; widths are percentages of
the caller's scale; labels are escaped; text never sits inside a coloured
fill.
"""
import re
import xml.etree.ElementTree as ET

from src.services.display_format import money, whole
from src.services.svg_charts import (
    DIVERGING_MID,
    DIVERGING_NEG,
    DIVERGING_POS,
    diverging_hbar,
    gantt,
    hbar_list,
    legend,
    line_chart,
    meter,
    stacked_hbar,
    stat_tile,
    table_twin,
)

EVIL = "<script>alert(1)</script>"


def parse(markup: str) -> ET.Element:
    return ET.fromstring(markup)


def texts(svg: ET.Element, cls: str | None = None) -> list[str]:
    return [t.text for t in svg.findall(".//{*}text") if cls is None or t.get("class", "").startswith(cls)]


def widths(markup: str, cls: str) -> list[float]:
    return [float(w) for w in re.findall(rf'class="{cls}" style="width:([0-9.]+)%', markup)]


def test_stat_tile_markup_warn_and_escaping():
    m = stat_tile("Total cost", "$31.18", "Run lifetime.", warn=True)
    parse(m)
    assert '<div class="sc-tile-value">$31.18</div>' in m and "sc-tile--warn" in m and "Run lifetime." in m
    e = stat_tile(EVIL, "1")
    assert "<script>" not in e and "&lt;script&gt;" in e


def test_meter_shows_label_value_and_detail_as_text():
    m = meter("Cache hit rate", 0.2894, "434,239 of 1,500,000 input tokens served from cache")
    parse(m)
    assert "Cache hit rate" in m and '<div class="sc-tile-value">29%</div>' in m and "served from cache" in m


def test_meter_clamps_fill_but_keeps_true_value_text():
    m = meter("Progress", 1.07, "1h 04m of 1h 00m", value_text="107%")
    root = parse(m)
    fill = root.find(".//{*}rect[@class='sc-meter-fill']")
    track = root.find(".//{*}rect[@class='sc-meter-track']")
    assert float(fill.get("width")) == float(track.get("width"))
    assert "107%" in m
    under = parse(meter("x", -0.5, "d")).find(".//{*}rect[@class='sc-meter-fill']")
    assert float(under.get("width")) == 0.0


def test_legend_one_swatch_per_item_escaped():
    m = legend([("input", "#2a78d6"), (EVIL, "#eb6834")])
    parse(m)
    assert m.count("sc-swatch") == 2 and "&lt;script&gt;" in m


def test_hbar_list_labels_values_and_numeric_axis_are_visible():
    m = hbar_list([("blackbird", 25.12, "$25.12 (7 calls)"), ("coller", 1.07, "$1.07 (7 calls)")],
                  unit="US$", axis_fmt=money)
    parse(m)
    assert "blackbird" in m and "$25.12 (7 calls)" in m and "coller" in m
    assert '<span class="sc-tick sc-tick--0">$0.00</span>' in m
    assert '<span class="sc-tick sc-tick--mid">$12.56</span>' in m
    assert '<span class="sc-tick sc-tick--max">$25.12</span>' in m
    assert '<span class="sc-axis-unit">US$</span>' in m


def test_hbar_list_widths_are_percent_of_max_and_zero_safe():
    assert widths(hbar_list([("a", 10.0, "10"), ("b", 20.0, "20")], unit="calls", axis_fmt=whole), "sc-hbar-fill") == [50.0, 100.0]
    assert widths(hbar_list([("a", 0.0, "0"), ("b", 0.0, "0")], unit="calls", axis_fmt=whole), "sc-hbar-fill") == [0.0, 0.0]


def test_hbar_list_escapes_and_has_one_keyed_table_twin():
    m = hbar_list([(EVIL, 1.0, "1")], unit="calls", axis_fmt=whole, caption="Cost by agent")
    assert "<script>" not in m and m.count("<details") == 1
    assert 'data-sc-key="Cost by agent"' in m and "<caption>Cost by agent</caption>" in m


def test_stacked_hbar_shared_scale_visible_values_and_no_text_in_segments():
    m = stacked_hbar("Sep 9 18:00", [("input", 30.0, "#2a78d6"), ("output", 20.0, "#eb6834")],
                     scale_max=100.0, value_fmt=whole)
    parse(m)
    assert widths(m, "sc-stack-seg") == [30.0, 20.0]
    assert '<span class="sc-stack-total">50</span>' in m
    values = m[m.index('class="sc-stack-values"'):]
    assert ">30</span>" in values and ">20</span>" in values
    assert re.search(r'class="sc-stack-seg"[^>]*>[^<]', m) is None      # segments are empty


def test_stacked_hbar_table_flag():
    assert "<details" not in stacked_hbar("h", [("a", 1.0, "#2a78d6")], scale_max=1.0, table=False)
    assert "<details" in stacked_hbar("h", [("a", 1.0, "#2a78d6")], scale_max=1.0)


def test_diverging_hbar_pinned_palette_and_counts():
    m = diverging_hbar("budget", 1, 2, 4, ("blocking", "gap", "adequate"), scale_max=7)
    fills = re.findall(r'class="sc-stack-seg" style="width:[0-9.]+%;background:(#[0-9a-f]{6})"', m)
    assert fills == [DIVERGING_NEG, DIVERGING_MID, DIVERGING_POS] == ["#e34948", "#a8a29e", "#2a78d6"]
    values = m[m.index('class="sc-stack-values"'):]
    assert ">1</span>" in values and ">2</span>" in values and ">4</span>" in values


def test_line_chart_has_axes_ticks_end_label_and_x_labels():
    m = line_chart([("18:00", 4.52), ("19:00", 29.95), ("20:00", 31.18)], unit="US$", value_fmt=money)
    svg = parse(m).find(".//{*}svg")
    ticks = texts(svg, "sc-tick")
    assert "$0.00" in ticks and "$15.59" in ticks and "$31.18" in ticks
    assert "18:00" in ticks and "20:00" in ticks
    assert "$31.18" in texts(svg, "sc-point-label")
    assert len(svg.findall(".//{*}circle")) == 3
    assert "<details" in m and "19:00" in m
    assert 'data-sc-key="US$"' in m   # defaults to the unit
    keyed = line_chart([("18:00", 4.52)], unit="US$", value_fmt=money, key="cumulative-cost")
    assert 'data-sc-key="cumulative-cost"' in keyed and 'data-sc-key="US$"' not in keyed


def test_line_chart_none_point_is_hollow_marker_at_top_with_labels():
    m = line_chart([("10", 2.0), ("11", None), ("12", 5.0)], unit="ratio",
                   value_fmt=lambda v: f"{v:.2f}", none_label="∞", none_table_label="∞ — no lab tokens this hour")
    svg = parse(m).find(".//{*}svg")
    hollow = svg.findall(".//{*}circle[@class='sc-none-marker']")
    assert len(hollow) == 1 and hollow[0].get("fill") == "none"
    assert "∞" in texts(svg, "sc-point-label")
    assert "∞ — no lab tokens this hour" in m
    assert "5.00" in texts(svg, "sc-tick") and "2.00" not in texts(svg, "sc-tick")


def test_line_chart_degenerate_series_do_not_raise_and_keep_distinct_ticks():
    for pts in ([], [("a", 5.0)], [("a", 3.0), ("b", 3.0)], [("a", None)], [("a", 0.0), ("b", 0.0)]):
        root = parse(line_chart(pts, unit="x", value_fmt=whole))
        svg = root.find(".//{*}svg")
        ys = {t.get("y") for t in svg.findall(".//{*}text") if t.get("class") == "sc-tick"}
        assert len(ys) == 3, pts   # 0 / mid / max gridline labels never collapse onto one line


def test_gantt_rows_axis_legend_and_clamping():
    m = gantt(
        [("velculescu", 0.0, 50.0, "#eb6834", "conditional — $4.70"), ("late", 80.0, 200.0, "#2a78d6", "t")],
        0.0, 100.0, tick_fmt=lambda t: f"T{t:.0f}", legend_items=[("announced", "#eb6834"), ("not announced", "#2a78d6")],
    )
    parse(m)
    assert "velculescu" in m and "announced" in m
    for tick in ("T0", "T25", "T50", "T75", "T100"):
        assert f">{tick}</span>" in m
    bars = re.findall(r'class="sc-gantt-bar" style="left:([0-9.]+)%;width:([0-9.]+)%', m)
    assert bars[0] == ("0.0", "50.0")
    assert float(bars[1][0]) + float(bars[1][1]) == 100.0
    assert "50s" in m  # duration column


def test_gantt_escapes_and_zero_span():
    m = gantt([(EVIL, -5.0, -1.0, "#2a78d6", EVIL)], 0.0, 10.0, tick_fmt=str, legend_items=[])
    assert "<script>" not in m
    assert re.search(r'class="sc-gantt-bar" style="left:0.0%;width:0.0%', m)


def test_table_twin_is_keyed_and_escaped():
    m = table_twin(["Domain", "blocking"], [[EVIL, "1"]], caption="Specialist mix", key="specialist-mix")
    parse(m)
    assert 'data-sc-key="specialist-mix"' in m and "<thead>" in m and "&lt;script&gt;" in m

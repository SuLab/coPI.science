"""Dependency-free SVG/HTML chart primitives for the simulation control panel.

Every renderer takes plain Python values and returns a plain `str` of
already-escaped markup -- no template engine, no client-side JS, and no
dependency beyond the standard library plus `markupsafe.escape` (already a
transitive dependency via Jinja2, so this module adds nothing new to
`pyproject.toml`).

Rules baked in throughout, taken from the dataviz skill's reference palette
and house style:

* **One axis.** Every chart here is a single bar/line scale; there is no
  chart that plots two independent axes against each other.
* **Thin marks, 2px gaps.** Bars are drawn thin, and `stacked_hbar` /
  `diverging_hbar` separate adjacent segments by exactly 2 pixels rather than
  scaling the gap into the value-encoded width.
* **Text in text tokens, never series colors.** Labels, captions and table
  fallback text always render in `TEXT_PRIMARY`/`TEXT_MUTED`; the palette
  colors are reserved for the data marks themselves (bars, segments, points).
* **A `<title>` per mark.** Every individual bar/segment/point carries its
  own `<title>` child -- the no-JS floor for a hover tooltip.
* **A `<details>` table fallback.** Every chart that renders a data series
  (everything except `stat_tile`, which has no series to tabulate) is paired
  with a `<details><summary>table view</summary>...</details>` holding the
  same data as plain text/table markup, so nothing here is graphical-only.

Colors are pinned to the values below; do not invent additional hexes.
"""

from markupsafe import escape

# Categorical palette (light surface), 7 slots.
CATEGORICAL_COLORS: tuple[str, ...] = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#4a3aa7",
)

# Sequential palette: tints of categorical slot 1 (blue), light to full.
SEQUENTIAL_COLORS: tuple[str, ...] = (
    "#d7e6f7",
    "#aecbef",
    "#7fabe3",
    "#4f8bd9",
    "#2a78d6",
)

# Diverging pair, pre-validated (dataviz reference palette): red = blocking,
# blue = adequate, with a neutral gray midpoint for gap. These three hexes
# are the only ones `diverging_hbar` ever emits.
DIVERGING_NEG = "#e34948"  # blocking
DIVERGING_MID = "#f0efec"  # gap
DIVERGING_POS = "#2a78d6"  # adequate

# Text tokens. Labels/values/notes always render in these, never in a series
# color -- the palette above is reserved for data marks.
TEXT_PRIMARY = "#1a1a1a"
TEXT_MUTED = "#6b6b6b"

_DEFAULT_BAR_COLOR = CATEGORICAL_COLORS[0]

# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

_HBAR_MAX_WIDTH = 220
_HBAR_HEIGHT = 14
_HBAR_ROW_GAP = 4

_STACK_MAX_WIDTH = 220
_STACK_HEIGHT = 16
_STACK_GAP = 2

_METER_TRACK_WIDTH = 200
_METER_HEIGHT = 10

_GANTT_MAX_WIDTH = 320
_GANTT_ROW_HEIGHT = 14
_GANTT_ROW_GAP = 8
_GANTT_BAR_THICKNESS = 8


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _details_table(rows_html: str, *, caption: str | None = None) -> str:
    caption_html = f"<caption>{escape(caption)}</caption>" if caption else ""
    return (
        '<details class="sc-chart-fallback"><summary>table view</summary>'
        f"<table>{caption_html}<tbody>{rows_html}</tbody></table>"
        "</details>"
    )


# --------------------------------------------------------------------------
# stat_tile
# --------------------------------------------------------------------------


def stat_tile(label: str, value: str, note: str | None = None, *, warn: bool = False) -> str:
    """A single KPI tile: a muted label, a big value, and an optional note.

    No series data to plot, so no `<details>` fallback -- everything here is
    already plain text.
    """
    classes = "sc-tile sc-tile--warn" if warn else "sc-tile"
    note_html = f'<div class="sc-tile-note">{escape(note)}</div>' if note else ""
    return (
        f'<div class="{classes}">'
        f'<div class="sc-tile-label">{escape(label)}</div>'
        f'<div class="sc-tile-value">{escape(value)}</div>'
        f"{note_html}"
        "</div>"
    )


# --------------------------------------------------------------------------
# meter
# --------------------------------------------------------------------------


def meter(label: str, fraction: float, detail: str) -> str:
    """A single-track proportional bar, clamped to `[0, 1]`."""
    clamped = _clamp(fraction, 0.0, 1.0)
    fill_width = round(_METER_TRACK_WIDTH * clamped, 2)
    svg = (
        f'<svg class="sc-meter" viewBox="0 0 {_METER_TRACK_WIDTH} {_METER_HEIGHT}" '
        f'width="{_METER_TRACK_WIDTH}" height="{_METER_HEIGHT}" role="img" '
        f'aria-label="{escape(label)}">'
        f'<rect class="sc-meter-track" x="0" y="0" width="{_METER_TRACK_WIDTH}" '
        f'height="{_METER_HEIGHT}" fill="{DIVERGING_MID}"/>'
        f'<rect class="sc-meter-fill" x="0" y="0" width="{fill_width}" '
        f'height="{_METER_HEIGHT}" fill="{_DEFAULT_BAR_COLOR}">'
        f"<title>{escape(detail)}</title>"
        "</rect>"
        "</svg>"
    )
    rows_html = (
        f'<tr><th scope="row">{escape(label)}</th>'
        f"<td>{clamped:.0%}</td>"
        f"<td>{escape(detail)}</td></tr>"
    )
    return f'<div class="sc-chart sc-chart-meter">{svg}{_details_table(rows_html)}</div>'


# --------------------------------------------------------------------------
# hbar_list
# --------------------------------------------------------------------------


def hbar_list(rows: list[tuple[str, float, str]], *, color: str = _DEFAULT_BAR_COLOR) -> str:
    """A list of horizontal bars, one per row, widths normalized to the max value."""
    max_value = max((value for _, value, _ in rows), default=0.0)
    row_height = _HBAR_HEIGHT + _HBAR_ROW_GAP
    total_height = row_height * len(rows) if rows else _HBAR_HEIGHT

    bars = []
    table_rows = []
    for i, (label, value, display) in enumerate(rows):
        width = 0.0 if max_value <= 0 else (value / max_value) * _HBAR_MAX_WIDTH
        y = i * row_height
        bars.append(
            f'<rect x="0" y="{y}" width="{round(width, 2)}" height="{_HBAR_HEIGHT}" '
            f'fill="{escape(color)}">'
            f"<title>{escape(label)}: {escape(display)}</title>"
            "</rect>"
        )
        table_rows.append(
            f'<tr><th scope="row">{escape(label)}</th><td>{value}</td>'
            f"<td>{escape(display)}</td></tr>"
        )

    svg = (
        f'<svg class="sc-hbar-list" viewBox="0 0 {_HBAR_MAX_WIDTH} {total_height}" '
        f'width="{_HBAR_MAX_WIDTH}" height="{total_height}" role="img">'
        + "".join(bars)
        + "</svg>"
    )
    return (
        f'<div class="sc-chart sc-chart-hbar">{svg}{_details_table("".join(table_rows))}</div>'
    )


# --------------------------------------------------------------------------
# stacked_hbar
# --------------------------------------------------------------------------


def stacked_hbar(label: str, segments: list[tuple[str, float, str]]) -> str:
    """A single row of stacked segments, separated by a fixed 2px gap."""
    total = sum(value for _, value, _ in segments)
    gap_total = _STACK_GAP * max(len(segments) - 1, 0)
    drawable = max(_STACK_MAX_WIDTH - gap_total, 0)

    rects = []
    table_rows = []
    x = 0.0
    for seg_label, value, hex_color in segments:
        width = 0.0 if total <= 0 else (value / total) * drawable
        rects.append(
            f'<rect x="{round(x, 2)}" y="0" width="{round(width, 2)}" '
            f'height="{_STACK_HEIGHT}" fill="{escape(hex_color)}">'
            f"<title>{escape(seg_label)}: {value}</title>"
            "</rect>"
        )
        table_rows.append(f'<tr><th scope="row">{escape(seg_label)}</th><td>{value}</td></tr>')
        x += width + _STACK_GAP

    svg = (
        f'<svg class="sc-stacked-hbar" viewBox="0 0 {_STACK_MAX_WIDTH} {_STACK_HEIGHT}" '
        f'width="{_STACK_MAX_WIDTH}" height="{_STACK_HEIGHT}" role="img" '
        f'aria-label="{escape(label)}">' + "".join(rects) + "</svg>"
    )
    table = _details_table("".join(table_rows), caption=label)
    return f'<div class="sc-chart sc-chart-stacked">{svg}{table}</div>'


# --------------------------------------------------------------------------
# diverging_hbar
# --------------------------------------------------------------------------


def diverging_hbar(
    label: str,
    neg: float,
    mid: float,
    pos: float,
    labels: tuple[str, str, str],
) -> str:
    """A three-segment bar on the pinned blocking/gap/adequate palette.

    `labels` is `(blocking_label, gap_label, adequate_label)`, matched
    positionally to `(neg, mid, pos)`. Implemented as a `stacked_hbar` with
    the three segment colors fixed to the diverging pair plus its neutral
    midpoint, so the two share their gap/tooltip/fallback-table behavior.
    """
    segments = [
        (labels[0], max(neg, 0.0), DIVERGING_NEG),
        (labels[1], max(mid, 0.0), DIVERGING_MID),
        (labels[2], max(pos, 0.0), DIVERGING_POS),
    ]
    return stacked_hbar(label, segments)


# --------------------------------------------------------------------------
# sparkline
# --------------------------------------------------------------------------


def sparkline(points: list[float], *, width: int = 240, height: int = 40) -> str:
    """A thin line with a small marker circle per point; safe on 0 or 1 points."""
    n = len(points)
    if n == 0:
        svg = (
            f'<svg class="sc-sparkline" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img"/>'
        )
        return f'<div class="sc-chart sc-chart-sparkline">{svg}{_details_table("")}</div>'

    lo = min(points)
    hi = max(points)
    span = hi - lo

    def _xy(i: int, value: float) -> tuple[float, float]:
        x = width / 2 if n == 1 else (i / (n - 1)) * width
        y = height / 2 if span <= 0 else height - ((value - lo) / span) * height
        return round(x, 2), round(y, 2)

    coords = [_xy(i, v) for i, v in enumerate(points)]

    polyline = ""
    if n > 1:
        points_attr = " ".join(f"{x},{y}" for x, y in coords)
        polyline = (
            f'<polyline points="{points_attr}" fill="none" '
            f'stroke="{_DEFAULT_BAR_COLOR}" stroke-width="1.5"/>'
        )

    marks = "".join(
        f'<circle cx="{x}" cy="{y}" r="1.5" fill="{_DEFAULT_BAR_COLOR}">'
        f"<title>{escape(points[i])}</title>"
        "</circle>"
        for i, (x, y) in enumerate(coords)
    )
    svg = (
        f'<svg class="sc-sparkline" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img">' + polyline + marks + "</svg>"
    )
    table_rows = "".join(
        f'<tr><th scope="row">{i}</th><td>{v}</td></tr>' for i, v in enumerate(points)
    )
    return f'<div class="sc-chart sc-chart-sparkline">{svg}{_details_table(table_rows)}</div>'


# --------------------------------------------------------------------------
# gantt
# --------------------------------------------------------------------------


def gantt(rows: list[tuple[str, float, float, str, str]], t0: float, t1: float) -> str:
    """One thin bar per row on a shared `[t0, t1]` scale; spans are clamped to it."""
    span = t1 - t0
    row_height = _GANTT_ROW_HEIGHT + _GANTT_ROW_GAP
    total_height = row_height * len(rows) if rows else _GANTT_ROW_HEIGHT

    def _x(value: float) -> float:
        if span <= 0:
            return 0.0
        clamped = _clamp(value, t0, t1)
        return ((clamped - t0) / span) * _GANTT_MAX_WIDTH

    bars = []
    table_rows = []
    for i, (label, start, end, hex_color, title) in enumerate(rows):
        x_start = _x(start)
        x_end = _x(end)
        left = min(x_start, x_end)
        bar_width = abs(x_end - x_start)
        y = i * row_height
        bars.append(
            f'<g class="sc-gantt-row">'
            f'<text x="0" y="{y + _GANTT_BAR_THICKNESS}" class="sc-chart-label" '
            f'fill="{TEXT_PRIMARY}">{escape(label)}</text>'
            f'<rect x="{round(left, 2)}" y="{y + _GANTT_BAR_THICKNESS + 2}" '
            f'width="{round(bar_width, 2)}" height="{_GANTT_BAR_THICKNESS}" '
            f'fill="{escape(hex_color)}">'
            f"<title>{escape(title)}</title>"
            "</rect>"
            "</g>"
        )
        table_rows.append(
            f'<tr><th scope="row">{escape(label)}</th><td>{start}</td><td>{end}</td>'
            f"<td>{escape(title)}</td></tr>"
        )

    svg = (
        f'<svg class="sc-gantt" viewBox="0 0 {_GANTT_MAX_WIDTH} {total_height}" '
        f'width="{_GANTT_MAX_WIDTH}" height="{total_height}" role="img">'
        + "".join(bars)
        + "</svg>"
    )
    return f'<div class="sc-chart sc-chart-gantt">{svg}{_details_table("".join(table_rows))}</div>'

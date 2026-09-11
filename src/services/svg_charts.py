"""Dependency-free HTML/SVG chart primitives for the simulation control panel.

Contract (tests/unit/test_svg_charts.py): every renderer returns ONE
well-formed, already-escaped fragment. Every value is VISIBLE as text; the
per-mark `title` tooltip and the `<details>` table twin are additions, never
the only place a number lives. Every bar chart draws an axis with 0 / mid /
max ticks and names its unit; every multi-series chart has a legend; text
never wears a series colour and never sits inside a coloured fill (stacked
segments print their values in a text line beside the bar).

Colours are pinned here and nowhere else (dataviz reference palette; the
diverging midpoint was darkened from #f0efec to #a8a29e on 2026-09-11
because the lighter step was invisible on a white card).
"""
from __future__ import annotations

from collections.abc import Callable
from math import ceil

from markupsafe import escape

from src.services.display_format import compact, duration, percent, whole

CATEGORICAL_COLORS: tuple[str, ...] = (
    "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7",
)
SEQUENTIAL_COLORS: tuple[str, ...] = ("#d7e6f7", "#aecbef", "#7fabe3", "#4f8bd9", "#2a78d6")
DIVERGING_NEG = "#e34948"
DIVERGING_MID = "#a8a29e"
DIVERGING_POS = "#2a78d6"
METER_TRACK = SEQUENTIAL_COLORS[0]
AXIS = "#6b7280"
GRID = "#e5e7eb"
TEXT_PRIMARY = "#1f2937"
TEXT_SECONDARY = "#4b5563"

_METER_W, _METER_H = 200, 10


def _pct(part: float, whole_: float) -> float:
    if whole_ <= 0:
        return 0.0
    return round(max(0.0, min(1.0, part / whole_)) * 100, 1)


def _details(inner_table: str, *, key: str) -> str:
    return (
        f'<details class="sc-chart-fallback" data-sc-key="{escape(key)}">'
        f"<summary>Show as table</summary>{inner_table}</details>"
    )


def table_twin(head: list[str], rows: list[list[str]], *, caption: str, key: str) -> str:
    thead = "<thead><tr>" + "".join(f"<th>{escape(h)}</th>" for h in head) + "</tr></thead>"
    tbody = "".join(
        "<tr>" + f'<th scope="row">{escape(r[0])}</th>' + "".join(f"<td>{escape(c)}</td>" for c in r[1:]) + "</tr>"
        for r in rows
    )
    return _details(f"<table><caption>{escape(caption)}</caption>{thead}<tbody>{tbody}</tbody></table>", key=key)


def stat_tile(label: str, value: str, note: str | None = None, *, warn: bool = False) -> str:
    cls = "sc-tile sc-tile--warn" if warn else "sc-tile"
    note_html = f'<div class="sc-tile-note">{escape(note)}</div>' if note else ""
    return (f'<div class="{cls}"><div class="sc-tile-label">{escape(label)}</div>'
            f'<div class="sc-tile-value">{escape(value)}</div>{note_html}</div>')


def meter(label: str, fraction: float, detail: str, *, value_text: str | None = None, warn: bool = False) -> str:
    clamped = max(0.0, min(1.0, fraction))
    fill_w = f"{_METER_W * clamped:g}"
    shown = value_text if value_text is not None else percent(clamped)
    cls = "sc-tile sc-tile--meter" + (" sc-tile--warn" if warn else "")
    svg = (f'<svg class="sc-meter" viewBox="0 0 {_METER_W} {_METER_H}" width="{_METER_W}" height="{_METER_H}" '
           f'role="img" aria-label="{escape(label)} {escape(shown)}">'
           f'<rect class="sc-meter-track" x="0" y="0" width="{_METER_W}" height="{_METER_H}" fill="{METER_TRACK}"/>'
           f'<rect class="sc-meter-fill" x="0" y="0" width="{fill_w}" height="{_METER_H}" fill="{CATEGORICAL_COLORS[0]}">'
           f"<title>{escape(detail)}</title></rect></svg>")
    return (f'<div class="{cls}"><div class="sc-tile-label">{escape(label)}</div>'
            f'<div class="sc-tile-value">{escape(shown)}</div>{svg}<div class="sc-tile-note">{escape(detail)}</div></div>')


def legend(items: list[tuple[str, str]]) -> str:
    spans = "".join(
        f'<span class="sc-legend-item"><i class="sc-swatch" style="background:{escape(h)}"></i>{escape(label)}</span>'
        for label, h in items
    )
    return f'<div class="sc-legend">{spans}</div>'


def _axis(unit: str, max_value: float, axis_fmt: Callable[[float], str]) -> str:
    return ('<div class="sc-axis" aria-hidden="true"><span class="sc-axis-label"></span><span class="sc-axis-track">'
            f'<span class="sc-tick sc-tick--0">{escape(axis_fmt(0.0))}</span>'
            f'<span class="sc-tick sc-tick--mid">{escape(axis_fmt(max_value / 2))}</span>'
            f'<span class="sc-tick sc-tick--max">{escape(axis_fmt(max_value))}</span></span>'
            f'<span class="sc-axis-unit">{escape(unit)}</span></div>')


def hbar_list(rows: list[tuple[str, float, str]], *, unit: str, axis_fmt: Callable[[float], str],
              color: str = CATEGORICAL_COLORS[0], caption: str | None = None) -> str:
    max_value = max((v for _, v, _ in rows), default=0.0)
    body = []
    for label, value, display in rows:
        body.append('<div class="sc-hbar-row">'
                    f'<span class="sc-hbar-label">{escape(label)}</span>'
                    f'<span class="sc-hbar-track"><span class="sc-hbar-fill" style="width:{_pct(value, max_value)}%;background:{escape(color)}" '
                    f'title="{escape(label)}: {escape(display)}"></span></span>'
                    f'<span class="sc-hbar-value">{escape(display)}</span></div>')
    twin = table_twin(["Row", unit], [[label, display] for label, _, display in rows],
                      caption=caption or "Values", key=caption or "hbar")
    return f'<div class="sc-chart sc-hbar">{"".join(body)}{_axis(unit, max_value, axis_fmt)}{twin}</div>'


def stacked_hbar(label: str, segments: list[tuple[str, float, str]], *, scale_max: float,
                 value_fmt: Callable[[float], str] = compact, table: bool = True) -> str:
    total = sum(v for _, v, _ in segments)
    segs = "".join(
        f'<span class="sc-stack-seg" style="width:{_pct(v, scale_max)}%;background:{escape(h)}" '
        f'title="{escape(seg_label)}: {escape(value_fmt(v))}"></span>'
        for seg_label, v, h in segments
    )
    values = "".join(
        f'<span class="sc-legend-item"><i class="sc-swatch" style="background:{escape(h)}"></i>{escape(value_fmt(v))}</span>'
        for _, v, h in segments
    )
    twin = table_twin(["Series", "Value"], [[s, value_fmt(v)] for s, v, _ in segments], caption=label, key=label) if table else ""
    return (f'<div class="sc-chart sc-stack">'
            f'<div class="sc-stack-bar" role="img" aria-label="{escape(label)}: {escape(value_fmt(total))}">{segs}</div>'
            f'<span class="sc-stack-total">{escape(value_fmt(total))}</span>'
            f'<div class="sc-stack-values">{values}</div>{twin}</div>')


def diverging_hbar(label: str, neg: float, mid: float, pos: float, labels: tuple[str, str, str], *,
                   scale_max: float, table: bool = True) -> str:
    return stacked_hbar(label, [(labels[0], max(neg, 0.0), DIVERGING_NEG), (labels[1], max(mid, 0.0), DIVERGING_MID),
                                (labels[2], max(pos, 0.0), DIVERGING_POS)],
                        scale_max=scale_max, value_fmt=whole, table=table)


# _MT leaves room for the unit caption above the plot; _MR for the last x tick,
# which is anchored at the plot's right edge rather than centred on it.
_ML, _MR, _MT, _MB = 56, 16, 26, 28


def _label_anchor(x: float, width: int, *, allow_start: bool = True) -> str:
    """Anchor a point label so it stays inside the viewBox at either edge.

    `allow_start=False` for the FIRST point: the unit caption sits at the plot's
    top-left, so a start-anchored label on a point at y-max would run into it;
    centred there is still inside the viewBox (x = _ML).
    """
    if x > width - _MR - 40:
        return "end"
    if allow_start and x < _ML + 40:
        return "start"
    return "middle"


def line_chart(points: list[tuple[str, float | None]], *, unit: str, value_fmt: Callable[[float], str],
               width: int = 560, height: int = 180, none_label: str = "∞",
               none_table_label: str | None = None) -> str:
    n = len(points)
    finite = [v for _, v in points if v is not None]
    y_max = max(finite) if finite and max(finite) > 0 else 1.0
    plot_w, plot_h = width - _ML - _MR, height - _MT - _MB

    def x_at(i: int) -> float:
        return round(_ML + (plot_w / 2 if n <= 1 else i * plot_w / (n - 1)), 2)

    def y_at(v: float) -> float:
        return round(_MT + plot_h - v / y_max * plot_h, 2)

    parts = []
    for frac in (0.0, 0.5, 1.0):
        y = y_at(y_max * frac)
        parts.append(f'<line x1="{_ML}" y1="{y}" x2="{width - _MR}" y2="{y}" stroke="{GRID}" stroke-width="1"/>')
        parts.append(f'<text class="sc-tick" x="{_ML - 6}" y="{y + 4}" text-anchor="end" fill="{TEXT_SECONDARY}">'
                     f"{escape(value_fmt(y_max * frac))}</text>")
    parts.append(f'<line x1="{_ML}" y1="{_MT + plot_h}" x2="{width - _MR}" y2="{_MT + plot_h}" stroke="{AXIS}" stroke-width="1"/>')
    parts.append(f'<text class="sc-axis-unit" x="{_ML - 6}" y="10" text-anchor="start" fill="{TEXT_SECONDARY}">{escape(unit)}</text>')

    k = max(1, ceil(n / 6)) if n else 1
    coords: list[tuple[float, float, float]] = []
    for i, (xl, v) in enumerate(points):
        x = x_at(i)
        if i % k == 0 or i == n - 1:
            anchor = "end" if (n > 1 and i == n - 1) else "middle"
            parts.append(f'<text class="sc-tick sc-tick--x" x="{x}" y="{height - 8}" text-anchor="{anchor}" '
                         f'fill="{TEXT_SECONDARY}">{escape(xl)}</text>')
        if v is None:
            y = y_at(y_max)
            parts.append(f'<circle class="sc-none-marker" cx="{x}" cy="{y}" r="4" fill="none" '
                         f'stroke="{CATEGORICAL_COLORS[0]}" stroke-width="2"><title>{escape(xl)}: {escape(none_label)}</title></circle>')
            parts.append(f'<text class="sc-point-label" x="{x}" y="{y - 8}" '
                         f'text-anchor="{_label_anchor(x, width, allow_start=i != 0)}" '
                         f'fill="{TEXT_PRIMARY}">{escape(none_label)}</text>')
        else:
            coords.append((x, y_at(v), v, i))
    if len(coords) > 1:
        parts.append(f'<polyline points="{" ".join(f"{x},{y}" for x, y, _, _ in coords)}" fill="none" '
                     f'stroke="{CATEGORICAL_COLORS[0]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    for (x, y, v, _i), (xl, _) in zip(coords, [p for p in points if p[1] is not None], strict=True):
        parts.append(f'<circle cx="{x}" cy="{y}" r="4" fill="{CATEGORICAL_COLORS[0]}" stroke="#ffffff" stroke-width="2">'
                     f"<title>{escape(xl)}: {escape(value_fmt(v))}</title></circle>")
    if coords:
        x, y, v, i = coords[-1]
        parts.append(f'<text class="sc-point-label" x="{x}" y="{y - 8}" '
                     f'text-anchor="{_label_anchor(x, width, allow_start=i != 0)}" '
                     f'fill="{TEXT_PRIMARY}">{escape(value_fmt(v))}</text>')

    svg = (f'<svg class="sc-line-svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" '
           f'aria-label="{escape(unit)} over time">{"".join(parts)}</svg>')
    rows = [[xl, value_fmt(v) if v is not None else (none_table_label or none_label)] for xl, v in points]
    return f'<div class="sc-chart sc-line">{svg}{table_twin(["Hour", unit], rows, caption=unit, key=unit)}</div>'


def gantt(rows: list[tuple[str, float, float, str, str]], t0: float, t1: float, *,
          tick_fmt: Callable[[float], str], legend_items: list[tuple[str, str]]) -> str:
    span = t1 - t0

    def pos(t: float) -> float:
        return _pct(max(t0, min(t1, t)) - t0, span)

    body, twin_rows = [], []
    for label, start, end, h, title in rows:
        left, right = pos(start), pos(end)
        w = round(max(0.0, right - left), 1)
        dur = max(0.0, min(t1, end) - max(t0, start)) if span > 0 else 0.0
        body.append('<div class="sc-gantt-row">'
                    f'<span class="sc-gantt-label">{escape(label)}</span>'
                    f'<span class="sc-gantt-track"><span class="sc-gantt-bar" style="left:{left}%;width:{w}%;background:{escape(h)}" '
                    f'title="{escape(title)}"></span></span>'
                    f'<span class="sc-gantt-dur">{escape(duration(dur))}</span></div>')
        twin_rows.append([label, tick_fmt(start), tick_fmt(end), duration(dur), title])
    ticks = "".join(f'<span class="sc-tick" style="left:{p}%">{escape(tick_fmt(t0 + span * p / 100))}</span>'
                    for p in (0, 25, 50, 75, 100))
    axis = (f'<div class="sc-gantt-axis" aria-hidden="true"><span class="sc-gantt-label"></span>'
            f'<span class="sc-gantt-track">{ticks}</span><span class="sc-gantt-dur"></span></div>')
    twin = table_twin(["Interview", "First reply", "Last message", "Span", "Outcome"], twin_rows,
                      caption="Interview timeline", key="interview-timeline")
    return f'<div class="sc-chart sc-gantt">{legend(legend_items)}{"".join(body)}{axis}{twin}</div>'

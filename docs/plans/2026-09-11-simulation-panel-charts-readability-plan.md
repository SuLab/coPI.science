# /admin/simulation charts & tables readability — implementation plan (2026-09-11)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Revision 2 — after a fresh-context adversarial audit (25 findings, all applied; log in §Audit at the end).

**Goal:** make every plot and table on `/admin/simulation` readable by a human without hovering or expanding anything — labelled axes with tick marks and units, direct value labels, legends, a one-sentence caption per card, human-formatted numbers and times — and bring the whole page's typography in line with the 2026-09-11 readability study.

**Architecture:** the server-side, dependency-free renderer `src/services/svg_charts.py` is rewritten (same module, new signatures) so every chart emits its own axis, ticks, labels and legend as plain HTML/SVG; a new `src/services/display_format.py` owns every number/time formatting rule; `templates/admin/simulation.html` gains a `<style>` block for the `sc-*` classes, a caption under every card, readability-scale type throughout the file, and a refresh script that preserves disclosure state; `_live_tab_context` in `src/routers/admin.py` supplies formatted labels and display sort orders. No new frontend stack, no JS library, no schema change.

**Tech Stack:** Python 3.12 / FastAPI / Jinja2 / `markupsafe.escape` / Tailwind Play CDN (already loaded by `base.html`) / stdlib only. Tests: pytest on the host.

**Spec:** `docs/audits/2026-09-11-simulation-panel-charts/README.md` (this page's audit) and `docs/audits/2026-09-11-assessment-readability/README.md` §3–§4 (the readability study).

## Global constraints

- **Run tests on the host, never over sshfs:** `ssh ubuntu@ec2-3-21-33-147.us-east-2.compute.amazonaws.com 'cd /home/ubuntu/blackbird-copi-science && .venv-test/bin/python -m pytest <file> -q'`. Never `pip install` into `.venv-test` from the sshfs client (CLAUDE.md hazard 1).
- **No deploy, no image build, no simulation start.** Report at the end that shipping is a **web-tier rebuild only** (`$DC build blackbird-app && $DC up -d blackbird-app`); nothing under `src/agent/` changes.
- **Pinned strings that must survive** (asserted by `tests/integration/test_admin_simulation_page.py` today): `"$9.00"`, `"500,000 of 1,500,000 input tokens served from cache"`, `"33%"`, `"sc-tile--warn"`, `"Unpriced model"`, `"Headlines owed"`, `'<div class="sc-tile-value">1</div>'`, `"v9.9.9 (deadbeefcafe)"`, `"Interview timeline"`, `"Stop-reason taxonomy"`, `"Hub : lab token burn ratio"`, `"REAL API CALLS"`, `"not turns"`, `"Active threads"`, `"Calls in window"`, `"∞ — no lab tokens this hour"`, `"could not be attributed to a"`, `"Cost by interview stage"`, `"Cost by specialist consult"`, `"Cost by call kind"`, `"scout_hub · decide"`, `"chemistry · unmatched"`, `"No classified turns yet"`, `"No per-call breakdown yet"`, `"STALE"`, `"not deployed"`, `"Override: disabled"`, `"No override"`. Three assertions change and are listed in Task 3 Step 1; nothing else in that file may need editing.
- **`src/services/simulation_stats.py` is not modified.** Display sorting happens in the router. (No test pins `by_agent`/`by_phase` order — `test_cost_summary_by_agent_and_by_phase` builds a dict — and the only consumer of those lists is `src/routers/admin.py`, so router-side sorting is sufficient. No test pins the funnel's current by-value order either.)
- **Type scale** (readability study; single source of truth): content text 16px (`text-base` / `1rem`) at `line-height 1.5`; metadata and chart tick/axis text **14px minimum** (`text-sm` / `.875rem`); `text-xs` (12px) only inside `rounded-full` chips; **no** `text-gray-400`, `text-gray-500`, `bg-gray-400` and **no** `uppercase` anywhere in `simulation.html` after Task 5 (whole file, including the Status/Commands/Recent-admin-actions cards and the line below `#sim-body`); card `h2` = `text-lg font-semibold text-gray-900`, sentence case; card caption `<p class="sc-caption">` (16px, `max-width: 68ch`, `#374151`); numeric table cells right-aligned `tabular-nums` via class `sc-num`.
- **Colours** are pinned in `svg_charts.py` and nowhere else. Categorical slots 1–4 unchanged (`#2a78d6 #eb6834 #1baf7a #eda100`; dataviz validator PASS with a <3:1 contrast WARN on slots 3–4 — relieved by the mandatory legend and the always-visible value line every stacked bar now carries). Diverging `#e34948` / **`#a8a29e`** (2.52:1 on white; was `#f0efec`, 1.15:1) / `#2a78d6`. Meter track `#d7e6f7` (= existing `SEQUENTIAL_COLORS[0]`). Axis baseline and tick marks `AXIS = "#6b7280"` (4.83:1); gridlines `GRID = "#e5e7eb"`, hairline solid, never dashed. Chart text `#1f2937` primary / `#4b5563` secondary — **never a series colour, and never set inside a coloured fill**: segment values are printed in a text line beside the bar, not on the segment.
- **Marks:** bar tracks 14px (`hbar`, `gantt`) and 16px (`stack`) thick — both under the dataviz 24px cap; 2px surface gap between stacked segments; 2px line, r=4 markers with a 2px white ring. Every chart keeps a `<details class="sc-chart-fallback">` table twin **in addition to** visible labels, one twin per card (never one per row).
- `ruff check` must be zero findings on `tests/` and must not raise the `src/` ceiling: no unused imports, isort-clean import blocks.
- Commit trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; one commit per task; `./scripts/ci.sh` on the host after Task 8.

## File structure

| File | Responsibility |
|---|---|
| Create `src/services/display_format.py` | Pure formatting: money, counts, compact tokens, durations, UTC hour/timestamp labels, plural, percent. Locale-independent. |
| Rewrite `src/services/svg_charts.py` | Renderers that emit axis + ticks + labels + legend + (optional) table twin. Palette constants. |
| Modify `src/routers/admin.py` (`_live_tab_context`, `_simulation_context`, module imports, `templates.env.filters`) | Sort for display, format values, build tick labels, call renderers. |
| Modify `templates/admin/simulation.html` (whole file) | `extra_head` stylesheet; captions; type scale; sections + jump nav; split Run/Process cards; visible drops; combined table twins; refresh script keyed by `data-sc-key`. |
| Create `tests/unit/test_display_format.py`, rewrite `tests/unit/test_svg_charts.py`, modify `tests/integration/test_admin_simulation_page.py` | Contracts. |
| Create `scripts/render_admin_simulation.py` | The one-off render script Task 7 uses (checked in). |
| Modify `docs/audits/2026-09-11-simulation-panel-charts/README.md` | Mark findings implemented (Task 9). |

Parallelism: Task 1 ∥ Task 2 → Task 3 → Task 4 ∥ Task 5 → Task 6 → Task 7 → Task 8 → Task 9.

---

### Task 1: `display_format.py`

**Files:** Create `src/services/display_format.py`; Test `tests/unit/test_display_format.py`.

**Produces:**
```python
def money(value, *, floor: bool = False) -> str      # "$31.18" / "≥ $31.18"
def count(value: int) -> str                         # "133,820"
def compact(value) -> str                            # "812" / "94.4K" / "1.3M" / "2.5B"
def duration(seconds: float) -> str                  # "47s" / "1m 00s" / "1h 04m" / "2d 03h"
def hour_label(dt: datetime) -> str                  # "Sep 9 18:00"  (UTC; locale-independent)
def timestamp(dt: datetime | None) -> str            # "2026-09-09 18:47 UTC" / "—"
def epoch_hm(ts: float) -> str                       # "18:49" (UTC)
def plural(n: int, noun: str) -> str                 # "1 call" / "7 calls"
def percent(fraction: float) -> str                  # "29%"
def whole(v: float) -> str                           # "0" / "3" / "12.5"  -> axis ticks for counts
```

- [ ] **Step 1: Tests**

```python
# tests/unit/test_display_format.py
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from src.services import display_format as f


def test_money_two_decimals_and_floor_prefix():
    assert f.money(Decimal("31.17807215")) == "$31.18"
    assert f.money(0) == "$0.00"
    assert f.money(Decimal("9"), floor=True) == "≥ $9.00"


def test_count_thousands_separated():
    assert f.count(133820) == "133,820"
    assert f.count(0) == "0"


def test_compact_tokens():
    assert f.compact(812) == "812"
    assert f.compact(94_365) == "94.4K"
    assert f.compact(1_318_439) == "1.3M"
    assert f.compact(2_474_129_000) == "2.5B"


def test_duration_buckets():
    assert f.duration(47) == "47s"
    assert f.duration(60) == "1m 00s"
    assert f.duration(3849) == "1h 04m"
    assert f.duration(2 * 86400 + 3 * 3600) == "2d 03h"


def test_hour_label_is_utc_month_day_hour_and_locale_free():
    assert f.hour_label(datetime(2026, 9, 9, 18, tzinfo=UTC)) == "Sep 9 18:00"
    est = datetime(2026, 9, 9, 14, tzinfo=timezone(timedelta(hours=-4)))
    assert f.hour_label(est) == "Sep 9 18:00"
    assert f.hour_label(datetime(2026, 1, 1, 0)) == "Jan 1 00:00"  # naive -> UTC


def test_timestamp_minute_precision_and_dash_for_none():
    assert f.timestamp(datetime(2026, 9, 9, 18, 47, 56, 231218, tzinfo=UTC)) == "2026-09-09 18:47 UTC"
    assert f.timestamp(None) == "—"


def test_epoch_hm():
    assert f.epoch_hm(1788979762.728549) == "18:49"  # 2026-09-09T18:49:22Z


def test_plural_percent_whole():
    assert f.plural(1, "call") == "1 call"
    assert f.plural(7, "call") == "7 calls"
    assert f.percent(0.2894) == "29%"
    assert f.percent(0) == "0%"
    assert f.whole(0.0) == "0" and f.whole(3.0) == "3" and f.whole(12.5) == "12.5"
```

- [ ] **Step 2:** run → `ModuleNotFoundError`.
- [ ] **Step 3: Implement**

```python
# src/services/display_format.py
"""Human-facing number and time formatting for the admin pages.

Every rule the /admin/simulation charts and tables apply to a figure lives
here. Pure functions, no I/O, no locale dependence (month names are a
tuple, not strftime('%b')). All times render in UTC — the simulation, the DB
and the Slack markers all speak UTC; a mixed-zone page is worse than a single
labelled one.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def money(value: Decimal | float | int, *, floor: bool = False) -> str:
    text = f"${Decimal(str(value)):,.2f}"
    return f"≥ {text}" if floor else text


def count(value: int) -> str:
    return f"{int(value):,}"


def compact(value: int | float) -> str:
    v = float(value)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= limit:
            return f"{v / limit:.1f}{suffix}"
    return f"{v:.0f}"


def duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 86400}d {(s % 86400) // 3600:02d}h"


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def hour_label(dt: datetime) -> str:
    u = _utc(dt)
    return f"{_MONTHS[u.month - 1]} {u.day} {u.hour:02d}:{u.minute:02d}"


def timestamp(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return _utc(dt).strftime("%Y-%m-%d %H:%M UTC")


def epoch_hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M")


def plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def percent(fraction: float) -> str:
    return f"{fraction * 100:.0f}%"


def whole(v: float) -> str:
    return f"{v:g}"
```

- [ ] **Step 4:** run → 8 passed; `ruff check` clean.
- [ ] **Step 5: Commit** `feat(admin): display_format helpers for money, counts, durations, UTC labels`

---

### Task 2: rewrite `svg_charts.py`

**Files:** Rewrite `src/services/svg_charts.py`; rewrite `tests/unit/test_svg_charts.py`.

**Produces** (every function returns ONE well-formed, escaped fragment parseable by `xml.etree.ElementTree.fromstring`):

```python
CATEGORICAL_COLORS: tuple[str, ...]  # unchanged 7 slots
SEQUENTIAL_COLORS: tuple[str, ...]   # unchanged
DIVERGING_NEG = "#e34948"; DIVERGING_MID = "#a8a29e"; DIVERGING_POS = "#2a78d6"
METER_TRACK = SEQUENTIAL_COLORS[0]   # "#d7e6f7"
AXIS = "#6b7280"; GRID = "#e5e7eb"; TEXT_PRIMARY = "#1f2937"; TEXT_SECONDARY = "#4b5563"

def stat_tile(label, value, note=None, *, warn=False) -> str
def meter(label, fraction, detail, *, value_text=None, warn=False) -> str
def legend(items: list[tuple[str, str]]) -> str
def hbar_list(rows: list[tuple[str, float, str]], *, unit: str, axis_fmt: Callable[[float], str],
              color: str = CATEGORICAL_COLORS[0], caption: str | None = None) -> str
def stacked_hbar(label, segments: list[tuple[str, float, str]], *, scale_max: float,
                 value_fmt: Callable[[float], str] = compact, table: bool = True) -> str
def diverging_hbar(label, neg, mid, pos, labels: tuple[str, str, str], *, scale_max: float, table: bool = True) -> str
def line_chart(points: list[tuple[str, float | None]], *, unit: str, value_fmt: Callable[[float], str],
               width: int = 560, height: int = 180, none_label: str = "∞",
               none_table_label: str | None = None) -> str
def gantt(rows: list[tuple[str, float, float, str, str]], t0: float, t1: float, *,
          tick_fmt: Callable[[float], str], legend_items: list[tuple[str, str]]) -> str
def table_twin(head: list[str], rows: list[list[str]], *, caption: str, key: str) -> str   # for cards that combine rows
```

Markup contract (the tests and Task 4's CSS depend on these exact classes):

| Renderer | Root | Visible parts |
|---|---|---|
| `stat_tile` | `div.sc-tile[.sc-tile--warn]` | `.sc-tile-label`, exactly `<div class="sc-tile-value">…</div>`, optional `.sc-tile-note` |
| `meter` | `div.sc-tile.sc-tile--meter` | label, `.sc-tile-value` = `value_text` or `percent(clamped)`, SVG with `rect.sc-meter-track` and `rect.sc-meter-fill` (widths emitted with `:g` so `200` never renders `200.0`), `.sc-tile-note` = detail |
| `legend` | `div.sc-legend` | `<span class="sc-legend-item"><i class="sc-swatch" style="background:#…"></i>label</span>` per item |
| `hbar_list` | `div.sc-chart.sc-hbar` | per row `div.sc-hbar-row` = `span.sc-hbar-label`, `span.sc-hbar-track > span.sc-hbar-fill[style="width:NN.N%;background:#…"][title]`, `span.sc-hbar-value` = display; then `div.sc-axis` with `span.sc-tick.sc-tick--0` = `axis_fmt(0)`, `.sc-tick--mid` = `axis_fmt(max/2)`, `.sc-tick--max` = `axis_fmt(max)`, `span.sc-axis-unit` = unit; then one `<details class="sc-chart-fallback" data-sc-key="…">` twin (key = caption or "hbar") |
| `stacked_hbar` | `div.sc-chart.sc-stack` | `div.sc-stack-bar[role=img][aria-label]` with `span.sc-stack-seg[style="width:NN.N%;background:#…"][title]` per segment (widths are `value/scale_max` so rows share one scale; **no text inside segments**); `span.sc-stack-total` = `value_fmt(sum)`; `div.sc-stack-values` = one `span.sc-legend-item` (swatch + `value_fmt(value)`) per segment — the always-visible numbers; twin only when `table=True` |
| `diverging_hbar` | delegates to `stacked_hbar` with the three pinned colours and `value_fmt=whole` |
| `line_chart` | `div.sc-chart.sc-line` (SVG `viewBox 0 0 W H`; margins L56 R16 T26 B28 — top margin raised 12→26 in Task 7 (3a7d480) so the unit caption clears the max tick; last x tick / point label end-anchored to stay inside the viewBox) | y: 3 gridlines (`GRID`) at 0 / ½ / max with `text.sc-tick` labels via `value_fmt`; baseline in `AXIS`; `text.sc-axis-unit` = unit at top-left; x: `text.sc-tick.sc-tick--x` for the first, last and every k-th point, k = ceil(n/6); 2px polyline; r=4 filled circles with 2px white stroke and a `<title>`; the last finite point gets `text.sc-point-label` = `value_fmt(v)`; a `None` point → `circle.sc-none-marker[fill=none]` at y-max with `text.sc-point-label` = `none_label`; **y scale floor:** `y_max = max(finite) if that is > 0 else 1.0`; twin lists every x label with `value_fmt(v)` or `none_table_label or none_label` |
| `gantt` | `div.sc-chart.sc-gantt` | `legend(legend_items)`; per row `div.sc-gantt-row` = `span.sc-gantt-label`, `span.sc-gantt-track > span.sc-gantt-bar[style="left:L%;width:W%;background:#…"][title]`, `span.sc-gantt-dur` = `duration(clamped span)`; `div.sc-gantt-axis` with 5 `span.sc-tick` at 0/25/50/75/100% via `tick_fmt`; twin with head Interview / First reply / Last message / Span / Outcome |
| `table_twin` | `details.sc-chart-fallback[data-sc-key]` | `<summary>Show as table</summary><table><caption>…</caption><thead>…</thead><tbody>…</tbody></table>` |

- [ ] **Step 1: Tests** (replace the file)

```python
# tests/unit/test_svg_charts.py
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
```

- [ ] **Step 2:** run → import errors / signature errors.
- [ ] **Step 3: Implement** (full replacement)

```python
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


_ML, _MR, _MT, _MB = 56, 16, 12, 28


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
    parts.append(f'<text class="sc-axis-unit" x="{_ML - 6}" y="{_MT - 2}" text-anchor="end" fill="{TEXT_SECONDARY}">{escape(unit)}</text>')

    k = max(1, ceil(n / 6)) if n else 1
    coords: list[tuple[float, float, float]] = []
    for i, (xl, v) in enumerate(points):
        x = x_at(i)
        if i % k == 0 or i == n - 1:
            parts.append(f'<text class="sc-tick sc-tick--x" x="{x}" y="{height - 8}" text-anchor="middle" '
                         f'fill="{TEXT_SECONDARY}">{escape(xl)}</text>')
        if v is None:
            y = y_at(y_max)
            parts.append(f'<circle class="sc-none-marker" cx="{x}" cy="{y}" r="4" fill="none" '
                         f'stroke="{CATEGORICAL_COLORS[0]}" stroke-width="2"><title>{escape(xl)}: {escape(none_label)}</title></circle>')
            parts.append(f'<text class="sc-point-label" x="{x}" y="{y - 8}" text-anchor="middle" fill="{TEXT_PRIMARY}">{escape(none_label)}</text>')
        else:
            coords.append((x, y_at(v), v))
    if len(coords) > 1:
        parts.append(f'<polyline points="{" ".join(f"{x},{y}" for x, y, _ in coords)}" fill="none" '
                     f'stroke="{CATEGORICAL_COLORS[0]}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    for (x, y, v), (xl, _) in zip(coords, [p for p in points if p[1] is not None], strict=True):
        parts.append(f'<circle cx="{x}" cy="{y}" r="4" fill="{CATEGORICAL_COLORS[0]}" stroke="#ffffff" stroke-width="2">'
                     f"<title>{escape(xl)}: {escape(value_fmt(v))}</title></circle>")
    if coords:
        x, y, v = coords[-1]
        parts.append(f'<text class="sc-point-label" x="{x}" y="{y - 8}" text-anchor="middle" fill="{TEXT_PRIMARY}">{escape(value_fmt(v))}</text>')

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
```

- [ ] **Step 4:** run both unit files → green; `ruff check src/services/svg_charts.py tests/unit/test_svg_charts.py tests/unit/test_display_format.py` → zero findings.
- [ ] **Step 5: Commit** `feat(admin): chart primitives with axes, ticks, direct labels and legends`

---

### Task 3: router — sort for display, format every value, feed the renderers

**Files:** Modify `src/routers/admin.py` — module imports (replace the `svg_charts` import line with the new names; add `from src.services import display_format as fmt`; add `from src.services.simulation_control import HEARTBEAT_STALE_SECONDS` if not already imported — check `grep -n HEARTBEAT_STALE_SECONDS src/routers/admin.py`), `_live_tab_context`, `_simulation_context`; register the filter once after `templates = Jinja2Templates(directory="templates")` at line 113: `templates.env.filters["ts"] = display_format.timestamp` (Starlette's `Jinja2Templates` exposes `.env`; confirm with `grep -n "self.env" .venv-test/lib/python3*/site-packages/starlette/templating.py` on the host).

**Produces** template keys: `cost_hero_html, burn_html, cache_meter_html, progress_html, headlines_owed_html, interviews_concluded_html, cumulative_cost_html, hourly_token_legend_html, hourly_token_bars: list[{hour_label, html, values: list[str]}], hourly_token_twin_html, cost_by_agent_html, cost_by_model_html, cost_by_phase_html, cost_by_stage_html, cost_by_specialist_html, cost_by_call_kind_html, call_kind_is_floor, funnel_html, drops_rows, unvetted_panel_count, specialist_legend_html, specialist_rows: list[{domain, historical, html}], specialist_twin_html, fanout_html, taxonomy_html, latency_rows (all-string cells), latency_capped, per_agent_rows, gantt_html, gantt_links (+span), unattributed_note, burn_line_html, run_facts, process_facts, heartbeat_stale_seconds, api_call_units_note, run_options (started_at pre-formatted via fmt.timestamp), stats_run`. Removed keys: `burn_sparkline_html`, `burn_rows`, `run_overview`.

- [ ] **Step 1: Edit the three pinned assertions in `tests/integration/test_admin_simulation_page.py` (they must fail first)**
  1. `test_live_tab_hub_lab_burn_none_ratio_plots_as_a_spike_not_a_floor`: replace the two `<title>` assertions with
     ```python
     assert section.count('class="sc-none-marker"') == 1     # exactly one hollow ∞ marker
     assert "∞ — no lab tokens this hour" in section          # table twin row (unchanged wording)
     assert ">5.00</text>" in section                         # y-max tick reads the peak
     ```
  2. `test_live_tab_renders_the_three_f1_cost_panels`: `"$2.00 (1 calls)"` → `"$2.00 (1 call)"`, `"≥ $5.00 (1 calls)"` → `"≥ $5.00 (1 call)"`.
  3. `test_live_tab_per_agent_table_uses_the_real_heartbeat_snapshot`: the four `'<td class="px-4 py-2 text-sm text-gray-500">N</td>'` assertions → `'<td class="px-4 py-2 sc-num">N</td>'`.

- [ ] **Step 2: Replace `_live_tab_context` from `# --- KPI row` to `return`** (data loading above it unchanged; also format `run_options[*]["started_at"] = fmt.timestamp(r.started_at)`):

```python
    # --- KPI row ------------------------------------------------------
    cost_hero_html = stat_tile(
        "Total cost", fmt.money(cost.total, floor=cost.is_floor),
        ("Unpriced model(s) excluded from this total: " + ", ".join(cost.unpriced_models)) if cost.unpriced_models
        else ("Floor: some rows predate cache-token logging." if cost.is_floor else None),
        warn=bool(cost.unpriced_models),
    )
    elapsed_hours = overview.elapsed_seconds / 3600
    burn_html = stat_tile(
        "Average burn rate",
        f"{fmt.money(float(cost.total) / elapsed_hours)}/h" if elapsed_hours > 0 else "—",
        f"Total cost ÷ run lifetime ({fmt.duration(overview.elapsed_seconds)}); not a current rate.",
    )
    cache_denominator = cost.total_input_tokens + cost.total_cache_read_tokens
    cache_fraction = cost.total_cache_read_tokens / cache_denominator if cache_denominator > 0 else 0.0
    cache_meter_html = meter("Cache hit rate", cache_fraction,
                             f"{cost.total_cache_read_tokens:,} of {cache_denominator:,} input tokens served from cache")
    if overview.planned_seconds:
        frac = overview.elapsed_seconds / overview.planned_seconds
        progress_html = meter(
            "Progress", frac,
            f"{fmt.duration(overview.elapsed_seconds)} of {fmt.duration(overview.planned_seconds)} planned"
            + (" — overran the limit" if frac > 1 else ""),
            value_text=fmt.percent(frac), warn=frac > 1,
        )
    else:
        progress_html = stat_tile(
            "Progress", "No time limit",
            (f"Running {fmt.duration(overview.elapsed_seconds)} so far" if overview.ended_at is None
             else f"Ran {fmt.duration(overview.elapsed_seconds)}"),
        )
    headlines_owed_html = stat_tile("Headlines owed", str(fun.headlines_owed),
                                    "Terminal verdicts with no #assessments-summary post yet; 0 on a healthy run.",
                                    warn=fun.headlines_owed > 0)
    interviews_concluded_html = stat_tile("Interviews concluded", str(fun.terminal),
                                          f"of {fun.interviews_opened} opened; {fun.provisional} still provisional")

    # --- cost over time -------------------------------------------------
    running = Decimal(0)
    cum_points: list[tuple[str, float | None]] = []
    for h in hours:
        running += h.cost
        cum_points.append((fmt.hour_label(h.hour), float(running)))
    cumulative_cost_html = line_chart(cum_points, unit="US$", value_fmt=fmt.money) if cum_points else None

    token_classes = [("input", CATEGORICAL_COLORS[0]), ("output", CATEGORICAL_COLORS[1]),
                     ("cache read", CATEGORICAL_COLORS[2]), ("cache write", CATEGORICAL_COLORS[3])]
    hourly_token_legend_html = legend(token_classes)
    hour_max = max((h.input_tokens + h.output_tokens + h.cache_read_tokens + h.cache_creation_tokens for h in hours), default=0)
    hourly_token_bars, hourly_twin_rows = [], []
    for h in hours:
        vals = [h.input_tokens, h.output_tokens, h.cache_read_tokens, h.cache_creation_tokens]
        hourly_token_bars.append({
            "hour_label": fmt.hour_label(h.hour),
            "html": stacked_hbar(fmt.hour_label(h.hour), [(n, float(v), c) for (n, c), v in zip(token_classes, vals, strict=True)],
                                 scale_max=float(hour_max), table=False),
        })
        hourly_twin_rows.append([fmt.hour_label(h.hour), *[fmt.count(v) for v in vals], fmt.count(sum(vals))])
    hourly_token_twin_html = table_twin(["Hour (UTC)", "input", "output", "cache read", "cache write", "total"],
                                        hourly_twin_rows, caption="Tokens per hour", key="tokens-per-hour") if hours else None

    # --- cost by … (descending by cost for display) --------------------------
    def _cost_rows(items, caption, label):
        return hbar_list(
            [(label(r), float(r.cost), f"{fmt.money(r.cost)} ({fmt.plural(r.call_count, 'call')})")
             for r in sorted(items, key=lambda r: float(r.cost), reverse=True)],
            unit="US$", axis_fmt=fmt.money, caption=caption,
        )

    cost_by_agent_html = _cost_rows(cost.by_agent, "Cost by agent", lambda a: a.agent_id) if cost.by_agent else None
    cost_by_model_html = hbar_list(
        [(m.model, float(m.cost) if m.cost is not None else 0.0,
          fmt.money(m.cost) if m.cost is not None else "unpriced — not in the price table")
         for m in sorted(cost.by_model, key=lambda m: float(m.cost or 0), reverse=True)],
        unit="US$", axis_fmt=fmt.money, caption="Cost by model") if cost.by_model else None
    cost_by_phase_html = _cost_rows(cost.by_phase, "Cost by phase", lambda p: p.phase) if cost.by_phase else None
    cost_by_stage_html = _cost_rows(stage_costs, "Cost by interview stage", lambda r: f"{r.role} · {r.thread_phase}") if stage_costs else None
    cost_by_specialist_html = _cost_rows(specialist_costs, "Cost by specialist consult",
                                         lambda r: f"{r.domain} · {r.verdict_signal}") if specialist_costs else None
    call_kind_is_floor = bool(call_kind_costs) and call_kind_costs[0].is_floor
    cost_by_call_kind_html = hbar_list(
        [(r.kind, float(r.cost), f"{fmt.money(r.cost, floor=r.is_floor)} ({fmt.plural(r.call_count, 'call')})")
         for r in sorted(call_kind_costs, key=lambda r: float(r.cost), reverse=True)],
        unit="US$", axis_fmt=fmt.money, caption="Cost by call kind") if call_kind_costs else None

    # --- funnel (pipeline order) + drops ---------------------------------------
    funnel_counts = [("Interviews opened", fun.interviews_opened), ("Verdicts stored", fun.verdicts_stored),
                     ("Terminal (thread closed)", fun.terminal), ("Provisional (still open)", fun.provisional),
                     ("Headline announced", fun.announced), ("Headlines owed", fun.headlines_owed)]
    funnel_html = hbar_list([(lbl, float(v), str(v)) for lbl, v in funnel_counts], unit="interviews",
                            axis_fmt=fmt.whole, caption="Funnel") if any(v for _, v in funnel_counts) else None
    drops_rows = sorted(fun.drops_by_reason.items(), key=lambda kv: kv[1], reverse=True)

    # --- specialist mix + fan-out ----------------------------------------------
    specialist_legend_html = legend([("blocking", DIVERGING_NEG), ("gap", DIVERGING_MID), ("adequate", DIVERGING_POS)])
    mix_max = max((d.blocking + d.gap + d.adequate for d in domains), default=0)
    specialist_rows = [{"domain": d.domain, "historical": d.historical,
                        "html": diverging_hbar(d.domain, d.blocking, d.gap, d.adequate, ("blocking", "gap", "adequate"),
                                               scale_max=float(mix_max), table=False)} for d in domains]
    specialist_twin_html = table_twin(
        ["Domain", "blocking", "gap", "adequate", "historical"],
        [[d.domain, str(d.blocking), str(d.gap), str(d.adequate), str(d.historical)] for d in domains],
        caption="Specialist mix", key="specialist-mix") if domains else None
    fanout_html = hbar_list(
        [(f"{fmt.plural(b.consult_count, 'consult')} per interview", float(b.interview_count), fmt.plural(b.interview_count, "interview"))
         for b in fanout], unit="interviews", axis_fmt=fmt.whole, caption="Panel fan-out") if fanout else None

    # --- stop reasons + latency ----------------------------------------------------
    taxonomy_html = hbar_list(
        [(lbl, float(n), fmt.count(n)) for lbl, n in sorted(taxonomy.items(), key=lambda kv: kv[1], reverse=True)],
        unit="API calls", axis_fmt=fmt.whole, caption="Stop-reason taxonomy") if taxonomy else None
    latency_rows = [{"phase": phase, "n": fmt.count(p.n),
                     "p50": fmt.count(round(p.p50)) if p.p50 is not None else "—",
                     "p95": fmt.count(round(p.p95)) if p.p95 is not None else "—",
                     "p99": fmt.count(round(p.p99)) if p.p99 is not None else "—"}
                    for phase, p in sorted(latency.items(), key=lambda kv: (kv[0] != "overall", kv[0]))]

    # --- per-agent table -----------------------------------------------------------
    agents_detail = _agents_detail_map(status_row, now)
    per_agent_rows = []
    for r in agents:
        active_threads, calls_in_window = _agent_live_columns(r.agent_id, agents_detail)
        per_agent_rows.append({"agent_id": r.agent_id, "role": r.role or "—", "registry_status": r.registry_status or "—",
                               "muted": r.muted, "message_count": fmt.count(r.message_count), "call_count": fmt.count(r.call_count),
                               "cost": fmt.money(r.cost), "last_activity": fmt.timestamp(r.last_activity),
                               "active_threads": active_threads, "calls_in_window": calls_in_window})

    # --- interview gantt (F11: spans start at the first REPLY, not the root post) ----
    cost_by_thread = {ic.thread_ts: ic for ic in per_interview}
    known = [t for s in timeline for t in (s.first_message_at, s.last_message_at) if t is not None]
    t0, t1 = (min(known), max(known)) if known else (0.0, 0.0)
    gantt_rows, gantt_links = [], []
    for s in sorted(timeline, key=lambda s: s.first_message_at if s.first_message_at is not None else t0):
        start = s.first_message_at if s.first_message_at is not None else t0
        end = s.last_message_at if s.last_message_at is not None else start
        ic = cost_by_thread.get(s.thread_id)
        cost_display = fmt.money(ic.cost, floor=ic.is_floor) if ic is not None else "—"
        label = s.subject_agent_id or s.thread_id
        gantt_rows.append((label, start, end, CATEGORICAL_COLORS[1 if s.announced else 0], f"{s.outcome} — {cost_display}"))
        gantt_links.append({"assessment_id": s.assessment_id, "label": label, "outcome": s.outcome, "cost": cost_display,
                            "announced": s.announced, "span": fmt.duration(end - start)})
    gantt_html = gantt(gantt_rows, t0, t1, tick_fmt=fmt.epoch_hm,
                       legend_items=[("headline announced", CATEGORICAL_COLORS[1]), ("not announced", CATEGORICAL_COLORS[0])]) if gantt_rows else None
    unattributed = cost_by_thread.get(None)
    unattributed_note = None
    if gantt_rows and unattributed is not None and unattributed.cost > 0:
        unattributed_note = (f"{fmt.money(unattributed.cost, floor=unattributed.is_floor)} in LLM calls could not be attributed to a "
                             "specific interview thread (no thread_ts recorded) and is excluded from every row above.")

    # --- hub:lab burn ratio -------------------------------------------------------------
    burn_line_html = line_chart([(fmt.hour_label(p.hour), p.ratio) for p in burn_points], unit="hub ÷ lab tokens",
                                value_fmt=lambda v: f"{v:.2f}", none_label="∞",
                                none_table_label="∞ — no lab tokens this hour") if burn_points else None

    run_facts = {"status": overview.status, "started": fmt.timestamp(overview.started_at), "ended": fmt.timestamp(overview.ended_at),
                 "elapsed": fmt.duration(overview.elapsed_seconds), "total_api_calls": fmt.count(overview.total_api_calls),
                 "total_messages": fmt.count(overview.total_messages), "announcement": overview.run_start_announcement}
    process_facts = {"build": overview.build_info, "hub": overview.hub_prompt_stamp, "pi": overview.pi_prompt_stamp,
                     "rubric_version": overview.rubric_version, "rubric_hash": overview.rubric_content_hash}

    return {
        "stats_run": selected_run, "run_options": run_options, "api_call_units_note": API_CALL_UNITS_NOTE,
        "cost_hero_html": cost_hero_html, "burn_html": burn_html, "cache_meter_html": cache_meter_html,
        "progress_html": progress_html, "headlines_owed_html": headlines_owed_html,
        "interviews_concluded_html": interviews_concluded_html, "cumulative_cost_html": cumulative_cost_html,
        "hourly_token_legend_html": hourly_token_legend_html, "hourly_token_bars": hourly_token_bars,
        "hourly_token_twin_html": hourly_token_twin_html,
        "cost_by_agent_html": cost_by_agent_html, "cost_by_model_html": cost_by_model_html,
        "cost_by_phase_html": cost_by_phase_html, "cost_by_stage_html": cost_by_stage_html,
        "cost_by_specialist_html": cost_by_specialist_html, "cost_by_call_kind_html": cost_by_call_kind_html,
        "call_kind_is_floor": call_kind_is_floor, "funnel_html": funnel_html, "drops_rows": drops_rows,
        "unvetted_panel_count": fun.unvetted_panel_count, "specialist_legend_html": specialist_legend_html,
        "specialist_rows": specialist_rows, "specialist_twin_html": specialist_twin_html, "fanout_html": fanout_html,
        "taxonomy_html": taxonomy_html, "latency_rows": latency_rows, "latency_capped": latency_capped,
        "per_agent_rows": per_agent_rows, "gantt_html": gantt_html, "gantt_links": gantt_links,
        "unattributed_note": unattributed_note, "burn_line_html": burn_line_html,
        "run_facts": run_facts, "process_facts": process_facts, "heartbeat_stale_seconds": HEARTBEAT_STALE_SECONDS,
    }
```

- [ ] **Step 3: Run** `tests/integration/test_admin_simulation_page.py` on the host. Expected at this point (template not yet updated): the three edited tests still fail on template markup, and any test rendering a run with latency data fails with `TypeError` at `'%.0f'|format(row.p50)` — both are fixed by Task 5. Every other test passes. Do not commit a red tree: Tasks 3–5 may land as one commit if the implementer prefers; otherwise commit Task 3 with the message noting the template follows.
- [ ] **Step 4: Commit** `feat(admin/simulation): sort for display, format values, feed labelled renderers`

---

### Task 4: stylesheet

**Files:** Modify `templates/admin/simulation.html` — add directly after the `{% block title %}` line:

```html
{% block extra_head %}
<style>
  /* Readability scale (docs/audits/2026-09-11-assessment-readability): content 16px,
     metadata and chart text 14px minimum, nothing lighter than #4b5563, measure ≤ 68ch. */
  #sim-body { font-size: 1rem; line-height: 1.5; color: #1f2937; }
  .sc-caption { max-width: 68ch; font-size: 1rem; line-height: 1.5; color: #374151; margin: 0 0 .75rem; }
  .sc-meta { font-size: .875rem; color: #4b5563; }
  .sc-num { text-align: right; font-variant-numeric: tabular-nums; }
  th.sc-num { text-align: right; }

  .sc-tile { display: flex; flex-direction: column; gap: .25rem; padding: 1rem; border: 1px solid #e5e7eb; border-radius: .75rem; background: #fff; min-height: 7.5rem; }
  .sc-tile--warn { border-color: #f59e0b; background: #fffbeb; }
  .sc-tile-label { font-size: .875rem; font-weight: 600; color: #4b5563; }
  .sc-tile-value { font-size: 1.75rem; font-weight: 600; line-height: 1.2; color: #111827; }
  .sc-tile-note { font-size: .875rem; color: #4b5563; max-width: 34ch; }
  .sc-meter { display: block; width: 100%; height: 10px; margin: .25rem 0; }

  .sc-legend, .sc-stack-values { display: flex; flex-wrap: wrap; gap: .25rem 1rem; font-size: .875rem; color: #1f2937; font-variant-numeric: tabular-nums; }
  .sc-legend { margin: 0 0 .5rem; }
  .sc-legend-item { display: inline-flex; align-items: center; gap: .375rem; }
  .sc-swatch { display: inline-block; width: 12px; height: 12px; border-radius: 2px; flex: none; }

  .sc-hbar-row, .sc-axis, .sc-gantt-row, .sc-gantt-axis { display: grid; grid-template-columns: minmax(9rem, 14rem) 1fr 9rem; align-items: center; gap: .75rem; }
  .sc-hbar-row, .sc-gantt-row { min-height: 1.75rem; }
  .sc-hbar-label, .sc-gantt-label { font-size: 1rem; color: #1f2937; overflow-wrap: anywhere; }
  .sc-hbar-track, .sc-gantt-track { position: relative; height: 14px; background: #f3f4f6; border-radius: 3px; }
  .sc-hbar-fill { display: block; height: 100%; border-radius: 0 3px 3px 0; }
  .sc-hbar-value, .sc-gantt-dur { font-size: .9375rem; color: #1f2937; text-align: right; font-variant-numeric: tabular-nums; }
  .sc-axis { margin-top: .25rem; }
  .sc-axis-track { position: relative; height: 1.5rem; border-top: 1px solid #6b7280; }
  .sc-axis .sc-tick, .sc-gantt-axis .sc-tick { position: absolute; top: 3px; font-size: .875rem; color: #4b5563; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .sc-axis .sc-tick::before { content: ""; position: absolute; top: -4px; left: 0; width: 1px; height: 5px; background: #6b7280; }
  .sc-tick--0 { left: 0; } .sc-tick--mid { left: 50%; transform: translateX(-50%); } .sc-tick--max { right: 0; }
  .sc-tick--mid::before { left: 50%; } .sc-tick--max::before { left: auto; right: 0; }
  .sc-axis-unit { font-size: .875rem; color: #4b5563; }

  .sc-stack { display: grid; grid-template-columns: 1fr 6rem; grid-template-areas: "bar total" "values values" "twin twin"; align-items: center; gap: .25rem .75rem; }
  .sc-stack-bar { grid-area: bar; display: flex; gap: 2px; height: 16px; background: #f3f4f6; border-radius: 3px; overflow: hidden; }
  .sc-stack-seg { display: block; height: 100%; flex: none; }
  .sc-stack-total { grid-area: total; font-size: .9375rem; color: #1f2937; text-align: right; font-variant-numeric: tabular-nums; }
  .sc-stack-values { grid-area: values; }
  .sc-stack .sc-chart-fallback { grid-area: twin; }

  .sc-line-svg { display: block; width: 100%; max-width: 640px; height: auto; }
  .sc-line-svg text { font-family: inherit; font-size: 14px; }
  .sc-line-svg .sc-point-label { font-weight: 600; }

  .sc-gantt-bar { position: absolute; top: 0; height: 100%; border-radius: 3px; }
  .sc-gantt-axis .sc-gantt-track { height: 1.5rem; background: none; border-top: 1px solid #6b7280; }
  .sc-gantt-axis .sc-tick { transform: translateX(-50%); }
  .sc-gantt-axis .sc-tick:first-child { transform: none; } .sc-gantt-axis .sc-tick:last-child { transform: translateX(-100%); }

  .sc-chart-fallback { margin-top: .5rem; font-size: .875rem; color: #374151; }
  .sc-chart-fallback summary { cursor: pointer; color: #4338ca; }
  .sc-chart-fallback table { margin-top: .5rem; border-collapse: collapse; }
  .sc-chart-fallback caption { text-align: left; font-weight: 600; margin-bottom: .25rem; }
  .sc-chart-fallback th, .sc-chart-fallback td { padding: .125rem .75rem .125rem 0; text-align: left; vertical-align: top; }
  .sc-chart-fallback td { font-variant-numeric: tabular-nums; }

  #sim-body a:focus-visible, #sim-body button:focus-visible, #sim-body summary:focus-visible,
  #sim-body select:focus-visible, #sim-body input:focus-visible, #sim-body textarea:focus-visible { outline: 2px solid #4338ca; outline-offset: 2px; }
  #sim-body section[id] { scroll-margin-top: 4rem; }
  @media print { .sim-jump-nav, form, button, .sc-chart-fallback summary { display: none !important; } }
</style>
{% endblock %}
```

Note the line-chart SVG has `viewBox 560×180`, so the 14px SVG text scales with the container — at the CSS `max-width: 640px` it renders ≥ 14px; do not let the SVG shrink below ~500px wide (the `md:grid-cols-2` cards it sits in are ≥ 560px at 1280px viewport; on narrower viewports Tailwind stacks them to one column).

- [ ] Commit `style(admin/simulation): stylesheet for sc-* chart classes and readability scale`

---

### Task 5: template content (whole file)

**Files:** Modify `templates/admin/simulation.html`.

- [ ] **Step 1: Whole-file typography sweep (top cards included).** Apply to every line of the file, including line 537 (the pause-checkbox label, outside `#sim-body`):
  - `text-gray-400` → `text-gray-600`; `text-gray-500` → `text-gray-600`; delete `uppercase` everywhere; every `th` → `class="px-4 py-2 text-left text-sm font-semibold text-gray-600"` (add `sc-num` on numeric columns); `text-xs` → `text-sm` unless the element has `rounded-full` (the six status pills in the Engine-status card keep `text-xs`).
  - Body text cells `text-sm text-gray-900` → `text-base text-gray-900`; explanatory paragraphs (announcement help text, template placeholders, stop-card text) → `text-base text-gray-700 max-w-[68ch]`.
  - Every datetime → `| ts`: `status_row.updated_at`, `status_row.detail.tick_at` is a string in JSON — leave it, `latest_run.started_at`, `pending_start.created_at`, `cmd.created_at`, `cmd.consumed_at` (drop `or '—'`), `ev.created_at`, `opt.started_at` (already a string from Task 3 — no filter).
- [ ] **Step 2: Sections + jump nav.** Directly under the `<h1>` row insert:

```html
<nav class="sim-jump-nav mb-6 flex flex-wrap gap-x-4 gap-y-1 text-base" aria-label="Sections">
  <a class="text-indigo-700 hover:underline" href="#sec-status">Status</a>
  <a class="text-indigo-700 hover:underline" href="#sec-controls">Start / stop</a>
  <a class="text-indigo-700 hover:underline" href="#sec-announce">Announcement</a>
  <a class="text-indigo-700 hover:underline" href="#sec-history">Command history</a>
  <a class="text-indigo-700 hover:underline" href="#sec-run">Selected run</a>
  <a class="text-indigo-700 hover:underline" href="#sec-cost">Cost</a>
  <a class="text-indigo-700 hover:underline" href="#sec-outcomes">Outcomes</a>
  <a class="text-indigo-700 hover:underline" href="#sec-panel">Specialist panel</a>
  <a class="text-indigo-700 hover:underline" href="#sec-calls">API calls</a>
  <a class="text-indigo-700 hover:underline" href="#sec-agents">Agents</a>
  <a class="text-indigo-700 hover:underline" href="#sec-timeline">Timeline</a>
</nav>
```
  Section boundaries (wrap each in `<section id="…">…</section>`; all inside `#sim-body`, which the refresh script replaces wholesale, so anchors survive):
  | id | wraps |
  |---|---|
  | `sec-status` | the Engine status card |
  | `sec-controls` | the Start/Stop two-card grid |
  | `sec-announce` | both announcement cards (channels, template) |
  | `sec-history` | Commands + Recent admin actions |
  | `sec-run` | Run selector + the two cards from Step 3 + KPI row |
  | `sec-cost` | Cumulative cost, Tokens per hour, the six Cost-by cards |
  | `sec-outcomes` | Funnel, Drops by reason |
  | `sec-panel` | Specialist mix, Panel fan-out |
  | `sec-calls` | Stop-reason taxonomy, Latency |
  | `sec-agents` | Per-agent activity |
  | `sec-timeline` | Interview timeline, Hub : lab token burn ratio |

- [ ] **Step 3: Replace the `{% if run_overview %}` card with two cards** (inside `sec-run`, after the Run selector):

```html
<div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
  <div class="bg-white border border-gray-200 rounded-xl shadow-sm p-5">
    <h2 class="text-lg font-semibold text-gray-900 mb-1">Selected run</h2>
    <p class="sc-caption">Facts recorded on the run row itself. Every chart below is scoped to this run only.</p>
    <dl class="grid grid-cols-2 gap-x-4 gap-y-1 text-base">
      <dt class="text-gray-600">Status</dt><dd>{{ run_facts.status }}</dd>
      <dt class="text-gray-600">Started</dt><dd>{{ run_facts.started }}</dd>
      <dt class="text-gray-600">Ended</dt><dd>{{ run_facts.ended }}</dd>
      <dt class="text-gray-600">Lifetime</dt><dd>{{ run_facts.elapsed }}</dd>
      <dt class="text-gray-600">Messages posted</dt><dd class="sc-num">{{ run_facts.total_messages }}</dd>
      <dt class="text-gray-600">API calls</dt><dd class="sc-num">{{ run_facts.total_api_calls }}</dd>
    </dl>
    <details class="mt-2 sc-meta" data-sc-key="api-call-units"><summary>How API calls are counted</summary><p class="mt-1 max-w-[68ch]">{{ api_call_units_note }}</p></details>
    <p class="mt-2 text-base">Run-start announcement:
      {% if run_facts.announcement %}posted to {{ run_facts.announcement.posted.keys() | join(', ') if run_facts.announcement.posted else '—' }};
      failed: {{ run_facts.announcement.failed | join(', ') if run_facts.announcement.failed else 'none' }}{% else %}not posted (resumed run, or announcements disabled){% endif %}</p>
  </div>
  <div class="bg-white border border-gray-200 rounded-xl shadow-sm p-5">
    <h2 class="text-lg font-semibold text-gray-900 mb-1">This web process</h2>
    <p class="sc-caption">Versions the web tier is running right now — not necessarily what the selected run used. The run's own rubric stamp is in the run selector above.</p>
    <dl class="grid grid-cols-2 gap-x-4 gap-y-1 text-base">
      <dt class="text-gray-600">Build</dt><dd>{{ process_facts.build.commit or 'unknown' }} ({{ process_facts.build.branch or 'unknown branch' }}{% if process_facts.build.dirty_files is not none %}, {{ process_facts.build.dirty_files }} dirty files{% endif %}; source: {{ process_facts.build.source }})</dd>
      <dt class="text-gray-600">Hub prompts</dt><dd>{{ process_facts.hub.version }} ({{ process_facts.hub.content_hash }})</dd>
      <dt class="text-gray-600">PI prompts</dt><dd>{{ process_facts.pi.version }} ({{ process_facts.pi.content_hash }})</dd>
      <dt class="text-gray-600">Rubric</dt><dd>{{ process_facts.rubric_version }} ({{ process_facts.rubric_hash }})</dd>
    </dl>
  </div>
</div>
```

- [ ] **Step 4: KPI row** — delete the "Single-run draw" paragraph; the progress cell becomes `<div>{{ progress_html | safe }}</div>`.

- [ ] **Step 5: Chart cards.** Every card is `<div class="bg-white border border-gray-200 rounded-xl shadow-sm p-5"><h2 class="text-lg font-semibold text-gray-900 mb-1">TITLE</h2><p class="sc-caption">CAPTION</p>…</div>` with the `h2` text **exactly** as in the first column (the Task 6 test slices on `>TITLE</h2>`). The old single "Cost over time" card is split into two cards.

| h2 (exact) | Caption | Body |
|---|---|---|
| Cumulative cost | Running total of priced LLM spend, in US dollars, by the UTC hour the call was logged. The last point equals Total cost above. | `cumulative_cost_html` or "No calls logged yet." |
| Tokens per hour | Tokens billed in each UTC hour, split by class. Bars share one scale, so a longer bar is a busier hour. | `hourly_token_legend_html`; per row `<div class="flex items-start gap-3"><span class="sc-meta w-28 shrink-0 pt-0.5">{{ b.hour_label }}</span><div class="flex-1">{{ b.html \| safe }}</div></div>`; then `hourly_token_twin_html` |
| Cost by agent | Priced spend per agent over the whole run, most expensive first. Call counts are logged turns, not billed API calls. | `cost_by_agent_html` |
| Cost by model | Priced spend per model. A model missing from the price table is listed as unpriced and excluded from every total. | `cost_by_model_html` |
| Cost by phase | Spend by the engine phase that made the call: new post, thread reply, memory, or one phase per specialist consult. | `cost_by_phase_html` |
| Cost by interview stage | Spend by agent role and interview stage (explore, decide, conclude). Rows written before migration 0045 are unclassified. | `cost_by_stage_html`; keep the empty-state text "No classified turns yet — rows written before migration 0045 are unclassified." |
| Cost by specialist consult | Spend by specialist domain and the verdict signal that consult produced. Unmatched means no consult row recorded the opinion. | `cost_by_specialist_html`; empty "No consult turns recorded for this run yet." |
| Cost by call kind | Spend per individual API call, by kind: a tool round, the final call, a forced final call, or a truncation retry. | `cost_by_call_kind_html`; keep "No per-call breakdown yet — rows written before migration 0032 have no call_stats." and the floor note (as `sc-meta`) |
| Funnel | How far this run's interviews got, in pipeline order. Headlines owed should read 0 on a healthy run. | `funnel_html`; then `<p class="sc-meta mt-2">Unvetted panel (gap or unrecorded): {{ unvetted_panel_count }}</p>` |
| Drops by reason | Sidecar verdicts the engine refused or lost, by reason. A run with none is healthy. | always-visible `<table class="min-w-full text-base">` with `<thead>` Reason / Count(`th.sc-num`); rows `<td>{{ reason }}</td><td class="sc-num">{{ count }}</td>`; `unwritable_row` row keeps `bg-yellow-50 text-yellow-800 font-medium` and its `title`, plus a visible `<p class="sc-meta mt-2">⚠ unwritable_row: the database refused a concluded verdict; this drop is its only trace.</p>` when present; empty state row "No drops recorded for this run." |
| Specialist mix | Opinions each specialist domain returned. Bars share one scale, so bar length is the number of consults. | `specialist_legend_html`; per domain `<div class="grid grid-cols-[9rem_1fr] items-start gap-3 mb-2"><span class="text-base pt-0.5">{{ s.domain }}{% if s.historical %} <span class="sc-meta">(+{{ s.historical }} historical)</span>{% endif %}</span>{{ s.html \| safe }}</div>`; then `specialist_twin_html`; empty "No specialist consults recorded for this run." |
| Panel fan-out | How many specialist consults each interview received. Weight at 0 or 1 means the hub is skipping the panel. | `fanout_html` |
| Stop-reason taxonomy | Why each API call ended. Truncated is a max_tokens stop; refused is a model refusal. | `taxonomy_html` |
| Latency | Wall-clock time per API call in milliseconds, per phase. n is the number of calls measured (newest 20,000 rows at most). | table headers `Phase · Calls (n) · P50 (ms) · P95 (ms) · P99 (ms)` (numeric `th.sc-num`); cells `{{ row.n }}`, `{{ row.p50 }}`, `{{ row.p95 }}`, `{{ row.p99 }}` as plain strings in `td.sc-num` — **remove the `'%.0f'\|format(...)` calls**; keep the cap note |
| Per-agent activity | One row per agent that logged a call this run. Live columns come from the engine heartbeat and read "—" once it is older than {{ heartbeat_stale_seconds }} seconds. | headers `Agent · Role · Status · Muted · Messages · Calls (logged) · Cost (US$) · Last activity (UTC) · Active threads · Calls in window`; numeric cells `<td class="px-4 py-2 sc-num">`; delete the old note paragraph |
| Interview timeline | Each interview thread from its first reply to its last message, in UTC. Bars start at the first reply, not the opening post. Colour shows whether the headline was announced. | `gantt_html`; the links table gains a Span column (`link.span`) and gets `sc-num` on Cost/Span; footnote `unattributed_note` |
| Hub : lab token burn ratio | Hub tokens divided by all lab tokens, per UTC hour. A hollow ∞ marker is an hour with hub activity and no lab activity at all. | `burn_line_html` only — **delete** the template's own `<details>`/`burn_rows` table |

- [ ] **Step 6: Refresh script keyed by `data-sc-key`**

```html
<label class="mb-4 flex items-center gap-2 text-sm text-gray-600">
    <input type="checkbox" id="sim-refresh-pause" class="rounded border-gray-300">
    Pause auto-refresh <span class="sc-meta">(refreshes every 30 s; open tables stay open)</span>
</label>
<script>
const simRefreshTimer = setInterval(() => {
  if (document.getElementById('sim-refresh-pause')?.checked) return;
  fetch(location.href).then(r => r.text()).then(h => {
    const d = new DOMParser().parseFromString(h, 'text/html');
    const next = d.getElementById('sim-body');
    const cur = document.getElementById('sim-body');
    if (!next || !cur) { clearInterval(simRefreshTimer); return; }
    const open = new Set([...cur.querySelectorAll('details[open][data-sc-key]')].map(el => el.dataset.scKey));
    cur.innerHTML = next.innerHTML;
    cur.querySelectorAll('details[data-sc-key]').forEach(el => { if (open.has(el.dataset.scKey)) el.open = true; });
  });
}, 30000);
</script>
```
  Every `<details>` the template itself writes must carry a unique `data-sc-key` (the renderers emit their own).

- [ ] **Step 7: Run** the integration file on the host → green; `ruff check tests/`. Commit `feat(admin/simulation): captions, units, split run/process cards, visible drops, jump nav, keyed refresh`

---

### Task 6: integration tests for the new contract

**Files:** append to `tests/integration/test_admin_simulation_page.py`. Add `import re` and `from src.models import AssessmentDrop` to the module imports (check `src/models/__init__.py` exports it; else import from `src.models.opportunity`). `AssessmentDrop` columns are `simulation_run_id, agent_id, subject_agent_id, thread_id, reason, detail, raw_verdict`; only `agent_id` and `reason` are NOT NULL besides the run id.

```python
_CAPTIONED_CARDS = [
    "Cumulative cost", "Tokens per hour", "Cost by agent", "Cost by model", "Cost by phase",
    "Cost by interview stage", "Cost by specialist consult", "Cost by call kind", "Funnel",
    "Drops by reason", "Specialist mix", "Panel fan-out", "Stop-reason taxonomy", "Latency",
    "Per-agent activity", "Interview timeline", "Hub : lab token burn ratio",
]


async def test_live_tab_every_chart_card_has_a_heading_and_a_caption(client, db_session):
    admin = await _admin(db_session, "sim-admin-cap@example.org")
    run = await factories.make_simulation_run(db_session)
    await db_session.commit()
    html = (await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))).text
    for title in _CAPTIONED_CARDS:
        assert html.count(f">{title}</h2>") == 1, title
        i = html.index(f">{title}</h2>")
        assert 'class="sc-caption"' in html[i:i + 400], f"{title} has no caption"


async def test_the_page_uses_the_readability_scale_throughout(client, db_session):
    admin = await _admin(db_session, "sim-admin-type@example.org")
    run = await factories.make_simulation_run(db_session)
    await db_session.commit()
    html = (await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))).text
    page = html[html.index('id="sim-body"'):]
    for banned in ("text-gray-400", "text-gray-500", "bg-gray-400", "uppercase"):
        assert banned not in page, banned
    for m in re.finditer(r'class="([^"]*\btext-xs\b[^"]*)"', page):
        assert "rounded-full" in m.group(1), f"text-xs outside a chip: {m.group(1)}"
    assert ".sc-tile-value" in html and "font-size: 14px" in html   # stylesheet shipped, chart text ≥ 14px


async def test_live_tab_bars_carry_visible_labels_values_and_a_numeric_axis(client, db_session):
    admin = await _admin(db_session, "sim-admin-bars@example.org")
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent(db_session, agent_id="blackbird", role="scout_hub")
    common = dict(cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=0, model="claude-opus-5")
    await factories.make_llm_call_log(db_session, run=run, agent_id="blackbird", phase="thread_reply", input_tokens=1_000_000, **common)
    await factories.make_llm_call_log(db_session, run=run, agent_id="labbot", phase="new_post", input_tokens=500_000, **common)
    await db_session.commit()
    html = (await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))).text
    card = html[html.index(">Cost by agent</h2>"):html.index(">Cost by model</h2>")]
    assert card.index("blackbird") < card.index("labbot")                       # sorted by cost, desc
    assert '<span class="sc-hbar-value">$5.00 (1 call)</span>' in card
    assert 'style="width:100.0%' in card and 'style="width:50.0%' in card
    assert '<span class="sc-tick sc-tick--0">$0.00</span>' in card
    assert '<span class="sc-tick sc-tick--mid">$2.50</span>' in card
    assert '<span class="sc-tick sc-tick--max">$5.00</span>' in card
    assert '<span class="sc-axis-unit">US$</span>' in card
    assert re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2} \d\d:\d\d", html)  # hour labels


async def test_live_tab_funnel_is_in_pipeline_order_and_drops_are_visible(client, db_session):
    admin = await _admin(db_session, "sim-admin-funnel@example.org")
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
                                         thread_id="T1", recommendation="advance", summary_posted_at=None))
    db_session.add(AssessmentDrop(simulation_run_id=run.id, agent_id="blackbird", thread_id="T2",
                                  reason="empty_reply", detail="x"))
    await db_session.commit()
    html = (await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))).text
    funnel = html[html.index(">Funnel</h2>"):html.index(">Drops by reason</h2>")]
    assert funnel.index("Interviews opened") < funnel.index("Verdicts stored") < funnel.index("Headlines owed")
    drops = html[html.index(">Drops by reason</h2>"):html.index(">Specialist mix</h2>")]
    assert "<details" not in drops.split("</table>")[0]
    assert "empty_reply" in drops and '<td class="sc-num">1</td>' in drops


async def test_live_tab_timestamps_are_minute_precision_utc(client, db_session):
    admin = await _admin(db_session, "sim-admin-ts@example.org")
    run = await factories.make_simulation_run(db_session, started_at=datetime(2026, 9, 9, 18, 48, 1, 880209, tzinfo=UTC))
    await db_session.commit()
    html = (await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))).text
    assert "2026-09-09 18:48 UTC" in html and "18:48:01.880209" not in html


async def test_live_tab_refresh_script_preserves_open_details_by_key(client, db_session):
    admin = await _admin(db_session, "sim-admin-js@example.org")
    html = (await client.get("/admin/simulation", headers=auth_headers(admin.id))).text
    assert "details[open][data-sc-key]" in html and "el.dataset.scKey" in html


async def test_live_tab_latency_and_progress_render_with_real_data(client, db_session):
    admin = await _admin(db_session, "sim-admin-lat@example.org")
    run = await factories.make_simulation_run(
        db_session, status="stopped", config={"max_runtime": 60},
        started_at=datetime(2026, 1, 1, 10, 0, tzinfo=UTC), ended_at=datetime(2026, 1, 1, 11, 4, 9, tzinfo=UTC))
    await factories.make_llm_call_log(
        db_session, run=run, model="claude-opus-5", phase="thread_reply",
        input_tokens=10, output_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        call_stats=[{"seq": 0, "kind": "final", "latency_ms": 133820, "stop_reason": "end_turn"},
                    {"seq": 1, "kind": "retry", "latency_ms": 500, "stop_reason": "end_turn"}])
    await db_session.commit()
    html = (await client.get(f"/admin/simulation?run={run.id}", headers=auth_headers(admin.id))).text
    assert "Internal Server Error" not in html
    assert "P50 (ms)" in html and '<td class="sc-num">2</td>' in html   # n, thousands-separated cells elsewhere
    assert '<div class="sc-tile-value">107%</div>' in html and "overran the limit" in html
```

Check `SimulationRun` has `ended_at` and `status` columns accepting those kwargs (`src/models/…` — grep `class SimulationRun`).

- [ ] Run on host → green. Commit `test(admin/simulation): captions, readability scale, labelled bars, pipeline funnel, UTC timestamps, keyed refresh`

---

### Task 7: render and look (dataviz step 7)

**Files:** Create `scripts/render_admin_simulation.py`:

```python
"""Render /admin/simulation for given run ids to stdout, as the first allowed
admin, without a browser session. Run INSIDE a one-off app container off the
current image with the working tree's src/ and templates/ mounted read-only:

  docker compose -f docker-compose.prod.yml run --rm -T \
    -v "$PWD/src:/app/src:ro" -v "$PWD/templates:/app/templates:ro" \
    -v "$PWD/scripts:/app/scripts:ro" blackbird-app \
    python scripts/render_admin_simulation.py <run-uuid> [<run-uuid> …] > /tmp/sim.html

GET only; nothing is written. Never `exec` this into the live app container.
"""
import asyncio
import sys

import httpx
from httpx import ASGITransport
from sqlalchemy import select


async def main(run_ids: list[str]) -> None:
    from src.database import get_session_factory
    from src.dependencies import get_current_user
    from src.main import create_app
    from src.models.user import User

    app = create_app()
    async with get_session_factory()() as s:
        admin = (await s.execute(
            select(User).where(User.user_role == "admin", User.access_status == "allowed").limit(1)
        )).scalars().first()

    async def _admin() -> User:
        return admin

    app.dependency_overrides[get_current_user] = _admin
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        for rid in run_ids:
            r = await c.get(f"/admin/simulation?run={rid}")
            sys.stdout.write(f"=====RUN {rid} STATUS {r.status_code}=====\n{r.text}\n")


asyncio.run(main(sys.argv[1:]))
```

- [ ] Render runs `00519a2c-ae71-44b3-8109-355a5fe99155`, `61ccad6d-eb1e-4023-81ba-adcea726a196`, `892d1ad3-c849-4627-ba9e-572c0555f3ea`; split on the `=====RUN` marker; serve the files with `python3 -m http.server` and screenshot at 1400px and 1000px with Playwright (full page).
- [ ] Check per card: no label collides with a bar; every bar shows label and value; axis 0 / mid / max ticks and unit visible; legend swatches match colours in use; the stacked bars' value line shows all four numbers; captions wrap ≤ 68ch; no text lighter than gray-600; the ∞ marker is hollow and labelled; gantt labels do not overlap; `61ccad6d`'s Progress tile reads "107%" in the warn style; open a table twin, wait 30 s, confirm it stays open.
- [ ] Fix layout defects in Task 4/5 files; re-run Task 6 tests; commit `fix(admin/simulation): layout fixes from render review` and `chore(scripts): admin simulation render script`.

### Task 8: full gate

- [ ] `ssh … 'cd /home/ubuntu/blackbird-copi-science && nohup ./scripts/ci.sh > /tmp/ci_sim_charts.log 2>&1 &'`; poll `tail -3 /tmp/ci_sim_charts.log` until the pytest summary prints. Expected: green, coverage floor met, ruff zero findings on tests, `src/` ceiling not exceeded.

### Task 9: close out

- [ ] Annotate every row of `docs/audits/2026-09-11-simulation-panel-charts/README.md` with "implemented in <commit>"; commit `docs(admin/simulation): mark chart readability findings implemented`.
- [ ] Report: what changed, CI result, and that shipping is a web-tier rebuild only. Do not deploy.

---

## Self-review against the spec

| Finding | Task |
|---|---|
| S1 no axis/labels/legend | 2, 3 |
| S2 no CSS | 4 |
| S3 tooltip-gated values | 2 (labels, value lines), 5 (drops visible) |
| S4 refresh collapses details | 5 step 6 (keyed) |
| S5 typography, whole file | 4, 5 step 1, 6 |
| KPI tiles / meter label+value / progress overrun / burn-rate wording / repeated note / indefinite | 3, 4 |
| Cumulative sparkline axes, hour keys | 2, 3 |
| Hourly bars shared scale, legend, hour label, integer table, one twin | 2 (`scale_max`, `table=False`), 3 (`hourly_token_twin_html`), 5 |
| Six cost charts: labels, `$`, numeric axis, sort desc, unpriced text | 2 (`axis_fmt`), 3 |
| Funnel order | 3 |
| Drops collapsed | 5 |
| Gap colour, counts, one legend, one twin | 2, 3 (`specialist_twin_html`), 5 |
| Fan-out / taxonomy labels | 3 |
| Latency units/alignment/thousands (cells as strings) | 3, 5 |
| Per-agent decimals/ISO/"—" semantics/SQL note | 3, 5 |
| Gantt overlap, axis, legend, epoch table, F11 caveat | 2, 3, 5 |
| Burn ∞ marker, single twin carrying the pinned wording | 2 (`none_table_label`), 3, 5 |
| Timestamps everywhere, Process card mix, build `source` kept | 3, 5 |
| Study: 16/14/12 rule, no gray-400/500, no uppercase, 68ch, heading scale, open-by-default, no tooltip-only meaning, focus/anchors/print | 4, 5, 6 |
| Study: no letter-spacing / dyslexia font / justification / dark default | nothing added |

Type consistency: `hbar_list(rows, *, unit, axis_fmt, color, caption)`, `stacked_hbar(label, segments, *, scale_max, value_fmt, table)`, `diverging_hbar(…, *, scale_max, table)`, `line_chart(points, *, unit, value_fmt, width, height, none_label, none_table_label)`, `gantt(rows, t0, t1, *, tick_fmt, legend_items)`, `meter(label, fraction, detail, *, value_text, warn)`, `table_twin(head, rows, *, caption, key)` — used with exactly these names in Tasks 2, 3 and 6.

## Audit log (2026-09-11)

A fresh-context adversarial auditor executed the plan's Task 1/2 code and tests locally and read every file the plan touches. 25 findings, all applied in this revision:

1. `Element.iter("{*}text")` does not accept the wildcard → tests use `findall(".//{*}…")`.
2. Meter `200.0` vs `200` width → compare as floats and emit widths with `:g`.
3. `fill="none"` also on the polyline → hollow marker carries `class="sc-none-marker"`; test counts that.
4. Deleting `burn_rows` lost the pinned "∞ — no lab tokens this hour" → `line_chart(none_table_label=…)` carries it in the single twin.
5. `AssessmentDrop` has no `channel_name` → removed from the test.
6. Latency cells were strings while the template still ran `'%.0f'|format` → Task 5 replaces the cells; Task 6 renders real latency data.
7. Three h2 texts did not match `_CAPTIONED_CARDS`, and two were h3s → exact h2 texts pinned; "Cost over time" split into two cards.
8. Top-of-page cards kept `text-xs`/`gray-500`/`gray-400` and the test sliced the whole body → Task 5 Step 1 is a whole-file sweep, including line 537; `uppercase` added to the banned list.
9. Unused `compact` import and isort → fixed import block.
10. Axis ticks were the max row's display string and a literal "½ max" → `axis_fmt` renders 0 / max÷2 / max as numbers; `whole()` added for counts.
11. `y_max ≤ 0` collapsed gridlines and put the ∞ marker on the baseline → y-scale floor 1.0; degenerate-series test asserts three distinct tick y's.
12. White 12px labels on `#eb6834`/`#e34948`/`#2a78d6` were 3.2–4.4:1 → no text inside fills at all; values print in a `.sc-stack-values` text line.
13. 12–13px chart text → every tick/axis/legend/value text is 14px; constraint amended.
14. Label overflow across segments → moot (no in-segment text).
15. Details state restored by index → keyed by `data-sc-key`; renderers emit keys; template details carry keys.
16. Nine dangling `#sec-*` anchors → section boundary table.
17. One table twin per domain/hour → `table=False` + one combined `table_twin` per card.
18. Dead duplicate `cost_hero_html` → single assignment.
19. Furniture colour inconsistency and `#cde2fb` not in the ramp → `AXIS #6b7280` (4.83:1), `GRID #e5e7eb`, `METER_TRACK = SEQUENTIAL_COLORS[0]`.
20. "≤16px" vs actual 14px → constraint states 14px/16px tracks.
21. False claim that a stats test pins order → corrected justification.
22. `build_info.source` dropped → kept.
23. Render script hand-waved → checked in as `scripts/render_admin_simulation.py`, inlined here.
24. Task 3 Step 3 vague; `uppercase` never removed → expected failures listed; `uppercase` removed file-wide and tested.
25. `strftime('%b')` locale-dependent → month tuple; test covers Jan.

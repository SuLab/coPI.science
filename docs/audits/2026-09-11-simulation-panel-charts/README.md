# /admin/simulation — plots and tables readability audit (2026-09-11)

Status: implemented on branch feat/sim-panel-charts-readability (2026-09-11);
per-finding commits below.

## Method

The page was rendered for three real production runs (`00519a2c`, `61ccad6d`,
`892d1ad3`) by driving `create_app()` inside the running `copi-blackbird-app-1`
container with `get_current_user` overridden to the first allowed admin, then
screenshotted at 1400px (Chromium via Playwright). Source read:
`templates/admin/simulation.html` (553 lines), `src/services/svg_charts.py`
(347), `src/services/simulation_stats.py` (1149), `src/routers/admin.py:2057-2710`
(`_live_tab_context`), `tests/unit/test_svg_charts.py` (39 tests),
`tests/integration/test_admin_simulation_page.py`. The dataviz skill's
`references/anti-patterns.md`, `marks-and-anatomy.md`, `palette.md` and
`scripts/validate_palette.js` were consulted; the earlier readability study is
`docs/audits/2026-09-11-assessment-readability/README.md`.

## Structural findings (cause of everything below)

| # | Finding | Evidence | Implemented in |
|---|---|---|---|
| S1 | No chart carries an axis, tick, scale, category label, value label or legend. `svg_charts.py` emits bare `<rect>`/`<polyline>` with `<title>` tooltips and a collapsed `<details>` table — nothing else. | `svg_charts.py` — the only `<text>` element in the module is the gantt row label | 3853f9a + 16167f0 |
| S2 | No CSS exists for any `sc-*` class the module emits (`sc-tile`, `sc-tile-label`, `sc-tile-value`, `sc-tile-note`, `sc-chart`, `sc-chart-fallback`, `sc-chart-label`, `sc-meter`). KPI tiles render as running text; the gantt `<text>` renders at the browser default 16px. | `grep -rn "sc-tile\|sc-chart" templates/ static/ src/` matches only `simulation.html` and `svg_charts.py` | f829802 |
| S3 | Every value is gated behind hover or a collapsed table — the dataviz rule "tooltips enhance, never gate" is violated on every chart. | screenshot: 14 cards of unlabeled blue bars | 3853f9a + ff7a003 |
| S4 | The 30 s auto-refresh replaces `#sim-body` `innerHTML`, so any `<details>` a reader has opened snaps shut within 30 s. | `simulation.html:541-552` | ff7a003 |
| S5 | Typography contradicts the 2026-09-11 readability study now applied to the assessment pages: 25× `text-gray-400` (2.5:1), 49× `text-gray-500`, 38× `text-xs` outside chips, all captions at 12–14px, headings a flat 18px with no captions. | class counts on `simulation.html` | f829802 + ff7a003 |

## Per-element findings

| Element | Findings | Implemented in |
|---|---|---|
| KPI row (`stat_tile`, `meter`) | Tiles unstyled (S2). Cache-hit meter has **no visible label and no visible value** — "Cache hit rate" is only `aria-label`, the % only in the collapsed table. Progress meter clamps 3849 s of 3600 s to 100% and shows seconds. Burn rate `$/h` is the run-lifetime average but is not labelled as such. "Single-run draw" note repeated three times. Indefinite progress renders the word "indefinite". | 16167f0 + f829802 |
| Cumulative cost sparkline | No x axis (hours), no y axis ($), no endpoint value. Table view keys rows `0,1,2` and prints `4.52028825`. | 3853f9a + 16167f0 |
| Tokens per hour stacked bars | Each hour is normalised to **its own** total, so every bar is 220px wide; hour-to-hour volume is invisible and the heading does not say so. No legend (colour order named in the h3 only). Hour label is a raw `datetime` repr (`2026-09-09 18:00:00+00:00`). Table prints `94365.0`. | 3853f9a + 16167f0 + ff7a003 |
| Six "Cost by …" `hbar_list` cards | No category labels, no values, no `$`. `cost.by_agent`/`by_phase` are sorted **alphabetically** (`simulation_stats.py:467,471`) so the largest bar sits at the bottom of "Cost by phase"; funnel/taxonomy are sorted by value — inconsistent. Table view prints `30.71378975` beside `$30.71`. Unpriced model = zero-width (invisible) bar. "Cost by specialist" is ~24 unlabeled `domain · signal` bars. | 3853f9a + 16167f0 |
| Funnel | Six equal unlabeled bars, sorted by count — destroying the pipeline order the word "funnel" implies. | 16167f0 |
| Drops by reason | Not a chart: an always-collapsed `<details>`; with no drops the card looks empty; with drops (61ccad6d: `empty_reply 1`) nothing is visible until clicked. | ff7a003 |
| Specialist mix (`diverging_hbar`) | "gap" segment `#f0efec` on a white card ≈ 1.1:1 — all-gap domains (`commercial`, `scientific`) look like empty tracks. No counts, no legend. Eight per-domain "table view" toggles in one card. | 3853f9a + 16167f0 + ff7a003 |
| Panel fan-out | Unlabeled bars; the x variable (consults per interview) and y variable (interviews) exist only in tooltips. | 16167f0 |
| Stop-reason taxonomy | Two unlabeled bars, the second a 1px sliver. Counts nowhere. | 16167f0 |
| Latency table | Unit only in the card title; headers `P50/P95/P99` carry no "ms"; `133820` not thousands-separated; column `n` unexplained; numbers left-aligned. | 16167f0 + ff7a003 |
| Per-agent table | Cost at 4 decimals (2 everywhere else). `Last activity` is ISO with microseconds. `Active threads`/`Calls in window` read "—" with no visible explanation of stale-heartbeat semantics or the window length. Units caveat under the header is a 40-word paragraph containing SQL. | 16167f0 + ff7a003 + 26c3823 |
| Interview timeline (`gantt`) | Confirmed overlap: 16px default `<text>` on 22px rows collides with the bars; first label clipped by the card top. No time axis, no t0/t1, no legend for orange (announced) vs blue (not). Table view prints `1788979762.728549` epoch floats. The router's F11 caveat (bars start at the first reply, not the root post) is not shown. | 3853f9a + 16167f0 + ff7a003 |
| Hub : lab burn ratio | No axes, no hour labels, no ratio values. ∞ hour plotted at the series peak with no distinguishing mark. **Two** stacked "table view" toggles (one from `sparkline()`, one from the template); the first is indexed 0..n with no hours. | 3853f9a + 16167f0 + ff7a003 |
| Run selector / Process card / Commands / Admin actions | Microsecond ISO timestamps throughout. "Process (web tier)" card mixes process stamps (build, prompt/rubric versions) with the **run's** API-call total and announcement record under one heading. Build shows `— (—, — dirty files, source=unavailable)` because `.build_info.json` is absent from the web image. | 16167f0 + ff7a003; the Build-source sub-finding not addressed here — needs the web image to bake `.build_info.json` |

## Palette validation (dataviz `validate_palette.js`, light surface)

* Categorical slots 1–4 (`#2a78d6,#eb6834,#1baf7a,#eda100`): PASS, with a
  contrast WARN on slots 3 and 4 (<3:1) — "relief required": visible labels
  or a table view. The plan adds visible labels and a legend to every use.
* Diverging (`#e34948,#f0efec,#2a78d6`): the midpoint fails the lightness band
  and chroma floor *as a categorical slot* (expected for a neutral midpoint)
  and sits at 1.12:1 against the surface — which is the observed
  "invisible gap segment". The plan darkens the midpoint to `#a8a29e`
  (≈2.5:1 on white) **and** direct-labels every segment with its count, so
  the mark never has to carry the value alone.

## What the readability study contributes

From `docs/audits/2026-09-11-assessment-readability/README.md` §3–§4
(fetched-source findings, already applied to the assessment pages):

1. Content text ≥16px, metadata 14px, 12px only in `rounded-full` chips (USWDS ≥16px floor; GOV.UK 19px; Rello 2016 directional). — landed in the f829802 stylesheet.
2. No text lighter than `gray-600` (7.56:1); `gray-400`/`gray-500` eliminated (WCAG 1.4.3; NN/g low-contrast). — landed in the f829802 stylesheet plus the ff7a003 template class sweep.
3. Reading columns capped ≈65–72ch (Dyson & Haselgrove 2001) — applies to every caption and note on this page. — landed in the f829802 stylesheet (68ch measure).
4. Real heading scale, sentence case, no uppercase past two words (BDA ≥20%). — landed in the f829802 stylesheet and the ff7a003 template.
5. Content a reader must compare is **open by default**; accordions only for secondary material (NN/g accordions on desktop) — applies to the drops table and every "table view" that is currently the *only* readable form. — landed in ff7a003 (visible drops, split cards).
6. Meaning never carried by `title` tooltips alone (visible legend or text). — landed in 3853f9a (chart primitives) and ff7a003 (template wiring).
7. `focus-visible` rings, `scroll-margin-top` on anchor targets, print rules; skip link already in `base.html`. — landed in the f829802 stylesheet (rings, print rules) and ff7a003 (jump-nav anchors).
8. Do **not** add letter-spacing, a dyslexia font, justified text, or a dark default. — respected; no such change appears in any commit.

Test coverage for the above is 67dd4d2. One item is deferred: the readability
test guards `#sim-body` only (`tests/integration/test_admin_simulation_page.py:710-713`
scopes the parsed HTML to between `id="sim-body"` and the `<!-- #sim-body -->`
comment), not the file tail, so nothing outside that block is asserted on by
the readability checks.

## Deferred minors (from task reviews)

- `markupsafe` is a transitive dependency only, not a direct one — no action needed.
- `line_chart`'s `data-sc-key` is the unit string, not a per-series identifier.
- `.sc-stack` reserves an unused "twin" grid area.
- The `| map(attribute=0) | list` in the `unwritable_row` guard is redundant.
- The `ts` filter is registered on the admin Jinja environment only, not globally.
- The "Cost (US$)" header plus "$"-prefixed values double the unit.
- The API-call accounting caveat lives inside a collapsed `<details>`, by ruling (not a defect to fix).

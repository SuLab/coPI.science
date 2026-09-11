# Assessment detail readability — implementation plan (2026-09-11)

**Spec:** `docs/audits/2026-09-11-assessment-readability/README.md` §4 (audited findings) plus the operator's instruction: in the top brief box, the "In one minute" pitch is the LEFT column and the Key points are the RIGHT column.

**Goal:** implement every §4 recommendation on both assessment detail pages (admin + manager share `templates/admin/_assessment_detail_body.html`).

**Global constraints**
- Do not edit any pinned label string: "Full rationale (N paragraphs)", "Dimension scores", "Human review", "Interview timeline", "Overall rating (1 = weak … 5 = strong)", "Learn", "Don't learn — log only", "Significance"/"Innovation"/"Commercial potential", "In one minute", "Key points", "The ask", "Red flags", "Gating criteria".
- Keep every `<details>` element (tests key on `<details` tags); open by adding the `open` attribute only.
- Keep class hooks: `assessment-jump-nav`, `review-impersonation-notice`, `review-dimension-scores`, `review-dimension-row`, `review-dim-*`, `assessment-brief-points`, `assessment-rationale`, `assessment-prose`, `panel-cut-off`, `review-status-*`, `review-mode-chip`, `review-bot-score`.
- Form field names and ids unchanged.
- `text-xs` only inside `rounded-full` chips; `text-gray-400`/`bg-gray-400`/`text-gray-500` must not appear in the body.
- Tests on host: `.venv-test/bin/python -m pytest <file> -v` (foreground; if a file exceeds 600 s use nohup + poll). Run `./scripts/ci.sh` at the end (Task D).
- Commit trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. No build/deploy; no simulation start.

## Type scale (single source of truth for all tasks)

| Role | Classes / CSS |
|---|---|
| Prose body (`.assessment-prose`) | `font-size: 1.0625rem` (17px); `line-height: 1.6`; `max-width: 68ch`; colour `#1f2937`; `p + p { margin-top: 1.25em }`; `text-wrap: pretty` |
| Content text outside prose (red flags, dimension anchors, panel notes, reviewer comments, consult opinions, timeline messages) | `text-base` (16px) with `leading-relaxed`; wrap each in `max-w-[68ch]` where it is running text |
| Metadata (dates, "Lab:", counts, rubric stamps, jump nav) | `text-sm` |
| Chips | `text-xs` inside `rounded-full` only |
| h1 | `text-2xl font-bold` (unchanged) |
| h2 brief headline | `text-2xl font-semibold` + `text-wrap: balance` |
| Section h2 / `<summary>` (Human review, Gating, Full rationale, Dimension scores, Interview timeline) | `text-lg font-semibold text-gray-900` (18px), sentence case (remove `uppercase`) |
| h3 (The ask, Red flags) | `text-base font-semibold text-gray-900` (16px), sentence case |
| Eyebrows ("In one minute", "Key points", "Status", …) | `text-sm font-semibold text-gray-600`, sentence case (remove `uppercase`) |
| Band labels | `text-amber-700` / `text-green-700` (replace 600) |
| `unconfirmed` glyph | `text-blue-700` |
| Clear button | `bg-gray-600 hover:bg-gray-700` |
| Marginal chips | `gap`/`conditional`: `text-amber-800 bg-amber-100`; `adequate`: `text-green-800 bg-green-100` |

---

## Task A — body template (owner: `templates/admin/_assessment_detail_body.html`, `tests/integration/test_assessment_detail_page.py`)

1. **Two-column brief.** Inside `#brief`, after the h2/label block, wrap the pitch and the key-points block in `<div class="assessment-brief-grid mt-4 grid grid-cols-1 md:grid-cols-2 gap-6">`; pitch column first (`<div class="assessment-brief-pitch">` containing the "In one minute" eyebrow + prose), key points second (`<div class="assessment-brief-keypoints">` containing the "Key points" eyebrow + the existing grouped/legacy list; keep `assessment-brief-points`). When `key_points` is empty, render only the pitch column full width (`md:grid-cols-1`), and when the pitch is NULL but key points exist, render key points alone — no empty column. Inside the grid, `.assessment-prose` keeps `max-width: 68ch` but the column is narrower, so also set `assessment-brief-pitch .assessment-prose { max-width: none }` via a class `max-w-none`.
2. **Open by default:** add `open` to `<details id="rationale">` and `<details id="scores">`. Gating, timeline, raw JSON stay collapsed.
3. **Expand all / Collapse all:** two `<button type="button">` in the jump nav (`data-details-toggle="open|close"`) with `text-sm`; JS in the body template bottom `<script>` toggling every `details` inside `main` (no framework). Keyboard-operable by nature.
4. **Heading scale and sentence case** per the table; remove every `uppercase` on headings/summaries/eyebrows (keep label text identical).
5. **Contrast fixes** per the table (band labels, glyph, Clear button, two chips).
6. **Size sweep:** content text → `text-base leading-relaxed`; metadata stays `text-sm`; wrap running-text blocks (red flag `<li>` list, dimension anchor `<p>`, panel banner text, reviewer comment, consult "Asked/Concerns/Questions", timeline message bodies) in `max-w-[68ch]`.
7. **Legacy rationale:** replace the single `whitespace-pre-line` `<p>` with `{% for para in a.rationale.split('\n\n') %}<p>{{ para }}</p>{% endfor %}` inside `.assessment-prose` (keep the `assessment-rationale` class on the wrapper).
8. **Tooltips → visible text:** gating glyph meanings get a visible legend line under the gating list ("✅ met · ❌ not met · ❓ unconfirmed — the PI was not asked"), and each glyph gets `aria-label`; keep the `title`s.
9. **Anchors:** add `scroll-mt-16` to every `id` target section (`#brief #rationale #panel #scores #review #timeline #gating`).
10. **Links:** jump-nav links `text-indigo-700 underline-offset-2 hover:underline`; back-link (in wrappers — Task B) not yours.
11. **Tests** (same file): update `test_the_detail_body_uses_readable_type_sizes` (ceiling stays ≤13 `text-xs`; add `assert "bg-gray-400" not in body`, `assert "text-gray-500" not in body`, `assert 'text-base' in body`); add `test_the_brief_is_two_columns_pitch_left_points_right` (assert `assessment-brief-grid`, and `html.index("assessment-brief-pitch") < html.index("assessment-brief-keypoints")`, and that with `key_points=None` the grid has no `assessment-brief-keypoints`); add `test_rationale_and_scores_are_open_by_default` (regex `<details[^>]*id="rationale"[^>]*\bopen\b` and same for scores; gating/timeline have no `open`); add `test_expand_all_controls_render`; add `test_legacy_rationale_renders_paragraphs` (a `prose_format=None` row with two `\n\n`-separated paragraphs yields two `<p>` inside `.assessment-rationale`); add `test_gating_legend_is_visible_text`. Keep all existing tests green (`test_the_rationale_is_collapsed_and_labelled_with_its_size` only asserts the summary string — verify).

## Task B — wrappers + base (owner: `templates/admin/assessment_detail.html`, `templates/manager/assessment_detail.html`, `templates/base.html`, new `tests/integration/test_assessment_detail_chrome.py`)

1. `.assessment-prose` in BOTH wrappers → the table's values (17px, 1.6, 68ch, `p + p 1.25em`, `text-wrap: pretty`); add `.assessment-brief-pitch .assessment-prose { max-width: none; }`; add `h2.assessment-headline { text-wrap: balance; }` (Task A adds the class `assessment-headline` to the brief h2 — coordinate: B adds the CSS, A adds the class).
2. Focus visibility: in both wrappers' `<style>`: `a:focus-visible, button:focus-visible, summary:focus-visible, select:focus-visible, textarea:focus-visible, input:focus-visible { outline: 2px solid #4338ca; outline-offset: 2px; }`.
3. Print: `@media print { .assessment-jump-nav, nav, footer, form, button { display: none !important; } details:not([open]) > *:not(summary) { display: block !important; } details { open: true } main { max-width: none; } .assessment-prose { max-width: none; } }` — since CSS cannot force `open`, ALSO add a `beforeprint` listener in each wrapper's script block that sets `open` on every `details` and restores on `afterprint`.
4. `base.html`: add a skip link as the first child of `<body>`: `<a href="#main-content" class="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:bg-white focus:px-3 focus:py-2 focus:rounded focus:shadow">Skip to main content</a>`, and `id="main-content"` on `<main>`. Back-link in both wrappers: `text-indigo-700 hover:underline` (replace grey).
5. Tests (new file): skip link present on `/login` and on `/manager/assessments/{id}`; `id="main-content"` present; both wrappers emit `focus-visible` and `@media print` and the 17px prose rule (assert `font-size: 1.0625rem` and `max-width: 68ch` in the HTML); back-link class.

## Task C — regression sweep (owner: `tests/integration/test_assessment_review_ui.py` read-only check, `tests/integration/test_assessment_pi_link_rendering.py` read-only, plus `docs/audits/2026-09-11-assessment-readability/README.md` §4 → mark each item "implemented in <commit>" after A and B land). Runs AFTER A and B commit: run the three detail-page test files + review UI tests, fix nothing in templates (report instead), update the audit doc, run `ruff check tests/`.

## Task D — full gate: `./scripts/ci.sh` on the final tree (nohup + poll); then report.

Parallelism: A ∥ B (disjoint files) → C → D.

---

**Status (2026-09-11): Task A done — `b72bea9`. Task B done — `212435e`. Task C done — regression sweep clean, no template/test edits needed, audit doc §4 annotated with implementing commits and §8 added (this pass). Follow-up fix `92398d5` also landed and is noted in §8. Task D (full `./scripts/ci.sh` gate) confirmed green (3544 passed) by the coordinator.**

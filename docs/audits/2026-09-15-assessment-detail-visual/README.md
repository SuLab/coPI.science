# Assessment detail page — visual and readability audit (2026-09-15)

**Status:** revision 3 — §3 IMPLEMENTED 2026-09-15 (see §5 for the
as-built record and after-measurements). Revision 2 was the post-adversarial-audit text. A read-only auditor checked
revision 1 against the templates, the rendered HTML, the tests and the two
prior readability documents (`docs/plans/2026-09-11-assessment-readability-implementation-plan.md`,
`docs/audits/2026-09-11-assessment-readability/README.md`). It returned 3
blocking, 8 major and 7 minor corrections plus 10 missed issues. Revision 1's
H1, M6 and L3 were withdrawn or rewritten; every surviving finding now states
whether it reverses a documented decision and which test pins the current
behaviour. Corrections are marked *(audit)*.

**Subject:** `/admin/assessments/{id}` and `/manager/assessments/{id}`, both
rendered from `templates/admin/_assessment_detail_body.html` at commit
`bf62a74` (deployed 2026-09-15 00:16 UTC). Measurements are of the **admin**
route; §1 marks what is admin-only *(audit)*.

**Method.** The newest production assessment (`a4dcc53f`, ME3BP-7,
conditional 3.10, 37 consults) was rendered by the deployed code with a
forged admin session, saved as HTML, and loaded in Chromium via Playwright
at 1440×1000 and 390×844 with the site's own `static/` tree served beside it
(an earlier capture without `static/js/markdown.js` left every
`data-markdown` block empty and was discarded). Every number below was read
from the DOM with `getComputedStyle` / `getBoundingClientRect`. The
literature basis is an `engineering:literature-review` run on 2026-09-15
(30 sources); its checkable heuristics are reproduced in §4 with the
numbering used in citations. Nothing here is taken from memory.

Screenshots in this directory: `audit-top.png` (header, jump nav, brief
top), `audit-brief.png`, `signals-v2.png` (strengths/risks, one row
expanded), `audit-ask.png`, `audit-mid.png` (meta card, panel, gating, red
flags), `audit-rationale.png`, `audit-scores.png`, `audit-review.png`,
`audit-timeline.png`, `audit-mobile-signals.png` (390 px).

---

## 1. Page inventory (default state, 1440 px viewport, admin route)

Page height 9,206 px, about nine viewports. Cards 1,024 px wide inside a
1,280 px `main`. Order and default state:

| # | Section | id | Height | Default | Words | Manager route |
|---|---|---|---|---|---|---|
| 1 | Breadcrumb, H1 "Assessment detail", "LLM calls" button, jump nav | — | 160 | — | 23 | no LLM-calls button; adds a "Read-only view" paragraph |
| 2 | Brief: headline, label, pitch / key points, "Why this score" | `brief` | 1,390 | open | ~360 | same |
| 3 | Strengths and risks | `signals` | 951 | rows collapsed | 368 | same |
| 4 | The ask (recommended next experiment) | none | 1,579 | open | ~450 | same |
| 5 | Meta card: lab, screener, channel, score, recommendation, band, rubric stamp | none | 187 | — | 32 | same |
| 6 | Specialist panel banner + 37 chips | `panel` | 202 | — | 127 | same |
| 7 | Gating criteria | `gating` | 28 | **closed** | 3 rows | same |
| 8 | Red flags | none | 98 | — | "None recorded." | same |
| 9 | Full rationale (8 paragraphs) | `rationale` | 3,072 | **open** | ~1,100 | same |
| 10 | Dimension scores | `scores` | 311 | **open** | 86 | same |
| 11 | Human review | `review` | 530 | — | 127 | same |
| 12 | Interview timeline | `timeline` | 28 | closed | — | ≈37 nested details, not 131 (tool chips are admin-only) |
| 13 | Raw verdict JSON | none | 62 | closed | — | **absent** |

Type in use (leaf text nodes): 14 px ×108, 12 px ×38 (all inside
`rounded-full` chips), 16 px ×36, 17 px ×13, 18 px ×5, 24 px ×2. Weights
400/500/600/700. Headings: H1 24/700, H2 24/600 (the hub's headline), H3
16/600 ×3, H2 18/600 ("Human review"), H4 16/600 ("Unplaced turns",
admin-only). 151 `<details>` (4 named sections + 94 tool chips + 37
raw-opinion + 8 signal rows + 6 scale notes + 1 add-feedback + 1 raw JSON),
2 open by default, maximum nesting depth 1. 64 `title=` attributes. 8
`truncate`, 9 `whitespace-nowrap`. One sticky element: the jump nav (37 px
at 1440, ~60 px at 390 where it wraps to two lines); the seven linked
sections carry `scroll-mt-16` (64 px). Focus rings are defined for links,
buttons, summaries and form controls (`assessment_detail.html:43-52`) and a
print stylesheet lifts the measure cap and opens closed disclosures
(`:55-60`).

---

## 2. Findings, ranked

Severity: **H** = measurably outside an authoritative guideline and on the
reader's critical path; **M** = outside a guideline or a visible defect off
the critical path; **L** = polish. **[reverses D]** marks a fix that undoes
a documented decision; **[breaks T]** names the test that pins today's
behaviour and would need rewriting.

### H1. "The ask" is 1,579 px of un-sectioned prose with no `id` and no jump-nav entry

The tallest open block after the rationale (`audit-ask.png`). It has no
`id`, is not in the jump nav (anchors: brief, signals, rationale, panel,
scores, review, timeline), and its structure (work packages A/B/C, decision
trigger, conditions) is carried only by Markdown bold runs inside one
`assessment-prose` div (`body:342-346`). NN/g's layer-cake scanning (§4 #19)
needs real subheads; the in-page-links study (#16) found readers use them
for specific needs, and "what would this fund" is the most specific need a
reviewer has.
*Fix:* `id="ask"` + nav link (no decision reversed; `test_expand_all_controls_render`
and the nav tests do not pin the anchor count). Optionally collapse after
the leading bold sentence — but only when `prose_format == 'markdown'` and a
leading bold run is present, because the "one-sentence ask first" contract
is scout_hub ≥1.4.0 (`phase4-thread-reply.md:342-349`) and older rows have
no such line *(audit)*.

### H2. Four multi-line prose blocks are 14 px, against the page's own type rule

The 2026-09-11 plan's rule (`:21-22`) is: content text `text-base`,
`text-sm` for metadata only. Four blocks break it with multi-line prose:
the Human-review instructions (seven lines a reviewer must read before
scoring, `body:802`, `audit-review.png`), the scores footnote (`:720`), the
gating explanation (`:633`), and the signals legend/provenance
(`:314/:317`). Carbon and Atlassian, the systems that permit 14 px in
product UI, confine it to "paragraphs of no more than four lines" and
space-limited metadata (§4 #22, #23).
*Fix:* promote those four blocks to `text-base`. Keep the 17 px
`assessment-prose` reading scale: it is a documented decision (plan `:20`,
prior audit §4 #1) pinned by `tests/integration/test_assessment_detail_chrome.py:64-67`,
and 17 px is inside every source's range *(audit — revision 1 proposed
dropping it; withdrawn)*.

### H3. Five prose blocks have no measure cap; the rest of the page is on one 68ch token

`.assessment-prose` caps at 68ch (`assessment_detail.html:29`), which
renders as ~77 counted mixed-case characters — one rule, not two; revision
1's "77 ch" framing was a unit mismatch *(audit)*. The brief's two columns
deliberately drop the cap (`:39-40`, plan A1) and wrap at 56 characters,
which is fine. The genuine gaps are five blocks with no cap at all: the
scores footnote (`body:720`, measured 140 ch at 14 px, `audit-scores.png`),
the signals legend (`:314`), the mid-scale note (`:232`), the timeline intro
(`:1061`), and the provenance footnote, which uses a second token
`max-w-[80ch]` (`:317`). Evidence range is 45–90 ch, 60–75 the target (§4
#8, #10, #28, #29); 140 is outside every source.
*Fix:* `max-w-[68ch]` on the five; no decision reversed, no test pinned.

### M1. Reflow at 390 px: the page scrolls to 778 px, and the signals rows overflow their card

Two separate defects *(audit — revision 1 misidentified both roots)*:

1. The document width of 778 px is caused by the **site-wide admin sub-nav
   in `base.html`** (the "Prompt Suggestions" link's right edge is 778 px),
   not by this page. Out of this page's scope; noted for the layout owner.
2. Inside `#signals` each collapsible row's `<summary>` is 267 px wide but
   its non-shrinking children (label span `shrink-0`, `body:200`, 101 px;
   badge `shrink-0 whitespace-nowrap`, `:201`, 137 px; glyph; "show") exceed
   it, so the preview truncates to zero and the badge and control run past
   the card edge (`audit-mobile-signals.png`). The scores rows do the same:
   `w-56 shrink-0` + `w-10 shrink-0` + `w-24 shrink-0` + gaps = 396 px
   before padding (`:703/:706/:714`).

WCAG 2.2 SC 1.4.10 (§4 #7) forbids two-dimensional scrolling at 320 px
except for content that needs 2-D layout for meaning. The prior audit
(`:62`) scoped reflow out as "staff-desktop-only"; this finding stands as a
lower-priority defect, not a regression *(audit)*.
*Fix:* `flex-wrap` on the summary and drop `shrink-0` from the label; stack
the scores row below `sm:`.

### M2. Jump nav: missing anchors, one linked id without scroll margin, actions styled like links

Seven anchors; "The ask" and "Gating" are absent. Expand all / Collapse all
are real `<button>`s (`body:64-65`, pinned by `test_expand_all_controls_render`)
but styled identically to the anchors, so navigation and action are
indistinguishable *(audit — revision 1 called them links; corrected)*. Of
the ids the page links to, only `#score-rationale` (`:142`) lacks
`scroll-mt-16`; the `#add-*` ids are `<label for>` targets, not link
targets *(audit)*. NN/g (#16) warns against sticky in-page nav covering
the target heading: at 1440 the 64 px margin clears the 37 px bar, but at
390 the bar wraps to ~60 px and at 320 likely three lines, so the margin
no longer clears it.
*Fix:* add the two anchors; `scroll-mt-16` on `#score-rationale`; render
the two actions as small outline buttons; `scroll-mt-24` below `sm:`.

### M3. Strengths/risks rows: one-line truncation drops the qualifying clause, and "and N more" has no on-card recovery

The preview is a one-line `truncate` (`body:202`); for risks the
qualifying clause usually sits at the end of the sentence and is what gets
cut (`signals-v2.png`). Expanding recovers the full first three quotes, but
every one of the 8 consult rows on this page then ends in a plain-text
"and 3/4/5 more" (`_capped_body`, `src/services/assessment_detail.py:636-650`)
whose remainder lives only inside the closed timeline's consult cards,
unlinked. Carbon (#21) requires truncated content to be recoverable;
the first cut is, the second is not *(audit — missed in revision 1)*.
The badge "latest of N consults" is a deliberate provenance qualifier
(`service:834-835`) and stays; revision 1's "6 consults" is withdrawn.
*Fix:* `line-clamp-2` on the preview; make "and N more" a link to that
domain's cards in the timeline (`#timeline` + open it).

### M4. Recommendation chip and band label are unlabelled and visually interchangeable

The meta card shows the model's `recommendation` as a chip (`body:455`) and
the computed `band` as the label beside the score (`:472`). Both read
"conditional" on this row, neither says which it is, and CLAUDE.md records
that they are independent columns that can disagree. A reader cannot tell
"the hub said conditional" from "the score bands as conditional".
*(audit — replaces revision 1's M6, which miscounted duplicates and
proposed moving the card above the brief, reversing design §5 of
`docs/plans/2026-09-09-reviewer-assessment-ui-plan.md:2827-2828`.)*
*Fix:* prefix the chip "hub:" and the band "score band:", or a two-cell
label row.

### M5. Heading hierarchy skips and mixed roles

H1 → H2 (hub headline, content) → H3 ×3 → H2 "Human review" → H4 "Unplaced
turns" (admin-only; skips H3). Gating, Rationale, Scores, Timeline are
`<summary>` text with no heading element, so a screen-reader heading list
shows seven headings for thirteen sections (§4 #14). `.md-content h1-h3`
styling (`assessment_detail.html:22`) also lets model-emitted `#` headings
enter the outline, so the list is data-dependent *(audit)*.
*Fix:* every section title an H2 (inside `<summary>` for disclosures); H4
→ H3; demote `.md-content` headings to bold paragraphs.

### M6. The specialist panel banner is 37 chips in four rows

31 `gap`, 5 `adequate`, 1 `blocking` at 12 px (`audit-mid.png`) *(audit —
counts corrected)*. The chips are the only default-visible surface for the
five `adequate` consults and for chronology, because `#signals` shows the
latest consult per domain and all eight are `gap` — so they are not
redundant, but they are unreadable as a set.
*Fix:* group per domain (`chemistry ×6: gap ×5, blocking ×1`) with the
sequence on hover/expand; keep the single `blocking` visually distinct.

### M7. Every timeline message is a 256 px scroll box inside a 9,000 px page

`max-h-64 overflow-y-auto` on every message card (`body:1103`, inside the
timeline loop) plus nested `pre` scrollers (`:419`, `:1187`,
`assessment_detail.html:136`) *(audit — "first card" corrected to "every
card")*. NN/g (#18) prefers staged disclosure to nested scrolling. The
print stylesheet lifts the measure cap but not these heights, so printed
messages clip at 256 px.
*Fix:* `line-clamp-6` + "show full message"; `max-h-none` under
`@media print`.

### M8. Dead space in the brief

The pitch column ends at ~580 px while key points run to ~1,150 px
(`audit-brief.png`): about 40% of the left column is blank inside a
1,390 px card *(audit — missed in revision 1)*.
*Fix:* move "Why this score" into the left column under the pitch, or let
the key points flow in two columns below a full-width pitch.

### L1. Gate definitions are reachable only by hover on a non-focusable element

The description is a `title` on the `<li>` (`body:195`) with the ⓘ marked
`aria-hidden` (`:222`), and the legend tells the reader to "hover" (`:315`).
`title` is exempt from SC 1.4.13 but is unreachable by keyboard, touch and
screen reader (§4 #6, #17); the description appears nowhere else on the
page.
*Fix:* muted second line inside the (closed) gating card; keep `title` for
glyphs only.

### L2. Disclosure summaries sit outside their card; plain sections have the title inside

Gating, rationale, scores, timeline render a `<summary>` above a card with
its top edge removed (`body:611/:660/:699/:1060`, `border-t-0
rounded-t-none`); brief, signals, review put the title inside the card.
Two title positions alternate down the page *(audit — replaces revision
1's L3, whose "flat sections" premise was false)*.
*Fix:* one convention: title inside, with the caret at the card's left
edge.

### L3. ✗ glyph contrast 4.41:1

`text-red-600` on `bg-red-50` (`#dc2626` on `#fef2f2`). Fails SC 1.4.3 only
if treated as text; as a labelled graphical glyph it is judged under SC
1.4.11 (3:1) and passes *(audit)*. Cosmetic: `text-red-700`.

### L4. Minor content defects

- Bare DOI URL in the pitch is not linkified (`audit-brief.png`).
- The assign-reviewer select shows a bare ORCID where a name exists
  (`audit-review.png`, `body:1047`).
- Body grey varies (gray-700 in signals, `#1f2937` in `assessment-prose`);
  revision 1's claim that Markdown blocks inherit black was unsupported and
  is withdrawn *(audit)*.

### Withdrawn from revision 1

- **Default-open state of rationale/scores vs. closed gating.** The
  premise was correct but the argument was not: gating precedes the
  rationale in the DOM, the gating states already render uncollapsed in
  `#signals`, the reviewer instructions require reading the rationale
  before scoring, and the defaults are plan A2 / prior-audit §4 #2 pinned by
  `test_rationale_and_scores_are_open_by_default` (`:1729-1744`). No new
  user evidence; withdrawn.
- **"Score shown twice, band three times."** Miscount; replaced by M4.
- **"Flat sections vs cards."** Premise false; replaced by L2.

### What passes

- Body line-height 1.5–1.62 throughout; no `text-xs` outside chips
  (38/38); no `text-gray-400/500` on running text (pinned).
- All five top-level disclosures use a caret; Expand/Collapse all exists;
  nesting depth 1 (§4 #12–15).
- Every status chip carries a text label; colour is never the only signal
  (#5); chip text contrast ≈6.4:1 *(audit — corrected from 7:1)*.
- Seven linked sections carry `scroll-mt-16`.
- Focus rings on every interactive element; print stylesheet present.
- Card width 1,024 px inside GOV.UK's 1,020 px convention (#10).

---

## 3. Recommended order of work

1. H1 (`id="ask"` + nav link) and M2 (anchors, `scroll-mt-16` on
   `#score-rationale`, action buttons). Template only.
2. H2 + H3: promote the four 14 px prose blocks to `text-base`; cap the
   five uncapped blocks at 68ch.
3. M4: label the recommendation chip and band.
4. M3: `line-clamp-2` previews; link "and N more" to the timeline.
5. M1 part 2: `flex-wrap` on signals summaries; stack scores rows on `sm:`.
   Report M1 part 1 (site sub-nav) to the `base.html` owner.
6. M6 + M7 + M8: per-domain chips; clamp timeline messages; brief column
   balance.
7. M5 + L1 + L2 + L3 + L4.

Step 1–5 touch no documented decision and no pinned test other than
`test_the_detail_body_uses_readable_type_sizes` (which counts `text-xs`
and forbids gray-400/500, both unaffected). Step 6–7 need the `_signals_card`
and never-collapsed tests re-run. Every step is verified by re-rendering
this same production row at 1440 and 390.

---

## 4. Heuristics applied (literature review, 2026-09-15)

1. WCAG 2.2 SC 1.4.8 (**AAA**, satisfiable by a user-agent mechanism): ≤80 ch, line-height ≥1.5, paragraph spacing ≥1.5×.
3. SC 1.4.3 (AA): text 4.5:1, large text 3:1. 4. SC 1.4.11: UI components and meaningful graphics 3:1.
5. SC 1.4.1: colour never the only signal.
6. SC 1.4.13: hover content dismissible/hoverable/persistent; native `title` exempt but a usability liability, not a WCAG failure.
7. SC 1.4.10 Reflow (AA): no 2-D scroll at 320 px except content that needs 2-D layout for meaning (tables, images, maps, diagrams, toolbars).
8. USWDS measure 45–90 ch, target 66. 9. USWDS line-height 1.5–1.62.
10. GOV.UK ≤75 ch, 1,020 px page. 11. GOV.UK body 19/25.
12. GOV.UK accordion: use only with evidence; not when all users need the content. 13. GOV.UK details: some users avoid clicking.
14. NN/g accordions: avoid when users need most content; caret signifier; expand/collapse all. 15. NN/g progressive disclosure: ≤2 levels.
16. NN/g in-page links for long pages; avoid sticky nav covering targets.
17. NN/g tooltips: never the only place for required information.
18. NN/g complex apps: staged disclosure, no nested scrolling.
19. NN/g layer-cake scanning needs real subheads. 20. NN/g cards are grouping, not rhythm.
21. Carbon: truncation must be recoverable; "show more" for large overflow. 22. Carbon 14 px only for ≤4-line productive text.
23. Atlassian 14 px default "where space is limited". 24. Polaris badge: one or two words; never colour alone.
28. Dyson & Haselgrove 2001: 55 cpl best comprehension. 29. Nanavati & Bias 2005: ≤70 cpl.


---

## 5. As built (2026-09-15)

All seven steps of §3 were implemented in one change on the same production
row, re-rendered through the same forged-session method, and re-measured.
After-screenshots: `after-brief.jpeg`, `after-signals.jpeg`,
`after-ask-meta-panel-gating.jpeg`, `after-mobile-signals.jpeg`.

| Finding | Change | Where |
|---|---|---|
| H1 | `id="ask"`, "Ask" nav link, `<h2>`; under the markdown contract the leading bold sentence stays open and the plan sits behind "Show the full plan" | `_assessment_detail_body.html` ask block |
| H2 | Review instructions, scores footnote, gating explanation, signals legend/provenance/mid-scale line promoted to `text-base leading-relaxed` | same |
| H3 | `max-w-[68ch]` on the five uncapped blocks; provenance 80ch → 68ch | same |
| M1 | Signals `<summary>` wraps (`flex-wrap`, label no longer `shrink-0`); scores rows wrap below `sm:`; site sub-navs and top nav in `base.html` wrap instead of forcing the document wide | body template, `base.html` |
| M2 | "Ask" and "Gating" anchors; `scroll-mt-16` on `#score-rationale`; `max-md:scroll-mt-28` on every linked section (the sticky nav is ~97 px at 390); Expand/Collapse rendered as outline buttons | body template |
| M3 | Preview `line-clamp-2` on its own row; the `_capped_body` "and N more" tail and the "earlier consults" note link to `#timeline` and open it (`data-open-details`) | body template + page script |
| M4 | "Hub recommendation" and "Score and band" labels on the meta card | body template |
| M5 | Section titles are `<h2>` (inside `<summary>` for disclosures); hub headline demoted to `<p class="assessment-headline">`; "Unplaced turns" `<h4>` → `<h3>`; `.md-content h1-h3` render at body size | body template, both wrappers |
| M6 | One chip per domain with a chronological tally (`chemistry · gap ×5 · blocking`), coloured by the worst signal; cut-off replies get their own neutral chip | `summarize_panel_domains` in `assessment_detail.py`, template |
| M7 | Timeline messages `max-h-64 overflow-hidden` with a fade and a "Show full message" toggle (hidden when short); print lifts the clamp | body template, page script, both wrappers |
| M8 | "Why this score" renders under the pitch when the brief is two-column | body template |
| L1 | Gate description as a visible second line in the gating card (live-rubric rows only, same provenance guard) | `gating_descriptions` context key, template |
| L2 | Disclosure sections carry the card on the `<details>` itself; the title sits inside the card | body template |
| L3 | ✗ glyph `text-red-700` | signals macro |
| L4 | Not changed: the bare DOI is model text (autolinking is a `markdown.js` decision); the reviewer select shows `u.name`, which for that account IS the ORCID — a data fix, not a template one |

**After-measurements (same row, 1440 px):** page 9,206 → 8,103 px; the ask
1,579 → 164 px collapsed; panel banner 202 → 146 px with 8 chips instead
of 37; brief 1,390 → 1,188 px; heading list H1 → H2 ×8 → H3, no skips;
zero 14 px paragraphs over 20 words; every prose block ≤ 68ch (≈77 counted
characters). At 390 px the page's own content no longer overflows; the
signals rows wrap and the sticky nav wraps to three lines under the 112 px
scroll margin.

**Deliberately unchanged:** rationale and scores open, gating closed (plan
A2, pinned test); the 17 px reading scale; the "latest of N consults" badge
wording; the brief's two-column `max-w-none`.

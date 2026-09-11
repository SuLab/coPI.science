# Assessment detail page — readability analysis (2026-09-11)

Status: findings for operator review, corrected after an adversarial audit (2026-09-11; 15 findings, all applied — see §7). Nothing here is implemented.
Measurements are from branch head `7f718b3` as deployed 2026-09-11; every
literature claim below names the source that was fetched (URL list at the end).
Claims that could not be verified against a fetched source are marked UNVERIFIED.

## 1. What the 2026-09-10 readability pass (F5) delivered

| Change | Evidence |
|---|---|
| `.assessment-prose` block (16px, line-height 1.625, `max-width: 65ch`, `#1f2937`) on pitch, key points, the ask, rationale | `templates/admin/assessment_detail.html:25-27`, `templates/manager/assessment_detail.html:25-27`; applied at `_assessment_detail_body.html:85, :97, :111, :127, :443` |
| `text-xs` (12px) reduced from 68 uses to 11, all inside `rounded-full` chips; the rest became `text-sm` (14px) | counts: `text-sm` 89, `text-xs` 11, `text-base` 0 |
| `text-gray-400` (2.5:1) eliminated; `text-gray-500` mostly → `text-gray-600` | `text-gray-600` 81 uses, `text-gray-500` 1 |

Result: the four narrative fields are now readable at 16px in a capped measure and every running-text colour clears WCAG AA. But the page is still, by count, **a 14px page**: 89 of 102 size classes are 14px, `text-base` is never used, and one of the five 16px prose blocks — the full rationale, the largest field — sits behind a collapsed `<details>` (the collapsed dimension-scores card is a bar list, not prose).

## 2. Measured state (what a reviewer's browser actually renders)

| Factor | Measured | Source |
|---|---|---|
| Font family | Never set. Tailwind Preflight default `font-sans`: `ui-sans-serif, system-ui, sans-serif, …` → the OS UI face (San Francisco / Segoe UI / Roboto), different per visitor | `base.html:9` loads the Play CDN with no config; within the three detail templates the only `font-family` rule is `.md-content code` (monospace) |
| Body prose size / leading | 16px / 26px (1.625) inside `.assessment-prose`; 14px / 20px (1.43) everywhere else; 12px / 16px in chips | Tailwind v3 `text-sm`/`text-xs` defaults |
| Prose measure | `65ch` ≈ 520–578px depending on OS font (`ch` = advance of "0"; digits are wider than average lowercase, so 65ch renders roughly **65–75 mixed-case characters** — an estimate, not a browser measurement); **all non-prose text runs the full ~976px card width at 14px ≈ 130–145 characters per line** (estimate) | wrappers `max-w-5xl` (1024px) at `admin/assessment_detail.html:36`, `manager/assessment_detail.html:38`; `p-6`/`p-5` cards |
| Heading hierarchy | h1 24px bold; brief h2 20px semibold; **every other h2/h3/h4/`<summary>`/eyebrow is 14px uppercase semibold**, distinguished only by gray-700 vs gray-600 | `_assessment_detail_body.html:571, :125, :419, :961, :391, :434, :468, :818, :80, :96, :110` |
| Text colours | 81× `gray-600` (#4b5563, 7.56:1 on white), 23× `gray-700` (10.3:1), prose `#1f2937` (14.7:1); no pure black | palette hexes from v3 docs; WCAG luminance arithmetic in the measurement report |
| AA failures remaining (normal-size text) | band labels `amber-600`/`green-600` on white at 14px: **3.19:1 / 3.30:1** (`:252-254`); `unconfirmed` glyph `blue-500`: 3.68:1 (`:401`); `Clear` button white on `gray-400`: **2.54:1** (`:656`) | computed; independently recomputed in the audit |
| Background | `bg-gray-50` page (#f9fafb), white cards; positive polarity throughout; no dark mode | `base.html:2` |
| Letter/word spacing | font default; one `tracking-wide` on the band label | `:252` |
| Uppercase | 15 short labels; no all-caps running text | grep |
| Italics / underline | italics 1 (`(edited)`); underline 3 (one cross-link + the `pi_link` macro in each wrapper); jump-nav links are colour-only at rest; back-link is grey at rest | `:687`, `:576`, `admin/assessment_detail.html:70`, `manager/assessment_detail.html:45`, `admin/assessment_detail.html:37` |
| Alignment | left; no justification | grep `text-justify` → 0 |
| Collapsed by default | Gating criteria, **Full rationale**, **Dimension scores**, Interview timeline, Raw verdict JSON | `details#gating :390`, `#rationale :433`, `#scores :467`, `#timeline :817` |
| Paragraph spacing inside prose | markdown-stamped rows: **16px** between paragraphs — `.assessment-prose p + p` (specificity 0,1,2) outranks `.md-content p` (0,1,1), so the intended rule wins; legacy rows (`prose_format` NULL): one `whitespace-pre-line` block — blank lines survive as empty lines (~26px) but there are no `<p>` elements, so no margin control and runs of newlines render literally | `assessment_detail.html:19, :26`, `_body:443-449` |
| Responsive breakpoints | none in the three detail templates (reflow only via `flex-wrap`); `base.html`'s container carries `sm:px-6 lg:px-8` | grep `sm:|md:|lg:` in the body → 0 |
| Focus styles / skip link / print / reduced-motion / `text-wrap` / hyphenation | none of them | grep → 0 each |
| Meaning carried only by `title` tooltips | gating glyphs, band-line note, unknown-status action | `:397, :399, :401, :403`, `:262`, `:639` |
| Text volume the page carries | headline ≤200 chars; key points 3–6 bullets ≤160 chars; pitch 3–5 sentences; the ask and rationale **uncapped** (rationale paragraphs open with a bold run-in + bold summary sentence) | `prompts/roles/scout_hub/phase4-thread-reply.md:230-278` |

## 3. Literature findings, applied to this page

Confidence: H = fetched primary source with numbers; M = fetched guideline or secondary; L/UNVERIFIED as marked.

| Factor | Evidence (fetched) | Gap on this page | Recommendation |
|---|---|---|---|
| **Body size** | Rello, Pielot & Marcos CHI 2016 (n=104, eye-tracked): comprehension significantly lower at 10 and 12 **points** than at 18 points; "at least 18-point… best subjective readability at 18"; no further gain past 22. **Units are points: 18pt = 24 CSS px, 22pt ≈ 29px.** Converted, it argues for a body size well above anything shipped by the design systems below — treat it as directional evidence that 14–16px is too small, not a warrant for a specific px value (H directional / M for any px target). GOV.UK ships 19px/25px body (H). USWDS: ≥16px floor for body (H). Bernard & Mills 2000: 10 vs 12pt no speed difference, 12 preferred (M). | Prose 16px is at the USWDS floor; 87% of the page's text is 14px, including red flags, dimension anchors, panel notes, review comments and the whole timeline | Body prose **17–19px** (anchored on GOV.UK 19px and USWDS ≥16px; Rello is directional); everything a reviewer reads as content (red flags, dimension anchors, reviewer comments, consult opinions, panel notes) **≥16px**; reserve 14px for true metadata and 12px for chips only |
| **Line height** | WCAG 1.4.12 requires surviving 1.5 line-height and 2× paragraph spacing (H). Rello 2016: "marginal" line-spacing effect; objectively only 0.8 was penalised on comprehension, 1.8 only on subjective ratings, though the paper's conclusion warns against both extremes (M). Dyson 2004: longer measures "are said to" need more leading — typographic convention, not measured (L). | Prose 1.625 is fine; 14px text at 1.43 on ~130–145-char lines is under-leaded for its measure | Keep 1.5–1.6 on prose; either shorten the 14px measures or raise them to 1.5 |
| **Paragraph spacing** | WCAG 1.4.12: ≥2× font size must be survivable (robustness, not a shipped value); BDA: extra space between paragraphs (M) | Markdown rationale already gets 16px (1em) gaps; legacy rationale has blank lines but no `<p>` structure | Optionally raise `p + p` to 1.25–1.5em; render legacy rows as real `<p>` per blank line |
| **Letter/word spacing** | Chung 2002: extra spacing does not speed reading (M, snippet). Front. Psychol. 2020 (n=24 non-impaired adults): wider tracking cost **9.45 wpm on average across the whole sample**, the decrement growing with baseline speed (one 347-wpm reader lost 27 wpm) (M). Zorzi 2012 benefit is for dyslexic children (M). BDA recommends extra tracking (M, uncited). | Default tracking — correct | Leave at 0; do not add tracking; verify layout survives the WCAG user override (chips and tables are the risk) |
| **Measure** | Dyson & Haselgrove 2001: 55 CPL gave better comprehension than 100 CPL with no speed-accuracy trade-off; the 25-CPL condition fell **between** them, so shorter is not monotonically better — which is what justifies ~65–72ch rather than something narrower (H). Butterick 45–90 (H); USWDS 45–90, target 66 (H); BDA 60–70 (M). The "Bringhurst 66" attribution is UNVERIFIED. | Prose ~65–75 CPL (estimate, OS-dependent); **all other text ~130–145 CPL** | Cap every reading column (red flags, panel notes, review comments, timeline messages, dimension anchors) at **~65–72ch**; keep tables and the score bars full width; use `max-width` in `ch` on the text, not the card |
| **Text colour / polarity** | Piepenbrock 2013 (n=169, young + old): positive polarity better for acuity (η²=.30) and proofreading (η²=.06), no speed trade-off (H). Dobres 2017 agrees (M). NN/g: light by default, dark as an option; cataract readers prefer dark (H). NN/g *Low-contrast text is not the answer* (H). WCAG 1.4.3/1.4.6: 4.5:1 / 7:1 (H). "Off-black beats pure black" and "white dazzles dyslexic readers" are **UNVERIFIED** (BDA asserts the latter without a study). | Already positive polarity; body colours clear 7:1; **four failures**: band labels 3.19/3.30, glyph 3.68, Clear button 2.54 | Fix the four failures (e.g. `amber-700`/`green-700`, `blue-700`, `gray-600` button); keep gray-600 as the darkest "secondary" (never lighter); optional user-selectable dark theme later — not a default |
| **Background** | BDA: single-colour, light not white; cream optional (M, uncited) | `bg-gray-50` page, white cards — acceptable | No change required; a warm off-white card (#fcfbf9-class) is a low-risk option, not evidence-mandated |
| **Font type** | Rello & Baeza-Yates 2016 (n=97): Arial/Helvetica/Verdana/CMU/Courier recommended; serif vs sans null, repeatedly (Arditi & Cho 2005) (H). **Italics always slower** (H). Dyslexia fonts do nothing (Kuster 2018, n=170/147) (H). x-height and Inter/Atkinson claims UNVERIFIED. | Unchosen OS UI font; italics used once; no all-caps prose | A deliberate humanist sans with disambiguated I/l/1 and 0/O (system stack is acceptable; if a web font is added, it must be a **UI-plus-text** face with real 400/600 weights); never a dyslexia font; keep italics to spans |
| **Headings** | BDA: headings ≥20% larger than body, bold, spaced (M). NN/g: information-bearing first words; F-pattern arises from unformatted text (H). | Flat hierarchy: h2=h3=h4=summary=eyebrow=14px uppercase | Real scale: h2 ~20–22px, h3 ~17–18px, eyebrows sentence-case 14px; drop uppercase for anything longer than two words |
| **Structure / disclosure** | NN/g *How users read* (+58% concise, +47% scannable, +124% combined; inverted pyramid) (H, 1997 data). NN/g *Accordions on desktop*: when users need information from several sections at once, "it is better to display all the content at once (even if it results in a longer page)"; provide Expand All / Collapse All (H). | **Full rationale and dimension scores collapsed by default** — the two things a reviewer must read and compare; timeline collapsed (appropriate); no Expand-all | Open rationale and dimension scores by default; keep gating/timeline/raw JSON collapsed; add Expand all / Collapse all; keep the panel banner and red flags open (already pinned by tests) |
| **Bullets vs prose** | NN/g scannable layout (H); no evidence bullets help arguments (inference) | Key points are bullets, rationale is prose with bold run-ins — right split | Keep |
| **Alignment / justification** | Trollip & Sales 1986 ~10% slower justified (M, contested); BDA left-align (M) | Left-aligned — correct | Keep |
| **Tables for comparison** | NN/g accordion guidance (comparison needs simultaneous visibility) (M); table-vs-prose UNVERIFIED as an experiment | Dimension scores are a bar list, collapsed | Keep as a table/bars, always visible |
| **Chips / colour coding** | BDA: avoid red/green as sole signal (M); WCAG 4.5:1 for chip text (H); cognitive-load evidence UNVERIFIED | Signal chips use text + colour (good); `adequate` green-700 on green-100 = 4.57:1 (marginal); `gap` amber-700/amber-100 = 4.51:1 (marginal) | Keep text labels; darken chip text one step to clear 4.5:1 with margin |
| **Focus / skip / print / reduced motion** | WCAG 2.4.1/2.4.7 (UNVERIFIED at citation level this session); WCAG 1.4.12 robustness (H) | None present; sticky nav (`z-10`) can cover a focused anchor target | Add `focus-visible` rings, a skip link, `scroll-margin-top` on anchored sections, `@media print` that expands `<details>` and drops the sticky nav |
| **Link affordance** | WCAG 1.4.1 Use of Colour (UNVERIFIED at citation level this session) | Jump-nav links are colour-only at rest; the back-link is grey at rest | Underline links that sit in running text, or give them a non-colour affordance; make the back-link indigo at rest |
| **Small screens / reflow** | none fetched | No breakpoints in the detail templates; long chip rows and the timeline rely on `flex-wrap` | Out of scope while the page is staff-desktop-only; if reviewers use tablets, add `md:` stacking for the verdict header and timeline |
| **`text-wrap`** | MDN: `balance` for headings (≤6 lines), `pretty` for body (H, mechanics only) | absent | `text-wrap: balance` on the 20px headline; `pretty` on `.assessment-prose p` — cheap, unproven benefit |
| **Tooltips** | Not available to touch/keyboard (accessibility principle; no study fetched) | gating glyph meanings are `title`-only | Render the meaning as visible text or `aria-label` + visible legend |

## 4. Prioritised recommendations

1. **Size and measure together** (the two variables with comprehension evidence): prose 17–19px at 1.5–1.6 (anchored on GOV.UK 19px / USWDS ≥16px; Rello 2016 is directional — its 18 is points, ≈24px); every content column capped ~65–72ch; 14px only for metadata. (Dyson & Haselgrove 2001 for the measure.) — implemented in b72bea9 / 212435e
2. **Open the rationale and dimension scores by default** by adding the `open` attribute to the existing `<details>` elements — NOT by replacing them with plain cards, which would break `test_the_panel_banner_is_never_inside_a_collapsed_details`, `test_a_non_empty_red_flag_list_is_never_collapsed` and `test_the_rationale_is_collapsed_and_labelled_with_its_size` (the last pins the "Full rationale (N paragraphs)" summary string); add Expand All / Collapse All. (NN/g accordions.) — implemented in b72bea9
3. **Fix the four AA contrast failures** and the two marginal chips. (WCAG 1.4.3.) — implemented in b72bea9
4. **Paragraph spacing**: the 1em `p + p` rule already wins the cascade for markdown rows; consider raising it to 1.25–1.5em, and render legacy (`prose_format` NULL) rationales as real `<p>` elements split on blank lines. (BDA / typographic practice; WCAG 1.4.12 only requires that the layout survive a user's 2× override.) — implemented in b72bea9 / 212435e
5. **Real heading scale**; sentence-case eyebrows by removing the `uppercase` class (label strings themselves are pinned by tests — do not edit them). (BDA ≥20%.) — implemented in b72bea9
6. **Robustness**: focus-visible, skip link, `scroll-margin-top`, print stylesheet, survive the WCAG spacing override. — implemented in b72bea9 / 212435e
7. Do **not**: add letter-spacing, adopt a dyslexia font, justify text, default to dark mode, or grey text below gray-600. — respected
8. Test note: `test_the_detail_body_uses_readable_type_sizes` asserts `text-gray-400` is absent from the body via a substring check that does not catch `bg-gray-400`; when fixing the Clear button, also tighten that assertion. — implemented in b72bea9

## 5. What was NOT verified

Off-black vs pure black; white-background "dazzle"; Bringhurst's 66 CPL; Tinker's all-caps penalty; x-height as a quantified factor; efficacy of Inter/Source Sans/Atkinson Hyperlegible; cognitive load of chips; sticky-TOC benefit; hyphenation; print stylesheets; `text-wrap` comprehension benefit; Legge & Bigelow critical-print-size figures (article body returned 403).

## 6. Sources fetched

Rello & Baeza-Yates 2016 (superarladislexia.org PDF); Rello, Pielot & Marcos 2016 (pielot.org PDF); Dyson 2004 review incl. Dyson & Haselgrove 2001 (westga.edu PDF); Piepenbrock et al. 2013 (hhu.de PDF); Kuster et al. 2018 (PMC5934461); Front. Psychol. 2020 letter-spacing (PMC7090332); BDA Dyslexia Style Guide (worc.ac.uk PDF); WCAG 2.2 Understanding 1.4.12 and 1.4.3 (w3.org); GOV.UK type scale (19px/25px — it does not cite the BDA; the 16px floor is USWDS's); USWDS typography; Butterick *Practical Typography* line length; NN/g: how-users-read, F-shaped pattern, accordions-on-desktop, low-contrast, dark-mode, legibility-readability-comprehension; MDN `text-wrap-style`; Tailwind v3 docs (font-family, preflight, font-size, customizing-colors). Snippet-only: Legge & Bigelow 2011, Zorzi 2012, Chung 2002, Arditi & Cho 2005, Dobres 2017, Trollip & Sales 1986, Bernard et al., GOV.UK design notes 2022, IDA on dyslexia fonts.

## 7. Audit log (2026-09-11)

A fresh-context adversarial auditor re-counted every class, re-derived all 15 contrast ratios (all matched), re-checked 26 line citations (22 correct, 4 corrected above), fetched the primary sources and found 15 issues, all applied: Rello 2016's sizes are points not pixels (18pt ≈ 24px) — the body-size recommendation is now anchored on GOV.UK/USWDS with Rello as directional evidence; only one (not three) prose block is collapsed; the `.md-content` cascade claim was inverted (the 1em rule already wins); legacy rows do show blank-line gaps; pre-pass `text-xs` count was 68 not 62; GOV.UK does not cite the BDA; the letter-spacing 9.45 wpm figure is a whole-sample mean (n=24, downgraded to M); Rello's "and 26" / "1.8 penalty" over-readings removed; Dyson's leading claim is convention (L); CPL estimates re-stated with direction and marked as estimates; underline count 3; breakpoint and font-family scope clarified; WCAG 1.4.12 no longer cited as authority for shipped spacing; NN/g accordion wording softened to the source; link-affordance and small-screen rows added; test-conflict notes added to §4.

## 8. Implementation (2026-09-11)

**`212435e` — Task B, page chrome (`templates/admin/assessment_detail.html`, `templates/manager/assessment_detail.html`, `templates/base.html`, `tests/integration/test_assessment_detail_chrome.py`):**

- `.assessment-prose` in both wrappers moved to 17px / 1.6 line-height, `max-width: 68ch`, `text-wrap: pretty`, and a 1.25em `p + p` paragraph gap; `.assessment-brief-pitch .assessment-prose` opts out of the measure cap; `h2.assessment-headline` gets `text-wrap: balance`.
- Both wrappers gained a visible `:focus-visible` outline on every interactive element (links, buttons, `<summary>`, form controls) — the page leans on `<summary>` toggles, which had no visible focus state before.
- Both wrappers gained `@media print` rules that hide the chrome (jump nav, nav, footer, forms, buttons) and a paired `beforeprint`/`afterprint` script that force-opens every `<details>` for print and restores its prior state afterward, since CSS alone cannot open a `<details>`.
- `base.html` gained a skip link as the first focusable element on every page (visually hidden until focused) targeting a new `<main id="main-content">`.
- Back-links in both wrappers moved from grey to link-coloured indigo.
- New test file `tests/integration/test_assessment_detail_chrome.py` (6 tests) covers the skip link on `/login` and the detail pages, the prose scale, focus-visible rules, print rules, and the back-link colour.

**`b72bea9` — Task A, shared detail body (`templates/admin/_assessment_detail_body.html`, `tests/integration/test_assessment_detail_page.py`):**

- Two-column brief inside `#brief`: "In one minute" (pitch) on the left, "Key points" on the right, collapsing to a single column when either half is NULL/empty (every pre-`0043` row is both).
- `<details id="rationale">` and `<details id="scores">` now render `open` by default; gating, timeline and raw-verdict JSON stay collapsed, per the plan's deliberate exception (the panel banner and non-empty red-flag list were already pinned open by existing tests).
- Expand-all / Collapse-all buttons added to the jump nav, wired by a small vanilla-JS block at the bottom of the include that toggles every `<details>` inside `<main>`.
- Heading scale and sentence case applied per the plan's type table; `uppercase` removed from headings, `<summary>` labels and eyebrows (the pinned label strings themselves are unchanged).
- Contrast fixes: band labels `600`→`700`, `unconfirmed` glyph to `blue-700`, Clear button to `gray-600`/`gray-700` hover, and the two marginal chips (`gap`/`conditional` amber-800, `adequate` green-800); no `text-gray-500`/`bg-gray-400` remains in the body.
- Running/content text moved to `text-base leading-relaxed` inside a `max-w-[68ch]` wrapper (red flags, dimension anchors, panel notes, reviewer comments, consult opinions, timeline messages); metadata stayed `text-sm`; `text-xs` stayed chip-only.
- Legacy (`prose_format` NULL) rationale now renders one `<p>` per blank-line-separated paragraph instead of a single `whitespace-pre-line` block.
- Gating glyph meanings are now visible legend text (plus `aria-label`s) in addition to the existing `title` tooltips.
- `scroll-mt-16` added to every anchor target; jump-nav links recoloured to real link colour.
- 50 tests in `tests/integration/test_assessment_detail_page.py` pass, including the six new/updated tests the plan specified (`test_the_detail_body_uses_readable_type_sizes`, `test_the_brief_is_two_columns_pitch_left_points_right`, `test_rationale_and_scores_are_open_by_default`, `test_expand_all_controls_render`, `test_legacy_rationale_renders_paragraphs`, `test_gating_legend_is_visible_text`).

**Follow-up: `92398d5` — fix(assessments): guard empty key-points groups, sync brief CSS, tighten legend test.** Landed after A/B, before this Task-C review:

- Added the missing `.assessment-brief-keypoints .assessment-prose { max-width: none; }` rule to both wrappers (kept byte-identical between admin and manager).
- Computed `has_points` once in the body template so a `key_points` mapping whose groups are all present but empty renders no right column (collapses to one column) instead of an empty card.
- Scoped `test_gating_legend_is_visible_text` to the `<p class="gating-legend">` element itself rather than the whole page, and added a regression test for the empty-groups key-points case.

**Deviation from the plan:** the plan's `details { open: true }` line inside `@media print` is not valid CSS (the `open` state of a `<details>` element cannot be forced by a CSS declaration). Both wrappers instead ship a `beforeprint`/`afterprint` JS listener that adds/removes the `open` attribute on every `<details>` directly, restoring each element's prior state after printing — functionally equivalent to the plan's intent, implemented the only way the platform allows.

**Task C regression sweep (this pass):** ran, unmodified, `tests/integration/test_assessment_detail_page.py` (50 passed), `tests/integration/test_assessment_detail_chrome.py` (6 passed), `tests/integration/test_assessment_review_ui.py` (16 passed), `tests/integration/test_assessment_pi_link_rendering.py` (4 passed), `tests/integration/test_login_page.py` (2 passed), `tests/integration/test_manager_views.py` (33 passed), `tests/unit/test_reachability.py` (23 passed). No template or test edits were needed; `./scripts/ci.sh` was separately confirmed green (3544 passed) as part of the coordinator's Task D run.

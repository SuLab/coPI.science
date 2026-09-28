# Chat entry points, a 250-word pitch, per-dimension rationale, and key points 1.9.0 — design

Date: 2026-09-28
Status: design, approved in chat 2026-09-28; revised after an adversarial
audit the same day (§10); implementation not started
Schema at design time: production stamped `0051` (measured against
`copi-blackbird-postgres-1`, 2026-09-28)
Prompt sets at design time: scout_hub `1.8.0`, rubric `3.5.0`

Five operator requests, landed as one change because four of them touch the
same two surfaces (`/admin|/manager/assessments` and their detail pages) and
the fifth touches the same prompt file:

1. a **Chat** button on the assessments LIST page that opens the detail page
   with the chat drawer already open;
2. the detail page's **"Ask about this assessment"** nav button replaced by a
   floating chat bubble, bottom-right;
3. the **elevator pitch** raised to 250 words;
4. **every rubric dimension** carrying a rationale for its score in the
   Evidence summary;
5. **key points** reworked: a new path-to-clinic/commercialization group, a
   shorter Lab Background, drastically less jargon, ONE bullet per group, and
   Key Questions/Experiment removed.

---

## 1. Measurements this design rests on

Taken 2026-09-28 against the production database over `ssh -4` +
`docker exec … psql`, read-only. Nothing here is assumed.

**`opportunity_assessments`: 32 rows, newest 2026-09-25 17:36 UTC.**

| `key_points` shape | rows |
| --- | --- |
| six-group 1.8.0 (`indication_audience` present) | 5 |
| legacy 1.3.0–1.7.1 (`significance`/`innovation`/`commercial_potential`) | 9 |
| flat list (≤1.2.0) | 6 |
| NULL | 12 |

All five 1.8.0 rows are from run `bf6da580` (rubric 3.5.0, 2026-09-25) and all
five carry `key_questions`; so do the nine legacy rows, under the legacy label.
**14 stored rows would lose a rendered group if the key were simply deleted
from the render order** — hence §6.2.

**Elevator pitches.** The five 1.8.0 rows: 113–222 words, 797–961 characters.
All 20 rows that have a pitch: mean 156 words / 1029 characters. Today's bound
(4–6 sentences, ≤900 characters, `_PITCH_SOFT_LIMIT`,
`src/agent/simulation.py:9262`) is therefore about a 150-word bound in practice.

**1.8.0 key-point groups**, total characters per group across the five rows:
`indication_audience` 281–336 (1 bullet), `lab_background` 545–930 (2),
`proposal` 646–815 (2), `clinical_actionability` 693–960 (2), `key_questions`
247–473 (1), `commercial_opportunity` 577–1076 (2). A two-bullet group runs
~300–500 characters per bullet against a 300-character bound that only warns.

**No per-dimension rationale exists anywhere in a stored row.** `scores` is a
flat map of dimension key → integer. The UNION of top-level keys in
`raw_verdict` across all 32 rows is 17 — `company_or_project`,
`subject_agent_id`, `headline`, `key_points`, `elevator_pitch`,
`score_rationale`, `strengths`, `risks`, `competitive_landscape`,
`evidence_maturity`, `gating`, `scores`, `red_flags`, `recommendation`,
`rationale`, `recommended_next_experiment`, `confidence` — and none of them is
a per-dimension reason (individual rows carry subsets: the narrative keys are
absent from every pre-`0043` row). Structurally confirmed too:
`src/models/opportunity.py` has no such column and the sidecar skeleton
(`prompts/roles/scout_hub/phase4-thread-reply.md:483-511`) has no such key. That
is why request 4 needs a new column, not a read-path change.

---

## 2. Decisions

- **D1.** The floating bubble REPLACES the nav button; it does not join it.
- **D2.** The list page's Chat control is a link to
  `/admin|/manager/assessments/{id}#chat`; the drawer's own script opens itself
  on that fragment. No route, no query parameter, no handler change.
- **D3.** Stored rows are never rewritten. A row that carries `key_questions`
  keeps rendering it, **under the label AND in the position it was written
  with**.
- **D4.** "All rubric dimensions include rationale" means a new sidecar field
  and a new column (migration `0052`). The Evidence summary gains a neutral
  **Mid-scale** section so a 3-of-5 dimension has somewhere to appear; today it
  appears in no bucket by design.
- **D5.** The per-dimension rationale is **visible to the reviewer tier**, like
  `score_rationale` and unlike `strengths`/`risks`/`competitive_landscape`/
  `evidence_maturity`. Operator decision, 2026-09-28: the Evidence summary is
  the reviewer's surface, and a six-row list of scores with the reasons blanked
  out would defeat the request. The prompt is therefore written to tell the
  model plainly that a reviewer reads this field (§5.2) — it is *not* described
  to the model as staff-only, because that promise is what
  `STAFF_ONLY_VERDICT_FIELDS` exists to keep.
- **D6.** 250 words is a CAP on the pitch, not a target-with-slack. The
  `#assessments-summary` excerpt stays at `PITCH_DISPLAY_CHARS = 600`:
  publishing more of a sidecar field to a public channel is a content-policy
  change that needed sign-off before and still would.
- **D7.** Key points: six groups, ONE bullet each, the 300-character bound kept
  (it is per bullet, and a group is now one bullet, so a group halves), plus
  explicit plain-language rules with a worked rewrite.
- **D8.** No rubric change. `prompts/rubric/blackbird-rubric.toml` stays at
  3.5.0, so there is no `revisions.toml` entry and no review-doc regeneration
  in this cycle.
- **D9.** The simulation is not started as part of the deploy.

---

## 3. Chat entry points

### 3.1 The bubble

`templates/admin/_assessment_detail_body.html:84-88` currently renders, inside
the sticky jump nav:

```jinja
{% if chat_available %}
<button type="button" data-chat-open aria-controls="assessment-chat" aria-expanded="false" class="rounded bg-indigo-600 …">Ask about this assessment</button>
{% elif chat_enabled %}
<span class="text-sm text-gray-600">Chat unavailable while impersonating</span>
{% endif %}
```

It becomes, in full:

```jinja
{% if chat_enabled and not chat_available %}
<span class="text-sm text-gray-600">Chat unavailable while impersonating</span>
{% endif %}
```

**The rewritten condition is load-bearing and is the one thing a naive edit
gets wrong.** An `{% elif %}` cannot be left standing alone, and the obvious
repair — `{% if chat_enabled %}` — would print "Chat unavailable while
impersonating" to every ordinary staff user, beside a working bubble. No
existing test would catch it: `tests/integration/test_assessment_chat_templates.py:38-51`
never asserts that string's absence, and `:72`'s
`assert "Chat unavailable" not in body` is the kill-switch case, where
`chat_enabled` is already false. §7 adds the missing assertion.

The control itself is added at the TOP of
`templates/admin/_assessment_chat_drawer.html`, before the `<aside>`:

```jinja
<button type="button" data-chat-open data-chat-bubble
        aria-controls="assessment-chat" aria-expanded="false"
        title="Ask about this assessment" aria-label="Ask about this assessment"
        class="ph-no-capture print:hidden fixed bottom-6 right-6 z-30 flex h-14 w-14 items-center justify-center rounded-full bg-indigo-600 text-white shadow-lg hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2">
  <svg …speech-bubble glyph…, aria-hidden="true"></svg>
</button>
```

Why each part:

- **Inside the drawer partial, not the detail body.** The partial is included
  only under `chat_available` (`_assessment_detail_body.html:1385`), so the
  bubble inherits the kill switch and the impersonation refusal for free, and
  one place decides whether a chat control exists at all.
- **`z-30` against the drawer's `z-40`.** On a narrow screen the drawer is
  `fixed inset-0`; an equal or higher z-index would float the bubble over the
  open panel. Verified there is no transform/filter/z-index on
  `<main id="main-content">` (`templates/base.html:181`) to create a competing
  stacking context.
- **`print:hidden`** and **`ph-no-capture`** match the drawer's own.
- **An SVG glyph with `aria-label`/`title`**, not a visible label: the button
  is 56px round, and the accessible name stays the sentence the nav button
  used, so nothing a screen reader hears changes.
- **No `text-xs`, no `text-gray-400`/`500`.** Measured: 11 `text-xs`
  occurrences inside `<main>` today, all `rounded-full` chips, against a
  ceiling of 13 (`tests/integration/test_assessment_detail_page.py:1779`). The
  bubble adds none.
- **Not a `<details>` and not a `<form>`** — the partial's two stated rules,
  for the Expand-all script and for the no-JavaScript case. The Expand/Collapse
  script (`_assessment_detail_body.html:1344-1382`) only touches
  `[data-details-toggle]`, `details`, `[data-open-details]` and
  `[data-unclamp]`, so a `<button>` is inert to it, and
  `test_expand_all_controls_render` still finds its two nav buttons.

### 3.2 Script changes (`static/js/assessment_chat.js`)

Three additions. The file collects every opener at load
(`const openers = Array.from(document.querySelectorAll("[data-chat-open]"))`,
:101) and binds them (:955-957); the script is `defer`red, so a bubble emitted
before the `<aside>` is found. No new binding is needed.

1. **Hide the bubble while the drawer is open.** In `openDrawer` (:907), beside
   the existing `openers.forEach(… aria-expanded=true)`, add
   `hidden` to `[data-chat-bubble]`; remove it in `closeDrawer` (:930). The
   class is Tailwind's own `hidden`, which the partial already relies on for
   the drawer itself (`:21`) — not an invented name, and not the
   `aria-expanded:` variant, because the page loads Tailwind from the Play CDN
   (`templates/base.html:9`) and a variant that silently failed to compile
   would leave the bubble over the drawer with no test able to see it in
   rendered HTML.
2. **Focus return.** `closeDrawer` calls
   `state.opener.focus({preventScroll: true})` (:951) after
   `clearInertForModal()` (:941); the un-hide must happen before that call, or
   focus lands on a hidden element. `setInertForModal`'s sibling walk
   (:883-898) does reach a bubble placed as a sibling of the drawer and undoes
   exactly it, so the mobile modal path stays correct.
3. **Open on `#chat`.** After `updateCounter()` (:984): open the drawer when
   `location.hash === "#chat"`, and bind `hashchange` for the same condition.
   The opener passed is the bubble, so Escape returns focus somewhere real.
   `openDrawer` is idempotent (`if (state.open) return`, :908), so a fragment
   arrival plus a click cannot double-open or double-`loadHistory`. Verified:
   nothing in the repo uses `#chat`, an element `id="chat"`, or `hashchange`
   today, and `test_the_chat_script_names_no_route` (:157-161) is not tripped
   by a `"#chat"` literal.

Deliberately NOT done: rewriting `location.hash` on open or close. The fragment
is a permalink to "open with the chat up"; mutating history would make Back
ambiguous. A reload or a Back navigation to `#chat` therefore reopens the
drawer — intended.

### 3.3 The list-page Chat button

`templates/admin/_assessments_body.html` renders the card's one way through to
the verdict as `{{ assessment_link(a) }}` inside `<div class="assessment-card-open">`
(`:380` in the pitch box, `:408` in the no-pitch fallback). Two tests pin
exactly ONE `assessment-open-link` per card
(`tests/integration/test_assessment_queue_controls.py:1430`, `:1454`), so the
new control carries a different class.

- Each wrapper gains a macro beside its existing `assessment_link`:

  ```jinja
  {% macro assessment_chat_link(a) %}<a class="assessment-chat-link inline-flex items-center gap-1 rounded border border-indigo-300 px-2 py-1 text-sm font-medium text-indigo-700 hover:bg-indigo-50 whitespace-nowrap" href="/admin/assessments/{{ a.id }}#chat" title="Open this assessment and ask about it">chat</a>{% endmacro %}
  ```

  and its `/manager/assessments/{{ a.id }}#chat` twin in
  `templates/manager/assessments.html`. The macros live in the wrappers, not in
  the shared body, for the reason that body's header comment gives: the paths
  must be literal per surface. `_link_credits`
  (`tests/unit/test_reachability.py:261-273`) only accepts a Jinja expression
  in a `{path_param}` slot, and `_normalize_link` strips the fragment
  (`:525`), so the link credits the existing detail route and adds none.
- Both call sites render
  `{{ assessment_link(a) }}{% if card_chat_available %} {{ assessment_chat_link(a) }}{% endif %}`.
- `card_chat_available` is computed once near the top of the shared body, with
  the same expression the detail body uses:

  ```jinja
  {% set card_chat_available = request is defined and request.app is defined
       and request.app.state.assessment_chat_enabled is sameas true
       and not impersonation_banner %}
  ```

  No new context key is needed. Verified by tracing both handlers, not just the
  base helper: `src/routers/admin.py:851` and `src/routers/manager.py:654` both
  render through `_template_context`, which sets `request` and
  `impersonation_banner` (`admin.py:186-189`, `manager.py:163-166`), and
  `create_app` sets `request.app.state.assessment_chat_enabled` once
  (`src/main.py:361`) on the single app both routers mount on.
- The manager surface serves the reviewer tier too, and the chat routes admit
  admin/manager/reviewer alike, so one condition covers all three.

Consequence to accept: a card is two buttons wide. The list is
recommendation-sorted cards, not a dense table, so this costs a line of
horizontal space.

---

## 4. Elevator pitch: 250 words

### 4.1 Prompt (`phase4-thread-reply.md` item 8)

- "Four to six sentences … and **at most 900 characters**" becomes "**at most
  250 words**"; the sentence count is no longer bounded.
- The six-element order (problem → solution → how it differs → provenance
  citation → what exists and what the money buys → what a clean read-out
  enables) is unchanged, and so is the rule that elements 1–4 must END within
  approximately 550 characters. That rule is not about length: it is what keeps
  the provenance citation inside the 600 characters `#assessments-summary`
  publishes, and a longer pitch makes violating it MORE likely, not less.
- **Three** passages presuppose the old sentence bound and all three are
  rewritten together, not just the first: `:343-344` ("Where the two bounds
  conflict, cut a sentence rather than run over"), `:366` ("If something has to
  go, cut from 5"), and `:367-368` ("At four sentences, elements 4 and 6 are
  the two that must survive: merge 1 with 3 and 2 with 5 before dropping
  either"). The replacement guidance: at 250 words nothing has to be dropped —
  spend the extra room on elements 5 and 6, and keep elements 1–4 complete
  inside the first ~550 characters.
- "Minimal jargon; spell out an abbreviation the first time" stays and points
  at the shared plain-language rule (§6.3).

### 4.2 Write path (`src/agent/simulation.py`)

- `_PITCH_SOFT_LIMIT = 900` (:9262) becomes `_PITCH_WORD_LIMIT = 250`, counted
  as `len(text.split())`; the warning text names words. Verified the constant
  has exactly one use site (:4536-4542) and that no test asserts its message.
- The citation-loss alarm (:4553-4572) is UNCHANGED and becomes the
  load-bearing check: it compares the citation set of the whole pitch against
  that of `_clip_at_sentence(pitch, PITCH_DISPLAY_CHARS)` and warns when a
  source is lost. Nothing else can see a pitch whose sentence four drifted past
  the cut.
- `PITCH_DISPLAY_CHARS` (600, `src/services/assessment_headline.py:72`) is not
  touched, so the public post is unchanged in size and shape.

### 4.3 Read paths

None change. `elevator_pitch` is `Text` (`src/models/opportunity.py:79`), both
surfaces render it in full and neither clamps it
(`_assessments_body.html:360-364` records the 2026-09-14 `line-clamp-6`
removal), and the markdown path is length-agnostic. Cards and the detail brief
get taller; that is the intended cost.

---

## 5. Per-dimension rationale (migration `0052`)

### 5.1 Why a column

The read path can classify a stored score but cannot invent a reason for it.
`score_rationale` (0048) is ONE paragraph about the weighted score as a whole;
`rationale` is the panel-sourced narrative; `assessment_reviews.dimension_scores`
is a *human* reviewer's own 1–5 map with no text. So the hub has to be asked
for the field and the row has to store it.

### 5.2 Sidecar and prompt

Item 2 of `phase4-thread-reply.md` ("The six dimension scores") gains a
requirement: alongside `scores`, emit `dimension_rationales` — an object with
the same six dimension keys, each **one sentence of at most 200 characters**
saying what drove THAT score: the evidence that set it, and what would move it.

Two constraints are stated to the model explicitly, and their wording matters
(D5):

- **Never posted to Slack** — like `score_rationale`, so it may reason about
  the score freely. The "never state a number for the weighted score or the
  band" rule applies, since those are computed server-side.
- **Read by Blackbird reviewers, not only by staff** — so, exactly as item 8
  binds the published pitch, it must not restate a PI's unpublished
  disclosure. This field is deliberately NOT described as staff-only: that
  promise is what `STAFF_ONLY_VERDICT_FIELDS`
  (`src/services/assessment_chat_record.py:64`) exists to keep, and this field
  is not in it.

Skeleton gains, immediately after `"scores"` so the model writes each reason
next to the number it explains:

```json
"dimension_rationales": {
  "differentiation_unmet_need": "", "scientific_credibility": "",
  "translational_path": "", "fundable_experiment": "",
  "venture_potential": "", "team_executability": ""
},
```

### 5.3 Column and normalizer

`alembic/versions/0052_assessment_dimension_rationales.py`: one additive
nullable JSONB column, `opportunity_assessments.dimension_rationales`. No
backfill, no NOT NULL, no index. `down_revision = "0051"` (the current head,
`alembic/versions/0051_assessment_chat.py:21`), so the tree keeps a single head.

`src/models/opportunity.py`: mapped as `JSONB(none_as_null=True)` — required,
not stylistic. `tests/unit/test_json_none_as_null.py` walks every nullable JSON
column and fails a new one that omits it, because without it SQLAlchemy writes
Python `None` as the JSONB scalar `null`, a second physical encoding of
"absent" that `WHERE col IS NULL` does not match (the defect `0031` and `0036`
exist to repair).

`src/services/assessment_detail.py` gains `normalize_dimension_rationales`,
modelled on `normalize_key_points`:

- accepts a non-empty dict whose values are non-blank strings; **keys are
  `strip().lower()`-normalized**, exactly as `build_assessment_detail`
  normalizes the `scores` map (`:1188-1192`) — without this, a sidecar
  emitting `"Scientific_Credibility"` would score fine and silently render no
  rationale;
- caps at 20 entries and 50 characters of key; strips values;
- rejects anything else → `None`, with `raw_verdict` keeping the original. A
  malformed narrative field never costs the verdict (the A20 rule).
- Keys are NOT validated against the live rubric's dimension keys: a row is
  rendered against the revision that scored it, and a future revision that
  renames a dimension must not make an older row's reasons unstorable. An
  unknown key simply matches no rendered dimension.

`_persist_assessment` assigns it next to `score_rationale`, and warns — never
drops — when a rationale exceeds 200 characters or when a scored dimension has
no rationale. Same warnings-only policy as key points.

### 5.4 Read path

`build_assessment_detail` (`src/services/assessment_detail.py:1211-1241`) adds
`"rationale": …` to each entry of `dimensions`, looked up by normalized key,
in BOTH loops — the revision-named dimensions and the unnamed-key fallback — so
a row scored under an older revision still shows its reasons.

`derive_strengths_and_risks` changes in three ways:

1. every DIMENSION entry carries `"rationale"` (strengths, risks, and the
   not-scored entries in `unestablished`). Entries from the other three
   sources (`gating`, `red_flag`, `consult`) carry the key too, always `None`,
   so the entry shape stays uniform and the pinned shape test below stays a
   single set comparison;
2. a FOURTH bucket, `mid_scale`, collects the dimensions today counted in
   `mid_scale_count` and put in no bucket (:992-994). Same entry shape, same
   `body` (the weight note), plus the rationale;
3. the contract docstring's bucket table — the function's actual contract — is
   updated for both.

**Two pinned tests move with it**, and they are exact-equality assertions, so
they fail the moment either change lands:
`tests/unit/test_assessment_strength_risk_derivation.py:117-120`
(`set(result) == {...}` — gains `mid_scale`) and `:134`
(`set(entry) == {"source","label","detail","body","preview","note"}` over every
entry in all buckets — gains `rationale`).

`templates/admin/_assessment_detail_body.html`:

- **`signal_row`'s two three-key maps must both learn the new kind**, or the
  page 500s: `:250` (`{'strengths':…, 'risks':…, 'unestablished':…}[kind]`,
  whose miss yields `Undefined` and raises at `glyph[1]`) and `:252` (the
  `signal-*` class map). A `mid_scale` entry is added to each — a neutral glyph
  (`–`), `text-slate-700`, aria-label "Mid-scale".
- `signal_row` renders `item.rationale`, when present, as a muted sentence
  under the label/detail line (`text-sm text-gray-600`, inside the existing
  `min-w-0` wrapper so it wraps rather than pushing the badge off).
- A new `<section class="assessment-signals-neutral">` sits between Risks and
  the landscape/maturity sections, slate-styled like "Not established", headed
  **Mid-scale**, with a one-line explanation ("a real, neutral answer — neither
  a strength nor a risk at this revision's thresholds"). The class is
  deliberately NOT `assessment-signals-midscale`: `signals-midscale` is already
  the class of the count paragraph at `:300`, and two elements whose class
  names are substrings of one another make both the tests and future greps
  ambiguous.
- **The count paragraph at `:297-313` is reworded.** All four of its variants
  today end "so neither column lists it/them", which becomes false the moment
  the section lists them. The new wording states the count and points at the
  section; the "neither column" phrasing survives only where it is still true.
  `tests/integration/test_assessment_detail_page.py:2426-2428` and `:2448-2450`
  assert on that text ("sit mid-scale", "All 6 scored dimensions",
  "5 of 6 scored dimensions", and that "All " does not appear in the paragraph
  when one dimension is not mid-scale) and move with it.
- **The `#scores` disclosure (`:804-834`) renders `d.rationale` too**, beside
  each bar. This is not duplication for its own sake — it is what makes §5.5's
  citation land somewhere useful and what keeps the parity rule true for rows
  whose revision is unknown (see §5.5).
- The provenance paragraph (`:401-416`) gains one clause: the per-dimension
  reasons are BlackbirdBot's own text from the verdict sidecar, like "In the
  hub's words".

**The test helper `_signal_columns`
(`tests/integration/test_assessment_detail_page.py:1948-1965`) must learn the
new section.** Its `risks_end` is
`min([k] + [card.index(cls) for cls in ("assessment-signals-landscape", "assessment-signals-maturity") if cls in card])`;
a new section between Risks and those two would fold into the risks slice and
silently weaken every `assert X not in risks` — precisely the failure its own
docstring was written to prevent. `assessment-signals-neutral` joins that
tuple.

Not `viewer_is_staff`-gated (D5), and not added to
`STAFF_ONLY_VERDICT_FIELDS`. The precedent is `score_rationale`, which is hub
prose about the scores and renders for every tier
(`_assessment_detail_body.html:140`, `:190`; chat record at
`assessment_chat_record.py:337-342`, with no tier test).

### 5.5 Chat record

`src/services/assessment_chat_record.py:491-515` builds one block per entry of
`detail["dimensions"]`, plus a "none recorded" fallback at `:514-515`. Each
block gains the stored rationale as its quoted line when present, keeping
`anchor="scores"`.

Both halves of that sentence matter:

- **The anchor stays `scores` only because §5.4 also renders the rationale in
  the `#scores` disclosure.** Otherwise "Show in page" on "why did
  translational path score 3?" would jump to a list of bars that does not
  contain the quoted sentence. The parity test's anchor check
  (`tests/integration/test_assessment_chat_parity.py:344-352`) only verifies
  the id exists, so nothing else would catch it.
- **Rendering in `#scores` is also what keeps the containment rule true for
  every row.** Parity is record ⊆ page (`:322-328`, verified), and
  `derive_strengths_and_risks` contributes NO dimension entries when the row's
  revision is unknown (`assessment_detail.py:962`) or its `scale_max` unusable
  (`:985-986`), while `build_assessment_detail` still populates `dimensions`
  from the stored score keys (`:1228-1241`). Evidence-summary rendering alone
  would therefore quote, for those rows, a rationale the page shows nowhere —
  a breach the parity fixture (a live-stamped revision, `:224`) could not
  catch.

`tests/assessment_chat_support.py`'s hand-built `synthetic_detail` (`:180-185`
for `dimensions`, `:201-222` for `verdict_signals`) backs
`test_assessment_chat_record.py`, `test_assessment_chat_service.py` and
`test_assessment_chat_stream.py`. It gains the `rationale` key and the
`mid_scale` bucket, or those three suites exercise a shape production no longer
produces.

### 5.6 Deploy-order consequence

`0052` is additive, so old code against the new schema is safe. New code
against the old schema breaks both ways, exactly like `0048`/`0049`/`0050`:

- READ: the new code maps the column, so every `select(OpportunityAssessment)`
  raises `UndefinedColumn` — both list pages, both detail pages,
  `src/services/review_bot.py:539` (on the worker, so every
  `review_feedback_analysis` job fails), `src/routers/reviews.py` (so feedback
  submit/edit 500s) and `src/services/directory.py:461`;
- WRITE: `_persist_assessment` names it in the INSERT, and that write is
  best-effort, so **every verdict of a running simulation is lost** to one
  ERROR line while the Slack replies keep looking normal.

`CLAUDE.md` gains a deploy box saying so.

---

## 6. Key points → scout_hub 1.9.0

### 6.1 The six groups

| key | label | bullets |
| --- | --- | --- |
| `indication_audience` | Indication / Audience | 1 |
| `lab_background` | Lab Background | 1 |
| `proposal` | Proposal | 1 |
| `clinical_actionability` | Clinical Actionability | 1 |
| `path_to_clinic` | Path to Clinic / Commercialization | 1 |
| `commercial_opportunity` | Commercial Opportunity | 1 |

`key_questions` leaves the write contract. The deciding question is not lost
from the page: item 5 (`recommended_next_experiment`) carries the experiment,
its readout, its threshold, its cost and its duration, and renders as "The ask"
directly under the brief.

Item 7 is rewritten group by group:

- **`indication_audience`** — unchanged in substance, now explicitly in plain
  words: the condition, roughly how many people have it, what goes wrong
  biologically, what those patients get today.
- **`lab_background`** — one bullet, and SHORTER: who the PI is and the
  specific published or interview-established work this idea builds on. The
  1.8.0 second bullet (wider platform, prior spin-outs, existing IP) is dropped
  from the contract; its IP half already belongs to `commercial_opportunity`,
  which asks for the novelty/IP caveat. The "state only what the public
  profile, the publications or this interview established; leave out anything
  not on that record" rule is kept verbatim —
  `tests/unit/test_rubric_prompt_sync.py:329-331` asserts the clause.
- **`proposal`** — one bullet: what the thing is, how it works, why it differs
  from what exists. The 1.8.0 "The work: …" half moves to `path_to_clinic`.
- **`clinical_actionability`** — one bullet: what patients get today, the
  nearest clinical-stage alternative with its stage, and how this differs. The
  route/endpoint/regulatory half moves to `path_to_clinic`.
- **`path_to_clinic`** (new) — one bullet: the development pathway from here to
  use in patients — the animal or disease model the next step runs in, the
  validation or IND-enabling work between now and a first-in-human or
  first-clinical-use study, the regulatory route or precedent, and who would
  carry it forward (the lab, a spin-out, a partner).
- **`commercial_opportunity`** — one bullet: the realistic shape (licence,
  platform partnership, spin-out), the closest deal comparable or funding
  signal with its date, and any novelty or IP caveat.

The closing "together the six must let a reviewer who reads nothing else say …"
sentence is re-derived for the new set: who this is for, who is behind it, what
it is, how it would change care, how it reaches patients, and why it is worth
building.

### 6.2 Retiring `key_questions` without hiding 14 rows

`src/services/assessment_detail.py` holds `KEY_POINT_GROUPS` (six, 1.8.0) and
`LEGACY_KEY_POINT_GROUPS` (five, 1.3.0–1.7.1); `key_point_sections` renders a
legacy-shaped row under the legacy labels and every other row under the current
labels followed by any legacy-only group it also carries.

Moving `key_questions` out of `KEY_POINT_GROUPS` naively would make it
legacy-only, so the five 1.8.0 rows would render it under the LEGACY label
("Key questions / experiments") instead of the one they were written with, and
— because legacy-only groups are appended — at the END instead of fifth. D3
forbids both drifts. So:

```python
#: Retired from the write contract by scout_hub 1.9.0, still rendered for rows
#: written under 1.8.0, under the label AND in the slot 1.8.0 gave it.
RETIRED_KEY_POINT_GROUPS = (("key_questions", "Key Questions/Experiment"),)
```

and the render order for a non-legacy row becomes
`indication_audience, lab_background, proposal, clinical_actionability,
key_questions (retired), path_to_clinic, commercial_opportunity` — the retired
group in its historical fifth slot, rendered only when the stored value has it.
A 1.9.0 row simply has nothing there. Legacy-only groups are appended after, as
today, and no key may render twice.

`key_point_shape` needs one adjustment: it classifies on `_CURRENT_ONLY` vs
`_LEGACY_ONLY` key sets, and with `key_questions` out of the current set a
1.8.0 row would classify `"mixed"`. The retired keys are therefore subtracted
from `_LEGACY_ONLY_KEY_POINT_KEYS` for classification — a retired key is
evidence of neither shape. This changes classification only; rendering is
governed by the order above.

`KEY_POINT_ACCEPTED_KEYS` gains `path_to_clinic` and keeps every current,
retired and legacy key, so neither skew direction drops the field.

**Three pinned tests move with this**, none of which the first draft named:
`tests/unit/test_key_point_sections.py:16-32`
(`KEY_POINT_GROUPS == (…)` literal, and
`KEY_POINT_ACCEPTED_KEYS == frozenset(CURRENT) | frozenset(LEGACY)`, which must
become the three-way union), the `# shared key only` comment at `:39` (now
false — `key_questions` is evidence of neither shape), and
`tests/unit/test_rubric_prompt_sync.py:287-302`, which pins the skeleton's
`key_points` dict AND its key order. `RETIRED_KEY_POINT_GROUPS` gets its own
pinned assertion.

### 6.3 Plain language

A new paragraph after the group list, binding EVERY key-point bullet:

- write for an intelligent reader who is not a specialist in this field;
- expand every abbreviation on first use, and gloss every gene, protein,
  receptor or pathway symbol in plain words the first time it appears
  (`IGHV4-34` → "a particular antibody gene segment, IGHV4-34");
- no stacked noun phrases and no slash-separated alternatives — the two rules
  item 6 already applies to the headline;
- one main clause per bullet;
- at most 300 characters.

With a worked rewrite taken from a real stored bullet (run `bf6da580`, `konig`,
`indication_audience`), so the example is in the model's own register:

> **Not:** "Systemic lupus erythematosus: on the order of 200,000-300,000 US
> patients, with a refractory fraction in the tens of thousands driven by
> autoreactive B cells and autoantibody (anti-dsDNA, anti-Sm/RNP). Today they
> get steroids, mycophenolate, belimumab, anti-CD20 rituximab/obinutuzumab
> off-label, or trial CD19 CAR-T."
>
> **Write:** "Lupus, an autoimmune disease affecting roughly 250,000 people in
> the US; for the tens of thousands whose disease resists treatment, the immune
> cells driving it survive today's drugs, which suppress the whole immune
> system rather than removing those cells."

### 6.4 Engine warnings (`src/agent/simulation.py:4578-4654`)

- `_KEY_POINT_GROUP_BULLETS` (`:9269`) becomes the six new keys, each `1`.
  `tests/unit/test_rubric_prompt_sync.py:326` asserts its key order equals the
  skeleton's, so the two move together; its `words` map (`:319`) already
  handles an all-one-bullet set.
- **The retired-key warning needs a NEW site, not an edit to the existing
  one.** The legacy warning at `:4610-4624` fires on
  `if shape in ("legacy", "mixed")`, and with §6.2's classification fix a
  1.8.0-prompt sidecar classifies `"current"`, so that branch never sees it. A
  separate, milder warning goes inside the `shape in ("current", "mixed")`
  branch: "`key_questions` is retired as of scout_hub 1.9.0; stored and
  rendered under its 1.8.0 label". The "is prompts/roles/scout_hub at 1.9.0 on
  this host?" hint stays on the genuinely-pre-1.8.0 path.
- The absent-group, per-group-count and 300-character warnings are unchanged in
  mechanism; only their expectations move. `_KEY_POINT_BULLET_CHARS` stays 300.
- **Two integration assertions move with the counts**, neither named in the
  first draft: `tests/integration/test_assessment_narrative_fields.py:474`
  asserts `"key_points.key_questions carries 2 bullets (contract asks for 1)"`,
  which can no longer fire once the key leaves `_KEY_POINT_GROUP_BULLETS`, and
  `:419` asserts `"key_points.lab_background carries 1 bullets (contract asks
  for 2)"`, which stops firing because one bullet is now correct. Both are
  re-pointed at groups that still violate the new counts.

### 6.5 `role.toml`

`prompts/roles/scout_hub/role.toml` `version` → `1.9.0`. The manifest's comment
mandates the bump on any edit to the set, and the run-start announcement stamps
it.

---

## 7. Verification

`./scripts/ci.sh` is the whole gate (alembic single-head + a `0018`-floor round
trip, ruff on tests at zero and on `src/` under `SRC_LINT_MAX=231`, full pytest
with `COV_MIN=60`). Run on the HOST, not through the sshfs mount.

`.venv-test/bin/python scripts/sync_prompt_set_docs.py` after the prompt edits
and before the full run — `tests/unit/test_doc_prompt_sync.py` asserts
`docs/specs/2026-08-07-hub-bot-prompts.md` embeds the prompt files verbatim.

| area | test work |
| --- | --- |
| prompt sync (`tests/unit/test_rubric_prompt_sync.py`) | six groups incl. `path_to_clinic`, order, 1 bullet each, `key_questions` gone from item 7 and the skeleton (`:287-302`, `:317-326`); `dimension_rationales` in item 2 and the skeleton, with a new `set(skeleton["dimension_rationales"]) == set(skeleton["scores"])` assertion mirroring the existing `scores`↔`RUBRIC_WEIGHTS` check (`:85-95`) — without it a skeleton typo stores a rationale under a key no dimension matches and renders nothing, silently; the plain-language paragraph; Lab Background's record rule still present (`:329-331`) |
| pitch contract (`tests/unit/test_pitch_contract.py`) | `test_the_pitch_sentence_count_is_four_to_six` (`:37-38`) is REPLACED by a 250-word assertion; the problem-first, closes-on-readout and 550-character assertions stay |
| key-point rendering (`tests/unit/test_key_point_sections.py`) | the pinned `KEY_POINT_GROUPS` tuple and the `KEY_POINT_ACCEPTED_KEYS` union (`:16-32`) move; a new pinned `RETIRED_KEY_POINT_GROUPS`; the `# shared key only` comment at `:39` corrected; new cases — a 1.8.0 row renders `key_questions` fifth under "Key Questions/Experiment", a legacy row renders it under "Key questions / experiments", a 1.9.0 row renders six groups in order, a mixed row hides nothing, no key renders twice |
| write acceptance (`tests/integration/test_assessment_narrative_fields.py`) | `path_to_clinic` stores; `key_questions` still stores and logs the retired-key warning; an unknown key is still dropped to NULL with the naming warning; `:419` and `:474` re-pointed (§6.4) |
| pitch write path | a 251-word pitch warns and still stores; 250 does not warn; the citation-loss alarm still fires when sentence four falls outside 600 chars |
| dimension rationale | `normalize_dimension_rationales` accepts/rejects the §5.3 shapes and lower-cases keys; `build_assessment_detail` threads it through both loops; `derive_strengths_and_risks` carries it on dimension entries and `None` elsewhere; a NULL column renders exactly as today; the two exact-equality contract asserts in `tests/unit/test_assessment_strength_risk_derivation.py` (`:117-120`, `:134`) move |
| mid-scale | a row with a 3-of-5 dimension renders it in `assessment-signals-neutral`; a row with none renders the reworded paragraph and no empty list; `_signal_columns`' `risks_end` tuple (`tests/integration/test_assessment_detail_page.py:1948-1965`) gains the new class; the two paragraph-text assertions (`:2426-2428`, `:2448-2450`) move |
| chat record | dimension blocks quote the rationale and still anchor `scores`; `tests/assessment_chat_support.py::synthetic_detail` gains both new shapes; `test_each_tier_record_carries_only_what_its_page_renders` passes on both tiers and both `prose_format` values, including a row whose revision is unknown |
| bubble (`tests/integration/test_assessment_chat_templates.py`) | `data-chat-open` present with no nav button; **"Chat unavailable" ABSENT for a normal staff session** (the assertion §3.1's condition needs and no test has today); present under impersonation; absent under the kill switch; `z-30`/`print:hidden` present; readable-type-size ceiling still met |
| list link (`tests/integration/test_assessment_queue_controls.py`) | `assessment-chat-link` renders once per card on both surfaces with the `#chat` fragment; `assessment-open-link` count stays 1 (`:1430`, `:1454`); absent under impersonation and the kill switch; no `/admin/` URL on the manager page |
| reachability | inventory unchanged — the fragment link credits the existing detail route and adds none |
| migration (`tests/unit/test_migration_checks.py`) | `test_supported_start_revisions_are_exactly_the_documented_set` (`:274-281`) gains `"0051"` and moves `DEFAULT_TARGET` to `"0052"` |

Manual check after deploy, before any run: open one detail page, confirm the
bubble opens the drawer and Escape returns focus to it; open a list page, click
**chat**, confirm the drawer is open on arrival.

---

## 8. Deploy

Migration `0052` plus a prompt-set bump: a migrate-before-serve deploy with a
prompt/image pairing hazard.

**Commit before building.** `Dockerfile:19-22` runs `git clean -ffdx` in the
builder stage, so an UNTRACKED
`alembic/versions/0052_assessment_dimension_rationales.py` is dropped from the
image while the modified, tracked `scripts/migrate/preflight.py` (carrying
`DEFAULT_TARGET = "0052"`) is kept — the image then targets a revision it does
not contain, and the failure surfaces mid-deploy, after the dump, as an alembic
"can't locate revision" or a stamp mismatch. `run_migration.sh:196-220` only
compares `.build_info.json`'s commit to host HEAD and WARNs on the dirty count,
so it does not catch this. Per CLAUDE.md's `.dockerignore` box,
`git status --porcelain --untracked-files=all -- src templates static prompts alembic scripts pyproject.toml alembic.ini`
must print nothing before the first `$DC build`.

Preflight plumbing (`scripts/migrate/preflight.py`), all five together:
`DEFAULT_TARGET` `0051` → `0052` (`:78`); `REVISION_ORDER` gains `"0052"`
(`:456-460`); `SUPPORTED_START_REVISIONS` gains `"0051"` (`:141-145`) —
production is stamped 0051, so it is the starting point and must be a supported
start, not a BLOCK, the same reasoning the file records for 0042…0050;
`PLANNED_OBJECTS` gains
`PlannedObject("0052", "column", "dimension_rationales", "opportunity_assessments")`
(nothing derives it from the migration files, so the manual entry is required);
and the revision-keyed prose at `:140`, `:205`, `:451` ("0019-0051",
"0036-0051") is re-dated.

```bash
DC="docker compose -f docker-compose.prod.yml"
# 0. /admin/simulation must show NO live engine before the tree lands:
#    prompts/ is the host working tree and _load_file read_text()s per use.
# 1. Commit first (see above), then:
for s in blackbird-app worker agent; do
  docker image tag copi-blackbird-$s:latest copi-blackbird-$s:rollback-pre-0052
done
$DC build blackbird-app worker
$DC --profile agent build agent
./scripts/migrate/run_migration.sh              # rehearse (writes nothing)
./scripts/migrate/run_migration.sh --apply      # dump → preflight → apply → postflight
$DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0052)
$DC up -d blackbird-app worker
$DC up -d agent                                 # supervisor returns IDLE
```

- **The agent rebuild is required.** `src/` is baked, `prompts/` is mounted.
- **The hazardous half is prompt-without-image**: a 1.9.0 sidecar carrying
  `path_to_clinic` hits an old image whose `KEY_POINT_ACCEPTED_KEYS` does not
  know the key, `normalize_key_points` returns `None`, and **`key_points` is
  stored NULL for every verdict**, surviving only in `raw_verdict`. The same
  window writes NULL into `dimension_rationales` forever.
  Image-without-prompt is benign: an old sidecar emits neither new key, and the
  union acceptance keeps `key_questions` storable.
- **No run may start between landing the tree and `up -d agent`.**
- The simulation is NOT started (standing operator rule). Start the next run
  FRESH: a resume would mix 1.8.0 and 1.9.0 key-point shapes and pre/post-`0052`
  rationales in one run, though per-row stamps keep them separable.
- Rollback: redeploy `rollback-pre-0052` and revert the commit. The column is
  harmless to old code; `alembic downgrade 0051` drops it and every rationale in
  it. No rubric revision entry is needed — 3.5.0 is untouched (D8).
- `CLAUDE.md` gains the `0052` deploy box and updated key-point and pitch
  descriptions. Its sync test (`tests/unit/test_claude_md_disclosure_sync.py`)
  is unaffected provided the reply-only bullet keeps "verdict inline",
  "thread_guidance" and "unpublished", and no new clause containing "a PI or
  another lab sees" names gating, recommendation, red flags or confidence.

---

## 9. Out of scope and residual risk

- **Stored rows are not rewritten.** The five 1.8.0 rows keep two bullets in
  four groups, keep `key_questions`, and have no `dimension_rationales`. They
  will read differently from anything written after this deploy. That is the
  archive rule, not an oversight.
- **Jargon is prompt-enforced only.** No code can measure readability; the
  300-character bound and the abbreviation rule are the only mechanical checks
  and both are warnings. The first 1.9.0 run is the real test, as 1.8.0's was.
- **One bullet per group loses content.** Four groups lose their second bullet;
  the material is redistributed (`proposal`'s "The work" and
  `clinical_actionability`'s route half into `path_to_clinic`) or dropped
  (`lab_background`'s platform/track-record bullet). A reviewer who valued the
  lab's wider platform must read the transcript or the rationale for it.
- **A 250-word pitch raises the citation risk.** More prose before element 4
  means more chances the provenance sentence falls outside the published 600
  characters. The existing alarm reports it after the fact; nothing prevents it.
- **The reviewer tier now reads the hub's per-dimension reasoning** (D5). The
  prompt is told so, but nothing mechanically stops the model putting an
  unpublished disclosure there — the same residual the published elevator pitch
  has carried since 2026-09-09.
- **The bubble overlaps the bottom-right corner** of a long page. Nothing
  clickable sits there on the manager wrapper; the admin wrapper's trailing
  blocks were not enumerated, so this is worth one look in the manual check.
- **Not addressed here:** the `/admin/assessments` "Dimension distribution"
  table still pools verdicts scored under different weights; the review bot
  still never sees headline, pitch or key points; no web/news tool for the hub;
  no PI-profile enrichment for Lab Background.

---

## 10. Audit record

This spec was audited adversarially against the tree on 2026-09-28 (Opus, xhigh
effort, read-only) before any code was written. The audit returned 21 confirmed
defects; every one is folded into the text above rather than listed as
follow-up work. The ones that changed a DESIGN decision rather than a reference:

1. `dimension_rationales` was described to the model as staff-only while being
   rendered to reviewers — incoherent, and it would have leaked a promise the
   prompt makes. Resolved as D5 (operator decision): reviewer-visible, and the
   prompt says so.
2. The `{% elif %}` edit in §3.1 was not expressible as written, and its naive
   repair would have shown the impersonation notice to every staff user.
3. `signal_row`'s two three-key dict lookups would have raised `UndefinedError`
   on a `mid_scale` kind and 500'd both detail pages.
4. The retired `key_questions` group would have moved from fifth to last on the
   five 1.8.0 rows — a D3 violation the first draft claimed to prevent.
5. The retired-key warning cannot be an edit to the legacy branch; under this
   spec's own classification fix that branch never fires for a 1.8.0 sidecar.
6. Quoting the rationale in the chat record while rendering it only in the
   Evidence summary would break parity for rows whose rubric revision is
   unknown, and would anchor the citation at a disclosure that does not show
   it.
7. Rationale keys had to be normalized the way `scores` keys already are.
8. The new Mid-scale section would have silently widened the risks-column test
   slice, and the kept count paragraph would have asserted the opposite of what
   the section shows.
9. The deploy sequence omitted the commit that `git clean -ffdx` in the builder
   makes mandatory for a new untracked migration file.

The remainder were unnamed tests and pinned contracts that move with the change
(§5.4, §6.2, §6.4, §7) and line-reference corrections. The audit could not reach
the production database; every figure in §1 is the author's own measurement,
re-stated there with the query conditions that produced it.

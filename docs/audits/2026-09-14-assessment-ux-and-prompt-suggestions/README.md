# Assessment UX, prompt-suggestion control, and Slack strikethrough — investigation (2026-09-14)

Nine operator-requested changes. This document records what the code and the
**production database** actually say about each, measured rather than assumed;
`docs/plans/2026-09-14-assessment-ux-and-prompt-suggestions-implementation-plan.md`
is the plan built on it.

Everything below was measured on 2026-09-14 against the live `copi` database of
the `copi-blackbird` stack (`alembic_version = 0047`) and against the working
tree at `9df21e1`.

---

## 0. The nine requests, mapped to code

| # | Request | Owning surfaces |
|---|---|---|
| 1 | Assessment titles are overwhelming; make them friendly, readable, specific, name the method and what it is for | `prompts/roles/scout_hub/phase4-thread-reply.md`, `_persist_assessment`, both assessment pages, `render_assessment_headline` |
| 2 | Add a bolded "key questions / experiments" group to key points | phase4 prompt, `KEY_POINT_GROUPS`, `normalize_key_points`, both assessment pages |
| 3 | Colour-coded strengths / risks box under the top card on the detail screen | `src/services/assessment_detail.py`, `templates/admin/_assessment_detail_body.html` |
| 4 | Human review from the assessments LIST page — compact, click-to-expand, no rubric dimensions, labelled "Quick scoring" | `templates/admin/_assessments_body.html`, `src/routers/reviews.py` |
| 5 | List page shows key points + elevator pitch side by side; pitch gains research background / source paper / score rationale; clinical-actionability group; visual separation with boxes and tinted backgrounds | phase4 prompt, migration, `directory.list_assessments`, both list wrappers, shared list body |
| 6 | Prompt suggestions must be able to target hub prompts, PI prompts, or both; a manual button to generate from current reviews; nothing automatic | `src/services/review_bot.py`, `prompts/review-bot.md`, `src/services/assessment_reviews.py`, `src/routers/reviews.py`, `templates/manager/prompt_suggestions.html` |
| 7 | Replace the card-bottom checkmarks with rubric scores, hidden by default | `directory.list_assessments`, shared list body |
| 8 | *(numbering note: request 7 in the operator's message covers items 7 above)* | — |
| 9 | Slack renders strikethrough where it should not | `src/agent/slack_client.py::markdown_to_mrkdwn`, four prompt files |

---

## 1. Titles (request 1) — measured, and worse than it looks

Two separate fields feed the title, and the prompt constrains only one of them.

`prompts/roles/scout_hub/phase4-thread-reply.md` item 6 defines `headline`
("one sentence, at most 200 characters … name the mechanism or tool, the target
disease or patient population, and the reason this is fundable"). It says of
`company_or_project` only that it "already carries" the project label — **there
is no contract for `company_or_project` anywhere in the prompt set.**

Measured over `opportunity_assessments`:

| field | rows | min | mean | max | over its display cap |
|---|---|---|---|---|---|
| `company_or_project` | 20 | 81 | 130 | 188 | **12 of 20 exceed `PROJECT_DISPLAY_CHARS = 120`** |
| `headline` | 8 | 182 | 200 | 232 | **3 of 8 exceed `_HEADLINE_SOFT_LIMIT = 200`** |

Consequences, both live today:

* `src/services/assessment_headline.py::_clip` hard-cuts `company_or_project`
  at 120 characters with no ellipsis and no word boundary
  (`assessment_headline.py:67-70`), so **any headline posted for one of those 12
  rows is published hard-cut at 120 characters.** Corrected after audit: the 60%
  is a measurement of STORED ROWS, not of posts. A headline is posted only when
  `announce = terminal and not already_announced` (`simulation.py:3236`) and only
  when a connected Slack client and channel exist
  (`_post_assessment_summary`, `:3770-3782`), and every pre-`0041` row has
  `summary_posted_at IS NULL` whatever actually happened in Slack — so the number
  of posts actually affected is NOT derivable from these 20 rows.
* `_HEADLINE_SOFT_LIMIT` only logs a WARNING (`simulation.py:4532`); nothing
  clips, so the card and the detail `<h2>` render the full 232 characters.

Representative current values — both fields, newest rows:

```
company_or_project: "iPSN cryptic-splicing panel as a TDP-43 loss-of-function
                     stratifier for the CHMP7/nucleocytoplasmic-transport axis
                     in sporadic ALS"                                (157 ch)
headline:           "A 20-target cryptic-splicing qRT-PCR readout of TDP-43
                     loss of function in patient iPSC motor neurons, proposed
                     as the missing patient stratifier for CHMP7-directed ALS
                     therapeutics."                                  (196 ch)
```

Both are accurate and both are unreadable to anyone who does not already know
the field. The request is therefore a **prompt-contract** change on two fields,
not a template change: nothing in the read path invents a title.

Finding **T1** — `company_or_project` has no length or style contract at all.
Finding **T2** — the `headline` contract asks for the whole story in one
sentence, which is what produces a 196-character sentence; it never forbids
undefined abbreviations, gene-symbol chains, or the "(Lab, JHU)" suffix that
7 of 20 rows carry and that the card already renders separately as `Lab:`.
Finding **T3** — the Slack clip is silent and mid-word.

## 2. Key points (requests 2 and 5)

`key_points` is already the three-group object introduced with scout_hub prompt
set 1.3.0: `significance`, `innovation`, `commercial_potential`
(`KEY_POINT_GROUPS`, `src/services/assessment_detail.py:113`). The operator's
"add a third bolded section" is therefore an **addition**, not a replacement —
confirmed with the operator 2026-09-14: the target is **five** groups, adding
`key_questions` ("Key questions / experiments") and `clinical_actionability`
("Clinical actionability").

Two hard constraints the change runs into:

Finding **K1 (blocking) — `normalize_key_points` demands EXACT set equality.**

```python
if isinstance(value, dict) and set(value) == _KEY_POINT_KEYS and all(...):
    return value
return None
```

It runs at WRITE time only (`_persist_assessment`), so stored rows are
unaffected by widening `KEY_POINT_GROUPS` — the templates iterate
`key_point_groups` and `.get(key)`, skipping absent keys. But with five keys
required, **a verdict that omits any one group stores `key_points = NULL`**
(the value survives only in `raw_verdict`). That is the exact
prompt-without-image / image-without-prompt hazard CLAUDE.md already records
for 1.3.0, in its more damaging direction: a rebuilt agent image running an
un-updated bind-mounted prompt (or a model that simply drops a group) silently
loses the whole narrative field.
`tests/integration/test_assessment_narrative_fields.py::test_normalize_rejects_wrong_shapes`
pins the strict behaviour (`normalize_key_points({"significance": ["s"]}) is None`).

Finding **K2 — the group-bullet contract is already being exceeded, and nothing
checks it.** Of the 12 stored bullets across the 4 grouped rows, **4 exceed the
160-character contract** (max 183). Two different contracts with two different
enforcement levels, and the difference matters:

* the **160-character per-bullet** limit is **prompt-only, with ZERO code
  enforcement** — no validation, no clip, not even a WARNING. Nothing anywhere
  reads a bullet's character length;
* the **1–2 bullets per group** limit (`_KEY_POINT_GROUP_MIN/MAX`,
  `simulation.py:9153-9154`) is checked as a WARNING only, on `len(group)` —
  the bullet COUNT (`simulation.py:4545-4556`).

Five groups × 2 bullets × 183 characters is ~1.8 KB of bullet text per card,
which is the opposite of the readability goal.

## 3. Strengths / risks box (request 3)

There is no `strengths` field and no `weaknesses` field. What a verdict actually
carries that bears on "positive" vs "risk":

| source | column / value | already rendered on the detail page? |
|---|---|---|
| per-dimension scores | `scores` (JSONB), rendered as weighted bars | yes, `<details id="scores" open>` |
| gating | `gating` tri-state `met` / `not_met` / `unconfirmed` | yes, `<details id="gating">` (collapsed) |
| red flags | `red_flags` (list) | yes, always-open card |
| specialist signals | `specialist_consults.verdict_signal` → `panel_summary` chips | yes, inside the panel banner |

Operator decision (2026-09-14): **derive the box from stored data**, not from
new model-written fields. That is also what the repo's own doctrine wants — a
render-time presentation of stored values, never a render-time re-derivation of
a write-time judgment (`panel_state`'s docstring, `panel_owed`'s column
comment).

Finding **S1 — a two-column green/red box would misfile every "unknown".**
`gating == "unconfirmed"` means "never asked", an unscored dimension counts as
zero in the weighted score but is *not* a scored zero, and a
`reply_truncated` consult's `verdict_signal` is a parse default nobody said.
This repo treats exactly that conflation as a named failure mode in four
places. The box needs a **third, neutral column** ("not established") or it
manufactures claims.

Finding **S2 — thresholds must come from the row's own revision.** The detail
service already resolves `revision` (`resolve_revision(rubric_version,
rubric_content_hash)`) and exposes `revision.scale_max`. Hardcoding "≥4 is
strong" would silently relabel any row scored on another scale — the same
defect class as the `panel_is_owed`-at-render-time bug. When the revision is
unknown, no scale is known and dimensions must contribute nothing.

Finding **S3 — red-flag text may not end up behind a disclosure.**
`tests/integration/test_assessment_detail_page.py::test_a_non_empty_red_flag_list_is_never_collapsed`
and the N8 comment in the body template pin this. The new box must be a plain
card, not a `<details>`.

Finding **S4 — the detail body is under a type/contrast ratchet.**
`test_the_detail_body_uses_readable_type_sizes` asserts, over the `<main>`
slice: `text-xs` count ≤ 13, and `text-gray-400` / `bg-gray-400` /
`text-gray-500` absent. Any new markup in that file must comply.

## 4. Quick scoring on the list page (request 4)

`POST /reviews/assessments/{id}/feedback` already does everything the request
needs: `score` (1–5), `comment`, `feedback_mode`, and
`_parse_dimension_scores(await request.form())`, which returns `{}` when no
`dim_*` field is posted — normalized to SQL NULL by
`_normalized_dimension_scores`. **No route or service change is needed to omit
the rubric dimensions**; a form that simply has no `dim_*` inputs is already
the supported "scored no dimensions" case
(`test_posting_no_dimension_fields_at_all_still_works`).

What does need work:

Finding **Q1 — the redirect always lands on the DETAIL page.**
`_assessments_redirect(surface, user, id)` returns
`/admin|/manager/assessments/{id}`. Submitting from the list would bounce the
reader out of the queue they were triaging, and lose their `run_id`/`sort`/`lab`
filters.

Finding **Q2 (blocking) — the return target must be a TOKEN, never a path.**
`tests/integration/test_manager_views.py::test_manager_assessments_never_links_into_admin`
asserts `"/admin/" not in body` for the whole manager list page. A hidden
`return_to="/admin/assessments"` input would fail that test on the manager
surface. The existing `surface` field is a bare token (`admin` / `manager`) for
exactly this reason; new values must follow suit and the literal path must be
assembled in `src/routers/reviews.py`.

Finding **Q3 — `<select>` labelling is a discipline on the DETAIL page only,
and the list page already violates it.**
`test_every_select_on_the_detail_page_has_an_external_label`
(`tests/integration/test_assessment_review_ui.py:561-580`) fetches only
`/manager/assessments/{id}`; **no test constrains `<select>` labelling on the
list page**, and the three filter selects already there carry no `id` and their
labels no `for` (`templates/admin/assessments.html:13-14`, `:27-29`, `:38-40`,
and the same in the manager twin). So the id/label rule is a design
recommendation for the new controls, not an inherited gate — and a page-wide
test of it would fail on the pre-existing filter selects unless those are fixed
in the same change. The blank-first-option point IS pinned, but also only for
`id="add-score"` on the detail page (`:579-580`); it still matters here, because
`score: int = Form(...)` would otherwise accept an untouched form as a 1. On a
list of up to 500 cards each new id must be per-row-unique — i.e. built from
`a.id`.

Finding **Q4 — edit must NOT be offered on the list.** `edit_feedback`
REPLACES `dimension_scores` from the posted form, so an edit submitted from a
dimension-free quick form would silently wipe per-dimension scores a reviewer
entered on the detail page. Add-only + status-only on the list.

Finding **Q5 — reviewers reach this page.** `manager_assessments` is one of the
four `_REVIEW`-gated GETs, and `/reviews` admits reviewers, so the quick form
must render (and work) for a reviewer on `/manager`. `assign`/`unassign` are
`_STAFF` and must not appear.

Finding **Q6 — page weight.** `ASSESSMENTS_LIMIT = 500`. Three new blocks per
card (quick-score form, scores disclosure, pitch box) on a 500-row "All runs"
render is a real size increase, and `<details>`/`<form>` markup is shipped even
when collapsed. Needs a measured ceiling, not an assumption.

## 5. Pitch + key points side by side, and the pitch contract (request 5)

Finding **P1 (severe, live) — every published pitch is truncated mid-word.**
`PITCH_DISPLAY_CHARS = 600`. Measured over the 8 rows that carry a pitch:
length **1173–1406, mean 1279 — all 8 exceed 600.** The clip is
`value[:max_len]` with no ellipsis, so the four newest headline posts end,
respectively, at `…picked by univ`, `…which patients`, `…(as oppose`,
`…built on a handfu`. The prompt asks for "three to five sentences"; the model
writes ~1300 characters. Making the pitch *longer* (background + source paper,
as requested) makes this strictly worse.

Finding **P2 — score rationale in the pitch would publish new content to
Slack.** `elevator_pitch` is field six of the `#assessments-summary` headline
(design D12, widened once on 2026-09-09 on the operator's assertion that PIs
cannot join the workspace). Putting "brief score rationale that explains the
assessment score" into the pitch publishes reasoning about the score to that
channel. Operator decision 2026-09-14: **a separate, app-only field**
(`score_rationale`) — research background and the source paper go in the pitch
(both are already-public facts by the prompt's own confidentiality rule), the
score rationale does not.

Finding **P3 — the list wrappers have no markdown renderer.** `marked`,
`DOMPurify`, `/static/js/markdown.js` and the `.md-content` / `.assessment-prose`
CSS are declared per-wrapper in `{% block extra_head %}`
(`templates/admin/assessment_detail.html`, `templates/manager/assessment_detail.html`).
`templates/admin/assessments.html` and `templates/manager/assessments.html`
have none, so rendering the pitch as markdown on the list needs those blocks
added to both list wrappers. `prose_format == 'markdown'` stays the gate; a
NULL-stamped row keeps its plain rendering.

Finding **P4 — no new top-level context key may be added to the shared list
body.** `admin_assessments` allowlists every key it forwards;
`manager_assessments` splats `**view`. A key added to `list_assessments` and not
to the admin allowlist renders as silently-falsy `Undefined` on the admin
surface only. Anything new must ride on the `assessments` rows (the
`panel_state` / `review_cols` precedent) or be a Jinja global (the
`key_point_groups` precedent).

Finding **P5 — "Expand all" / "Collapse all" are forbidden strings on the list
page.** `test_admin_assessments_page_renders_no_inline_detail_rows` asserts
`"Expand all" not in html`, `"Collapse all" not in html`, `"Click for
rationale" not in html`, and `"assessment-detail" not in html`. New classes and
copy must avoid all four.

## 6. Rubric scores on the card, hidden by default (request 7)

The card-bottom "checkmarks" are the gating row
(`_assessments_body.html`, the `{% if a.gating %}` block): ✅ met / ❌ not met /
❓ unconfirmed.

Per-dimension score chips used to be on this page and were **deliberately
removed on 2026-08-27** ("per-dimension scores are detail-page content" —
`list_assessments`' own return-dict comment, which also names the two context
keys that left with them, `rubric_weights` and `row_scales`). Restoring them is
an operator-directed reversal of that decision and should be recorded as one.

Finding **R1 — a test pins gating on the card face.**
`test_the_card_keeps_gating_panel_flags_and_rubric_on_its_face` asserts
`"life sciences domain" in html`. Operator decision 2026-09-14: gating **moves
into** the same collapsed disclosure as the scores, so nothing is lost; that
test moves with it (and must then assert the string is present *inside* the
disclosure, not absent from the page).

Finding **R2 — titles, weights and scale must come from each row's own
revision**, exactly as on the detail page (S2). `directory.py` does not import
`rubric_revisions` today; the detail service does.

Finding **R3 — the panel badge, flag count and rubric version must stay on the
card face.** `test_the_card_keeps_gating_panel_flags_and_rubric_on_its_face`
(`tests/integration/test_assessment_queue_controls.py:803-824`) asserts exactly
four strings: `"life sciences domain"`, `"2 flags"`, `"3.4.0"` (the rubric
version) and `"panel not recorded"`. Its N9 reasoning: "rendering a non-verified
panel as unremarkable is a named failure mode". The review chips are pinned
separately and ROW-SCOPED, by `test_list_pages_show_reviewer_columns`
(`:634-669`). Only the gating glyph row moves.

## 7. Prompt suggestions: both roles, and manual only (request 6)

### 7.1 The PI-bot prompts are already supplied — unlabelled

`review_bot._prompt_file_set()` already sends `prompts/agent-system.md`,
`prompts/identity.md`, `prompts/phase4-thread-reply.md` and
`prompts/phase5-new-post.md`. Per `src/agent/roles.py`, **those four files
are the pi_lab prompt set** ("pi_lab is the absence of overrides"). And
`_STATIC_TARGETS` already contains `"pi_lab"`.

Finding **PS1 — nothing tells the model which files belong to which role.**
The rendered blocks are `--- FILE: prompts/agent-system.md (sha256:…) ---`;
only the `prompts/roles/scout_hub/` paths are self-describing.
`prompts/review-bot.md` calls the whole section "the live prompt files the
assessment's own agent runs against", which is true of neither set alone. A
model asked to propose a `pi_lab` edit has to infer the mapping.

Finding **PS2 — the prompt forbids multi-target suggestions outright.**
`prompts/review-bot.md`: "Stay within the scope of one target at a time. If the
same feedback plausibly implicates more than one target, name the single most
direct one and say in the rationale that the others are secondary." That is
exactly the behaviour the operator wants changed.

Finding **PS3 — two role manifests are missing from the file set**
(`prompts/roles/pi_lab/role.toml`, `prompts/roles/scout_hub/role.toml`). They
carry the prompt-set `version` and, for the hub, `post_types = []` — the
declaration that makes the hub reply-only. A suggestion that proposes a
post-type change has no text to quote.

Finding **PS4 — per-phase interview guidance is Python, not a prompt file.**
`src/agent/thread_guidance.py`'s `_PI_LAB` / `_SCOUT_HUB` strings are the
EXPLORE/DECIDE/CONCLUDE guidance actually injected into every phase-4 turn, and
they are not in `_prompt_file_set()`. A reviewer complaint about interview
*behaviour* therefore has no quotable source. Recorded; deliberately deferred
(see the plan's "considered and deferred").

Finding **PS5 — `target` is `String(40)` and one row per job.** A
comma-joined multi-target value would fit the column but would break the
suggestions page's target CELL (`templates/manager/prompt_suggestions.html:61`,
`prompt_suggestion_detail.html:37`). Corrected after audit: there is **no target
filter** to break — `manager_prompt_suggestions` accepts and filters on `status`
only (`src/routers/manager.py:789-821`), so the display column is the whole
coupling. One `PromptChangeSuggestion` **row per target** still needs no schema
change and fits the existing page for free.

Finding **PS6 (blocking) — changing the top-level JSON shape would break the
malformed-reply recovery.** `_LEADING_TARGET_RE` is anchored on `target` being
the object's **first key**, and it exists because 3 of 12 live `claude-opus-5`
replies were invalid JSON in the 2026-09-03 evaluation (fixtures in
`tests/fixtures/review_bot_replies/`). A `{"targets": [...]}` or
`{"proposals": [...]}` contract makes that regex match nothing and sends every
malformed reply back to `out_of_scope`. The compatible shape keeps `target`
first and adds an optional sibling array. Two further couplings to the same
line: `test_review_bot_prompt_contract.py::test_target_vocabulary_matches_the_validator_in_both_prompts`
regexes `"target":\s*"([^"]+)"` out of both the prompt file and
`_DEFAULT_REVIEW_PROMPT`, and `scripts/eval_review_bot.py:254` calls
`review_bot._parse_model_output(call["raw"])` expecting a 2-tuple.

Finding **PS7 — `prompt_files` metadata shape is pinned.**
`test_happy_path_stores_a_suggestion_and_consumes_feedback` asserts
`set(entry) == {"path", "sha256_12"}` for every entry, and
`manager._prompt_file_status` reads exactly those two keys. Role labelling must
go in the rendered TEXT header, not into the stored metadata.

### 7.2 "Should not run automatically" — what runs today

`submit_feedback` and `edit_feedback` both call `enqueue_analysis_if_absent`
whenever `feedback_mode == "learn"`. That is the only trigger; there is no
button anywhere. Making it manual means deleting both call sites and adding one.

Finding **PS8 — at least ELEVEN test functions encode the automatic enqueue**
and must be inverted, not deleted. (This finding said "seven" before the audit;
the corrected enumeration is below, and two of the originally-named four turn
out not to FAIL but to become VACUOUS, which is worse — a test that passes for
the wrong reason.)

* `tests/integration/test_reviews_router.py` (5):
  * `test_reviewer_can_submit_feedback_and_learn_enqueues_one_deduped_job` —
    **fails**
  * `test_editing_log_only_to_learn_enqueues_the_analysis_job` — **fails**
  * `test_only_the_author_can_edit` (`:224-260`) — **fails**: `:259-260`
    asserts `len(jobs) == 1` after an edit flipping `log_only` → `learn`
  * `test_two_pending_jobs_already_exist_dedupe_still_succeeds` (`:102-148`) —
    **passes vacuously**: it seeds the two pending jobs itself and asserts
    `len(jobs) == 2`, so it stops testing the dedupe entirely
  * `test_log_only_feedback_enqueues_nothing` (`:151-163`) — **passes
    vacuously**: `jobs == []` becomes true for every mode
* `tests/integration/test_review_pipeline_races.py` — **all FIVE** tests
  (`:53`, `:93`, `:124`, `:174`, `:199`) unpack
  `(job,) = await _review_jobs(db_session)` immediately after
  `submit_feedback` (`:62`, `:102`, `:149`, `:185`, `:218`), and **three** of
  them assert the post-race queue is `["pending", "processing"]` (`:85`,
  `:121`, `:171`)
* `tests/integration/test_review_job_end_to_end.py::test_learn_feedback_becomes_a_suggestion_through_the_worker`
  submits feedback and then drains the queue
* `tests/unit/test_review_bot_edges.py::test_deleting_the_reviewer_deletes_their_pending_job_but_keeps_the_review`
  (`:300-321`) — `:311` asserts a job exists immediately after
  `submit_feedback(..., feedback_mode="learn")`

Finding **PS9 — the race guarantees survive going manual, but one changes
shape.** `consumed_at_predicates` is what prevents a mid-flight edit from being
re-stamped consumed, and it is independent of the enqueue. What changes is the
*second half*: the edited row stays unconsumed but nothing requeues it, so it
waits for the next manual generate instead of for an auto-enqueued replacement
job. That is strictly what "should not run automatically" asks for, it is
non-lossy, and the three race tests' `["pending", "processing"]` assertion
becomes `["processing"]` plus "a manual generate then enqueues it".

Finding **PS10 — the "Learn" label becomes misleading, and there is no prose to
correct: prose has to be ADDED.** Corrected after audit: neither surface
currently claims anything about the trigger. The mode select is a bare
`<label for="add-mode">Feedback mode</label>` plus two options
(`templates/admin/_assessment_detail_body.html:796-801`); the card's only
explanatory paragraph, `review-rubric-instructions` (`:603-615`), is entirely
about scoring the proposal and never mentions the review bot, an analysis job,
or "Learn". The suggestions page intro
(`templates/manager/prompt_suggestions.html:18-22`) never mentions "Learn"
either. So the misleading thing is the bare word `"Learn"` itself, and going
manual requires writing new explanatory copy on both surfaces rather than
editing existing copy. The label strings `"Learn"` and `"Don't learn — log
only"` are pinned (`tests/integration/test_assessment_review_ui.py:118-119`)
and must not change.

## 8. Slack strikethrough (request 9) — root-caused

**Mechanism, confirmed against Slack's own docs**
(<https://docs.slack.dev/messaging/formatting-message-text>, fetched
2026-09-14): Slack mrkdwn strikethrough is a **single** tilde, `~strike~`.
There is no documented escape for `~`; the only documented suppression is an
inline code span, inside which "text … will not use any other formatting".

**Where the tildes come from:** the corpus uses `~` to mean "approximately".
Measured over `agent_messages` (1751 rows):

| tildes in one message | messages |
|---|---|
| 1 | 186 |
| 2 | 55 |
| 3 | 13 |
| 4 | 7 |
| 5 | 8 |
| 6 | 3 |
| 9 | 1 |
| 10 | 1 |
| **total with ≥1** | **274** |
| **total with ≥2** | **88** |

Of those, **9 messages contain a same-line `~…~` pair that Slack will render as
strikethrough** (regex `~(?=\S)[^~\n]*?(?<=\S)~`). Examples:

```
… a FITC-dextran ladder (~3, 10, 70 kDa) … linezolid (~337 Da, neutral …
… NfL (~68 kDa filament fragment) and UCH-L1 (~25 kDa cytosolic enzyme) …
… (~$5-8K/specimen for 8-10 banked specimens (~$60-90K, 3-5 months) …
… **~3–4 months, ~$80–100K, roughly 40% salary …
```

**`markdown_to_mrkdwn` does not touch `~` at all** — it converts `**bold**` →
`*bold*` and `- ` → `• ` and nothing else. So the tilde reaches Slack verbatim
and Slack pairs it.

Finding **SL1 — the fix already exists on the web side and was never applied to
Slack.** `static/js/markdown.js` disables GFM strikethrough with this comment:
*"the corpus uses single tildes for 'approximately' (e.g. '~30-37%'), which
marked otherwise pairs into a `<del>` span. No content uses intentional
`~~strikethrough~~`."* Same defect, same corpus, one transport unfixed.

Finding **SL2 — the prompt set teaches the pattern.** Four prompt files use `~`
as "approximately", including the PI-bot system prompt's own instrument table:
`prompts/agent-system.md:77` (`~$1M–$5M seed`),
`prompts/specialists/budget.md:14` (`(~$1M–$5M)`),
`prompts/profile-synthesis-sparse.md:40`, `prompts/daily_audit.md:144`.

Finding **SL3 — the substitution must be length-preserving.**
`split_for_slack`'s contract says `markdown_to_mrkdwn` "never lengthens a
string", and it splits the SOURCE markdown on that basis so each chunk is still
within Slack's 4000-character limit after conversion. A one-character-for-one
replacement (`~` → `≈`) keeps it; wrapping in backticks does not.

Finding **SL4 — guard code spans and link spans.** Slack link syntax is
`<url|label>` and `render_assessment_headline` emits one. The current corpus has
**0** tildes inside a URL, a `<…>` span, or an inline code span (measured), but
a `http://host/~user` permalink is a permanently available way to corrupt a
link, and inline code is the one place a literal `~` is already safe.

Finding **SL5 — the assessment prose fields carry tildes too.** 18 of 20
`rationale` values and 18 of 20 `recommended_next_experiment` values contain a
tilde; 0 of 8 `elevator_pitch` values do. `rationale` and
`recommended_next_experiment` never reach Slack, but `elevator_pitch` does (see
P1), so the transport fix covers the one field that matters and the web-side
renderer already handles the other two.

---

## 9. Cross-cutting constraints any implementation must respect

1. **Prompt-set version.** Any edit under `prompts/roles/scout_hub/` bumps
   `prompts/roles/scout_hub/role.toml` `version` (1.3.0 → 1.4.0).
   Corrected after audit: **nothing in the suite enforces this today.**
   `test_the_scout_hub_prompt_set_version_moved_with_the_contract`
   (`tests/unit/test_rubric_prompt_sync.py:308-321`) asserts only
   `version != "1.0.0"`, its sibling asserts `>= (1,3,0)`
   (`:286-288`), and `prompt_set_stamp` (`src/agent/roles.py:159`) reports the
   version and hash without comparing them. A scout_hub prompt edit can ship at
   1.3.0 with a green suite, which is exactly the unrecorded edit the stamp
   exists to catch — so the version floor must be raised in the same change.
   No rubric-document edit is needed, so `prompts/rubric/revisions.toml` needs
   **no** new entry.
2. **Doc sync.** After any `prompts/**` or `_PI_LAB`/`_SCOUT_HUB` edit, run
   `.venv-test/bin/python scripts/sync_prompt_set_docs.py`;
   `tests/unit/test_doc_prompt_sync.py` is the gate.
3. **Sidecar drift alarms.** `tests/unit/test_rubric_prompt_sync.py` parses the
   `<assessment_json>` skeleton and asserts, among other things,
   `skeleton["key_points"] == {"significance": [], "innovation": [],
   "commercial_potential": []}` and `"funnel" not in` the prompt body. New
   prompt prose must avoid the word "funnel".
4. **Deploy pairing.** `prompts/` is bind-mounted into `blackbird-app` and
   `agent`; `src/` is BAKED into both images. A prompt change without an agent
   rebuild, or the reverse, is the hazard CLAUDE.md documents for `0043` and
   `0045`.
5. **Migration ordering.** New mapped columns mean migrate-before-serve, and
   `_persist_assessment` names new columns in its INSERT, so a pre-migration
   engine write fails *silently* (best-effort `except`). Production is at
   `0047`; the next revision id is `0048`.
6. **Lint ratchet.** `ruff check src` is at **214** findings against a ceiling
   of **231** (`SRC_LINT_MAX`) — 17 of headroom. `tests/` must stay at zero.
7. **`is_staff` excludes reviewers**; `_REVIEW` is admin|manager|reviewer.
   Router-level gates are not the real gate — the per-handler singleton is.
8. **Reachability.** A new POST route needs a literal `action="/reviews/…"` in
   a reachable template, and `tests/integration/test_reviews_router.py::test_the_reviews_router_posts_are_an_explicit_allowlist`
   must be extended by hand.

---

## 10. Additional findings from the adversarial audit (2026-09-14)

Twelve gaps the first pass missed. Each was confirmed against the tree.

Finding **A1 (blocking) — `prompts/agent-system.md` is not a free file to edit:
its tilde line is inside the pi_lab GOLDEN MASTER.** `~$1M–$5M seed`
(`prompts/agent-system.md:77`) appears **7 times** in
`tests/characterization/__snapshots__/test_agent_turn_gm.ambr` (verified:
`grep -c` returns 7), as part of the composed pi_lab system prompt. CLAUDE.md
forbids `pytest --snapshot-update` to clear a mismatch; the only sanctioned path
is an operator-directed regeneration with the diff audited hunk by hunk (three
on record). Since the transport fix (SL1) neutralizes the tilde regardless, the
prompt edit is optional cleanup at a high process cost — it should be dropped
rather than triggering a fourth regeneration. The other three tilde sites are
safe: `prompts/specialists/budget.md` is **not** in the `.ambr` (verified:
`grep -c` returns 0), and `profile-synthesis-sparse.md` / `daily_audit.md` are
not composed into any agent turn.

Finding **A2 (blocking) — a new migration requires four coordinated edits to
`scripts/migrate/preflight.py`, each pinned by a different test.** Verified
against the tree:

| constant | current value | pinned by |
|---|---|---|
| `DEFAULT_TARGET` (`preflight.py:74`) | `"0047"` | `tests/unit/test_migration_checks.py:1001-1006` — `data["heads"] == pf.DEFAULT_TARGET` |
| `REVISION_ORDER` (ends `"0047"`) | 0018…0047 | `:1009-1012` — `REVISION_ORDER[-1] == DEFAULT_TARGET` |
| `SUPPORTED_START_REVISIONS` (ends `"0046"`) | 0018…0046 | `:262-268` — every `REVISION_ORDER[:-1]` entry ≥ 0023 must be a supported start, so `0047` becomes mandatory |
| `PLANNED_OBJECTS` (ends at the 0047 tables) | — | `:870-915` — re-derives `create_table`/`add_column`/`create_index`/`create_unique_constraint` names from every revision in `REVISION_ORDER` and fails on any not declared |

`scripts/migrate` is also in `scripts/ci.sh`'s `LINT_TARGETS` at **zero**
findings, so these edits are held to the test-suite lint bar, not the `src/`
ratchet. `postflight.VERIFIED_REVISIONS` is frozen at 0019–0023 (a documented
pre-existing gap) and needs **no** change.

Finding **A3 — `test_an_overlong_project_is_clipped_to_a_headline` hardcodes
120.** `tests/unit/test_assessment_headline_render.py:86-92` asserts
`"z" * 120 in text` and `"z" * 121 not in text` — inlining the number, unlike
the pitch test at `:223-231`, which imports `PITCH_DISPLAY_CHARS`. So of the
three obvious remedies for T3, raising `PROJECT_DISPLAY_CHARS` fails the second
assertion and clipping at a word boundary fails the first; only appending an
ellipsis, or fixing the label length upstream in the prompt, leaves it green.

Finding **A4 — a status control on the list card must never print the word
"Approved" or "Disapproved" in an untouched row.**
`tests/integration/test_assessment_queue_controls.py:664-668` row-scopes
`assert "Approved" not in untouched_row` / `"Disapproved" not in
untouched_row`. Buttons labelled `Approve` / `Disapprove` with lowercase
`value="approved"` are safe (case-sensitive, not substrings); a `<select>` whose
option TEXT is "Approved"/"Disapproved" — the natural compact control, and the
exact `VALID_STATUS_ACTIONS` vocabulary — breaks it.

Finding **A5 — the manual generate POST must not go on the manager router.**
`tests/integration/test_manager_views.py:55-85` asserts the manager router's
POST paths are exactly eight and its methods exactly `{"GET","POST"}`.
`/manager/prompt-suggestions` is the page the button belongs on, so the obvious
placement is a ninth manager POST. `/reviews` is the correct home: it is already
the router every review write lives on, it has its own explicit POST allowlist
to extend, and its `_STAFF` singleton is the gate the action needs.

Finding **A6 — the new card boxes must be fully conditional, because the common
case is a row with neither field.**
`test_a_row_with_no_headline_falls_back_to_the_short_label`
(`tests/integration/test_assessment_queue_controls.py:779-800`) asserts
`"assessment-card-points" not in html` and `"assessment-card-label" not in
html`, and its own docstring records that every production row has `headline IS
NULL, key_points IS NULL`. A grid wrapper or a tinted box emitted
unconditionally fails on today's entire corpus.

Finding **A7 — role labelling in the review-bot payload must not add a section.**
`tests/unit/test_review_bot_prompt_contract.py:35-38` asserts the prompt
describes exactly the four `##` sections the code sends, and `:41-45` asserts
`"five sections" not in PROMPT`. A role map added as a fifth `##` section fails
both; the labels must go inside the existing `CURRENT PROMPT FILES` block
headers.

Finding **A8 — `_parse_model_output`'s 2-tuple is pinned by SEVEN tests**, not
the two PS6 names: `tests/unit/test_review_bot_edges.py:64-142` holds
`test_blank_body_falls_back_to_the_raw_text`,
`test_specialist_label_variants_are_out_of_scope`,
`test_truncated_json_with_no_recoverable_target_is_out_of_scope`,
`test_real_unparseable_opus_replies_keep_their_declared_target`,
`test_recovery_never_invents_a_target_from_prose`,
`test_recovery_rejects_an_invalid_recovered_target`,
`test_recovery_is_not_rubric_specific`. Keeping the signature is therefore not a
courtesy to the eval script; it is the cheapest way to leave nine pinned
behaviours untouched.

Finding **A9 — the shared list body has no surface discriminator.** The detail
body has `admin_view` from its routers; the list context has nothing equivalent
— `admin_assessments`' allowlist (`src/routers/admin.py:835-870`) forwards no
such key and `manager_assessments`' `**view` (`src/routers/manager.py:631-636`)
carries none, and P4 forbids adding one. Two carriers exist: `active_page`
(`admin.py:177` = `"admin"`, `manager.py:150` = `"manager"`), already in both
contexts, or a third wrapper-level `{% set %}`/macro beside the existing
`assessment_link` / `pi_link` CONTRACT (`_assessments_body.html:12-16`). The
wrapper `{% set %}` is preferable: it is explicit at the two call sites and it
cannot be confused with the nav-highlighting variable.

Finding **A10 — the detail page already implements "side by side", and its
no-empty-column rule is pinned.**
`_assessment_detail_body.html:89-128` renders pitch-left / key-points-right with
classes `assessment-brief-grid` / `assessment-brief-pitch` /
`assessment-brief-keypoints`, and
`tests/integration/test_assessment_detail_page.py:1634-1651` pins both the
column order and that a row with no key points renders no second column. The
list page should mirror that markup and that rule rather than invent a second
layout.

Finding **A11 — any new nullable JSON/JSONB column needs `none_as_null=True`.**
`tests/unit/test_json_none_as_null.py:55-67` walks `Base.metadata` and fails on
any nullable JSON column without it. Not triggered by a `Text` column, but it
governs every alternative shape a "score rationale" or a "strengths" field could
have taken.

Finding **A12 — `0047` is the current head and `0048` is free.** Verified:
`alembic heads` prints `0047 (head)`; no `alembic/versions/0048_*` exists.

---

## 11. Implementation status, and what turned out differently

Implemented 2026-09-14 in one merged working tree (**uncommitted** — the
operator did not ask for a commit, so there is no per-finding commit hash to
cite; the tree at the time of writing IS the implementation). Plan:
`docs/plans/2026-09-14-assessment-ux-and-prompt-suggestions-implementation-plan.md`
revision 2. Gate: `./scripts/ci.sh` — 3730 passed / 93 skipped, branch coverage
85.03% against the 60% floor, `ruff` on `tests/` and `scripts/migrate` at zero,
`ruff src` 214 against the 231 ceiling (unchanged from baseline, so no new
debt), alembic single head `0048` with a clean upgrade→downgrade→upgrade round
trip, and 13 golden-master snapshots passing unregenerated.

### 11.1 Findings, and where each one landed

| finding | outcome |
|---|---|
| T1, T2, T3 | Task A1.2/A1.5 — `headline` recontracted at ≤140 chars in plain language, `company_or_project` given its first contract at ≤70. `_HEADLINE_SOFT_LIMIT` 200→140, new `_PROJECT_SOFT_LIMIT`/`_PITCH_SOFT_LIMIT`. T3's clip fixed by `_clip_at_sentence`, not by raising the cap. |
| K1 | Task A3.2 — `normalize_key_points` relaxed to a subset check (D5). |
| K2 | Recorded, not enforced: the 160-char per-bullet limit is still prompt-only. The bullet-COUNT bounds warn as before. |
| S1–S4 | Task A3.3/B1 — three-bucket derivation, thresholds from the row's own revision, plain card (never a `<details>`), ratchet respected with zero new `text-xs`. |
| Q1–Q6 | Task C3/F1 — quick scoring, token-not-path surface, per-row unique labelled ids, add-only, reviewer-reachable. Q6 measured: see 11.3. |
| P1 | Task A4. P2 → D3 (`score_rationale` app-only). P3 → Task D. P4, P5 respected. |
| R1–R3 | Task C2 — gating moved verbatim into the collapsed disclosure; face keeps flag count, panel badge, rubric version, review chips. |
| PS1–PS3, PS5, PS6 | Task G1/G2 — role-labelled block headers, both `role.toml`s added, one row per target, `target` still the first key. |
| PS4 | Recorded in the prompt only: the bot is now TOLD that `thread_guidance.py` is Python with no quotable text. |
| PS7–PS10 | Metadata key set unchanged; all 11 enqueue-dependent tests inverted; new copy added (there was none to correct). |
| SL1–SL5 | Task I1/I2 — with two corrections, see 11.2. |
| §10 A1–A12 | All addressed except A3 (recorded: the project-clip test's hardcoded 120 is why the project cap was NOT raised) and A11/A12 (verified non-issues). |

### 11.2 Where the plan was wrong, and what the implementation did instead

**The tilde guard was wrong as drafted, and only measurement caught it.** The
plan's first pattern protected any `<…>` span. This corpus writes `<` and `>`
as inequality operators (`p<0.05`, `CI >0.20` — 6 of the 274 tilde-bearing
messages), so a broad matcher pairs them across prose and swallows the tilde
between, reproducing the bug. Both patterns score 0/274 on stored messages, so
no ordinary test separates them; the narrow pattern (code, Slack links,
mentions, bare URLs) plus an adversarial inequality probe is what shipped
(decision D9).

**`prompts/agent-system.md` was not edited** (decision D8). Its `~$1M–$5M seed`
line is inside the pi_lab golden master at 7 locations, and the transport fix
neutralizes it regardless. `prompts/roles/pi_lab/role.toml` therefore needed no
version bump either.

**Three published-headline defects the plan's `_clip_at_sentence` still had**,
found by the post-merge audit and fixed: a terminator at the very end of the
window is `max_len` and therefore always the largest candidate, so it beat
every real boundary and published "… at 2." for a pitch whose 600th character
is the `.` of "2.5-fold"; the word-boundary fallback searched for a literal
space only, so a markdown pitch whose only window whitespace is `\n` fell
through to the mid-word cut the function exists to remove; and the "a complete
sentence needs no ellipsis" rule was **reversed** — `rfind` takes the highest
qualifying boundary and the pitch contract requires a citation in sentence two,
which is exactly where `et al. ` and `e.g. ` live, so the marker is now
unconditional on a real truncation. An abbreviation blocklist would be a guess;
"there is more" is a fact.

**A defaulted specialist signal was being published as a risk.**
`specialist_consults.read_state` has three values and TWO of them mean the
stored `verdict_signal` is not something a specialist said: `truncated` (which
the plan handled) and `defaulted` — a reply that arrived complete but from
which `parse_opinion` could read no signal, so `gap` was substituted. That case
carries `truncated=False` and landed in the red Risks column as a finding
nobody made, violating the very docstring point the plan made mandatory for
that function. `read_state` was already in the dict being passed in.
`read_state is None` (pre-`0038`) deliberately stays on the signal path: the
question was never recorded, which is not the same as a defaulted answer.

**Widening `key_points` to five groups made a new silent-loss shape.** With all
five keys present plus a sixth, nothing is absent and every value is a list, so
the plan's missing-group WARNING could not see it and the whole field was
dropped to `raw_verdict` in silence. `_persist_assessment` now warns whenever
`normalize_key_points` rejects a truthy value, naming the unknown key.

**The LLM-spend cap had to be applied to what is enqueued, not to the
candidates.** The security review found `_eligible_assessment_ids` uncapped,
with the set SIZE controlled by the least-privileged review role. The first cap
sliced the oldest N candidates — which kept their pending jobs, so the next
press re-picked the same N, enqueued nothing, and a backlog larger than the cap
could never drain. `MAX_ANALYSES_PER_PRESS = 25` now bounds the enqueued count,
with the already-pending set fetched in one query.

**Two "not done" items from the plan-auditor were real**: the `_PITCH_SOFT_LIMIT`
comment still said "3-5 sentences" after the contract moved to 3-4, and one
races-test docstring still asserted the auto-enqueue F2 deleted.

**One near-unfalsifiable assertion was hiding a real gap.** `assert "first" in
low` passed on unrelated prose; made falsifiable it FAILED, because
`review-bot.md` writes "the object's **first** key" with markdown emphasis
while the code's fallback prompt writes "FIRST key". Emphasis is now stripped
before matching.

**Recorded, deliberately not changed:** `slack_web.post_message` does not apply
`markdown_to_mrkdwn` and so does not carry the tilde fix — but it has NO caller
in `src/` (verified 2026-09-14; every live Slack post goes through the agent
transport), so converting it would change behaviour for a hypothetical future
caller with nothing exercising it. The hazard is documented at the function.
Likewise the concurrent double-press race in `enqueue_analysis_if_absent`'s
SELECT-then-INSERT: pre-existing, needs a partial unique index (a migration),
and the over-claiming docstring was corrected instead.

### 11.3 Finding Q6 answered with numbers

Measured against the real served page — the "before" figures from a git
worktree at `9df21e1`, not derived from a per-card estimate:

| rows | before | after | ratio |
|---|---|---|---|
| 50, every narrative field populated | 252,494 | 728,695 | 2.9x |
| 50, no narrative fields (today's production shape) | 184,354 | 503,955 | 2.7x |
| **500 (`ASSESSMENTS_LIMIT`, the "All Runs" worst case), populated** | **2,400,255** | **7,130,306** | **3.0x** |
| 500, no narrative fields | 1,718,365 | 4,882,416 | 2.8x |

The first cut measured 8,669,804 at 500 rows; whitespace control in the two new
disclosures and factoring the repeated hidden inputs into one macro took it to
the figure above with byte-identical rendered output. The scores disclosure
alone had been 6,676 bytes per card against 2,415 for the whole card face —
template indentation multiplied by 500 rows was the single largest item on the
page.

The DEFAULT view is run-scoped and holds 6-20 rows (~280 KB), so the
multi-megabyte figure is reachable only via "All Runs". **The residual 3.0x is
an open decision, not a closed one**: taking it back down means rendering the
two collapsed per-card disclosures client-side on first open, which removes
them from the payload entirely but introduces client-side rendering of scores
and a JS-cloned form, degrades without JavaScript, and is a design change
neither the plan nor the operator authorised.

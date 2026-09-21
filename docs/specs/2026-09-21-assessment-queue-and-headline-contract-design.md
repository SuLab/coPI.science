# Assessment queue, brief and headline contract — design

**Date:** 2026-09-21
**Status:** implemented and audited 2026-09-21 (plan: `docs/plans/2026-09-21-assessment-queue-and-headline-contract-plan.md`)
**Scope:** seven operator-requested changes to the two assessment list pages, the
two assessment detail pages, and the `scout_hub` headline contract.
**Migration:** none. **Deploy:** app + agent image (prompt-set bump), no schema change.

Spec location follows this repo's own convention (`docs/specs/*-design.md`,
`docs/plans/*-plan.md`) rather than the superpowers default
`docs/superpowers/specs/`.

Every line reference below was checked against the working tree on 2026-09-21.
§13 records the audit that produced the corrections and the two findings that
were refuted.

---

## 0. Evidence this design rests on

### 0.1 Production data

Measured against **production** (`copi-blackbird` stack, `copi` database) on
2026-09-21 by read-only `SELECT` over ssh. These figures are **not verifiable
from the checkout** — they are recorded here so a later reader knows what the
design was sized against, and can re-measure.

| fact | value |
|---|---|
| `opportunity_assessments` rows | 22 |
| rows with `headline` / `elevator_pitch` | 10 / 10 |
| rows with `key_points` | 10 |
| rows with `strengths` / `risks` | **0 / 0** (0049 shipped after the last run) |
| `prose_format` | `markdown` 16, NULL (plain) 6 |
| pitches containing `https?://` | 2 (one of them carries **two** URLs) |
| `rationale` containing a URL | 2 (both `markdown` rows) |
| `recommended_next_experiment` containing a URL | 1 (a **plain-text** row) |
| `score_rationale` / `key_points` / `strengths` / `risks` containing a URL | 0 |
| `assessment_reviews` rows | 2 |
| `assessment_review_events` rows | **0** |

Two representative production headlines, showing the range change 1 targets:

> *Low-coverage-WGS cfDNA fragmentome classifier proposed to identify F2–F3
> at-risk MASH in the FIB-4 indeterminate zone — the resmetirom prescribing
> gate — on a published, running platform.*

> *An oral drug that blocks the enzyme making a brain metabolite that builds up
> to toxic levels in children with Canavan disease.*

Both were written under the **same** prompt (item 6 of
`prompts/roles/scout_hub/phase4-thread-reply.md:230`). That item already asks for
"the method / what it is for / why it is fundable", already bans colon-stacked
noun phrases and gene-symbol chains, and already carries a `Write:` / `Not:`
example pair — and 8 of 10 production rows still read like the first example.
The contract does not *require* a named disease or an explicit statement of what
the intervention does, and that is the gap.

A citation in a pitch is a **bare URL in parentheses**, not a markdown link:

> *… published by the Slusher lab at Johns Hopkins in ACS Med Chem Lett 2026
> (https://doi.org/10.1021/acsmedchemlett.5c00623), with supporting mitochondrial
> flux modelling on bioRxiv (https://doi.org/10.64898/2026.06.29.735215).*

### 0.2 Third-party render behaviour, measured not assumed

Measured 2026-09-21 against CDN bundles whose sha384 matches the SRI attributes
in all four wrappers byte for byte (so they are exactly what production serves).
Also not verifiable from the checkout; re-derivable by downloading the two
pinned URLs and hashing them.

* **marked 12.0.2**, with this repo's `del`-disabled config
  (`static/js/markdown.js:14-16`), **does** autolink a bare URL and correctly
  leaves a trailing `)` outside the link:
  `… 2026 (https://doi.org/10.1021/…)` renders
  `… 2026 (<a href="https://doi.org/10.1021/…">https://doi.org/10.1021/…</a>)`.
  So a markdown row today already produces a working link **whose visible text
  is the full URL** — which is the half of requirement 6 to fix.
* `[cited paper](<url> "url")` renders `<a href="url" title="url">cited paper</a>`.
* **Tailwind Play CDN preflight contains `a{color:inherit;text-decoration:inherit}`**,
  and `templates/base.html:10-12` adds only an `[x-cloak]` rule. No wrapper
  styles `.md-content a`. That is why those links render as unstyled body text —
  the "blue and underlined" half of requirement 6.
* **DOMPurify 3.1.6's default `ALLOWED_ATTR` does NOT contain `target`.** Its
  HTML attribute list holds `href`, `title`, `rel`, `class`, and
  `popovertarget` / `targetx` / `targety`, but no bare `target`.
  `static/js/markdown.js:27` calls `DOMPurify.sanitize(marked.parse(md))` with
  **no config**, so there is one shared policy and no `ADD_ATTR`.

---

## 1. Decisions

Each closes a fork the implementation must not reopen. D1–D10 were taken with
the operator during brainstorming; D11–D14 were forced by the audit and D11 was
put back to the operator before being recorded.

| # | Decision | Rejected alternative |
|---|---|---|
| D1 | The title change touches **`headline` only**, worded for any modality. | Also constraining `company_or_project`; wording the rule around "drugs". |
| D2 | All three status buttons (Approve / Disapprove / **Clear**) are removed; the read-only Status line, Status history and the card's status chip **stay**. | Keeping `Clear`; deleting the route and its service function. |
| D3 | `POST /reviews/assessments/{assessment_id}/status` and `set_review_status` **survive** as code and get a `ROUTE_ALLOWLIST` entry. | Deleting the route (destroys the capability; drops the reviews POST allowlist 8 → 7). |
| D4 | "Reviewed" means **≥1 `assessment_reviews` row**. | Feedback OR an approve/disapprove status; feedback OR an assignment. |
| D5 | The sub-tabs are **server-side**, a `?review=` query param alongside `run_id`/`sort`/`lab`. | Client-side JS toggle over all rendered cards. |
| D6 | Quick review stays a `<details>` and gains `open`. The detail page's **Add feedback** disclosure gains `open` too. | Removing the `<details>` wrapper entirely. |
| D7 | The detail control becomes a **button at the foot of the "In one minute" box**. | A full-width button row under the grid. |
| D8 | Score **and band label** are fully hidden until "Why this score" is expanded. | Keeping either in the collapsed `<summary>`; moving the number but leaving the band word on the face. |
| D9 | The "Why this score" box renders **unconditionally**, so a pre-0048 row still carries its score. | Rendering the box only when `score_rationale` is non-NULL. |
| D10 | The "cited paper" rewrite covers **every assessment-authored narrative field that each page actually renders** — which is not the same set on the two pages (§7). | The elevator pitch only; the first URL only. |
| D11 | The band label moves with the score, and the **accessibility regression is recorded, not papered over**: `test_admin_assessments_page_renders_band_as_text_not_just_colour` keeps passing by accident (its regex reads the whole page), so its docstring is corrected and it gains an assertion that the label now sits inside the closed disclosure. Operator-confirmed 2026-09-21 after the audit raised it. | Leaving the band word on the card face. |
| D12 | The citation anchor carries **no `target`** — same tab, identical on both render paths. It does carry `rel="noreferrer"` on the plain path (revised 2026-09-21 after the security review): unlike `target`, `rel` survives DOMPurify and has no visible behaviour, so the two paths cannot be *seen* to differ, and it keeps the assessment's own URL out of the `Referer` sent to a cited host. Markdown has no syntax for `rel`, so markdown rows fall back to the browser's referrer policy. | `target="_blank"`, which DOMPurify strips on the markdown path only, or widening the shared sanitiser with `ADD_ATTR: ['target']` for every markdown surface in the app including the interview transcript. |
| D13 | `_list_filter_query` emits `review` **only when it is not the default** (`reviewed` or `all`). | Always emitting it, which would rewrite three exact-`Location` assertions for no gain. |
| D14 | The default tab is **`unreviewed`**; the two existing tests that render a reviewed fixture without a `review` param are updated to pass `?review=all`. | Defaulting to `all` to avoid touching those tests, which would make the split opt-in and defeat its purpose. |

---

## 2. Change 1 — the headline must name the disease and what the intervention does

### Requirement
> "modify the generated assessment title, titles should include the specific
> disease area and what the drug is/does"

### Current state
`prompts/roles/scout_hub/phase4-thread-reply.md`, sidecar item 6, **begins at
line 230** and runs to the `Record it in \`headline\`.` line. It already
carries a `Write:` example (:242-243) and a `Not:` counter-example (:245-248),
a 140-character cap, the bans listed in §0.1, and the "this is NOT the project
label" note. The `"headline": ""` slot sits in the `<assessment_json>` skeleton.

### Change
Rewrite item 6 so two of its three elements become **named, mandatory,
checkable** rather than implied:

1. **The disease area** — the specific disease, condition or patient population
   the work serves, named in words a non-specialist recognises. A biomarker
   panel's disease area is the disease it stratifies, not the assay chemistry.
   For a genuinely disease-agnostic platform, name the disease of the **first**
   application; "multiple indications" is not a disease area.
2. **What the intervention is and what it does** — what the thing physically is
   (a pill, an antibody, a blood test, an implant, a screening platform) and the
   action that produces the benefit (blocks an enzyme, identifies responders
   before treatment, kills cells carrying a transporter). A noun phrase naming
   the modality without its action does not satisfy this.
3. **Why it is fundable** stays as the third element, and the rewrite states
   explicitly that it **may be carried implicitly** by naming a decision the
   funder acts on ("predicts which patients will respond"), because all three
   elements plus a disease name rarely fit 140 characters otherwise. This is
   stated because the model answer below spends ~128 characters on elements 1
   and 2 alone.

Keep: the 140-character cap, every existing ban, the one-abbreviation rule, the
"not the project label" note.

Add: a **second `Write:` example**, so the item carries two positive examples and
one counter-example. Use the two production headlines in §0.1 — the Canavan one
as a second model answer, the MASH one as a second counter-example. Both are
real output of the current prompt, which is the point: the prompt as written
permits the bad one.

Wording must be **modality-neutral** — "the drug" appears nowhere. The corpus
holds oligonucleotides, small molecules, cfDNA classifiers, qRT-PCR panels,
activity-based probes and a CRISPR screening platform.

### Consequences
* `prompts/roles/scout_hub/role.toml` `version` **1.5.0 → 1.6.0**.
* `.venv-test/bin/python scripts/sync_prompt_set_docs.py` runs in the same
  commit; `tests/unit/test_doc_prompt_sync.py` asserts the embedded copy.
* The sidecar skeleton gains no key, so `tests/unit/test_rubric_prompt_sync.py`
  should stay green — proved by running it, not by assertion (§12).
* **No backfill.** The 10 existing headlines stand; a regenerated one would be
  indistinguishable from one the hub wrote at the time. Takes effect on the next
  interview a rebuilt agent concludes.
* Coverage limit, stated: this improves the 10 rows that *have* a `headline`.
  The other 12 fall back to `company_or_project` (D1 leaves that field alone),
  so their card titles are unchanged by this work.
* `prompts/` is bind-mounted, so this change alone needs no image rebuild — but
  it ships with changes 2–7, which do.

### Acceptance
* A new test in `tests/unit/` asserts item 6 names **both** new mandatory
  elements and carries **two** `Write:` examples. The existing file already has
  one `Write:` and one `Not:`, so an "at least two worked examples" assertion
  would pass against the unmodified file and detect nothing — the assertion must
  be "two positive examples" plus a phrase check for the disease and
  intervention-action requirements.
* This is a prompt-text assertion, not a model-behaviour assertion: the suite
  drives `tests/fakes.py`'s `FakeAnthropic` and cannot observe what the hub
  writes.
* `role.toml` reads 1.6.0 and `scripts/sync_prompt_set_docs.py --check` is clean.

---

## 3. Change 2 — remove the approve/disapprove controls

### Requirement
> "remove the approve/disapprove buttons from the quick review and human review
> sections on the assessment list and assessment details pages"

### Current state
Two three-button forms, each `POST`ing to `/reviews/assessments/{id}/status`
with `action=approved|disapproved|cleared`:

* `templates/admin/_assessments_body.html:563-568` — inside the card's Quick
  scoring disclosure (shared by both list pages).
* `templates/admin/_assessment_detail_body.html:948-953` — inside the Human
  review card (shared by both detail pages).

Two shared bodies, four rendered surfaces.

### Change
Delete both `<form>` blocks. Also delete the now-orphaned justification comment
at `_assessments_body.html:534-538` ("The status control is BUTTONS, not a
`<select>` …"), which explains markup that will no longer exist; the constraint
it records — a card must never print the words "Approved"/"Disapproved" outside
the chip — moves to the comment above the chip itself.

Keep, untouched:

* the detail page's **Status** line and **Status history** disclosure
  (`_assessment_detail_body.html:894-945`);
* the list card's **Approved / Disapproved** chip
  (`_assessments_body.html:446-453`) and `ReviewColumns.status`;
* `set_review_status`, `VALID_STATUS_ACTIONS`, `_CHIP_STATUSES`, the
  `assessment_review_events` table and the `recorded_by_user_id` plumbing.

**Recorded consequence:** the retained chip and Status line now display a state
that no UI in the app can set. With 0 event rows in production nothing renders
today, but a restored backup would show a chip a reader cannot change. That is
the accepted price of D2/D3 over deletion.

Because no template links it, `POST /reviews/assessments/{assessment_id}/status`
becomes unreferenced and `tests/unit/test_reachability.py::test_no_unreachable_routes`
fails (the two template references are the only ones; no string in `src/` names
that path). Add a `ROUTE_ALLOWLIST` entry keyed
`("POST", "/reviews/assessments/{assessment_id}/status")`. Two notes for the
implementer:

* `test_every_allowlist_entry_has_a_reason` requires a reason of more than 20
  characters.
* Every existing entry names a **real external caller** (orcid.org, nginx, a
  hand-typed URL). This entry is a **new category**: a route deliberately kept
  with no caller at all. Say so in the reason, in those words, so the next
  reader does not hunt for a caller that does not exist.
* The entry is gated by `test_route_allowlist_has_no_stale_entries`, so
  re-adding a button later forces it back out.

`src/routers/reviews.py:136-142` — `_assessments_redirect`'s docstring names
`set_review_status` as one of the two call sites that pass `run_id`/`sort`/`lab`.
That remains true of the code and false of the app; amend the docstring to say
the second caller has no UI.

### Tests this breaks — named, because the spec's first draft named the wrong one
* `tests/integration/test_assessment_queue_controls.py::test_quick_scoring_posts_to_literal_review_paths_on_both_surfaces`
  — **the one that actually breaks.** `:956` asserts the status form's action;
  `:960-963` assert `quickscore.count(...) == 2` for each of the four hidden
  inputs *because there are two forms*; `:967-969` assert the three
  `name="action"` values. Parametrized over both surfaces, so ten failing
  assertions. The counts become **1**, and change 3 takes the hidden-input set
  from four to **five**.
* `tests/integration/test_assessment_review_ui.py::test_the_forms_post_to_literal_review_paths:187`
  — asserts `/reviews/assessments/{id}/status` is present on all three detail
  surfaces (admin, manager, reviewer).
* `tests/integration/test_assessment_queue_controls.py::test_quick_scoring_offers_no_edit_delete_or_assign_controls`
  — **does NOT break.** It asserts only that `/reviews/feedback/`, `/assign` and
  `/unassign` are absent (`:1003-1005`). Recorded because the first draft of
  this spec named it as the affected test; it is not.

### Acceptance
* No rendered admin or manager assessment page (list or detail) contains
  `name="action"` with any of the three status values — asserted on all four
  surfaces.
* `POST /reviews/assessments/{id}/status` still works when posted directly; its
  router tests pass unchanged.
* Reachability suite green with exactly one new allowlist entry.

---

## 4. Change 3 — reviewed / unreviewed sub-tabs on the list pages

### Requirement
> "split the assessment list panel into two sub-tabs for reviewed assessments and
> unreviewed assessments"

### Current state
`src/services/directory.py::list_assessments` (≈340-700) filters by run and lab,
orders by `sort`, caps at `ASSESSMENTS_LIMIT = 500`, and returns `total_count`,
the rows behind the five recommendation cards, `dimension_stats`, `band_counts`,
`off_rubric_count`, two warning counts and the control state.
`admin_assessments` (`src/routers/admin.py:816`) **allowlists** every context key
it forwards; `manager_assessments` (`src/routers/manager.py:624`) splats `**view`.

### Change

**Service.** `list_assessments(db, run_id, *, sort, lab, review)` with values
`unreviewed` (default, D14), `reviewed`, `all`.

* Validate as `sort`/`lab` are validated — an unrecognised value falls back to
  the default and renders the queue rather than 400ing.
* The predicate is an `EXISTS (SELECT 1 FROM assessment_reviews WHERE
  assessment_id = opportunity_assessments.id)` correlated subquery (D4), defined
  **once** as a module-level helper on the `unvetted_panel_filter()` precedent,
  and used by both the row query and the counts.
* Apply it **before** `total_count` and before the `LIMIT`, so the "top N of
  TOTAL" note, the five summary cards and `dimension_stats` describe the active
  tab.
* Do **not** narrow `incomplete_panel_count`, `drop_counts`/`drops_total`,
  `lab_options`, or `assessment_counts_by_run` by it. The first two follow the
  existing rule for `lab` (the failure mode of a warning is under-warning); the
  last two follow the existing rule that the dropdowns are computed pre-filter,
  so the reader always has a way back. Say all four in the docstring.
* Return `review` and `review_counts`, a **three**-key mapping
  `{"unreviewed": n, "reviewed": n, "all": n}` under the same run+lab scope, so
  the template never has to derive the All count by addition.

**Routers.** Both handlers accept `review: str | None = None` and pass it
through. The admin handler forwards `review` and `review_counts` **explicitly** —
a key added to the service and not to that allowlist is silently-falsy Jinja
`Undefined` on the admin surface only.

**Templates.** The tab strip lives in **each wrapper**, not the shared body,
because the hrefs must be literal `/admin/...` / `/manager/...` for
`_link_credits`. Three links — Unreviewed (n) · Reviewed (n) · All (n) — each
carrying the current `sort`, `lab` and the run id **in the form the page already
round-trips**, `{{ 'all' if show_all_runs else selected_run_id }}` (the same
expression `qs_filters()` uses at `_assessments_body.html:257`); using
`selected_run_id` alone renders a UUID for an All-Runs view and silently pins the
tab to one run. Active tab marked by `aria-current="page"` **and** a visible
style, never colour alone. Renders above the summary cards.

**Three pieces of existing copy become false and must change with it:**

* the run/sort/lab **GET form** (`admin/assessments.html:99-134`,
  `manager/assessments.html:100-128`) has no hidden inputs, so any control the
  reader changes drops `review` from the URL and silently returns them to the
  default tab. Add `<input type="hidden" name="review" value="{{ review }}">` to
  both forms, and carry `review` on the "view all runs" links
  (`admin/assessments.html:157`, `manager/assessments.html:151`).
* the truncation note ("… the rest sit further down that order — **not excluded
  by the run or lab filter**", `admin/assessments.html:164-169`,
  `manager/assessments.html:156-161`) must name the review filter too.
* the empty state (`_assessments_body.html:573-578`, "No assessments stored for
  this run — the run menu shows each run's stored count") is false on a tab with
  zero matches: the rows exist, they are on the other tab, and the run menu's
  own count contradicts the sentence on the same screen. Make it tab-aware.

**Round-trip.** `_list_filter_query` (`src/routers/reviews.py:81`) gains
`review`, emitted **only when it is `reviewed` or `all`** (D13) — an absent or
default value is dropped, exactly as an invalid `sort` is, which is what keeps
the three exact-`Location` assertions at `test_reviews_router.py:822`, `:852` and
`:877` green. `submit_review_feedback` forwards the new form field and
`qs_filters()` gains a **fifth** hidden input (it already has four,
`_assessments_body.html:251-257`).

### Consequences and edge cases
* **A card scored from the Unreviewed tab leaves that tab.** The redirect keeps
  `review=unreviewed`, the row no longer matches, and `#a-<id>` lands on nothing
  (browsers scroll to top; no error). Intended queue behaviour, recorded so it is
  not later read as a bug.
* **D4 diverges from what the card already says.** `review_columns_for`
  (`src/services/assessment_reviews.py:596-608`) builds `reviewed_by_names` from
  feedback authors **union status-event actors**, and `_CHIP_STATUSES` drives the
  chip off events alone. So a row with a status event and no feedback renders
  "Reviewed by: …" and an Approved chip **inside the Unreviewed tab**. Zero such
  rows exist in production today and change 2 removes the only way to make more,
  but a restore would produce them. Mitigation: the tab strip carries one line of
  help text stating that a tab counts **written feedback**, and the divergence is
  recorded here rather than silently reconciled.
* **The shared body's own header** (`_assessments_body.html:246-250`) says "NO NEW
  CONTEXT KEY may be introduced here". `qs_filters()` reading `review` is a new
  top-level key in that file. It is admissible on the same terms as the four keys
  that macro already reads (`list_surface`, `show_all_runs`, `selected_run_id`,
  `sort`, `lab_filter`) — *provided* the admin allowlist forwards it. Update that
  header comment to name `review` rather than leaving a reader to reconcile the
  prohibition with the code.
* `ASSESSMENTS_LIMIT` is unchanged; the tab can only shrink a page.
* A reviewer reaches `/manager/assessments`; the tabs are read-only links and
  need no extra gate.

### Tests this breaks
* `tests/integration/test_assessment_queue_controls.py::test_list_pages_show_reviewer_columns`
  (`:656 assert "Reviewed Co" in html`) and
  `tests/integration/test_reviewer_role.py::test_reviewer_sees_the_review_columns_on_manager_assessments`
  (`:448 assert "Reviewer Role Co" in html`). Both seed via `_seed_reviewed_row`
  (`test_assessment_queue_controls.py:502`), which inserts an `AssessmentReview`,
  so their fixture is *reviewed* and vanishes from the default tab. Each must
  request `?review=all`.
* Rule for the implementer: **every pre-existing list-page test that seeds a
  reviewed fixture and GETs without a `review` param must be audited**, not just
  these two.

### Acceptance
* `?review=reviewed` renders only rows with ≥1 feedback row; `?review=unreviewed`
  only rows with none; `?review=all` renders today's set — on both surfaces.
* The three tab counts satisfy `unreviewed + reviewed == all` for the same
  run+lab scope.
* `total_count`, the five summary cards and `dimension_stats` follow the tab;
  `incomplete_panel_count`, `drops_total`, `lab_options` and
  `assessment_counts_by_run` do not.
* An unknown `?review=` value renders the default tab, not an error.
* Changing Run, Sort or Lab from the Reviewed tab stays on the Reviewed tab.
* A run whose every row is reviewed, viewed on the Unreviewed tab, shows an empty
  state that does not claim the run has no assessments.
* Posting quick feedback from `/manager/assessments?review=reviewed&sort=recent`
  redirects back to a URL carrying both params; posting from the default tab
  produces a `Location` byte-identical to today's.
* An admin-surface test proves `review` and `review_counts` are not `Undefined`.

---

## 5. Change 4 — quick review is expanded by default

### Requirement
> "always keep the quick review section expanded"

Interpreted as **open on every page load, still collapsible by the reader** (D6).
Recorded because "always" and "open by default" are different properties and the
acceptance below only proves the second.

### Change
* `templates/admin/_assessments_body.html:540` — add `open` to
  `<details class="assessment-card-quickscore …">`.
* `templates/admin/_assessment_detail_body.html:1048` — add `open` to the **Add
  feedback** disclosure.

Both stay `<details>`, so the detail page's Expand-all / Collapse-all buttons and
the `beforeprint`/`afterprint` handler in all four wrappers keep working.

### Consequences
* `test_quick_scoring_is_collapsed_and_labelled` **inverts**. Rename to
  `…_is_expanded_and_labelled`, keep the assertion shape (it uses
  `_details_open_tag`, which locates a disclosure by `class="{klass} "` —
  trailing space required — and walks back to the `<details` tag), and rewrite
  the docstring to record that the operator reversed the 2026-09-09 call on
  2026-09-21.
* `test_the_list_page_stays_under_a_size_ceiling`: ` open` costs 5 bytes per card
  (~2.5 KB at 500 rows). Re-run it and record the measured number in the
  docstring; raise the ceiling only if it actually fails, and then to the
  measured value with no speculative headroom.
* Print: `@media print` hides `form, button` (`admin/assessments.html:60-65`),
  and `beforeprint` opens every `<details>`. An open quick-review form is still
  hidden in print, exactly as it is today.
* `test_rationale_and_scores_are_open_by_default` is the precedent for asserting
  the detail page's open-by-default state.

### Acceptance
Both disclosures render with `open` on every card / on the detail page, on both
surfaces, and remain collapsible.

---

## 6. Change 5 — the detail link becomes a button in the pitch box

### Requirement
> "on the assessments list pages, the details link should become a button and
> move it underneath the elevator in one minute paragraph"

### Current state
`{{ assessment_link(a) }}` renders at `_assessments_body.html:457`, last item of
the card's footer row, as an underlined text link reading `detail →`. The macro
lives in each wrapper because `_link_credits` only credits a literal path, and it
carries a class that deliberately avoids the substring `assessment-detail`.

### Change
* Re-style the anchor in **both** wrapper macros as a button — padding, border or
  fill, consistent with the card's existing
  `bg-indigo-600 text-white px-2 py-1 rounded` controls. It **stays an `<a>`**:
  it navigates, and a navigation dressed as a button must keep anchor semantics
  for middle-click, copy-link and screen readers. Keep the
  `assessment-open-link` class and the `detail →` text; a wider label requires
  updating the tests that match on it in the same commit.
* Move the call site from the footer row to the **foot of the "In one minute"
  box** (inside `assessment-card-pitch`, after the prose, ≈`:373`), per D7.
* **Fallback, required:** 12 of 22 production rows have no `elevator_pitch`, so
  that box does not render. When `a.elevator_pitch` is falsy the button renders
  on its own row immediately after the pitch/key-points grid — or immediately
  after the header block when the grid itself is skipped — and before the "Why
  this score" disclosure. Every card carries exactly one detail control in every
  combination of present/absent pitch and key points.
* Side effect, stated: it stays an `<a>`, so the `@media print` rule that hides
  `button` does not hide it. Printed cards keep their detail URL. No change from
  today.

### Consequences
* `test_both_assessment_lists_link_to_the_detail_page` and
  `test_the_manager_controls_never_point_into_admin` locate the link; both must
  still pass.
* The footer row loses its `ml-auto` item; the remaining chips close up.

### Acceptance
Exactly one detail button per card — inside the pitch box when there is a pitch,
directly below the grid when there is not — pointing at
`/admin/assessments/{id}` on the admin surface and `/manager/assessments/{id}`
on the manager surface, with no `/admin/` string anywhere in a manager render.
A test covers the no-pitch row specifically.

---

## 7. Change 6 — cited-paper links in assessment prose

### Requirement
> "when there is a linked paper in the elevator pitch, it should be a hyperlink
> with the text 'cited paper' instead of showing the full URL and it should be
> blue and underlined"

Widened by D10 to every assessment-authored narrative field **each page actually
renders** — which is a different set per page (see "Render sites" below).

### Current state
Two render paths per field, chosen by the write-time `prose_format` stamp:
`md-content` + `data-markdown` (client-side marked + DOMPurify), or escaped plain
text. §0.2 records what each does today with a bare URL, and why the result is
unstyled.

### The helpers
New module `src/services/prose_citations.py`, two pure functions, no I/O:

```
CITATION_LABEL = "cited paper"

markdown_with_citation_links(text: str | None) -> str | None
plain_with_citation_links(text: str | None) -> Markup | None
```

Rules, each with a unit test:

* **URL detection runs on the RAW string**, never on an escaped one. Escaping
  first and matching second is wrong: `escape()` turns `&` into `&amp;`, the
  matcher then captures `…?a=1&amp;b=2`, and a second escape yields
  `&amp;amp;` — a dead link — while the trailing-`;` strip truncates the entity.
  The plain function therefore splits the raw text into URL and non-URL spans,
  escapes each appropriately, and assembles `Markup`. Unit case:
  `https://clinicaltrials.gov/search?cond=A&term=B`.
* Matcher is conservative `https?://`, stopping at whitespace and stripping
  trailing `)`, `.`, `,`, `;`, `:`, `>` so a parenthesised DOI — the only shape
  in production — keeps its bracket outside the link.
* **The markdown form is `[cited paper](<url> "title")`**, angle-bracketed. A
  bare destination breaks on a real DOI containing parentheses
  (`10.1002/(SICI)1521-3773…`), and marked then renders the literal text
  `[cited paper](https://…` to the reader. `<`, `>` and `"` inside the URL are
  escaped for the destination and the title. Unit cases: a parenthesised DOI, and
  a URL containing `"`.
* **Skipped, not rewritten:** a URL inside backticks or a fenced code block, a
  `<https://…>` autolink, a reference-style link, and an existing markdown link
  whose text is *not* its own URL (the hub said something deliberate). A markdown
  link whose visible text *is* the URL is rewritten. Unit case for each skip.
* `title` carries the full URL. Two citations in one paragraph — a real
  production case — both read "cited paper"; hover and the status bar
  distinguish them. No numbering; "cited paper 2" invents an ordering the hub
  never asserted.
* Anchor attributes are **`href`, `title`, `class` only** (D12).
* Both functions are **total**: `None` → `None`; a string with no URL comes back
  unchanged (the plain one as `Markup(escape(text))`); applying either twice is
  a no-op.
* `mailto:`, bare `www.` and scheme-less DOIs are **not** linkified. A false link
  on a staff reviewing surface is worse than a visible URL.

### Registration
Both helpers are registered as Jinja globals (or filters) in **both**
`src/routers/admin.py` and `src/routers/manager.py`. Each module owns its own
`templates` object; the repo's only precedent,
`templates.env.globals["key_point_groups"]`, is registered twice for exactly this
reason (`admin.py:143`, `manager.py:97`). Registering once leaves the other
surface raising `UndefinedError` — a 500 on a live page. A manager-surface render
test is part of acceptance.

### Render sites — enumerated, because the field list is not the same per page
**List card** (`templates/admin/_assessments_body.html`) renders only three of
the narrative fields: `elevator_pitch` (`:368` markdown, `:370` plain),
`key_points` (`:383`, `:393`), `score_rationale` (`:412`, `:414`).
`rationale`, `recommended_next_experiment`, `strengths` and `risks` appear
nowhere in that file, deliberately (`:405-406` records the 2026-08-27 removal).

**Detail body** (`templates/admin/_assessment_detail_body.html`):
`elevator_pitch` `:108`/`:110`; `score_rationale` **twice**, `:129`/`:131` and
`:182`/`:184`; `recommended_next_experiment` across **four** branches,
`:396` (`ask_parts[0]`), `:399` (`ask_parts[1]`), `:402` (`ask_text` markdown),
`:404` (`ask_text` plain); `rationale` `:736` markdown and `:742` plain — and the
plain path is **not** one `whitespace-pre-line` block, it splits on `\n\n` and
emits one `<p>` per paragraph, so the helper is applied **per paragraph**; the
hub's `strengths`/`risks` bullets at `:298`/`:323`, which are gated on
`viewer_is_staff` (`:216-217`) and so never render for a reviewer.

A rewrite applied to `a.recommended_next_experiment` and `a.rationale` as
columns, rather than at these sites, misses most of them.

### Not covered, each for a stated reason
* the interview timeline's agent messages (`:1173`) — a Slack transcript, not the
  assessment's own prose; a URL there is part of what was said;
* reviewer comments — `:957` records that they must never leave the escaped
  plain-text path;
* the derived strengths/risks rows built from specialist consult text — derived
  display, not stored assessment prose;
* `src/services/assessment_headline.py::render_assessment_headline` — it
  publishes clipped raw text to `#assessments-summary` and is out of scope.
  `_clip_at_sentence` must keep operating on the **raw** pitch, or the
  600-character clip boundary moves.

### Styling
One rule, added to the `<style>` block of **all four** wrappers:

```css
.md-content a, .citation-link { color: #2563eb; text-decoration: underline; }
```

* `#2563eb` is Tailwind `blue-600`; contrast must be checked against white and
  against the `slate-50` / `amber-50` / `green-50` box backgrounds these links
  sit on.
* **No test currently compares the four wrappers.**
  `tests/integration/test_assessment_list_chrome.py:46-84` asserts that specific
  substrings are present on both surfaces; a rule added to one wrapper only would
  pass every existing test. Add the new rule to that test's substring list on
  both surfaces, and add the equivalent for the two detail wrappers.
* This rule also styles links in the **interview timeline**, which renders through
  `md-content`. Intentional: a link should look like a link everywhere on the
  page. The written scope above is about *rewriting text*, not about styling.

### Degradation, accepted
On `static/js/markdown.js`'s fail-closed path (CDN blocked or SRI mismatch) the
markdown branch renders `data-markdown` as literal text. After this change a
reader in that state sees `[cited paper](<https://doi.org/…> "https://doi.org/…")`
where today they see a bare URL. Accepted: the fail-closed path is already a
degraded rendering of every markdown surface in the app.

### Acceptance
* A `markdown` row whose pitch carries two bare DOIs renders two anchors, both
  reading "cited paper", both blue and underlined, neither showing the URL as
  text, parentheses intact around them — on the list card **and** the detail
  brief.
* A plain-text row whose `recommended_next_experiment` carries a URL (one exists
  in production) renders the same anchor on the detail page, and the surrounding
  text is still escaped — proved with a fixture containing `<script>` and `&`.
* The same rendering appears on a **manager** render, proving both registrations.
* `render_assessment_headline(...)` output is byte-identical before and after the
  change for the same row.

---

## 8. Change 7 — the score moves into a collapsed "Why this score"

### Requirement
> "on the assessments list pages the score should be moved into the why this
> score box and the box should be collapsed by default."

### Current state
* `_assessments_body.html:307-333` — the card's top-right block: recommendation
  chip, then the 2-decimal `weighted_score` in `text-xl font-bold`, then the
  uppercase `band-label` span (`pass` printed as the rubric's `pass_label`,
  "decline").
* `_assessments_body.html:407-418` — the `assessment-card-score-rationale` box
  (its explanatory comment runs `:400-406`), eyebrow "Why this score", rendered
  **only when** `a.score_rationale` is non-NULL.

### Change
* Top-right keeps the **recommendation chip only**. Score and `band-label` both
  move (D8/D11).
* The box becomes `<details class="assessment-card-score-rationale …">`, **closed
  by default**, `<summary>` reading exactly `Why this score`. Give the class a
  trailing space in the attribute so `_details_open_tag`
  (`test_assessment_queue_controls.py:862-873`) can locate it — that helper
  matches `class="{klass} "` literally.
* Rendered **unconditionally** (D9). Inside, in order: the weighted score (2 dp)
  and the `band-label` span, styled exactly as today (green/amber/grey by band;
  the band always visible text, never colour alone; `pass` printed via
  `banding.pass_label`); then the rationale prose when `score_rationale` is
  non-NULL, or one line stating that the hub recorded no score rationale and that
  rows written before migration 0048 were never asked for one.
* A row with `weighted_score IS NULL` keeps today's em-dash, and band `—`.
* The card now carries **two** collapsed disclosures — `assessment-card-scores`
  (per-dimension bars) and `assessment-card-score-rationale`. Both closed, both
  reachable; the summaries must read distinctly ("Rubric scores & gating" vs
  "Why this score").

### Consequences — including one regression, recorded
* **`sort=score` becomes an invisible ordering.** The control still offers it and
  the rows are still in that order, but the numbers are one click down. The
  operator chose this over keeping score+band in the summary. Cheap reversal if
  it bites: move the number into the `<summary>`.
* **Accessibility regression, accepted (D11).** `band-label` exists because band
  used to be conveyed by font colour alone, which a colour-blind reader could not
  see (`test_admin_assessments_page_renders_band_as_text_not_just_colour`,
  `test_opportunity_assessment_persistence.py:1356-1388`). Putting it behind a
  closed disclosure hides it again visually. The recommendation chip — the model's
  own call — stays on the face, so a recommendation/band disagreement is no longer
  visible at a glance.
* **Three tests keep passing for the wrong reason**, and must be corrected rather
  than left: `_band_label` (`test_opportunity_assessment_persistence.py:1247-1262`)
  is a **page-wide regex** over `<span class="band-label…">`, and `assert "3.05" in
  resp.text` is a page-wide substring — both still match inside a closed
  `<details>`. So `:1312`, `:1319`, `:1386-1388` and
  `test_manager_views.py:505` all stay green while the property they were written
  to protect is gone. Required: correct the docstring of
  `test_admin_assessments_page_renders_band_as_text_not_just_colour` to say the
  band now lives one click down, and add an assertion that the label is inside the
  `assessment-card-score-rationale` disclosure — so the test states something true.
* Byte cost: the box now renders on every card instead of the ~45 % that have a
  rationale. Re-measure the size ceiling in the same commit.

### Tests this breaks
* `tests/integration/test_assessment_queue_controls.py::test_a_row_with_no_pitch_or_points_renders_neither_box:1171`
  — row-scoped `assert "assessment-card-score-rationale" not in card` for two bare
  rows. D9's unconditional render breaks it. The assertion becomes: present,
  closed, and containing the em-dash score.

### Acceptance
* No card renders the weighted score or the band label outside the `Why this
  score` disclosure.
* The disclosure has no `open` attribute and is present on **every** card,
  including one with `score_rationale IS NULL` and one with
  `weighted_score IS NULL`.
* The recommendation chip is unchanged and still on the card face.
* The detail page's own score rendering is untouched.

---

## 9. Contracts this change must not break

1. **No absolute `/admin/` or `/manager/` path in either shared body.** Links go
   in the wrappers as literal strings; `/reviews/...` action paths are the
   recorded exception. (`_link_credits`;
   `test_manager_assessments_never_links_into_admin`.)
2. **The two list wrappers stay identical to each other, and the two detail
   wrappers likewise**, in their `extra_head` script and style blocks. This is a
   *written* contract with **no test that compares them** —
   `test_assessment_list_chrome.py:46-84` only asserts named substrings are
   present on both surfaces. The new CSS rule goes into all four **and** into
   that test's substring list.
3. **No new top-level context key may reach only one surface.** Admin allowlists
   (`admin.py:816`), manager splats. Per-row data rides on `assessments`;
   page-level data (`review`, `review_counts`) must be added to the allowlist
   explicitly. The shared body's own header (`_assessments_body.html:246-250`)
   states the stricter form of this rule and must be updated to name `review`.
4. **Whitespace control inside the per-card disclosures is load-bearing**
   (measured: indentation alone was the largest item on a 500-row render). New
   markup inside the score or quick-review disclosures uses the same
   `{%-`/`-%}` discipline.
5. **`list_surface` is a bare token, never a path.**
6. **Reviewer comments never become markdown or HTML.**
7. **Prompt-set edits require the `role.toml` bump and the doc sync.**
8. **`.ambr` snapshots for `pi_lab` guidance must not be regenerated.** Nothing
   here touches `src/agent/thread_guidance.py`; a moved snapshot is a finding,
   not a snapshot to update.

## 10. Non-goals

* No schema migration, no new column, no backfill of `headline`,
  `score_rationale`, `strengths` or `risks`.
* No change to `#assessments-summary` output, `PITCH_DISPLAY_CHARS`, or
  `_clip_at_sentence`.
* No change to the review-bot / prompt-suggestion pipeline.
* No change to per-dimension reviewer scoring, assignment, or the detail page's
  dimension-score form.
* No removal of `set_review_status`, `assessment_review_events`, or the status
  chip.
* No change to `thread_guidance.py` or any `pi_lab` prompt.
* No widening of the shared DOMPurify policy.

## 11. Deploy

No migration, so the migrate-before-serve ordering does not apply. Both images
are rebuilt, and the prompt-set bump makes the agent rebuild **mandatory**:
`prompts/` is bind-mounted while `src/` is baked, so a prompt-only deploy leaves
the announced prompt-set version and the running image out of step.

```
DC="docker compose -f docker-compose.prod.yml"
$DC build blackbird-app worker
$DC --profile agent build agent
$DC up -d blackbird-app worker
$DC up -d agent          # supervisor returns IDLE; starting a run is a separate
                         # operator action from /admin/simulation
```

Per the standing operator preference, **the simulation is not started as part of
this deploy.**

## 12. Verification — commands, not adjectives

Every acceptance item above is proved by one of these. The gate is
`./scripts/ci.sh`; these are the narrow checks to run first.

| what | command |
|---|---|
| prompt item 6 + doc sync | `.venv-test/bin/python scripts/sync_prompt_set_docs.py --check` and `.venv-test/bin/python -m pytest tests/unit/test_doc_prompt_sync.py tests/unit/test_rubric_prompt_sync.py` |
| status removal + reachability | `.venv-test/bin/python -m pytest tests/unit/test_reachability.py tests/integration/test_reviews_router.py tests/integration/test_assessment_review_ui.py` |
| sub-tabs, cards, disclosures | `.venv-test/bin/python -m pytest tests/integration/test_assessment_queue_controls.py tests/integration/test_reviewer_role.py tests/integration/test_manager_views.py` |
| score/band relocation | `.venv-test/bin/python -m pytest tests/integration/test_opportunity_assessment_persistence.py -k "assessments_page or band"` |
| citation helpers | `.venv-test/bin/python -m pytest tests/unit/test_prose_citations.py tests/integration/test_assessment_detail_page.py` |
| page size | `.venv-test/bin/python -m pytest tests/integration/test_assessment_queue_controls.py -k size_ceiling` — record the printed/measured byte count in the docstring |
| headline unchanged | `.venv-test/bin/python -m pytest tests/unit/test_assessment_headline_render.py` |
| everything | `./scripts/ci.sh` |

Test-plan summary:

| area | new tests | changed tests |
|---|---|---|
| prompt contract | item-6 content assertion (two `Write:` examples + both mandatory elements) | doc-sync regeneration |
| status removal | absence assertions on 4 surfaces; 1 `ROUTE_ALLOWLIST` entry | `test_quick_scoring_posts_to_literal_review_paths_on_both_surfaces`, `test_assessment_review_ui.py::test_the_forms_post_to_literal_review_paths` |
| sub-tabs | filter ×3 values ×2 surfaces; counts sum; unknown value; filter survives a sort change; tab-aware empty state; admin-allowlist non-`Undefined`; redirect round-trip | `test_list_pages_show_reviewer_columns`, `test_reviewer_role.py::test_reviewer_sees_the_review_columns_on_manager_assessments` |
| quick review open | detail-page Add-feedback open assertion | `test_quick_scoring_is_collapsed_and_labelled` → `…_is_expanded_and_labelled`; size ceiling |
| detail button | exactly-one-per-card; present with no pitch | `test_both_assessment_lists_link_to_the_detail_page` |
| cited paper | unit tests for both helpers (none/one/two URLs, parenthesised DOI, `&`-bearing query, `"` in URL, backticks, `<…>` autolink, existing link, `None`, idempotency); render tests on both pages, both prose formats, both surfaces; headline byte-identity | `test_assessment_list_chrome.py` substring list |
| score relocation | not-on-face; in-disclosure; closed; NULL rationale; NULL score | `test_a_row_with_no_pitch_or_points_renders_neither_box`; docstring + assertion of `test_admin_assessments_page_renders_band_as_text_not_just_colour` |

## 13. Audit record

Two read-only audits ran against the first draft: a repository fact-check and an
adversarial completeness review. Corrections folded in above include: the four
tests the draft failed to name, the escape-order defect in the plain citation
path, the markdown destination form, the missing Jinja registration point, the
`review` param's loss through the GET filter form, the tab-aware empty state and
truncation note, the three-key `review_counts`, the `qs_filters` four→five
correction, the list-card field set, the detail-page render-site enumeration,
`render_assessment_headline` (the draft invented `build_headline`), item 6's true
line number and existing example pair, and the three line ranges that were off by
a few lines.

Two audit findings were checked and **refuted**:

* *"The three exact-`Location` assertions in `test_reviews_router.py` break."*
  They post no `review` field, and D13 drops an absent or default value, so they
  are unaffected.
* *"`test_quick_scoring_offers_no_edit_delete_or_assign_controls` breaks."* It
  asserts only that `/reviews/feedback/`, `/assign` and `/unassign` are absent
  (`:1003-1005`); it never mentions the status form.

### Post-implementation audit, 2026-09-21

Three read-only audits ran against the merged, CI-green tree: plan completeness,
semantics, and security. Everything they found was reproduced before it was
fixed. Confirmed and fixed:

* `_rewrite_protected` had **no scheme check**, so a self-titled inline link
  became a citation whatever its destination —
  `[mailto:pi@jhu.edu](mailto:pi@jhu.edu)`,
  `[/admin/users/x](/admin/users/x)` and `[javascript:alert1](javascript:alert1)`
  all rewrote. The last was inert only because DOMPurify rejects the scheme,
  i.e. a CDN bundle was the last defence after the label had replaced the one
  signal a reader had. Now gated on `_is_linkable`.
* `[]()` produced `[cited paper](<> "")` — an anchor pointing at the current
  page. Same gate.
* A URL ending in `\` escaped the destination's closing `>` and then the
  title's closing `"`, so the inline link never parsed and marked rendered the
  raw markdown to the reader — the exact failure the angle brackets exist to
  prevent. `\` is now outside `_URL_RE`.
* `_split_trailing` could peel a match down to a bare `https://` and present a
  dead link as a citation.
* Both helpers raised `TypeError` on a non-string, and two call sites feed them
  raw JSONB elements (`key_points` bullets, hub `strengths`/`risks`). A
  model-written bullet arriving as a number would have 500'd the list page.
* Quadratic backtracking in `_MD_PROTECTED_RE`'s angle-destination
  alternative: measured 0.046 s for `"[](<>" + " " * 5000` and 0.802 s for
  20000, on fields the hub writes to ~5000 characters, once per card, up to 500
  cards.
* The empty state tested the tab branches **before** the nothing-at-all branch,
  so a run with zero assessments — every fresh run, on the tab every reader
  lands on — read "all 0 have review feedback. Nothing is owed here".
* `key_points` bullets were linkified on the card and not on the detail page:
  the same field rendered two ways one click apart. (The omission traces to
  §7's own render-site enumeration, which listed the field and then left it out
  of the detail list.)
* `dimension_stats`/`band_counts` follow the active tab, but the panel still
  called itself the run's distribution; it now names the tab.
* Ten acceptance criteria had no covering test — among them `?review=reviewed`
  at HTTP level on either surface, the `.citation-link` rule in the two detail
  wrappers, `open` on the manager surface, and the `qs_filters` `review` input
  (whose only assertion was satisfied by a byte-identical string in the
  wrapper's GET form). All now covered.

Accepted, not fixed, with reasons:

* **Link laundering.** Every destination shows the same "cited paper" label.
  That is the operator's explicit request; the URL is preserved in `title`.
* **`set_review_status` has no undo path.** A reviewer-role account can still
  POST `disapproved` directly and no in-app control can clear it. This follows
  from D2's removal of all three buttons.
* **The `#a-<id>` fragment is dead on the default path.** Scoring a card on the
  Unreviewed tab moves it off that tab, so the redirect's anchor finds nothing.
  Intended queue behaviour per §4, restated here because the audit raised it
  independently.
* **The size ceiling was not lowered** to the new 685,309-byte measurement. It
  exists to make the next addition declare its cost, not to track every shrink.

One finding was **partly refuted and partly upheld**: the three band/score tests
do **not** fail (their matchers are page-wide and still match inside a closed
`<details>`), but that is precisely why §8 requires their docstring and
assertions to be corrected — a test that passes while its property is gone is
worse than one that fails.

# Assessment UX, prompt-suggestion control, and Slack strikethrough — implementation plan (2026-09-14)

**Revision 2 (2026-09-14), post-adversarial-audit.** Two read-only auditors
checked the investigation document's claims and this plan's executability
against the tree at `9df21e1`. Revision 1 was **not executable**: four blocking
defects (a golden-master collision, three unpinned head-revision constants, an
incomplete test-inversion list, and an under-specified clip). All four are
corrected below and the corrections are marked *(audit)*. The investigation
document carries the matching corrections in place plus a new §10.

**Spec:** `docs/audits/2026-09-14-assessment-ux-and-prompt-suggestions/README.md`
(findings T1–T3, K1–K2, S1–S4, Q1–Q6, P1–P5, R1–R3, PS1–PS10, SL1–SL5), plus
the operator's nine requests and the four decisions recorded in §0.2 below.

**Goal:** nine operator-requested changes, delivered together, across the
scout_hub sidecar contract, both assessment surfaces, the prompt-suggestion
pipeline, and the Slack transport.

---

## 0.1 What is and is not in scope

In scope: the nine requests. Out of scope: the rubric document itself (no
`[meta].version` bump, therefore **no** `prompts/rubric/revisions.toml` entry),
and any simulation start.

**The pi_lab golden master is out of scope, and that required a decision
*(audit, BL-1)*.** Revision 1 justified the exclusion with "this plan touches no
`_PI_LAB` string" — true but irrelevant: the golden master snapshots the whole
COMPOSED pi_lab system prompt, which embeds `prompts/agent-system.md`
verbatim. Verified: the tilde line `~$1M–$5M seed`
(`prompts/agent-system.md:77`) appears **7 times** in
`tests/characterization/__snapshots__/test_agent_turn_gm.ambr` (`:206`, `:698`,
`:1194`, `:1722`, `:2219`, `:2602`, `:2975`), and the GM agent is `agent_id="su"`
— pi_lab (`tests/characterization/test_agent_turn_gm.py:64`), with
`_hermetic_profiles` deliberately leaving `PROMPTS_DIR` alone because "the
committed prompts/*.md ARE the behavior we want pinned" (`:59`).

So Task I2 **drops `prompts/agent-system.md` from its edit list** (decision D8).
The alternative — a fourth operator-directed, hunk-audited `.ambr` regeneration
— buys nothing here: Task I1's transport fix neutralizes that tilde at the Slack
boundary whether or not the prompt still contains it, so the prompt edit is
hygiene rather than the fix. `prompts/specialists/budget.md` is verified **not**
in the `.ambr` (`grep -c` returns 0) and the other two files are not composed
into any agent turn, so the remaining three edits are safe.

A consequence, recorded: `prompts/roles/pi_lab/role.toml` `version` therefore
does **not** need a bump. Had Task I edited `prompts/agent-system.md`, it would
have — that manifest's own header says "Bump on ANY edit to those files", and
nothing in the suite enforces it (`tests/unit/test_prompt_set_stamp.py:90-95`
asserts only non-emptiness), so the omission would have been an unrecorded edit
by the repo's own definition *(audit, G-1)*.

## 0.2 Operator decisions (2026-09-14)

| # | Decision |
|---|---|
| **D1** | `key_points` becomes **five** groups: `significance`, `innovation`, `clinical_actionability`, `key_questions`, `commercial_potential`, in that render order. |
| **D2** | The strengths/risks box is **derived from stored data** — dimension scores, gating, red flags and specialist signals — not from new model-written fields. No migration for it, and it works on all 20 existing rows. |
| **D3** | "Brief score rationale" goes in a **new, app-only `score_rationale` field**, never into `elevator_pitch`, because the pitch is published to `#assessments-summary`. The pitch still gains research background and the source paper (both already-public facts). |
| **D4** | The card-bottom gating checkmarks **move into** the same collapsed per-card disclosure as the rubric scores. Nothing is deleted from the page; the card face keeps the flag count, the panel badge, the review chips and the detail link. |

Two further design calls made in this plan, with their reasoning stated at the
task that implements them:

| # | Decision |
|---|---|
| **D5** | `normalize_key_points` is relaxed from exact set equality to **"every key is a known group key"** (finding K1). Exact equality means one omitted group stores `key_points = NULL` — the strictest possible reaction to the mildest possible defect, on a field whose whole documented policy is "a malformed narrative field never costs the verdict". |
| **D6** | Multi-target suggestions are expressed as an **optional `additional_proposals` array with `target` still the object's first key** (finding PS6), and each element becomes its own `PromptChangeSuggestion` row (PS5). No schema change, `_LEADING_TARGET_RE` keeps working, and `scripts/eval_review_bot.py` keeps its 2-tuple call. |
| **D7** | Going manual is **total** (PS9): neither `submit_feedback` nor `edit_feedback` enqueues. The anti-loss guarantee that mattered (`consumed_at_predicates` never re-stamps a row that changed) is independent of the enqueue and is unaffected. |
| **D8** *(audit)* | `prompts/agent-system.md` is **not** edited — its tilde line is inside the pi_lab golden master (see §0.1). The transport fix covers it. |
| **D9** *(audit)* | The tilde guard protects **Slack link/mention syntax and bare URLs specifically**, not any `<…>` span. Measured: this corpus uses `<`/`>` as inequality operators, and a broad `<[^<>\n]*>` matcher pairs them across prose and swallows the tilde between — reproducing the bug it was meant to fix. See Task I1. |

---

## 1. Global constraints

These are gates, not preferences. Every one has a test behind it.

**Pinned label strings — do not reword:** "Full rationale (N paragraphs)",
"Dimension scores", "Human review", "Interview timeline", "Overall rating
(1 = weak … 5 = strong)", "Learn", "Don't learn — log only", "Significance",
"Innovation", "Commercial potential", "In one minute", "Key points", "The ask",
"Red flags", "Gating criteria", "Assigned", "Reviewed by".

**Forbidden strings on the assessments LIST page** (`test_admin_assessments_page_renders_no_inline_detail_rows`):
`assessment-detail`, `Expand all`, `Collapse all`, `Click for rationale`. Also
`rationale` text, red-flag TEXT and `derisking_milestones` values.

**Forbidden on the MANAGER list page** (`test_manager_assessments_never_links_into_admin`):
the substring `/admin/`. Surface discriminators are bare tokens; paths are
assembled in `src/routers/reviews.py`.

**Detail-body type/contrast ratchet** — corrected *(audit, FA-4)*. The test
(`tests/integration/test_assessment_detail_page.py:1606-1626`) asserts exactly
FOUR things, over a slice taken by `html.split("Assessment detail", 1)[1]
.split("</main>", 1)[0]` (a string split, **not** the `<main>` element):
`body.count("text-xs") <= 13`; `'class="assessment-prose'` present;
`text-gray-400` / `bg-gray-400` / `text-gray-500` absent; `text-base` present.
The "`text-xs` only inside `rounded-full` chips" and "running text wrapped in
`max-w-[68ch]`" rules are docstring prose, not assertions — follow them as house
style, but do not expect the test to catch a violation. If new markup pushes
`text-xs` past 13, raise the ceiling in the same commit **to the measured
number, with the measurement recorded in the docstring** — no speculative
headroom (this resolves the revision-1 contradiction between this paragraph and
Task B4 *(audit, CT-6)*).

**No new top-level context key in the shared bodies** (finding P4). New per-row
data rides on the `assessments` rows; new page-level constants are Jinja globals
registered on **both** `Jinja2Templates` instances (`src/routers/admin.py` and
`src/routers/manager.py` — each keeps its own `env.globals`).

**Shared-body contract additions** are template-local `{% set %}` in each
wrapper before the `{% include %}`, documented in the body's header comment —
the same mechanism as the existing `assessment_link` / `pi_link` macros.

**Every NEW `<select>`** carries a unique `id` with a matching `<label for>`;
every new score/mode select keeps a blank, `disabled selected` first option.
Corrected *(audit, G-5)*: this is an inherited gate on the DETAIL page only
(`tests/integration/test_assessment_review_ui.py:561-580`, which asserts
`selects_total == len(select_ids)` for that page). The LIST page is not gated and
already violates the rule — its three filter selects carry no `id` and their
labels no `for` (`templates/admin/assessments.html:13-14`, `:27-29`, `:38-40`,
and the manager twin). Task D therefore fixes those six attributes so the
list-page test can be page-wide rather than scoped to the new controls; without
that fix, C4's test must be scoped and must say so.

**Colour is never the only signal.** Every strength/risk/unknown entry, every
chip and every badge prints its own words.

**Never `pytest --snapshot-update`.** And nothing in this plan may make a
golden master mismatch in the first place — see §0.1/D8.

**A card on the list page must never print the words "Approved" or
"Disapproved"** *(audit, A4)*.
`tests/integration/test_assessment_queue_controls.py:664-668` row-scopes
`assert "Approved" not in untouched_row` and the same for `"Disapproved"`.
Buttons labelled `Approve` / `Disapprove` with lowercase `value="approved"` are
safe (case-sensitive, not substrings); a `<select>` whose option TEXT is
"Approved"/"Disapproved" — the natural compact control, and the exact
`VALID_STATUS_ACTIONS` vocabulary — breaks it.

**`scripts/migrate/` is linted at ZERO findings**, not against the `src/`
ratchet: it is in `scripts/ci.sh`'s `LINT_TARGETS` ("it is the code an operator
runs against a live database during an outage window, so it gets held to the
same bar"). Task A2 edits it.

**Lint.** `ruff check tests/` must be zero. `ruff check src` is at 214 against
`SRC_LINT_MAX=231`; do not raise the ceiling. New handlers take the
module-level `Depends` singletons (`_DB`/`_STAFF`/`_REVIEW`/`_ADMIN`), never an
inline `Depends(...)` in an argument default (ruff B008).

**Running tests:** on the host, `.venv-test/bin/python -m pytest <file> -v`.
Never `pip install` into `.venv-test` from an sshfs mount. Full gate:
`./scripts/ci.sh`.

**Commit trailer:** `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

**No build, no deploy, no simulation start as part of implementation.** The
deploy runbook is §8 and is the operator's to run.

---

## 2. Migration

**`0048_assessment_score_rationale`** — down-revision `0047`. One additive
nullable column:

```
opportunity_assessments.score_rationale  TEXT NULL
```

Nothing else. No backfill: the 20 existing verdicts were never asked for a
score rationale, and a generated one would be indistinguishable from one the
hub wrote — the same rule `headline`/`key_points`/`elevator_pitch` follow. Every
read path renders nothing when it is NULL.

`downgrade()` uses a **bare `op.drop_column`** — corrected *(audit, CT-5)*.
`0037`/`0040`/`0041`/`0043` are all bare (`0041_…:45`, `0040_…:50`,
`0037_…:45`), and `if_exists=True` on `op.drop_column` only exists from Alembic
1.16 while `pyproject.toml:20` pins `alembic>=1.13.0` — so the guarded form
could `TypeError` on a legitimately resolved 1.13–1.15. `.venv-test` currently
has 1.19.0, which is exactly the environment skew CLAUDE.md warns about for the
Anthropic SDK, applied to Alembic. The `ci.sh` round trip does execute the
downgrade (`scripts/ci.sh:191-194`), so this is exercised.

**Deploy ordering is migrate-before-serve**, and both failure directions must be
written into the migration docstring:

* Old code, new schema: safe (additive, nullable, unmapped by old code).
* New code, old schema: **breaks both ways.** READ — the new code maps the
  column, so every `select(OpportunityAssessment)` (both list pages, both detail
  pages) raises `UndefinedColumn`. WRITE — `_persist_assessment` names it in the
  INSERT, and that write is best-effort, so **every verdict of a running
  simulation is lost to one ERROR line in a log nobody is tailing** while the
  Slack replies keep looking normal. Same shape as the `0043` box in CLAUDE.md.

---

## 3. Task graph

Tasks are partitioned by **file ownership**. A, B, C, D, E, F, G, H, I can be
implemented in parallel; J runs last.

One file has a deliberate, test-by-test split, and it is the only one
*(audit, CT-2)*: `tests/unit/test_review_bot_edges.py` is Task G's, except for
`test_deleting_the_reviewer_deletes_their_pending_job_but_keeps_the_review`,
which depends on the enqueue Task F deletes and is therefore F's. Both briefs
must name it. Every other file has exactly one owner — see §7's four declared
cross-task couplings for the three places where one task *specifies* a change
another task *makes*.

```
A1 prompts/roles/scout_hub/*            ─┐
A2 models + migration + engine           │
A3 src/services/assessment_detail.py     ├─► J  (doc sync, CLAUDE.md, full ci.sh)
A4 src/services/assessment_headline.py   │
B  detail body template                  │
C  list body template                    │
D  list wrappers                         │
E  src/services/directory.py             │
F  reviews router + reviews service      │
G  review_bot + review-bot.md + eval     │
H  prompt-suggestions page + manager rtr │
I  slack_client + four prompt files     ─┘
```

---

## Task A1 — the scout_hub sidecar contract

**Owns:** `prompts/roles/scout_hub/phase4-thread-reply.md`,
`prompts/roles/scout_hub/role.toml`,
`tests/unit/test_rubric_prompt_sync.py`.

### A1.1 Bump the prompt set

`role.toml`: `version = "1.3.0"` → `"1.4.0"`.

### A1.2 Rewrite sidecar item 6 (`headline`) — request 1

**Do not renumber items 1–8.** `_SCOUT_HUB[DECIDE]` and the prompt's own prose
cross-reference "item 5"; renumbering silently breaks those references. New
items are appended as 9 and 10.

Replace item 6 with a plain-language contract. Required content:

* "**One plain-language sentence of at most 140 characters**", for a reviewer
  who is not a specialist in this field and has never heard of this lab.
* Three things it must make obvious, named as such: **the method** (what the
  thing physically is, in words a scientifically literate non-specialist reads
  without stopping), **what it is for** (the disease, patient population, or
  decision it serves), **why it is fundable**.
* Style prohibitions, each stated: no colon-stacked noun phrases; no
  slash-separated alternatives; no parenthetical lab/institution suffix (the
  page already shows the lab separately, and 7 of 20 stored rows carry one); at
  most one abbreviation, spelled out on first use; a chain of gene symbols is
  not a headline.
* Keep the existing sentence "This is NOT the project label;
  `company_or_project` already carries that, and both are stored."
* A worked write/do-not-write pair, using a real stored value as the "not":

  > Write: *"A blood test taken before treatment that predicts which
  > liver-cancer patients will respond to immunotherapy."*
  >
  > Not: *"Pre-treatment plasma IL-17F/IL-21/IL-23/IL-8 signature for
  > exceptional ICI response in HCC/biliary cancer — real association, but no
  > fitted classifier and no demonstrated edge over published IL-8 alone."*

### A1.3 Rewrite sidecar item 7 (`key_points`) — requests 2 and 5

Five labelled groups, **in this order**, each holding ONE or TWO bullets of at
most 160 characters, each a complete claim rather than a topic:

| key | label | what it holds |
|---|---|---|
| `significance` | Significance | why the problem matters and for whom |
| `innovation` | Innovation | what is genuinely new versus the state of the art |
| `clinical_actionability` | Clinical actionability | in brief: who would be treated, tested or triaged differently if this worked, and at what point in their care — **one bullet** |
| `key_questions` | Key questions / experiments | the open questions and the experiments that would answer them — **one bullet** unless two are genuinely independent |
| `commercial_potential` | Commercial potential | path to a product, IP, market or partner |

Keep the existing closing sentence ("Together they must let a reviewer who
reads nothing else state what the idea is, what is established, and the
deciding risk") and update it to name the five. State explicitly that
`clinical_actionability` and `key_questions` are **one bullet each by default**
— finding K2 measured 4 of 12 existing bullets already over the 160-character
contract, and five groups × two long bullets defeats the readability goal that
motivated the request.

### A1.4 Rewrite sidecar item 8 (`elevator_pitch`) — request 5

Keep "plain language, for a scientifically literate reader who is not a
specialist", and add, as explicit required content:

* **Where the work comes from** — the published paper, preprint or dataset the
  idea builds on, cited the way the lab's own public profile cites it (DOI or
  PubMed link), or "unpublished" plainly when there is none.
* **What exists today** versus what the money would buy (already there).
* **Why the answer matters** (already there).

Add a hard bound: **"at most 900 characters"**, stated as a number, with the
reason given in one clause — the first 600 characters are posted publicly to
`#assessments-summary` and the rest is app-only, so the first two or three
sentences have to stand alone. (Finding P1: all 8 stored pitches are 1173–1406
characters and every published headline to date is cut mid-word.)

**Reconcile the two bounds in the same edit, or the warning fires on every
verdict** *(audit, G-8)*. Revision 1 kept "three to five sentences", added a
required new element (where the work comes from), and imposed a budget 27%
below the current measured *minimum*. Those cannot all hold. Resolution, to be
written into the prompt in this order of priority:

1. **Three to four sentences**, down from three to five — the sentence count
   moves with the budget rather than against it.
2. **At most 900 characters**, and if the two conflict, cut a sentence rather
   than run over.
3. **Sentence one must name what the thing is and who it is for**, and
   **sentence two the work it builds on** — because those two are what the
   600-character public excerpt will contain, and "what the money would buy"
   can survive in the app-only tail if something has to.

`_PITCH_SOFT_LIMIT = 900` (Task A2) is then a drift alarm rather than a
permanent siren. Expect it to fire on early verdicts anyway: the measured mean
is 1279, so the contract is asking for roughly a 30% cut in a field the model
currently over-writes by 2-4x against its own sentence count.

**Do not** put score rationale here (D3).

Keep the existing "Formatting `rationale`, `recommended_next_experiment` and
`elevator_pitch`" paragraph, and **extend its asterisk rule to tildes**:

> Never write a bare `~` to mean "approximately" — Slack reads a pair of them
> as strikethrough and silently strikes out everything between. Write
> "approximately", "about", or `≈`.

Place the tilde sentence so it governs **every** field, not only the three that
paragraph currently names *(audit, minor)*. As written, the formatting paragraph
covers `rationale`, `recommended_next_experiment` and `elevator_pitch`
(`prompts/roles/scout_hub/phase4-thread-reply.md:250-257`), which leaves
`headline`, `key_points` and the new `score_rationale` uncovered — harmless for
Slack, since none of those three is published, but it makes A1.6's
cross-reference to "the formatting paragraph" a half-truth. Either hoist the
tilde rule to its own sentence above the field list, or name all six fields.

### A1.5 Add sidecar item 9 (`company_or_project`) — request 1, finding T1

A new numbered item, because the field has had no contract at all:

> 9. **Project label.** `company_or_project` is the SHORT name, not a
>    description: **at most 70 characters**, what you would write as a slide
>    title or a deal name. No lab or institution suffix, no em-dash clauses, no
>    "proposed as …" framing — the headline above carries the story and the page
>    shows the lab separately. This label is the ONE project field posted to
>    Blackbird's public summary channel, where it is clipped at 120 characters,
>    so a label longer than that is published truncated. Record it in
>    `company_or_project`.

### A1.6 Add sidecar item 10 (`score_rationale`) — request 5, decision D3

> 10. **Score rationale.** Two to three sentences, at most 500 characters,
>     saying in plain language why the dimension scores you gave add up to the
>     score they do: which dimension carried the most weight in this verdict,
>     which one held it back, and what would have to change to move the band.
>     Never state a number for the weighted score or the band — those are
>     computed server-side and you never emit them. Record it in
>     `score_rationale`. **Staff-only: unlike the elevator pitch, this field is
>     never posted to Slack**, so it may reason about the score freely — but it
>     is still bound by the confidentiality rule above and must not restate a
>     PI's unpublished disclosure.

### A1.7 Update the `<assessment_json>` skeleton

```json
{
  "company_or_project": "",
  "subject_agent_id": "",
  "headline": "",
  "key_points": {"significance": [], "innovation": [], "clinical_actionability": [], "key_questions": [], "commercial_potential": []},
  "elevator_pitch": "",
  "score_rationale": "",
  "gating": { ...unchanged... },
  "scores": { ...unchanged... },
  "red_flags": [],
  "recommendation": "advance | conditional | pass | route-to-incubation",
  "rationale": "",
  "recommended_next_experiment": "",
  "confidence": "High | Moderate | Speculative"
}
```

`scores` and `gating` keys are **unchanged** — they are pinned to the rubric
document by `test_skeleton_scores_keys_are_exactly_the_rubric_dimensions` and
`test_skeleton_gating_keys_are_exactly_the_documents_gating_criteria`.

### A1.8 Tests (same task)

In `tests/unit/test_rubric_prompt_sync.py`:

* `test_skeleton_carries_the_three_narrative_fields` — rename to
  `..._the_narrative_fields`, add `score_rationale`, and change the
  `key_points` equality to the five-key object in the order above.
* `test_the_scout_hub_prompt_set_version_is_1_3_0_or_later` — raise to
  `>= (1, 4, 0)`.
* New `test_phase4_bounds_the_headline_and_the_project_label` — asserts the
  prompt states `140` for the headline and `70` for the project label, so the
  prose and `_HEADLINE_SOFT_LIMIT`/`_PROJECT_SOFT_LIMIT` (Task A2) cannot drift.
* New `test_phase4_forbids_the_bare_approximation_tilde` — asserts the
  formatting paragraph names the tilde (SL2's counterpart on the hub side).
* New `test_phase4_marks_the_score_rationale_staff_only` — asserts the item
  says the field is never posted to Slack; this is the only thing standing
  between D3 and a future edit that re-merges it into the pitch.
* Re-verify `test_phase4_states_the_single_scale_and_has_no_funnel_stage`
  still passes: **no new prose may contain the substring "funnel"**.

---

## Task A2 — column, migration, and the engine write path

**Owns:** `src/models/opportunity.py`, `alembic/versions/0048_*.py`,
`src/agent/simulation.py`,
`tests/integration/test_assessment_narrative_fields.py`, and — added after the
audit *(BL-2)* — `scripts/migrate/preflight.py`,
`tests/unit/test_migration_checks.py`,
`tests/integration/test_harness_smoke.py`, `src/routers/admin.py` (one stale
comment, step 6).

1. **Model.** Add `score_rationale: Mapped[str | None] = mapped_column(Text,
   nullable=True)` beside the other three narrative columns, with a comment
   covering: sidecar item 10; NULL on every pre-`0048` row and never
   backfilled; **and that it is deliberately NOT published to Slack** (cite
   `assessment_headline.py`'s six-field list, so a future widening has to argue
   with a comment).
   While in the file, correct the stale comment on
   `recommended_next_experiment` — it says "Sidecar item 10 (rubric v2.1.0)" and
   that field is item **5**; item 10 is now `score_rationale`, and two fields
   claiming the same item number is exactly the drift this repo's comments
   otherwise avoid.
2. **Migration** per §2.
3. **Engine.** In `_persist_assessment`:
   * `score_rationale=_str_or_none(verdict.get("score_rationale"))` in
     `assessment_kwargs`, placed with the other narrative fields and sharing
     their "a wrong type becomes None and `raw_verdict` keeps the original"
     comment.
   * `_HEADLINE_SOFT_LIMIT`: `200` → `140`.
   * New `_PROJECT_SOFT_LIMIT = 70` and `_PITCH_SOFT_LIMIT = 900`, with
     WARNING-only shape checks in the same style as the existing headline check
     (A4 discipline: shape checks are warnings, never drops). The project
     warning must name the 120-character Slack clip, because that is the
     consequence an operator reading the log needs.
   * The grouped-`key_points` warning loop already iterates `KEY_POINT_GROUPS`
     and therefore picks up the two new groups for free — **verify, do not
     assume**: it reads `for group_key, _label in KEY_POINT_GROUPS`.
4. **Move the head-revision pins** *(audit, BL-2 — revision 1 named only the
   revision id and would have gone red in two test files)*. Adding `0048` to the
   chain needs six coordinated edits, each behind a different assertion.
   Verified against the tree; the `0047` commit (`095fdd0`) set the precedent,
   touching `preflight.py` (+15) and `test_migration_checks.py` (+4):

   | file | edit | the assertion that forces it |
   |---|---|---|
   | `scripts/migrate/preflight.py:74` | `DEFAULT_TARGET = "0048"` | `tests/unit/test_migration_checks.py:1004-1006` — `check_alembic_scripts` against the real tree asserts `data["heads"] == [pf.DEFAULT_TARGET]`; with `0048` on disk heads is `["0048"]` |
   | `preflight.py:134-138` | append `"0047"` to `SUPPORTED_START_REVISIONS` | `:239-271` — every `REVISION_ORDER[:-1]` entry at/after `0023` must be a supported start |
   | `preflight.py:422-426` | append `"0048"` to `REVISION_ORDER` | `:1009-1012` — `REVISION_ORDER[-1] == DEFAULT_TARGET` |
   | `preflight.py` `PLANNED_OBJECTS` | add `PlannedObject("0048", "column", "score_rationale", "opportunity_assessments")`, beside the 0043 column entries | `:870-915` — re-derives every `add_column` name from every revision in `REVISION_ORDER` and fails on any not declared |
   | `tests/unit/test_migration_checks.py:230-236` | the literal tuple **and** `assert pf.DEFAULT_TARGET == "0047"` | `test_supported_start_revisions_are_exactly_the_documented_set` pins both verbatim |
   | `tests/integration/test_harness_smoke.py:59` | `assert v == "0048"`, plus a `0048` line in the per-revision comment block above it | it asserts the migrated test DB's stamped head literally |

   `scripts/migrate/postflight.py` needs **nothing**: `VERIFIED_REVISIONS` is
   frozen at `0019`–`0023`, a documented pre-existing gap. `scripts/migrate` is
   linted at zero findings (§1), so these edits are held to the test-suite bar,
   not the `src/` ratchet.

5. **Warn on a MISSING key-point group** *(audit, G-4)*. D5 relaxes
   `normalize_key_points` to accept a partial object, and the existing
   grouped-shape check cannot see one: it reads
   `group = key_points.get(group_key)` then
   `if isinstance(group, list) and not (MIN <= len(group) <= MAX)`
   (`src/agent/simulation.py:4546-4556`), so an absent group is `None`, fails
   the `isinstance`, and logs nothing. Without a warning, D5 trades a loud
   failure (the whole field NULLed) for total silence — not the trade it is
   meant to make. Add one WARNING naming the absent group keys, beside the new
   soft-limit warnings. A3.2's claim that the existing warning path "catches it"
   is true only of UNKNOWN keys.

6. **Correct the stale contract comments** *(audit, G-9)*. This task owns two:
   `src/agent/simulation.py:4602` ("Sidecar item 10 (rubric v2.1.0)" on
   `recommended_next_experiment` — it is item 5) and `src/routers/admin.py:138`
   ("`key_points` >= 1.3.0 is the three-group object"), which revision 1 left
   unowned. Two more belong to their file's owner and are named here so the set
   is visible: `templates/admin/_assessment_detail_body.html:136` (the same
   "item 10" error) is Task B's, and
   `src/services/assessment_detail.py:107-112` ("three named groups") is Task
   A3's.

7. **Tests.** Extend `test_persist_assessment_stores_the_three_narrative_fields`
   to four fields (rename accordingly); add
   `test_a_wrong_typed_score_rationale_degrades_to_null_and_keeps_raw_verdict`
   (mirrors the existing `key_points` case); add
   `test_an_overlong_headline_or_project_label_warns_but_still_stores` asserting
   the row is written and the WARNING fired (`caplog`); add
   `test_a_missing_key_point_group_is_stored_and_warned` (step 5); and **invert
   `test_normalize_rejects_wrong_shapes`** *(audit, CT-3 — revision 1 told Task
   A3 to coordinate on this and then omitted it from A2's own list)*: the
   existing `assert normalize_key_points({"significance": ["s"]}) is None`
   (`tests/integration/test_assessment_narrative_fields.py:187`) becomes a
   round-trip under D5, while `{"…": [], "extra": []}` and `{}` still return
   `None` and a non-`str` member still returns `None`.

**Verify by reading, not by assumption:** `_str_or_none` exists and is used for
`headline`/`elevator_pitch`; `_bounded_str` is the clipping helper used only for
the four bounded VARCHAR columns. `score_rationale` is `Text`, so `_str_or_none`
is correct and `_bounded_str` is not.

---

## Task A3 — `src/services/assessment_detail.py`

**Owns:** `src/services/assessment_detail.py`, and the new-function tests in
`tests/unit/test_assessment_strength_risk_derivation.py` (new file).

### A3.1 Five key-point groups

```python
KEY_POINT_GROUPS = (
    ("significance", "Significance"),
    ("innovation", "Innovation"),
    ("clinical_actionability", "Clinical actionability"),
    ("key_questions", "Key questions / experiments"),
    ("commercial_potential", "Commercial potential"),
)
```

This tuple is the render order on **both** surfaces and is already registered as
a Jinja global on both routers — no router change needed.

### A3.2 Relax `normalize_key_points` (D5 / finding K1)

```python
if isinstance(value, dict) and value and set(value) <= _KEY_POINT_KEYS and all(
    isinstance(v, list) and all(isinstance(x, str) for x in v) for v in value.values()
):
    return value
```

Docstring must record: accepts the legacy flat list (scout_hub ≤ 1.2.0), the
three-group object (1.3.0) and the five-group object (1.4.0); a **subset** of
known keys is accepted because exact equality made one omitted group store NULL
and lose the whole field to `raw_verdict`, which is a worse outcome than a
partial object the templates already render correctly (they iterate
`KEY_POINT_GROUPS` and `.get`). An **unknown** key is still rejected outright —
that is real shape drift and the warning path in `_persist_assessment` plus
`test_skeleton_carries_the_narrative_fields` are what catch it.

Update `tests/integration/test_assessment_narrative_fields.py::test_normalize_rejects_wrong_shapes`
(Task A2 owns that file — **coordinate**: A3 changes the function, A2 changes
that test; state the expected new behaviour in both task briefs):
`{"significance": ["s"]}` now round-trips, `{"...": [], "extra": []}` and
`{}` (empty dict) still return `None`, non-str members still return `None`.

### A3.3 Derive strengths / risks / not-established (request 3, D2)

New public function, in this module because it reads exactly the three things
`build_assessment_detail` has already resolved:

```python
STRENGTH_THRESHOLD_FRACTION = 0.8   # of the revision's scale_max
RISK_THRESHOLD_FRACTION = 0.4

def derive_strengths_and_risks(
    assessment, *, dimensions, consults, revision
) -> dict[str, Any]:
    """Three buckets, from STORED values only — never a new judgement. ..."""
```

Returns
`{"strengths": [...], "risks": [...], "unestablished": [...], "scale_known": bool}`
where each entry is `{"source": str, "label": str, "detail": str}` and `source`
is one of `dimension` / `gating` / `red_flag` / `consult`.

Classification rules — **write every one of them into the docstring**:

| input | strength | risk | not established |
|---|---|---|---|
| dimension score | `>= 0.8 × scale_max` (4 on a 1–5 scale) | `<= 0.4 × scale_max` (2 on a 1–5 scale) | `score is None` → "not scored — counted as zero in the weighted score" |
| dimension score in between | — | — | **not listed at all**: a 3 of 5 is a real, neutral answer, not an unknown |
| `gating` value | `"met"` | `"not_met"` | `"unconfirmed"` → "never asked"; any other value → "unrecognised gating value" |
| `red_flags` entry | — | each entry, full text | — |
| consult | `verdict_signal in ("adequate", "clear")` | `verdict_signal in ("blocking", "gap", "caution")` | `reply_truncated` is True → regardless of signal |
| consult with a NULL or unrecognised `verdict_signal` *(audit, G-12)* | — | — | third bucket, in its own words: "signal not recognised — nothing can be said about this consult" |
| `revision is None` | dimensions contribute **nothing** to any bucket, and `scale_known=False` |

Four things the docstring must say explicitly, because each is a defect this
repo has already paid for once:

1. **The third bucket is not decoration.** `unconfirmed` means "never asked",
   an unscored dimension is not a scored zero, and a truncated consult's signal
   is `specialists.py`'s parse default rather than anything a specialist said.
   Filing any of the three as a strength or a risk manufactures a claim nobody
   made — the same error `panel_state`'s five states and
   `OpportunityAssessment.missing_domains`' three states exist to prevent.
2. **Thresholds come from the ROW's own revision**, via the `revision` argument,
   never from a literal 4 and 2 — a hardcoded threshold silently relabels every
   row scored on another scale, which is precisely the render-time re-derivation
   `panel_owed` was added to end.
3. **Nothing here is stored.** This is presentation of stored values; it writes
   no column and must never be mistaken for a write-time finding.
4. **It cannot raise.** A malformed `gating` value, a non-string red flag, a
   `scores` dict with a bool in it — each degrades into the third bucket or is
   skipped. A brief card must never 500 a page.

Add the result to `build_assessment_detail`'s returned context as
`verdict_signals` (a single new key; the detail context is a `**detail` splat on
both routers, so there is no allowlist to update — **verify** that
`admin_assessment_detail` and `manager_assessment_detail` both splat, they do).

### A3.4 Tests (new file `tests/unit/test_assessment_strength_risk_derivation.py`)

Pure unit tests, no DB, hand-built `OpportunityAssessment` instances plus
literal `dimensions`/`consults`/`revision` structures:

* each of the twelve rows of the table above, one test apiece or one
  parametrized sweep;
* `test_a_mid_scale_dimension_is_in_no_bucket`;
* `test_an_unknown_revision_puts_no_dimension_in_any_bucket_and_says_so`
  (`scale_known is False`);
* `test_a_truncated_consult_is_never_a_strength_or_a_risk`;
* `test_a_thresholds_are_read_from_the_revision_scale` — a synthetic revision
  with `scale_max=10` must classify 8 as a strength and 4 as a risk, proving no
  literal 4/2 survives;
* `test_it_never_raises_on_malformed_stored_values` — bool in `scores`, int in
  `red_flags`, unrecognised gating string, `gating=None`, `scores=None`;
* `test_an_unrecognised_consult_signal_lands_in_the_third_bucket` *(audit,
  G-12)* — `verdict_signal=None` and `verdict_signal="clear-ish"`.
  `specialist_consults.verdict_signal` is a plain stored column with no CHECK
  constraint, so falling off the end of the branch silently is the S1 defect in
  miniature.

---

## Task A4 — the Slack headline renderer

**Owns:** `src/services/assessment_headline.py`,
`tests/unit/test_assessment_headline_render.py`.

Finding P1: all 8 published pitches are cut mid-word at exactly 600 characters.
Fix the **clip**, not the cap — raising `PITCH_DISPLAY_CHARS` would publish more
sidecar prose to a public channel, which is the widening D3 exists to avoid.

Add, beside `_clip`:

```python
def _clip_at_sentence(value: object, max_len: int) -> str | None:
    """A non-empty string ending at a sentence boundary within ``max_len``..."""
```

Behaviour, and its reasoning, in the docstring:

* Under the cap → returned **unchanged**, byte-identical to `_clip` today, so
  every short pitch and every NULL pitch renders exactly as it does now and
  `scripts/backfill_assessment_headlines.py` stays consistent with the engine.
* Over the cap → cut after the last sentence terminator (`. `, `! `, `? `, or a
  terminator at the very end of the window) that leaves at least half the
  budget; append nothing — a complete sentence needs no ellipsis.
* No such boundary but whitespace exists → clip to `max_len` **first**, then
  back off to the last space and append `" …"`, so a truncation is visible as
  one rather than reading like the author stopped mid-word. The suffix is
  appended AFTER the `max_len` clip, so the returned string may be
  `max_len + 2` characters.
* **No boundary and no whitespace at all → `value[:max_len]`, with NO suffix.**
  Specified explicitly *(audit, BL-4)*: revision 1 said only "fall back to the
  last space", which is undefined for that input — and that input is exactly
  what the existing test feeds.
  `tests/unit/test_assessment_headline_render.py:223-231` calls
  `render_assessment_headline(..., elevator_pitch="x" * (PITCH_DISPLAY_CHARS + 500))`
  and asserts `"x" * PITCH_DISPLAY_CHARS in text` and
  `"x" * (PITCH_DISPLAY_CHARS + 1) not in text`. An implementer who reserved
  room for the two-character suffix inside `max_len` — the natural reading of
  "ending at a boundary *within* `max_len`" — would fail the first assertion.
* A non-string is dropped outright, exactly as `_clip` does.

Use it for `elevator_pitch` only. `project` and `recommendation` keep `_clip`:
they are labels, not prose, and a label's truncation is now prevented upstream
by A1.5's 70-character contract instead. That is not only a design preference —
`test_an_overlong_project_is_clipped_to_a_headline`
(`tests/unit/test_assessment_headline_render.py:86-92`) **hardcodes** `120`
(`assert "z" * 120 in text` / `assert "z" * 121 not in text`) rather than
importing `PROJECT_DISPLAY_CHARS` the way the pitch test imports its constant,
so raising the project cap fails its second assertion and word-boundary-clipping
the project fails its first *(audit, A3)*. Fixing the label length in the prompt
is the only remedy that leaves it green.

Update the module docstring's "exactly six fields" paragraph to record that the
pitch segment is a sentence-bounded excerpt of a field that is routinely longer
than the cap, and that `score_rationale` is deliberately **not** a seventh field.

Tests: the byte-identity case (short pitch unchanged); a long pitch clipped at a
sentence boundary with no ellipsis; a long pitch with no boundary clipped at a
word with `" …"`; a pitch of exactly 600 characters unchanged; a non-string
dropped; and two existing tests re-run UNCHANGED, both settled by the audit:
`tests/unit/test_assessments_summary_post.py`'s D12 six-field sentinel
(`:118-147`) asserts sentinel absence plus two positive substrings and uses a
short pitch — it does **not** pin a byte-exact body, so it is unaffected; and
`test_an_overlong_pitch_is_clipped` (`:223-231`), which is the test that *does*
pin exact clip bytes and which the no-whitespace rule above exists to keep
green.

---

## Task B — the assessment DETAIL body

**Owns:** `templates/admin/_assessment_detail_body.html`,
`tests/integration/test_assessment_detail_page.py`.

### B1 Strengths / risks box — request 3

A plain card (never a `<details>`, finding S3), placed **immediately after the
`#brief` card and before "The ask"**, which is what "under the top card" means
on this page. Skeleton:

```html
<div id="signals" class="assessment-signals scroll-mt-16 bg-white rounded-xl shadow-sm border border-gray-200 p-5 mb-6">
  <h3 class="text-base font-semibold text-gray-900 mb-3">Strengths and risks</h3>
  <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
    <div class="assessment-signals-strengths rounded-lg border border-green-200 bg-green-50 p-3">
      <div class="text-sm font-semibold text-green-900">Strengths</div>
      ... one <li> per entry, each prefixed with a ✓ glyph carrying aria-label="Strength" ...
    </div>
    <div class="assessment-signals-risks rounded-lg border border-red-200 bg-red-50 p-3"> ... ✗ / aria-label="Risk" ... </div>
    <div class="assessment-signals-unestablished rounded-lg border border-slate-200 bg-slate-50 p-3"> ... ? / aria-label="Not established" ... </div>
  </div>
  ... footnote ...
</div>
```

Requirements:

* Three columns, always all three rendered; an empty column says what its
  emptiness means ("No dimension scored at or above 4, and no gate met" /
  "None recorded" / "Nothing left unanswered") rather than showing blank space.
  An absent column would let a reader mistake "we did not classify this" for
  "there are none".
* Each entry prints its glyph **and** its words. `aria-label` on every glyph,
  per the gating-row precedent.
* Footnote, always visible, naming the provenance in one sentence: derived from
  this verdict's stored dimension scores, gating states, red flags and
  specialist signals, with the score thresholds taken from the revision that
  scored the row — and, when `scale_known` is False, saying that this row's
  revision is not in the registry so its dimension scores could not be
  classified.
* Add `#signals` to the sticky jump nav, after `Brief`.
* The existing **Red flags** card stays exactly as it is. The risk column
  repeats the flag text; on a page where both are visible and uncollapsed that
  duplication is the safe direction, and the N8 pin
  (`test_a_non_empty_red_flag_list_is_never_collapsed`) keeps applying to the
  card it was written about.

### B2 `score_rationale` — request 5

Inside the `#brief` card, below the two-column grid, as its own boxed block:

```html
{% if a.score_rationale %}
<div class="assessment-brief-score-rationale mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3">
  <div class="text-sm font-semibold text-amber-900">Why this score</div>
  ... prose_format-gated markdown / plain fallback, .assessment-prose max-w-none ...
</div>
{% endif %}
```

Gate the markdown path on `a.prose_format == 'markdown'` exactly as the pitch
and rationale do — a NULL stamp means legacy plain text that a markdown pass
would corrupt. Whole block conditional: NULL on every pre-`0048` row.

### B2b Fix the stale "item 10" comment *(audit, G-9)*

`templates/admin/_assessment_detail_body.html:136` says "Sidecar item 10 (rubric
v2.1.0)" on the `recommended_next_experiment` block; that field is sidecar item
**5** (`prompts/roles/scout_hub/phase4-thread-reply.md:213`), and after A1.6
item 10 is `score_rationale`. Two blocks on the same page claiming the same item
number is the drift this repo's comments otherwise avoid.

### B3 Key points — requests 2 and 5

No markup change needed: the grouped branch already loops
`key_point_groups`, so A3.1's two new groups appear automatically. **Verify**
that the `has_points` expression still works for the five-key object — it is
`(a.key_points.values() | map('length') | sum) > 0`, which is key-count
agnostic. Add a test rather than trusting the reading.

### B3b Review-mode copy *(audit, PS10 / plan §H5)*

Task H specifies the wording; this task makes the edit, because it owns the
file. Note what the audit settled: there is **no existing prose to correct**.
The mode select is a bare `<label for="add-mode">Feedback mode</label>` plus two
options (`:796-801`), and the card's only explanatory paragraph,
`review-rubric-instructions` (`:603-615`), is entirely about scoring the
proposal and never mentions the review bot, an analysis job, or "Learn". So this
is new copy, added under the mode select: choosing **Learn** marks this feedback
as eligible for the prompt-suggestion bot and nothing more; suggestions are
generated only when a human presses the button on
`/manager/prompt-suggestions`. The two option label strings do not change —
they are pinned (`tests/integration/test_assessment_review_ui.py:118-119`).

### B4 Tests

* `test_the_strengths_and_risks_box_renders_three_columns_and_a_footnote`
* `test_an_unconfirmed_gate_is_in_neither_the_green_nor_the_red_column`
* `test_a_row_with_an_unknown_revision_says_its_dimensions_could_not_be_classified`
* `test_the_box_is_never_inside_a_collapsed_details` (regex the `#signals`
  element and assert no enclosing `<details`, the same shape as the existing
  `test_the_panel_banner_is_never_inside_a_collapsed_details`)
* `test_the_score_rationale_renders_markdown_only_when_stamped` (two rows)
* `test_a_pre_0048_row_renders_no_score_rationale_block`
* `test_the_five_key_point_groups_render_in_order` (index-ordering assertions)
* **Re-run and fix** `test_the_detail_body_uses_readable_type_sizes`: re-measure
  `text-xs` and update the ceiling **to the measured number + 2**, and keep the
  four colour assertions true (use `text-gray-600`/`text-slate-700`, never
  `-400`/`-500`).
* Re-run `test_the_brief_is_two_columns_pitch_left_points_right` unchanged.

---

## Task C — the assessments LIST body

**Owns:** `templates/admin/_assessments_body.html`,
`tests/integration/test_assessment_queue_controls.py`,
`tests/integration/test_opportunity_assessment_persistence.py`.

That third file was added after the audit *(CT-1)*: it is where
`test_admin_assessments_page_renders_no_inline_detail_rows` actually lives
(`:1474`), and revision 1 ordered that test edited (C4) without assigning its
file to anyone.

### C0 Contract additions to the body's header comment

Document that each wrapper must, before the include, define:

* macros `assessment_link(a)` and `pi_link(a)` (unchanged, existing);
* `{% set list_surface = 'admin-list' %}` / `'manager-list'` — a **bare token**,
  never a path (finding Q2: `test_manager_assessments_never_links_into_admin`
  asserts `/admin/` appears nowhere on the manager page).

### C1 Card id + two-column narrative — request 5

* Card div: `<div id="a-{{ a.id }}" class="assessment-card ...">`. The `id`
  goes **before** `class`; `_row_slice` locates `class="assessment-card "` and
  then the preceding `<div`, so ordering is safe — but re-run
  `test_row_slice_stops_at_the_next_card` and
  `test_row_slice_stops_at_the_end_of_the_last_card` to confirm.
* Replace the current flat `assessment-card-points` block with a two-column
  grid mirroring the detail page: **pitch LEFT, key points RIGHT**, collapsing
  to one column when either half is absent, and skipped entirely when both are.
  Reuse the detail page's own `has_points` expression.
* Boxes and tinted backgrounds (request 5's "visually separate … while
  maintaining readability"). One tint per section, not per group:

| block | classes |
|---|---|
| pitch | `assessment-card-pitch rounded-lg border border-slate-200 bg-slate-50 p-3` |
| key points | `assessment-card-points rounded-lg border border-gray-200 bg-white p-3` |
| score rationale | `assessment-card-score-rationale rounded-lg border border-amber-200 bg-amber-50 p-3` |

  Keep `assessment-card-points` as the class name — it is asserted by
  `test_a_row_with_no_headline_falls_back_to_the_short_label`
  (`assert "assessment-card-points" not in html`), so the name must survive AND
  must still be absent when `key_points` is NULL.
* **Gate the points box on `has_points`, not on `is mapping`** *(audit, G-13)*.
  Today's line is `{% if a.key_points is mapping %}`
  (`_assessments_body.html:294`) with no content check, so under D5 a mapping of
  only empty lists — now a storable shape — renders an empty tinted box. The
  detail page already has the twin test
  (`tests/integration/test_assessment_detail_page.py:1666`,
  `test_a_mapping_of_only_empty_lists_renders_no_key_points_column`); add the
  list-page version.
* **Every one of these boxes must be fully conditional** *(audit, A6)*. Every
  production row today has `headline IS NULL` and `key_points IS NULL` — that
  test's own docstring records it — so a grid wrapper or a tinted box emitted
  unconditionally fails on the entire current corpus, not on an edge case.
* Pitch length on a card: wrap the pitch body in `line-clamp-6` (Tailwind Play
  CDN is JIT, so the utility resolves) with the `detail →` link as the escape.
  A 1300-character pitch is a wall on a triage card; the full text is one click
  away. Do **not** truncate server-side — the DOM must carry the whole value so
  nothing is lost to a CSS-off reader.
* `score_rationale` box under the grid, whole block conditional, with the same
  eyebrow label Task B2 uses on the detail page — **"Why this score"** — so the
  two surfaces do not name one field two ways *(audit, minor)*.
* Markdown: `prose_format`-gated `data-markdown` exactly as on the detail page.
  Task D supplies the renderer.

### C2 Collapsed "Rubric scores & gating" disclosure — request 7, D4

Replace the gating glyph row in the card footer with:

```html
<details class="assessment-card-scores mt-3 border-t border-gray-100 pt-3">
  <summary class="cursor-pointer text-sm font-medium text-gray-700">Rubric scores &amp; gating</summary>
  ... per-dimension rows from a.dimension_rows (Task E) ...
  ... the existing gating glyph markup, moved verbatim ...
</details>
```

* **Closed by default** (no `open` attribute) — the request.
* Dimension rows render title, score, weight note and a bar, from
  `a.dimension_rows` (Task E), using the ROW's own revision. A row whose
  revision is unknown renders no weights and no bars and says so; its `title` is
  the **humanised key** (`key.replace("_", " ")`), matching what
  `build_assessment_detail` already does for an unnamed score key
  (`src/services/assessment_detail.py:637-644`). Revision 1 said "no titles",
  which would have diverged from the detail page *(audit, minor)*.
* The gating markup moves **verbatim**, including the three glyphs, their
  `title`s and the `gating-row gating-*` classes, so
  `test_the_card_keeps_gating_panel_flags_and_rubric_on_its_face`'s
  `"life sciences domain"` assertion still holds; the test's docstring gains a
  note that the string is now inside a collapsed disclosure (D4), and a new
  sibling test asserts it is inside `assessment-card-scores`.
* The top-of-page gating legend paragraph stays where it is — it now explains
  glyphs that live one click down, so reword it to say so.
* The card face keeps, unchanged: flag count, panel badge (all five states,
  terminal `{% else %}` still a badge), review status chip, Assigned,
  Reviewed by, detail link (finding R3).

### C3 Quick scoring — request 4

One collapsed disclosure per card, after the footer row:

```html
{% set qs = 'qs-' ~ a.id ~ '-' %}
<details class="assessment-card-quickscore mt-3 border-t border-gray-100 pt-3">
  <summary class="cursor-pointer text-sm font-medium text-indigo-700">Quick scoring</summary>
  <p class="mt-2 text-sm text-gray-600 max-w-[68ch]">
    Rates the PROPOSAL's own merit, not the bot's performance. Per-dimension
    rubric scoring is on the detail page.
  </p>
  {% if impersonation_banner %}<p class="...">Reviewing as <strong>{{ impersonation_banner.name }}</strong>; your own account is recorded as the person who entered it.</p>{% endif %}
  <form method="post" action="/reviews/assessments/{{ a.id }}/feedback" class="mt-2 space-y-2">
    <input type="hidden" name="surface" value="{{ list_surface }}">
    <input type="hidden" name="run_id" value="{{ 'all' if show_all_runs else selected_run_id }}">
    <input type="hidden" name="sort" value="{{ sort }}">
    <input type="hidden" name="lab" value="{{ lab_filter or '' }}">
    <label for="{{ qs }}score" ...>Overall rating (1 = weak … 5 = strong)</label>
    <select id="{{ qs }}score" name="score" required ...>
      <option value="" disabled selected>&mdash;</option>
      {% for n in range(1, 6) %}<option value="{{ n }}">{{ n }}</option>{% endfor %}
    </select>
    <label for="{{ qs }}comment" ...>Comment</label>
    <textarea id="{{ qs }}comment" name="comment" rows="2" ...></textarea>
    <label for="{{ qs }}mode" ...>Feedback mode</label>
    <select id="{{ qs }}mode" name="feedback_mode" required ...>
      <option value="" disabled selected>&mdash;</option>
      <option value="learn">Learn</option>
      <option value="log_only">Don't learn — log only</option>
    </select>
    <button type="submit" ...>Submit feedback</button>
  </form>
  <form method="post" action="/reviews/assessments/{{ a.id }}/status" class="mt-3 flex items-center gap-2">
    ... same four hidden inputs ...
    <button name="action" value="approved">Approve</button>
    <button name="action" value="disapproved">Disapprove</button>
    <button name="action" value="cleared">Clear</button>
  </form>
</details>
```

Rules:

* **No `dim_*` fields.** `_parse_dimension_scores` then returns `{}`, which
  `_normalized_dimension_scores` stores as SQL NULL — already the supported
  path (`test_posting_no_dimension_fields_at_all_still_works`). No route or
  service change is needed for this half.
* **No edit, no delete, no assign/unassign** (findings Q4, Q5). Edit is
  excluded because `edit_feedback` REPLACES `dimension_scores` from the posted
  form, so a dimension-free edit would silently wipe scores entered on the
  detail page; say so in a comment.
* Every `id` is per-row unique via `a.id`; every `<select>` has a
  `<label for>`; the score and mode selects keep a blank `disabled selected`
  first option.
* Labels: the summary text is exactly **"Quick scoring"** (the request's
  wording). The mode option strings stay the two pinned ones.
* **The status control must be BUTTONS, not a `<select>`** *(audit, A4)*.
  `tests/integration/test_assessment_queue_controls.py:664-668` row-scopes
  `assert "Approved" not in untouched_row` and the same for `"Disapproved"`. The
  buttons above read `Approve` / `Disapprove` / `Clear` with lowercase
  `value="approved"` etc., which is safe because the assertion is case-sensitive
  and those are not substrings; a `<select>` whose option TEXT is
  "Approved"/"Disapproved" — the natural compact control, and the exact
  `VALID_STATUS_ACTIONS` vocabulary — breaks it.
* **Add-only, and say so in the UI** *(audit, minor)*. This form creates a new
  `AssessmentReview` row on every submission; there is no edit path here (see
  above) and no existing-feedback display on the card, so a reviewer who submits
  twice silently gets two rows. One line under the submit button — "Adds a new
  note; edit or remove one on the detail page" — makes that visible rather than
  surprising.
* Impersonation: the write half stays visible (F4 allows impersonated review
  writes); one short notice line, matching the detail card's wording.

### C4 Tests

* `test_quick_scoring_is_collapsed_and_labelled` — `assessment-card-quickscore`
  present, summary text "Quick scoring", no `open` attribute.
* `test_quick_scoring_posts_to_literal_review_paths_on_both_surfaces` —
  `/reviews/assessments/{id}/feedback` and `/status` present on `/admin` and
  `/manager`; `method="post"` present.
* `test_quick_scoring_offers_no_rubric_dimension_fields` — `name="dim_"` absent
  from the whole list page.
* `test_quick_scoring_offers_no_edit_delete_or_assign_controls` —
  `/reviews/feedback/`, `/assign`, `/unassign` absent.
* `test_quick_scoring_select_ids_are_unique_and_labelled` — two seeded rows;
  collect every `<select id=...>` on the page, assert no duplicates and a
  matching `for=` for each (the list-page twin of the detail-page test).
* `test_quick_scoring_renders_for_a_reviewer_on_the_manager_surface`.
* `test_a_submitted_quick_score_returns_to_the_filtered_list` — **moved to Task
  F** *(audit, CT-4)*. Revision 1 put it here "expected to fail until the
  integrated run", which contradicts §7's claim that wave 1 is parallel and
  disjoint. It belongs with the redirect it tests, which Task F owns.
* `test_the_card_scores_disclosure_is_closed_by_default_and_holds_the_gating`.
* `test_the_card_scores_use_the_rows_own_revision` — a row stamped with an
  archived revision renders that revision's dimension titles, not today's.
* `test_the_list_page_renders_the_pitch_and_key_points_side_by_side` —
  `assessment-card-pitch` index < `assessment-card-points` index.
* `test_a_row_with_no_pitch_or_points_renders_neither_box`.
* **Page-weight guard** (finding Q6): `test_the_list_page_stays_under_a_size_ceiling`
  — seed 50 assessments with full narrative fields, GET `/admin/assessments`,
  assert `len(resp.text) < CEILING` where CEILING is the **measured** value of
  the new page plus 20%. Record the measured number in the test's docstring so
  the next change can see what it cost.
* **Re-run and fix** `test_admin_assessments_page_renders_no_inline_detail_rows`
  and `test_no_detail_prose_in_new_columns`: still no rationale, red-flag text
  or milestones; still no `assessment-detail` / `Expand all` / `Collapse all` /
  `Click for rationale`. The pitch, score rationale and dimension SCORES are new
  and deliberate — narrow the test's docstring to say which fields are now on
  the card and why (the same "deliberate, bounded reversal" note the 2026-09-09
  `key_points` change added), and cite the 2026-08-27 removal this reverses.
* Re-run `test_the_card_leads_with_the_headline_and_keeps_the_short_label`,
  `test_a_row_with_no_headline_falls_back_to_the_short_label`,
  `test_the_manager_surface_renders_the_same_cards`, and all three
  `panel_state` badge tests unchanged.

---

## Task D — the two list wrappers

**Owns:** `templates/admin/assessments.html`, `templates/manager/assessments.html`,
`tests/integration/test_assessment_list_chrome.py` (new file).

1. Add `{% set list_surface = 'admin-list' %}` / `'manager-list'` before the
   include, beside the two existing macros, with a comment pointing at the
   body's contract note and at Q2 (why it is a token and not a path).
2. Add `{% block extra_head %}` to both, carrying **the same three scripts and
   the same style block as the two detail wrappers**, copied verbatim including
   the SRI hashes: `marked@12.0.2`, `dompurify@3.1.6`, `/static/js/markdown.js`,
   then the `.md-content` rules, the `.assessment-prose` reading scale, the
   `focus-visible` outline, and `@media print`. Note the correction *(audit,
   FA-5)*: "the detail wrappers' style blocks are already required to be
   identical" is a **comment**
   (`templates/admin/assessment_detail.html:25-27`), not a test —
   `tests/integration/test_assessment_detail_chrome.py:64-99` asserts each rule
   on both rendered surfaces but never compares the two blocks. So state the
   keep-identical rule in the two NEW wrappers and leave the existing pair
   alone (Task B and Task D must not both edit the detail wrappers).
3. **Give the three filter selects an `id` and their labels a `for`** on both
   list wrappers *(audit, G-5)*. `templates/admin/assessments.html:13-14`,
   `:27-29`, `:38-40` and the manager twin currently have
   `<label class="text-sm text-gray-500">Run:</label>` against
   `<select name="run_id" …>` — no `id`, no `for`. Six attributes, no behaviour
   change, and it is what lets Task C4's labelling test be page-wide instead of
   scoped to the new controls. Without it the page-wide form of that test fails
   on pre-existing markup.
4. Add `.assessment-card-pitch .assessment-prose { max-width: none; }` — the
   card column is already narrower than the 68ch measure.
5. Tests (new file): both list pages carry the three script tags with their
   integrity attributes; both carry the 17px prose rule
   (`font-size: 1.0625rem`) and `max-width: 68ch`; both carry `focus-visible`
   and `@media print`; the manager page contains no `/admin/`; and every
   `<select>` on both pages has an `id` with a matching `<label for>` (step 3).

---

## Task E — per-row dimension rows for the list

**Owns:** `src/services/directory.py`, `tests/unit/test_directory_assessments.py`.

In `list_assessments`, after the existing `panel_state` / `review_cols` /
`pi_user_ids` attach loops, add a fourth: per row, resolve its own revision and
build the rows the card's disclosure renders.

```python
from src.services.rubric_revisions import resolve_revision   # new import
...
for _row in assessments:
    _row.dimension_rows, _row.revision_view = _assessment_dimension_rows(_row)
```

`_assessment_dimension_rows(row)` is a module-private helper returning
`(rows, revision_view)` where each row is
`{"key", "title", "score", "weight", "weight_note", "pct"}` — the same shape
`build_assessment_detail` produces, deliberately, so the two surfaces cannot
disagree about a dimension's title or bar length.

Requirements:

* **Attach to the row, never a new context key** (finding P4). The comment on
  the existing `panel_state` loop already explains why; extend it rather than
  repeating it.
* Normalize score keys `strip().lower()`, matching the detail page and the
  existing `off_rubric_count` computation.
* Keys the resolved revision does not name still render, untitled and
  unweighted — a stored row must show its data (the pre-registry page dropped a
  v2 row's 13 scores on the floor; do not reintroduce that).
* `revision is None` → rows with `pct = None` and `weight_note = None`, and
  `revision_view = None` so the template can say the scale is unknown.
* 500 `resolve_revision` calls are cheap, but **not for the reason revision 1
  gave** *(audit, FA-6)*. It is not a bare `_BY_HASH` lookup: it calls
  `live_revision_view()` unconditionally on entry
  (`src/services/rubric_revisions.py:145`), which constructs a
  `RubricRevisionView` plus six `RevisionDimension`s and six f-strings every
  time. That is cheap only because `load_rubric()` behind it returns an
  import-time singleton (`src/services/blackbird_rubric.py:415-420`) — no file
  I/O per call. Fine at 500 rows; worth knowing before anyone calls it per
  dimension rather than per row.
* The comment in the returned dict that records the 2026-08-27 removal of the
  score chips must be **updated, not deleted**: say that per-dimension scores
  are back, behind a collapsed per-card disclosure, at operator request
  (2026-09-14), and that they now ride on the rows rather than on the
  `rubric_weights`/`row_scales` context keys that were removed with them.

Tests: a row stamped with an archived revision gets that revision's titles and
weights; an unstamped row gets the live one; a row whose `scores` keys are all
off-rubric still yields one entry per stored key; a row with `scores=None`
yields `[]`; `test_the_rows_carry_no_new_top_level_context_key` — assert the
returned dict's key set is exactly what it is today (guards P4 directly).

---

## Task F — review writes: list-page return, and the end of the automatic enqueue

**Owns:** `src/routers/reviews.py`, `src/services/assessment_reviews.py`,
`tests/integration/test_reviews_router.py`,
`tests/integration/test_review_pipeline_races.py`,
`tests/integration/test_review_dimension_scores.py`,
`tests/integration/test_review_job_end_to_end.py`, and — added after the audit —
`tests/unit/test_review_bot_edges.py`'s ONE enqueue-dependent test
*(CT-2: that file is otherwise Task G's; this is the fourth cross-task coupling
§7 must declare)*, plus Task C4's relocated
`test_a_submitted_quick_score_returns_to_the_filtered_list` *(CT-4)*.

### F1 List-page redirect (request 4, findings Q1/Q2)

Extend `_assessments_redirect` to four surfaces:

```python
_LIST_SURFACES = {"admin-list", "manager-list"}
```

* `admin` / `manager` → unchanged, the detail page.
* `admin-list` (admins only) / `manager-list` (everyone else) → the LIST page,
  with `run_id` / `sort` / `lab` re-emitted via `urllib.parse.urlencode` and the
  fragment `#a-<assessment_id>`.
* An unrecognised surface keeps today's behaviour: fall through to
  `/manager/assessments/{id}`. A surface string is unvalidated form input and a
  bad one must land the reader somewhere real, not 400.
* **Validate the three params the same way `list_assessments` does**, and drop
  anything else: `sort` must be in `ASSESSMENT_SORTS` (import it) or is
  dropped; `run_id` must be `"all"` or parse as a UUID or is dropped; `lab` is
  opaque and is passed through `urlencode` only. Rationale, in the docstring:
  these values are echoed into a `Location` header, `urlencode` is what makes
  CR/LF and `&` injection impossible, and silently dropping a junk value
  reproduces the page's own "a stale bookmark renders the queue, not an error"
  behaviour.
* Keep the existing warning: build full literal paths, never a bare `/admin`
  constant, or `test_reachability`'s `src_strings` scan marks the allowlisted
  `GET /admin` entry stale.
* The **admin-surface whitelist stays**: `admin-list` maps to `/admin/...` only
  when `current_user.is_admin`, exactly as `admin` does today
  (`test_a_reviewer_posting_surface_admin_is_clamped_to_manager` is the pin;
  add its `-list` twin).

**The helper's own signature does change** *(audit, G-11 — revision 1 said "no
handler signature changes", which was true of the handlers and false of the
helper it hid behind)*. `_assessments_redirect` gains keyword-only
`run_id=None, sort=None, lab=None`. It has **six** call sites
(`src/routers/reviews.py:211, 251, 270, 294, 315, 335`); only
`submit_review_feedback` and `set_review_status` pass the new arguments.
`edit` / `delete` / `assign` / `unassign` keep passing nothing, which is the
documented consequence: a `*-list` surface posted to one of those four lands on
the UNFILTERED list rather than 400ing — the same
land-somewhere-real-rather-than-error posture the surface fallback already has.

Handler signatures: `surface: str = Form("manager")` already carries whatever
the form posts. `submit_review_feedback` already calls `await request.form()`
(for `_parse_dimension_scores`), so it reads the three from there.
`set_review_status` does **not** read the form today (confirmed:
`src/routers/reviews.py:273-294` takes only `action` and `surface`) — add the
three as optional `Form(None)` parameters rather than reading the raw form, so
the signature documents them. The audit confirmed this is safe for
`test_reviewer_can_approve_and_history_appends`
(`tests/integration/test_reviews_router.py:441-447`) and for the Origin guard
(`tests/conftest.py:149-159` sets `Origin` on the default client).

### F2 Manual-only prompt suggestions (request 6, D7)

1. **Delete both auto-enqueue call sites** in `submit_feedback` and
   `edit_feedback`. Keep `enqueue_analysis_if_absent` — it becomes the batch
   helper's idempotency guard and is what makes the new button safe to press
   twice.
2. **New service function** in the same module:

   ```python
   async def enqueue_pending_analyses(
       db: AsyncSession, *, requested_by: User, assessment_id: uuid.UUID | None = None
   ) -> tuple[int, int]:
       """Enqueue one review_feedback_analysis job per assessment that has at
       least one unconsumed 'learn' review and no PENDING job. Returns
       (enqueued, eligible). Caller commits."""
   ```

   Implementation notes to write down:
   * Eligibility is one `SELECT DISTINCT assessment_id FROM assessment_reviews
     WHERE feedback_mode = 'learn' AND consumed_at IS NULL`, then
     `enqueue_analysis_if_absent` per id — the dedupe already skips an id that
     has a pending job, so pressing the button twice enqueues nothing the second
     time and the return pair makes that visible instead of silent.
     `processing` is deliberately **not** counted as covering an id, for the
     reason `enqueue_analysis_if_absent`'s docstring already gives.
   * `assessment_id`, when given, narrows to that one id (used by the optional
     per-assessment button, F4).
   * `user_id=requested_by.id` on the job, so `/admin/jobs` attributes it.
3. **New route**, `POST /reviews/suggestions/generate`:

   ```python
   @router.post("/suggestions/generate")
   async def generate_prompt_suggestions(
       assessment_id: str = Form(""),
       db: AsyncSession = _DB,
       current_user: User = _STAFF,
   ):
   ```
   * `_STAFF`, not `_REVIEW`: a reviewer cannot see
     `/manager/prompt-suggestions` (a suggestion can quote an unpublished PI
     disclosure verbatim), so a reviewer must not be able to create one either.
   * `_refuse_impersonation(current_user)` — this spends real Opus calls and
     belongs with `assign`/`unassign`/suggestion-status, not with the review
     writes F4 opened up.
   * Malformed `assessment_id` → 400 (`_parse_assignee_id`'s pattern).
   * Redirect to the literal `/manager/prompt-suggestions?generated=<n>&eligible=<m>`.
   * One INFO log naming actor, enqueued and eligible counts.
4. **Extend the POST allowlist test**
   (`test_the_reviews_router_posts_are_an_explicit_allowlist`) to 8 paths, and
   update its docstring with the reason.

### F3 Tests to invert (finding PS8)

Each of these encodes the automatic enqueue. Invert the assertion and keep the
property being tested:

* `test_reviewer_can_submit_feedback_and_learn_enqueues_one_deduped_job` →
  `test_learn_feedback_enqueues_nothing_until_a_manual_generate`: submit twice,
  assert zero jobs; call the generate route; assert exactly one job.
* `test_two_pending_jobs_already_exist_dedupe_still_succeeds` → drive the dedupe
  through `enqueue_pending_analyses` instead of through `submit_feedback`.
* `test_log_only_feedback_enqueues_nothing` → keep, and add that a manual
  generate also enqueues nothing for a `log_only`-only assessment.
* `test_editing_log_only_to_learn_enqueues_the_analysis_job` →
  `..._makes_it_eligible_for_the_next_manual_generate`.
* `test_reviews_router.py::test_only_the_author_can_edit` (`:224-260`) —
  **added after the audit** *(BL-3)*. It asserts `len(jobs) == 1` at `:259-260`,
  a side effect of `edit_feedback`'s enqueue
  (`src/services/assessment_reviews.py:277-280`). Drop the job assertion; the
  test is about authorship.
* `tests/unit/test_review_bot_edges.py::test_deleting_the_reviewer_deletes_their_pending_job_but_keeps_the_review`
  (`:300-321`) — **added after the audit** *(BL-3)*. `:311` asserts a job exists
  immediately after `submit_feedback(..., feedback_mode="learn")`. It must
  enqueue explicitly; the cascade behaviour it tests is unrelated to who
  enqueued. This is the one test Task F owns inside Task G's file (CT-2).
* `test_review_pipeline_races.py`, **all FIVE** — corrected from "all four"
  *(audit, BL-3)*: `:53`, `:93`, `:124`, `:174`
  (`test_unchanged_rows_are_still_stamped_and_no_warning_fires`) and `:199`
  (`test_repointed_job_consumes_the_repointed_reviews_under_the_new_id`) all
  unpack `(job,) = await _review_jobs(db_session)` at `:62`, `:102`, `:149`,
  `:185`, `:218`, which raises `ValueError` on an empty list. Replace each with
  an explicit `enqueue_pending_analyses` call (or a literal `Job(...)`, matching
  `test_review_job_end_to_end`'s no-op test), and change the three
  `== ["pending", "processing"]` assertions (`:85`, `:121`, `:171`) to
  `== ["processing"]` **plus** a new line proving the row is still unconsumed and
  that a manual generate then enqueues it. For `test_repointed_job…` the explicit
  enqueue must run BEFORE the re-point, so `job.payload` still names
  `retired.id` (`:219`). The docstrings must record the shape change: the
  anti-loss guarantee is `consumed_at_predicates`, which is untouched; what
  changed is only who schedules the replacement pass.
* **Two tests become VACUOUS rather than failing, and must be rewritten anyway**
  *(audit, PS8)* — a test that passes for the wrong reason is worse than one
  that fails. `test_two_pending_jobs_already_exist_dedupe_still_succeeds`
  (`:102-148`) seeds its own two pending jobs and asserts `len(jobs) == 2`, so
  it stops exercising the dedupe entirely; drive it through
  `enqueue_pending_analyses` instead. `test_log_only_feedback_enqueues_nothing`
  (`:151-163`) asserts `jobs == []`, which becomes true for every mode; make it
  assert that a manual generate enqueues nothing for a `log_only`-only
  assessment.
* **Rename the tests whose names become lies** *(audit, minor)*:
  `test_learn_feedback_submitted_mid_job_gets_its_own_job`
  (`test_review_pipeline_races.py:53`) no longer does. Revision 1 said to
  rewrite docstrings and not names.
* `test_review_dimension_scores.py::test_a_dimension_only_edit_survives_an_in_flight_analysis_job`
  — settled by the audit: it builds the UPDATE by hand and never reads the
  queue (`:307-368`), so it passes **unchanged**. Only its docstring (`:310`)
  goes stale and needs a line.
* `test_review_job_end_to_end.py::test_learn_feedback_becomes_a_suggestion_through_the_worker`
  — add an explicit enqueue between `submit_feedback` and `_one_round`.

### F3b Stale docstrings the removal leaves behind *(audit, G-10)*

Three code sites and two test docstrings assert the deleted behaviour. Each is
named here with its owner, because one of them is in Task G's file:

* `src/services/assessment_reviews.py:1-12` — the module docstring opens on
  "the enqueue dedupe that keeps rapid submissions/edits from buying repeated
  Opus calls … enqueueing one per submission". Task F.
* `src/services/assessment_reviews.py:237-239` — `edit_feedback`: "enqueues a
  replacement job … picked back up by the next analysis job". Task F.
* `src/services/review_bot.py:538-539` — "stays unconsumed for the job the
  edit/submit path already enqueued". **Task G's file, Task F's cause** — put it
  in G's brief with the new wording supplied by F.
* `tests/integration/test_review_dimension_scores.py:310` and
  `tests/integration/test_review_pipeline_races.py:132-134` — both narrate the
  auto-enqueue. Task F.

### F4 Optional, clearly marked

A second call site for the same route on the detail page's Human-review card
("Generate a prompt suggestion from this assessment's feedback", posting
`assessment_id`). It is one hidden input against an already-built route. **Not
required by the request** — the global button satisfies it — and it would make
Task F and Task B share `_assessment_detail_body.html`. Do it as a follow-up
commit after both land, or drop it.

---

## Task G — the review bot: both roles, and multi-target suggestions

**Owns:** `src/services/review_bot.py`, `prompts/review-bot.md`,
`scripts/eval_review_bot.py`, `tests/unit/test_review_bot.py`,
`tests/unit/test_review_bot_edges.py`,
`tests/unit/test_review_bot_prompt_contract.py`,
`tests/unit/test_review_bot_inputs.py`.

### G1 Label the prompt files by role (finding PS1)

`_prompt_file_set()` returns bare paths and already includes all four pi_lab
base files. **Keep it returning a flat list of path strings and add a separate
path→label map** — the `(path, role_label)` pairs alternative revision 1 offered
is excluded *(audit, G-3)*: `tests/unit/test_review_bot_edges.py:253-258` does
`real = review_bot._prompt_file_set()` then monkeypatches it to
`lambda: real + ["prompts/does-not-exist.md"]`, so a pair-returning version
would make `_render_prompt_files` call `Path(tuple)` and raise `TypeError`.
Either way:

* `prompts/agent-system.md`, `prompts/identity.md`,
  `prompts/phase4-thread-reply.md`, `prompts/phase5-new-post.md` →
  `"PI lab bot (pi_lab) — the lab agents' prompt set"`
* `prompts/roles/scout_hub/*` → `"Scouting hub bot (scout_hub) — BlackbirdBot's prompt set"`
* `prompts/rubric/blackbird-rubric.toml` → `"Scoring rubric"`
* `prompts/specialists/*.md` → `"Specialist persona"`

Render it into the block header only:

```
--- FILE: prompts/agent-system.md [PI lab bot (pi_lab) — the lab agents' prompt set] (sha256:…) ---
```

**`prompt_files` metadata keeps exactly `{"path", "sha256_12"}`** (finding PS7):
`test_happy_path_...` asserts the key set and `manager._prompt_file_status`
reads those two keys. The label is a rendering concern only.

**The labels go inside the existing `CURRENT PROMPT FILES` block, never as a new
section** *(audit, A7)*. `tests/unit/test_review_bot_prompt_contract.py:35-38`
asserts the prompt describes exactly the four `##` sections the code sends, and
`:41-45` asserts `"five sections" not in PROMPT`. A role map added as a fifth
`##` section fails both — and so does a fifth `- **ALLCAPS**` bullet inside
"What you will be given", which `:30-38` regexes with
`^- \*\*([A-Z][A-Z ]+)\*\*` and requires to be exactly four.

Add the two role manifests to the set (finding PS3):
`prompts/roles/pi_lab/role.toml`, `prompts/roles/scout_hub/role.toml`, labelled
with their role. They are ~20 lines each and they are where `post_types` and the
prompt-set version live.

### G2 Multi-target output contract (request 6, D6 / finding PS6)

Prompt and code both change to:

```json
{
  "target": "scout_hub",
  "suggestion": "...",
  "rationale": "...",
  "additional_proposals": [
    {"target": "pi_lab", "suggestion": "...", "rationale": "..."}
  ]
}
```

* `target` stays the object's **first key**, so `_LEADING_TARGET_RE` keeps
  working untouched (settled by the audit: it is
  `^\s*\{\s*"target"\s*:\s*"([^"\\]+)"`, anchored on the first key only,
  `src/services/review_bot.py:107`).
* **The pipe-joined vocabulary string must remain the FIRST `"target":`
  occurrence in both prompt texts** *(audit, G-2)*.
  `test_target_vocabulary_matches_the_validator_in_both_prompts`
  (`tests/unit/test_review_bot_prompt_contract.py:48-57`) uses `re.search` — the
  FIRST match — of `"target":\s*"([^"]+)"` and requires the captured value to
  split on `|` into exactly `_STATIC_TARGETS | {"specialist:<domain>"}`. Today
  that first match is `prompts/review-bot.md:107` and
  `src/services/review_bot.py:81`. So the output-contract block keeps
  `"target": "scout_hub | pi_lab | specialist:<domain> | rubric | out_of_scope"`
  verbatim and the `additional_proposals` element appears AFTER it; any
  worked example naming a concrete target must come later in the file, not
  earlier. Add a test asserting the vocabulary line is the first `"target":`
  occurrence, so the ordering is pinned rather than remembered.
* `additional_proposals` is **optional**; absent means today's behaviour
  exactly.
* Cap it at **two** entries (three proposals total) in the prompt and enforce
  the cap in code — each entry is a row and each job is already a 70–90k-token
  Opus call; an unbounded list is an unbounded row count off one button press.
* New `_parse_additional_proposals(parsed: dict) -> list[tuple[str, str]]`:
  validates each entry's `target` with the existing `_is_valid_target`, composes
  its body with the existing `_compose_suggestion_body`, **drops** an entry
  whose target is invalid or whose body is blank (with one WARNING naming the
  count), de-duplicates against the primary target, and truncates to the cap.
  It must never raise and must never be able to lose the primary proposal.
* `_parse_model_output` keeps its exact `(target, suggestion_text)` signature
  and semantics. The coupling is larger than revision 1 stated *(audit, A8)*:
  `scripts/eval_review_bot.py:254` plus **seven** tests in
  `tests/unit/test_review_bot_edges.py:64-142`
  (`test_blank_body_falls_back_to_the_raw_text`,
  `test_specialist_label_variants_are_out_of_scope`,
  `test_truncated_json_with_no_recoverable_target_is_out_of_scope`,
  `test_real_unparseable_opus_replies_keep_their_declared_target`,
  `test_recovery_never_invents_a_target_from_prose`,
  `test_recovery_rejects_an_invalid_recovered_target`,
  `test_recovery_is_not_rubric_specific`). Keeping the signature is not a
  courtesy to the eval script; it is the cheapest way to leave eight pinned
  behaviours untouched.
* `execute_review_analysis` writes one `PromptChangeSuggestion` per proposal,
  all in the **same commit** as the `consumed_at` stamps, all sharing the same
  `feedback_snapshot`, `prompt_files`, `model`, `transcript_available`,
  `input_truncated` and `raw_response`. The commit comment must say why they
  share `raw_response`: the model emitted one reply and each row is a view of
  it, so a reader of any row can reconstruct the whole.

### G3 `prompts/review-bot.md`

* Replace the "Stay within the scope of one target at a time" bullet with the
  multi-target rule: propose one primary target; when the same feedback
  genuinely implicates a second (most often: a hub-prompt change and the
  matching PI-prompt change), add it to `additional_proposals` with **its own**
  quoted current text and replacement, at most two; do not pad the array with
  restatements of the primary.
* Rewrite the **CURRENT PROMPT FILES** bullet to name the two prompt sets
  explicitly and say which paths belong to which, with the
  absence-of-overrides rule stated: `prompts/*.md` are the PI lab bot's set and
  `prompts/roles/scout_hub/*.md` override them for the hub.
* Update the output-contract block to show `additional_proposals`, keeping
  `target` first.
* Add: the two `role.toml` manifests are now supplied; `post_types = []` in the
  hub's is what makes it reply-only, and proposing its removal is a functional
  change, not a wording one (the same class of warning the placeholder section
  already gives).
* Record the one thing the bot still cannot see (finding PS4): the per-phase
  EXPLORE/DECIDE/CONCLUDE guidance is Python in
  `src/agent/thread_guidance.py`, not a prompt file, so a defect in interview
  *behaviour* may have no quotable text — say so plainly and tell the model to
  describe the change in prose and name that module rather than inventing a file.
* Mirror every contract change into `_DEFAULT_REVIEW_PROMPT` in
  `src/services/review_bot.py` —
  `test_target_vocabulary_matches_the_validator_in_both_prompts` checks both.
* Update `src/services/review_bot.py:538-539`'s docstring, which asserts the
  auto-enqueue Task F deletes (see F3b).

**Recorded, not fixed: the bot cannot see the narrative half of the verdict it
critiques** *(audit, minor)*. `_assessment_fields`
(`src/services/review_bot.py:209-241`) is a fixed field list that already omits
`headline`, `key_points` and `elevator_pitch`, and will omit `score_rationale`
too. Requests 1, 2 and 5 all change those fields, so from this change onward the
review bot is asked for suggestions about a contract whose output it is never
shown. Pre-existing, but a fourth narrative field makes it worth a decision:
either add the four to `_assessment_fields` (cheap, and it is what would let
reviewer feedback about a bad headline become a suggestion about the headline
contract) or record that the omission is deliberate. Not folded into this task's
required scope, because it changes the bot's input contract and belongs with its
own before/after evaluation.

### G4 Tests

* `test_two_targets_store_two_rows_sharing_one_snapshot`
* `test_an_invalid_additional_target_is_dropped_and_warned_not_fatal`
* `test_additional_proposals_are_capped_at_two`
* `test_a_duplicate_additional_target_is_dropped`
* `test_a_blank_additional_body_is_dropped`
* `test_additional_proposals_absent_behaves_exactly_as_today`
* `test_prompt_files_metadata_still_carries_only_path_and_sha256_12` (PS7)
* `test_the_rendered_blocks_label_each_file_with_its_role` — assert the pi_lab
  label appears against `prompts/agent-system.md` and the hub label against
  `prompts/roles/scout_hub/agent-system.md`
* `test_both_role_manifests_are_in_the_prompt_file_set`
* `test_prompt_names_both_prompt_sets_and_the_absence_of_overrides_rule`
  (prompt-contract file)
* Re-run all three `tests/fixtures/review_bot_replies/` regression cases
  unchanged — they are the PS6 guard.
* `scripts/eval_review_bot.py`: grade the primary proposal exactly as today and
  add the additional ones to the record (`parsed_additional_targets`), so the
  eval keeps working and starts reporting the new behaviour.
  `tests/unit/test_eval_review_bot_grader.py` gains one case.

---

## Task H — the prompt-suggestions page

**Owns:** `templates/manager/prompt_suggestions.html`, `src/routers/manager.py`,
`tests/integration/test_prompt_suggestions_page.py`.

1. **Generate button.** A form beside the status filter:

   ```html
   <form method="post" action="/reviews/suggestions/generate">
     <button type="submit" ...>Generate suggestions from current reviews</button>
   </form>
   ```

   Literal action, `method="post"` — both load-bearing for `test_reachability`'s
   route credit, and the audit settled that no allowlist entry is needed:
   this template is rendered by src (`src/routers/manager.py:811`) so it is
   reachable, and `_link_credits` matches a literal path exactly
   (`tests/unit/test_reachability.py:247-259`). Rendered only for
   `effective_user.is_staff` and **not** while impersonating, matching the
   route's own gate (a live-looking control that 403s is worse than none).

   **The route must NOT go on the manager router** *(audit, A5)*.
   `tests/integration/test_manager_views.py:55-85` asserts the manager router's
   POST paths are exactly eight and its methods exactly `{"GET","POST"}`, so the
   obvious placement — a POST beside the page it belongs to — is a ninth manager
   write and fails that gate. `/reviews` is correct: every review write already
   lives there, it has its own explicit POST allowlist to extend, and its
   `_STAFF` singleton is the gate this action needs. (While in this file, fix the
   stale `src/routers/manager.py:799` docstring, which still says "four-route
   allowlist" when the real count is eight *(audit, minor)*.)
2. **Eligibility count.** `manager_prompt_suggestions` gains one read — the
   number of assessments with unconsumed `learn` feedback — and passes it as
   `eligible_count`. The button's label/subtext says it: "N assessment(s) have
   review feedback marked *Learn* that no suggestion has consumed yet." When
   it is 0, render the button **disabled** with "Nothing to analyse" rather
   than hiding it; a missing control reads as a missing feature.
   Reuse F2's eligibility query — expose it from
   `src/services/assessment_reviews.py` as
   `count_pending_analysis_candidates(db)` so the page and the button can never
   disagree about what is eligible. (Cross-task: Task F owns that file and adds
   the function; Task H imports it.)
3. **Flash.** Read `generated` / `eligible` from the query string and render one
   line: "Queued N of M. The worker processes them in the background; refresh
   this page in a minute." Say plainly that already-queued assessments were
   skipped — that is what makes a second press look like a no-op rather than a
   bug.
4. **Rewrite the intro paragraph** (finding PS10): suggestions are generated
   **only** when a human presses this button; reviewer feedback marked *Learn*
   marks a row eligible and nothing more; nothing is ever applied to a prompt
   file automatically. Keep the existing "status is staff-set attribution only"
   sentence.
5. **Add the same clarification to the detail page's review card copy** — that
   text lives in `_assessment_detail_body.html`, which **Task B owns**; Task B
   makes the edit, Task H specifies the wording. The pinned option labels
   "Learn" / "Don't learn — log only" do not change.
6. Give `templates/manager/prompt_suggestions.html:8-10`'s status `<select>` an
   `id` and its label a `for` *(audit, G-5)*, the same six-attribute fix Task D
   applies to the list wrappers. Confirmed by the audit that a reviewer 403s
   this page (`tests/integration/test_reviewer_role.py:238`), so F2.3's `_STAFF`
   choice is consistent with the surface.
7. Tests: the button renders for admin and manager and not for a reviewer
   (a reviewer already 403s the page — assert the 403 stays); it is absent while
   impersonating; it is disabled at `eligible_count == 0`; the flash renders;
   pressing it enqueues jobs for exactly the eligible assessments and is a no-op
   on a second press.

---

## Task I — Slack strikethrough

**Owns:** `src/agent/slack_client.py`, `prompts/specialists/budget.md`,
`prompts/profile-synthesis-sparse.md`, `prompts/daily_audit.md`,
`tests/unit/test_markdown_to_mrkdwn.py` (new file).

**Does NOT own, and must not touch, `prompts/agent-system.md`** — removed from
this task after the audit (D8 / §0.1): its tilde line is inside the pi_lab
golden master at 7 locations, and Task I1's transport fix neutralizes it without
the prompt edit.

### I1 Neutralize the approximation tilde (findings SL1, SL3, SL4)

In `markdown_to_mrkdwn`, after the bold and bullet conversions, replace every
ASCII `~` with `≈` **outside** fenced code blocks, inline code spans, Slack
link/mention spans, and bare URLs.

**The protected-span pattern is NARROW, and that is load-bearing.** Revision 1
proposed `(```.*?```|`[^`\n]*`|<[^<>\n]*>)` — protecting *any* `<…>` span. That
is wrong, and it was caught by running it over the corpus rather than by
reading it *(decision D9; audit, G-7 and MINOR)*. This corpus uses `<` and `>`
as inequality operators — `p<0.05`, `<20% RH`, `ρ < 0.4`, `CI >0.20` appear in
6 of the 274 tilde-bearing messages — and a broad matcher pairs them across
arbitrary prose, swallowing any tilde between and leaving the strikethrough bug
intact. Measured:

```
BROAD:  Retrospective cut with <25 per arm at ~$40K or bootstrap CI >0.20 …   # tilde survives -> still strikes through
NARROW: Retrospective cut with <25 per arm at ≈$40K or bootstrap CI >0.20 …   # correct
```

Both score 0/274 on today's stored messages, so **only that adversarial probe
distinguishes them** — which is exactly why it belongs in the test file and not
in a comment.

The narrow pattern, covering the four things that genuinely must be protected:

```python
_PROTECTED = re.compile(
    r"(```.*?```"                              # fenced block
    r"|`[^`\n]*`"                              # inline code span
    r"|<https?://[^>\s|]+(?:\|[^>\n]*)?>"      # Slack link, with or without |label
    r"|<(?:@|#|!)[^>\s|]+(?:\|[^>\n]*)?>"      # Slack user/channel/special mention
    r"|https?://\S+)",                         # BARE url or a markdown link target
    re.DOTALL,
)
```

The bare-URL alternative is the one revision 1 omitted while its own docstring
named the case *(audit, G-7)*: a model-written `http://host/~user`, or
`[label](http://host/~user)`, is not inside `<…>` and would have become
`…/≈user` — a corrupted link.

Implement with `finditer` and explicit slicing, **not** `re.split` parity: the
odd/even split is correct for a single capturing group (verified), but a naive
`parts[1::2]` substitution inverts the fix, and this pattern will gain
alternatives. Note also that `` `[^`\n]*` `` partially consumes an *unpaired*
triple fence (it matches the first two backticks), so a chunk split mid-fence
protects the wrong span — harmless today (no tildes in any fenced content) and
worth one line of comment.

The docstring must carry all of:

* **Why:** Slack mrkdwn strikethrough is a SINGLE tilde (`~strike~`,
  docs.slack.dev/messaging/formatting-message-text, confirmed 2026-09-14) and
  there is no escape for it. The corpus uses `~` to mean "approximately":
  measured 2026-09-14 over `agent_messages`, 274 of 1751 messages contain a
  tilde, 88 contain two or more, and **9 contain a same-line `~…~` pair that
  Slack strikes through**. No content anywhere uses intentional strikethrough.
* **The precedent:** `static/js/markdown.js` disabled GFM strikethrough for
  exactly this corpus and exactly this reason; this is the same fix on the
  transport that was missed.
* **Length safety:** one character for one character, so
  `split_for_slack`'s "`markdown_to_mrkdwn` never lengthens a string" contract
  — which is what lets it split the SOURCE markdown against the 4000-character
  limit — still holds. A backtick-wrapping fix would break it.
* **The guards, and why they are narrow:** inline code is the one place a
  literal `~` is already safe (Slack suppresses formatting inside it), and
  `<url|label>` / `<@U…>` are Slack's own link and mention syntax, where
  rewriting a `~` would corrupt the target. A bare `http://host/~user` is
  covered separately. The guard deliberately does NOT protect an arbitrary
  `<…>` span, because this corpus writes `<` and `>` as inequality operators
  and a broad matcher re-opens the very bug this function is closing — measured,
  see the probe above. The current corpus has 0 tildes inside any protected
  position, so the guards are insurance, not a fix for something observed.
* **The trade, stated:** intentional `~strike~` and `~~strike~~` become
  literal `≈`. Nothing in the prompt set asks for strikethrough, and this is the
  same call `markdown.js` already made.

### I2 Stop teaching the pattern (finding SL2)

Replace the bare `~` in **three** prompt files — not four *(audit, BL-1 /
decision D8)*:

| file:line | current | replacement |
|---|---|---|
| ~~`prompts/agent-system.md:77`~~ | ~~`~$1M–$5M seed`~~ | **NOT EDITED** — inside the pi_lab golden master at 7 locations; see §0.1 |
| `prompts/specialists/budget.md:14` | `(~$1M–$5M)` | `(≈$1M–$5M)` |
| `prompts/profile-synthesis-sparse.md:40` | `(down to ~80)` | `(down to about 80)` |
| `prompts/daily_audit.md:144` | `under ~400 lines` | `under about 400 lines` |

Verified: `prompts/specialists/budget.md`'s line does **not** appear in
`tests/characterization/__snapshots__/test_agent_turn_gm.ambr` (`grep -c`
returns 0 — the golden master drives pi_lab, and specialist personas are
hub-side), and the other two files are not composed into any agent turn. So
these three are safe and `prompts/roles/pi_lab/role.toml` needs no version bump.

`prompts/specialists/budget.md` is embedded verbatim in
`docs/specs/2026-08-07-hub-bot-prompts.md:1212` — Task J runs the sync.
`prompts/profile-synthesis-sparse.md` and `prompts/daily_audit.md` are **not**
embedded in either spec (settled by the audit), so they need no sync. The hub's
own instruction not to write a bare tilde is Task A1.4.

### I3 Tests (new file)

* `test_a_bare_tilde_becomes_an_approximation_sign`
* `test_two_tildes_on_one_line_cannot_render_as_strikethrough` — assert the
  output does not match `~(?=\S)[^~\n]*?(?<=\S)~`
* `test_a_tilde_inside_an_inline_code_span_is_left_alone`
* `test_a_tilde_inside_a_fenced_block_is_left_alone`
* `test_a_tilde_inside_a_slack_link_span_is_left_alone` — the exact shape
  `render_assessment_headline` emits, with a `~` in the URL
* `test_a_tilde_inside_a_bare_url_is_left_alone` and
  `test_a_tilde_inside_a_markdown_link_target_is_left_alone` *(audit, G-7)*
* `test_an_inequality_pair_does_not_shield_the_tilde_between_them` — the
  adversarial probe, verbatim:
  `"Retrospective cut with <25 per arm at ~$40K or bootstrap CI >0.20 is uninformative."`
  must convert to `≈$40K`. This is the ONE case that separates the narrow
  pattern from the broad one; without it, a future "simplification" back to
  `<[^<>\n]*>` passes every other test in this file *(decision D9)*
* `test_the_conversion_never_lengthens_the_string` — property test over the
  bold/bullet/tilde cases together, the `split_for_slack` contract
* `test_bold_and_bullet_conversion_is_unchanged` — regression, so the new pass
  cannot disturb the two existing ones
* `test_real_corpus_shapes_from_the_2026_09_14_measurement` — the four quoted
  production excerpts from the audit doc, asserted struck-through before and
  not after
* Re-run `tests/unit/test_run_marker.py` (it round-trips the announcement
  through `markdown_to_mrkdwn`; the template has no tilde, so it must pass
  unchanged — **verify, do not assume**).

---

## Task J — sync, docs, and the gate

Runs after every other task has merged. Owns `CLAUDE.md`, the two
`docs/specs/2026-08-07-*-bot-prompts.md` files (via the script), and the audit
document.

1. `.venv-test/bin/python scripts/sync_prompt_set_docs.py` — **required**: A1
   edited `prompts/roles/scout_hub/phase4-thread-reply.md`
   (`docs/specs/2026-08-07-hub-bot-prompts.md:309`) and I edited
   `prompts/specialists/budget.md` (`:1212`). Corrected *(audit)*:
   `prompts/agent-system.md` is no longer edited (D8), and
   `prompts/profile-synthesis-sparse.md` / `prompts/daily_audit.md` are not
   embedded in either spec, so the hub doc is the only one that moves. The
   `role.toml` bump does not appear in a spec either — the script only rewrites
   `.md`-sourced blocks (`scripts/sync_prompt_set_docs.py:49`). Then
   `.venv-test/bin/python scripts/sync_prompt_set_docs.py --check` must be
   clean.
2. `CLAUDE.md` updates — each one is a claim the repo tests or an operator
   depends on:
   * A **deploy-order box for `0048`**, in the house format, covering both
     failure directions from §2 and stating that the **agent image must be
     rebuilt** because `src/` is baked and `prompts/` is bind-mounted: prompt
     set 1.4.0 emits `key_points` as five groups and a new `score_rationale`,
     and image-without-prompt is benign (D5's subset acceptance) while
     prompt-without-image writes NULL into the new column forever.
   * In the BlackbirdBot bullet's `#assessments-summary` paragraph: the pitch
     segment is now a **sentence-bounded excerpt** at 600 characters (it was a
     mid-word cut, measured on all 8 rows), and `score_rationale` is a sidecar
     field that is deliberately **not** published — the seventh field D12 does
     not have.
   * In the "Adding New PIs"/review-bot section: prompt suggestions are
     **manual only** as of 2026-09-14 — `submit_feedback`/`edit_feedback` no
     longer enqueue, `POST /reviews/suggestions/generate` is the only trigger,
     `learn` now means "eligible" rather than "queued", and one job can now
     produce more than one `PromptChangeSuggestion` row (one per target).
   * The manager write allowlist count is **unchanged at eight** — the new POST
     is on `/reviews`, which goes to **eight** there.
   * Re-check `tests/unit/test_claude_md_disclosure_sync.py` after editing: the
     `a PI or another lab sees` clause must not name `gating`,
     `recommendation`, `red flags` or `confidence`, and the assessment bullet
     must still contain "verdict inline", "thread_guidance" and "unpublished".
3. Annotate `docs/audits/2026-09-14-assessment-ux-and-prompt-suggestions/README.md`
   with the implementing commit per finding, and add a §10 recording anything
   that turned out differently from the investigation.
4. `./scripts/ci.sh` on the final tree. Report the pass count, the `src/` lint
   number against the 231 ceiling, and the coverage figure against 60.
   **Report the lint number explicitly**: the pre-change baseline measured for
   this plan is **214 of 231**, i.e. 17 findings of headroom, and Task E's
   helper, Task F's new route plus two service functions, and Task A3's new
   derivation all add `src/` code. If the final number is at or near 231, pay
   debt down in the files this change touched — the ceiling does not move
   (`scripts/ci.sh` says so in its own comment, and raising it is how the
   original 16 findings got in).
5. Record the list page's rendered byte size at 500 rows, before and after
   *(audit, G-6)*, alongside Task C4's 50-row ceiling test. The 50-row test as
   specified is a ratchet for FUTURE changes; it cannot tell anyone whether THIS
   change made the "All runs" page too heavy, because its ceiling is measured on
   the post-change page. Two numbers in the completion report answer the
   question finding Q6 actually asked.

---

## 4. Test matrix

**New test files (3)** — count corrected *(audit, FA-7)*:
`tests/unit/test_assessment_strength_risk_derivation.py`,
`tests/unit/test_markdown_to_mrkdwn.py`,
`tests/integration/test_assessment_list_chrome.py`. Everything else is new
cases folded into existing files.

**Existing test files edited (17), verified-only (5)** — enumerated and counted
*(audit, FA-7)*.

Edited: `test_rubric_prompt_sync.py`,
`test_assessment_narrative_fields.py`, `test_assessment_headline_render.py`,
`test_assessment_detail_page.py`, `test_assessment_queue_controls.py`,
`test_opportunity_assessment_persistence.py`, `test_directory_assessments.py`,
`test_reviews_router.py`, `test_review_pipeline_races.py`,
`test_review_job_end_to_end.py`, `test_review_bot.py`,
`test_review_bot_edges.py`, `test_review_bot_prompt_contract.py`,
`test_prompt_suggestions_page.py`, `test_eval_review_bot_grader.py`, and — added
after the audit — **`test_migration_checks.py`** and
**`test_harness_smoke.py`** (both BL-2).

Verified only, expected to pass unchanged: `test_assessments_summary_post.py`
(settled: no byte-exact body), `test_review_dimension_scores.py` (settled:
docstring only), `test_manager_views.py`, `test_reviewer_role.py`,
`test_reachability.py` (settled: no allowlist entry needed).

Non-test files added to the plan after the audit: `scripts/migrate/preflight.py`
(Task A2, BL-2) and `src/routers/admin.py` (Task A2, one stale comment, G-9).

**Tests that must pass UNCHANGED** — if any of these needs editing, stop and
re-read the constraint it encodes:
`tests/characterization/__snapshots__/test_agent_turn_gm.ambr`,
`test_doc_prompt_sync.py` (after the sync script runs),
`test_panel_state.py`, `test_json_none_as_null.py`,
`test_claude_md_disclosure_sync.py`, `test_origin_guard.py`,
`test_enrichment_isolation.py`,
`test_review_bot.py::test_the_bot_module_imports_no_transport`,
`test_review_bot.py::test_the_bot_module_stays_free_of_web_tier_rubric_modules`,
`test_manager_views.py::test_manager_router_mutations_are_an_explicit_allowlist`,
`test_reviewer_role.py::test_reviewer_manager_surface_is_exactly_the_read_slice`.

---

## 5. Risk register

| risk | why it matters | mitigation in this plan |
|---|---|---|
| Prompt 1.4.0 deployed without an agent-image rebuild | five-group `key_points` + `score_rationale` emitted, old parser discards them; NULL forever in the new column | D5 makes the subset case benign; the CLAUDE.md box (J2) names the hazardous direction explicitly; the deploy runbook rebuilds `agent` before starting anything |
| Migration skipped | `_persist_assessment` names `score_rationale`; the write is best-effort, so **every verdict of a running run is lost silently** | §2 + §8 make migrate-before-serve a numbered step, and §8 verifies `alembic current` |
| Derived strengths/risks read as the model's own claim | the repo has paid for this conflation four times | mandatory third bucket, mandatory footnote naming the provenance, thresholds from the row's own revision, unit tests for all twelve rules |
| List page bloat | three new blocks × up to 500 cards | measured page-size ceiling test (C4); `line-clamp` instead of a server-side clip; every new block collapsed by default |
| Losing the malformed-reply recovery | 3 of 12 live Opus replies were invalid JSON | D6 keeps `target` as the first key; the three real fixtures re-run unchanged |
| Manual-only breaks a race guarantee | 4 race tests assert a requeue | PS9: the guarantee is `consumed_at_predicates` (untouched); only the scheduler changes, and F3 rewrites the assertions to say so |
| A review edited from the list wipes its dimension scores | `edit_feedback` replaces the set | edit is not offered on the list (Q4), with the reason in a comment |
| `/admin/` leaking onto the manager list page | pinned test, and a 403-on-click control | surface is a bare token; the path is built in `reviews.py` (Q2) |
| Publishing more to `#assessments-summary` | D12 is a recorded policy with sign-off | D3 keeps score rationale app-only; A4 fixes the clip rather than raising the cap; J2 records both |
| Tilde rewrite corrupting a link or code span | a permalink with `~` in the path | narrow protected-span regex (links, mentions, bare URLs, code) + four dedicated tests |
| A broad tilde guard silently re-opening the bug *(audit, D9)* | this corpus writes `<`/`>` as inequality operators; a `<…>` matcher pairs them across prose and swallows the tilde. Scores 0/274 on stored messages, so no ordinary test catches it | the narrow pattern, plus the adversarial inequality probe as a named test |
| A golden-master mismatch forcing a forbidden `--snapshot-update` *(audit, BL-1)* | `prompts/agent-system.md:77` is in the pi_lab `.ambr` 7 times | D8 drops that one prompt edit; the transport fix covers the behaviour |
| A new revision going red in two test files *(audit, BL-2)* | `DEFAULT_TARGET`, `REVISION_ORDER`, `SUPPORTED_START_REVISIONS`, `PLANNED_OBJECTS`, and two literal head assertions | Task A2 step 4's six-row table, with the forcing assertion named per row |
| Running out of lint headroom | 214 of 231; this change adds `src/` code in four files | Task J reports the number; debt is paid down in the touched files, never by raising the ceiling |
| A test that passes for the wrong reason *(audit, PS8)* | two enqueue tests become vacuous rather than failing | F3 names both and requires them rewritten, not left green |

---

## 6. Considered and deliberately deferred

* **`thread_guidance.py` as a review-bot input** (finding PS4). The
  EXPLORE/DECIDE/CONCLUDE strings are the guidance actually injected into every
  phase-4 turn and the bot cannot see them, so a reviewer complaint about
  interview *behaviour* has no quotable source. Deferred because those strings
  are pinned byte-for-byte by the pi_lab golden master and a
  suggestion-to-Python-edit workflow is a larger change than this request. G3
  makes the bot **say** it cannot see them, which is the honest interim.
* **Dropping the standalone Red flags card** now that the risk column repeats
  it. Kept: it is what `test_a_non_empty_red_flag_list_is_never_collapsed` was
  written about, and duplication on one uncollapsed page is the safe direction.
* **Backfilling `score_rationale`, or a friendlier `headline`, for the 20
  existing rows.** Refused, consistent with every narrative column before it: a
  generated one would be indistinguishable from one the hub wrote.
* **Raising `PITCH_DISPLAY_CHARS`.** Refused (D3): it publishes more sidecar
  prose to a channel whose content policy required sign-off.
* **A per-assessment generate button** (F4). One hidden input on an already-built
  route; left as an explicit optional follow-up because the request asked for
  "a button" and the global one is it.
* **Vendoring Tailwind / an SRI-pinned build.** Pre-existing (SEC-18), untouched.
* **Giving the review bot the narrative half of the verdict** *(audit)*.
  `_assessment_fields` (`src/services/review_bot.py:209-241`) omits `headline`,
  `key_points` and `elevator_pitch` today and will omit `score_rationale`, so
  from this change onward the bot is asked to propose fixes to a contract whose
  output it never sees. Deferred with the reason recorded in Task G3: it changes
  the bot's input contract and deserves its own before/after evaluation rather
  than riding along on a UI change.
* **A fourth pi_lab golden-master regeneration**, to let
  `prompts/agent-system.md` drop its own approximation tilde. Refused (D8): the
  transport fix makes it cosmetic, and CLAUDE.md reserves that path for
  operator-directed, hunk-audited changes.

---

## 7. Parallelism and sequencing

```
wave 1 (parallel, disjoint files): A1  A2  A3  A4  B  C  D  E  F  G  H  I
wave 2 (integration):              merge; run ./scripts/ci.sh once; repair
wave 3:                            J (doc sync, CLAUDE.md, audit annotation, final gate)
wave 4:                            adversarial completeness audit against this plan
```

**Wave 1 is not individually gate-green, and revision 1 implied it was**
*(audit, CT-4)*. Each package compiles and its own new tests pass, but the
integrated suite is only expected green after wave 2 — that is the whole premise
of implement-all-then-verify-once. The one test that made this explicit has been
moved to the task that owns its dependency.

FOUR cross-task couplings, which must be stated verbatim in the implementers'
briefs because they cross file ownership:

* **A3 changes `normalize_key_points`; A2 owns the test that pins it.** Both
  briefs carry the new expected behaviour (D5) verbatim, and A2 step 7 now names
  `test_normalize_rejects_wrong_shapes` explicitly *(CT-3)*.
* **F adds `count_pending_analysis_candidates`; H imports it.** H's brief
  carries the signature.
* **H specifies detail-page review copy; B makes the edit** (B3b). B's brief
  carries the wording.
* **F must edit one test inside G's file** *(audit, CT-2)*:
  `tests/unit/test_review_bot_edges.py::test_deleting_the_reviewer_deletes_their_pending_job_but_keeps_the_review`
  depends on the enqueue F deletes. Also F supplies the new wording for
  `src/services/review_bot.py:538-539`, which G edits (F3b).

---

## 8. Deploy runbook (operator, after the gate is green)

`0048` is additive but mapped, and `_persist_assessment` writes it, so this is
migrate-before-serve. Prompt set 1.4.0 is bind-mounted and the parser is baked,
so the agent image must be rebuilt in the same deploy.

```bash
DC="docker compose -f docker-compose.prod.yml"

# 0. Confirm which stack you are on. copi-blackbird = this repo.
docker inspect copi-blackbird-agent-1 --format '{{index .Config.Labels "com.docker.compose.project"}}'

# 0b. Confirm the schema you are starting from.
$DC exec -T postgres psql -U copi -d copi -t -A -c \
  "SELECT version_num FROM alembic_version;"      # expect 0047

# 1. Stop the supervisor gracefully (420s; verify by the log line, not the exit code)
docker stop -t 420 copi-blackbird-agent-1
docker logs copi-blackbird-agent-1 2>&1 | tail -40 | grep 'Simulation stopping'

# 1b. Save the log BEFORE anything removes the container (CLAUDE.md's step 2).
docker logs copi-blackbird-agent-1 > logs/blackbird_run_$(date +%s).log 2>&1
ls -t logs/blackbird_run_*.log | tail -n +11 | xargs -r rm -f

# 2. BUILD ONLY — do not start anything
$DC build blackbird-app worker
$DC --profile agent build agent

# 3. Migrate, from a one-off container off the image just built
$DC run --rm blackbird-app alembic upgrade head
$DC run --rm blackbird-app alembic current        # must equal `alembic heads` (0048)

# 4. Start the web tier on the migrated schema
$DC up -d blackbird-app worker

# 5. Bring the supervisor back — it returns IDLE and starts nothing
$DC up -d agent
```

Then, as separate explicit operator actions:

* Start a run from `/admin/simulation` if and when wanted. The startup banner's
  `Screening rubric: version 3.4.0` is unchanged (no rubric edit); the
  run-start announcement's **hub prompt version must read 1.4.0**, which is the
  tell that the prompt and the image shipped together. The **PI prompt version
  stays 1.0.0** — correct, because D8 leaves the pi_lab prompt set untouched; a
  changed PI hash with an unchanged PI version would be the unrecorded edit
  CLAUDE.md warns about.
* On `/manager/prompt-suggestions`, press **Generate suggestions from current
  reviews** once. Nothing is queued automatically any more, so any `learn`
  feedback left over from before this deploy is waiting there.

Nothing in this change touches `--fresh`, the assessment archive, or
`simulation_runs`; the standing archive rules are unaffected.

---

## 9. What was and was not verified while writing this plan

Stated so the next reader can tell a measurement from a reading.

**Measured against the live `copi` database** (stack `copi-blackbird`, stamped
`0047`): every row count and length distribution in the spec — 20 assessments,
`company_or_project` 81–188 chars with 12 over 120, `headline` 182–232 with 3
over 200, 8 pitches at 1173–1406 with all 8 over 600 and four sampled
truncations landing mid-word, 12 key-point bullets with 4 over 160 chars,
274 of 1751 messages containing a tilde and 88 containing two or more.

**Measured by executing the proposed code** against the exported corpus: the
tilde converter, both candidate protected-span patterns, 0/274 mis-protections
for each, and the inequality probe that separates them (D9 / Task I1).

**Verified by reading the tree at `9df21e1`**: every file path, symbol,
constant, route set and test assertion cited anywhere in this plan or the spec,
including the 7 golden-master hits, the six `0048` head pins, the five race
tests, and the seven `_parse_model_output` pins.

**NOT run, and why:**

* `./scripts/ci.sh` — no code has been written yet, so a run now measures the
  pre-change baseline only. The `src/` lint number WAS measured directly
  (`ruff check src --output-format=concise --quiet | grep -c .` → **214**,
  ceiling 231). The suite's pre-change pass count is taken from this repo's own
  record — the 2026-09-11 readability plan's closing note ("3544 passed") — not
  from a run today, and is therefore the one number in this plan that is
  second-hand. Task J's gate run is what establishes it firsthand.
* Branch-coverage impact of the new code against the 60% floor — unmeasured
  until Task J. Four new `src/` code paths (a derivation, a helper, a route, two
  service functions) with unit tests each should move it up, not down, but that
  is a prediction.
* Anything in a browser. No Playwright server is available in this session
  (the MCP server failed to connect), so every claim about the rendered result
  of a template change in this plan is a claim about the HTML, not about how it
  looks. The two list surfaces and the detail page should be looked at once
  after wave 2.

---

## 10. Shared interface contracts — pinned before fan-out

Every package codes against these exactly. Where an implementer finds an
ambiguity, the simplest reading of THIS section wins over any prose above it,
and the assumption is reported.

**C1 — `KEY_POINT_GROUPS`** (`src/services/assessment_detail.py`, Task A3).
Verbatim, including order and label strings:

```python
KEY_POINT_GROUPS: tuple[tuple[str, str], ...] = (
    ("significance", "Significance"),
    ("innovation", "Innovation"),
    ("clinical_actionability", "Clinical actionability"),
    ("key_questions", "Key questions / experiments"),
    ("commercial_potential", "Commercial potential"),
)
```

Already a Jinja global as `key_point_groups` on both routers, so no router
change propagates it.

**C2 — `normalize_key_points(value)`** returns `value` unchanged for: a flat
`list[str]`; or a NON-EMPTY `dict` whose key set is a SUBSET of the five C1 keys
and whose every value is a `list[str]`. Everything else returns `None`,
including `{}` and any dict carrying an unknown key.

**C3 — `derive_strengths_and_risks`** (Task A3), consumed by Task B:

```python
def derive_strengths_and_risks(
    assessment, *, dimensions, consults, revision
) -> dict[str, Any]
```

* `dimensions` is the list `build_assessment_detail` already builds (dicts with
  `key, title, weight, weight_note, score, pct`).
* `consults` is the list `_load_consults` already returns (dicts carrying at
  least `domain`, `verdict_signal`, `reply_truncated`).
* `revision` is a `RubricRevisionView | None`.

Returns exactly:

```python
{
    "strengths":     [{"source": str, "label": str, "detail": str}, ...],
    "risks":         [...],
    "unestablished": [...],
    "scale_known":   bool,
}
```

`source` is one of `"dimension"`, `"gating"`, `"red_flag"`, `"consult"`.
`label` is the short bolded name (the dimension's `title`, the gating key with
underscores replaced by spaces, `"Red flag"`, or the consult's `domain`).
`detail` is the qualifying phrase (`"scored 5 of 5"`, `"met"`, the flag's own
text, `"blocking"`, `"never asked"`, `"not scored — counted as zero in the
weighted score"`, `"reply cut off — no signal"`, `"signal not recognised"`).
Exposed by `build_assessment_detail` under the single new context key
**`verdict_signals`**.

**C4 — per-row list data** (`src/services/directory.py`, Task E), consumed by
Task C. Attached as ordinary instance attributes on each row of `assessments`:

* `row.dimension_rows: list[dict]` — keys `key, title, score, weight,
  weight_note, pct`, the SAME shape as C3's `dimensions`, so the two surfaces
  cannot disagree.
* `row.revision_view: RubricRevisionView | None`.

No new top-level context key.

**C5 — `list_surface`** (Task D emits, Task C consumes). `{% set list_surface =
'admin-list' %}` in `templates/admin/assessments.html` and `'manager-list'` in
`templates/manager/assessments.html`, before the `{% include %}`. A bare token,
never a path.

**C6 — quick-scoring hidden inputs** (Task C emits, Task F consumes). Both forms
carry exactly these four:

```html
<input type="hidden" name="surface" value="{{ list_surface }}">
<input type="hidden" name="run_id"  value="{{ 'all' if show_all_runs else selected_run_id }}">
<input type="hidden" name="sort"    value="{{ sort }}">
<input type="hidden" name="lab"     value="{{ lab_filter or '' }}">
```

**C7 — `_assessments_redirect`** (Task F):

```python
def _assessments_redirect(
    surface: str, current_user: User, assessment_id: uuid.UUID,
    *, run_id: str | None = None, sort: str | None = None, lab: str | None = None,
) -> RedirectResponse
```

`admin-list` → `/admin/assessments?<qs>#a-<id>` for an admin, `manager-list` and
an unrecognised surface → `/manager/assessments?<qs>#a-<id>`; `admin` /
`manager` unchanged. `<qs>` is `urlencode` over only the values that survive
validation (`sort` in `ASSESSMENT_SORTS`; `run_id` == `"all"` or a parsable
UUID; `lab` non-empty), omitted entirely when nothing survives.

**C8 — `enqueue_pending_analyses`** (Task F):

```python
async def enqueue_pending_analyses(
    db: AsyncSession, *, requested_by: User, assessment_id: uuid.UUID | None = None
) -> tuple[int, int]   # (enqueued, eligible)
```

**C9 — `count_pending_analysis_candidates`** (Task F provides, Task H imports):

```python
async def count_pending_analysis_candidates(db: AsyncSession) -> int
```

Counts DISTINCT `assessment_id` over `assessment_reviews` where
`feedback_mode = 'learn'` and `consumed_at IS NULL`.

**C10 — the new route** (Task F provides, Task H links):
`POST /reviews/suggestions/generate`, one optional form field
`assessment_id: str = Form("")`, redirecting to
`/manager/prompt-suggestions?generated=<n>&eligible=<m>`.

**C11 — the new column** (Task A2): `OpportunityAssessment.score_rationale`,
`Mapped[str | None]`, `Text`, nullable. Templates read `a.score_rationale`.
Migration id `0048`, down-revision `0047`.

**C12 — sidecar keys** (Task A1): `score_rationale` is a string;
`key_points` is an object with the five C1 keys.

**C13 — new CSS class names.** Task C emits, Task D's stylesheet targets:
`assessment-card-pitch`, `assessment-card-points` (existing name, kept),
`assessment-card-score-rationale`, `assessment-card-scores`,
`assessment-card-quickscore`; each card's id is `a-<assessment uuid>`.
Task B emits: `assessment-signals`, `assessment-signals-strengths`,
`assessment-signals-risks`, `assessment-signals-unestablished`,
`assessment-brief-score-rationale`; the section's id is `signals`.

# Reviewer-facing assessment UI, rubric-dimension human scoring, and review permissions

**Date:** 2026-09-09
**Status:** design, approved in brainstorming; implementation plan to follow
**Supersedes nothing.** Extends `docs/plans/2026-09-04-assessment-ui-and-proposal-limit-design.md`
(which redefined the human-review score as proposal merit) and
`docs/plans/2026-08-28-human-review-feedback-adversarial-analysis.md` (which
established the review tables and their FK rules).

---

## 0. What triggered this

Three requests, in the operator's words:

1. *"the reviewer and manager roles dont seem to have permissions to add human
   reviews to assessments ... only managers should be able to assign reviewers"*
2. *"add additional instructions to the human review section asking the human to
   review based on the topics in the rubric and also score the assessment 1-5
   based on the rubric sections, there should be additional drop down score boxes
   for each rubric area"*
3. *"make the title of assessments more reviewer-friendly ... a bullet point
   summary ... a brief elevator pitch ... each paragraph in the rationale section
   should have a bolded sentence which summarizes the proceeding paragraph(s)"*

### 0.1 Request 1 is not a permissions defect — measured

The permission code already grants what was asked. Evidence gathered
2026-09-09 against the running deployment:

| Fact | Evidence |
|---|---|
| Feedback + approve/disapprove admit admin ∨ manager ∨ reviewer | `src/routers/reviews.py:117`, `:194` (`_REVIEW` = `get_review_user`, `src/dependencies.py:234`) |
| Assign/unassign admit admin ∨ manager only | `src/routers/reviews.py:218`, `:239` (`_STAFF` = `get_staff_user`, `src/dependencies.py:214`) |
| The deployed image carries the review card | `copi-blackbird-app-1:/app/.build_info.json` → commit `56f480e`, branch `feat/assessment-ui-review-score-login-proposal-limit`; `grep -c 'Human review' /app/templates/admin/_assessment_detail_body.html` → 2 |
| Every write form is hidden under impersonation | `templates/admin/_assessment_detail_body.html:436` — `{% set can_write = not impersonation_banner %}` |
| Every `/manager/assessments/{id}` view in the last 10 days happened while impersonating | app log 2026-09-04: `POST /admin/impersonate` 16:56:58 → detail GETs 17:03/17:04 → `impersonate/stop` 17:06:50; second cycle 17:34:22 → 17:59:15 |
| No review write has ever been attempted | zero `POST /reviews/...` in 240h of logs; `assessment_reviews` = 0 rows, `assessment_review_events` = 0 rows |
| No reviewer account exists to test with | `SELECT user_role, count(*) FROM users GROUP BY 1` → admin 3, manager 2, pi 74; **reviewer 0** |

So the observation is real and the cause is `can_write`, which suppresses the
whole write half of the card silently. The design treats this as a UX defect
(an invisible, unexplained absence) rather than as an authorization change.

### 0.2 Decisions taken during brainstorming

Numbered `N*` deliberately. This repo already has a `D*` design-decision
namespace (D10, D11, D12, D14, D16 are cited in `CLAUDE.md` and below), and
reusing it here would make two of the citations in this document ambiguous.

| # | Decision | Rationale / who decided |
|---|---|---|
| N1 | Keep review writes blocked under impersonation; add a visible explanatory notice | Operator. A review must be attributable to the real human; the silent blank is the actual bug. |
| N2 | "Only managers may assign" means **not reviewers**; admins keep the ability | Operator. Matches today's `is_staff` gate — no code change, a new test. |
| N3 | Six **optional** per-dimension scores, existing overall merit score stays required | Operator. Reviewers may skip a dimension they cannot judge; the overall stays the comparable number `review_bot` already reads. |
| N4 | New `headline` column beside `company_or_project`, not a repurpose | Operator. `company_or_project` is the only project field the public Slack headline renders (design D12) and is clipped at 120 chars. |
| N5 | **No backfill** of the 12 existing rows | Operator. See adversarial finding A3 for the cost. |
| N6 | Rationale paragraphs open with a short run-in **label** AND a bolded **summary sentence** | Operator. Label capped at 2–4 words so the summary dominates (A5). |
| N7 | List page becomes a card list on **both** `/admin/assessments` and `/manager/assessments` | Operator. One shared body, one layout, one test set. |
| N8 | Detail page: reviewer brief on top, dense evidence collapsed | Operator. Panel banner and non-empty red flags stay uncollapsed (A8). |
| N9 | Card face keeps gating icons, panel badge, red-flag count, rubric version + date | Operator. |
| N10 | Sticky jump-nav on the detail page | Operator. |
| N11 | Show BlackbirdBot's per-dimension score beside the reviewer's dropdown | Operator, with the anchoring cost stated (A9). |
| N12 | Elevator pitch is posted to `#assessments-summary`, and may carry the PI's unpublished disclosures | Operator, on the asserted premise below. |

**N12 rests on one operator-asserted premise, recorded here because no code
enforces it:** *"PIs can actually not join the slack workspace."* The premise
is load-bearing — `#assessments-summary` is human-joinable and
workspace-visible (design D11), and only *bots* are structurally excluded from
it (`src/agent/channels.py:23-30`). The repo still renders a workspace join
link to every PI on their own dashboard (`SLACK_INVITE_URL`,
`src/routers/agent_page.py:37`; used at `templates/agent/dashboard.html:71`
and `:296`), so if that invite is ever re-enabled the premise breaks silently.
See A6.

---

## 1. Permissions and the impersonation notice

**No change to any dependency, any router gate, or any role predicate.**

### 1.1 Template change

`templates/admin/_assessment_detail_body.html`, inside the Human-review card,
where `can_write` is false:

> Review actions are hidden while you are impersonating another account. A
> review is recorded against the person who wrote it, so stop impersonating
> to review as yourself.

Rendered once, above the status row, in the same muted style as the card's
other explanatory copy. It replaces the current silent absence; the forms
themselves stay hidden, exactly as today.

### 1.2 Tests added

- A `reviewer`-role user can `POST /reviews/assessments/{id}/feedback` (200/302)
  and `POST /reviews/assessments/{id}/status`.
- A `reviewer`-role user is 403 on `/assign` and `/unassign`.
- A `manager` can do all four.
- An impersonating admin sees the notice and no write form.

### 1.3 Operational follow-up (not code)

Production has no reviewer account. Before this is called done, provision one
(`/admin/users/{id}` → Account Type, or
`docker compose -f docker-compose.prod.yml exec blackbird-app python -m src.cli role:set --orcid ... --role reviewer`)
and walk the path signed in as that account, not by impersonating it.

---

## 2. Rubric-dimension human scoring

### 2.1 Schema (`assessment_reviews`, migration `0043`)

| column | type | nullability | meaning |
|---|---|---|---|
| `dimension_scores` | `JSONB(none_as_null=True)` | NULL | sparse `{dimension_key: int}`, only the dimensions the reviewer actually scored |
| `rubric_version` | `String(20)` | NULL | the `[meta].version` live when the review was written |
| `rubric_content_hash` | `String(20)` | NULL | first 12 hex of the document sha256, same as the assessment's own stamp |

`none_as_null=True` is mandatory, not stylistic: without it Python `None`
persists as the JSONB scalar `null`, a second physical encoding of "absent"
that `WHERE col IS NULL` does not match. That bug has already shipped twice on
this schema (`missing_domains`, repaired by `0031`; `AssessmentDrop.raw_verdict`,
repaired by `0036`). `tests/unit/test_json_none_as_null.py` walks
`Base.metadata` and is the standing alarm.

**Why the review carries its own rubric stamp.** A per-dimension human score is
uninterpretable without knowing which dimensions existed when it was given.
Reading it back against *today's* document is the same class of error as
`panel_state` re-deriving `panel_is_owed` at render time — a write-time fact
answered at read time, which silently re-labels every older row each time the
document moves. `specialist_consults` got exactly this pair of columns in
`0038` for exactly this reason.

### 2.2 Write path

`src/services/assessment_reviews.py`:

- `submit_feedback(..., dimension_scores: dict[str, int] | None = None)` and
  `edit_feedback(...)` gain the parameter.
- `_validate` gains: every key must be a key of `load_rubric().dimensions`;
  every value must be an int within `[scale_min, scale_max]`. A violation
  raises `ValueError`, which the router already converts to a 400.
- Both writers stamp `rubric_version` / `rubric_content_hash` from
  `blackbird_rubric.RUBRIC_VERSION` / `RUBRIC_CONTENT_HASH` — the module-level
  constants, so a running process cannot stamp a document it did not load.
- An empty dict normalizes to `None` ("scored nothing"), so the column has one
  encoding of absence.

`src/routers/reviews.py`: `submit_review_feedback` and `edit_review_feedback`
parse `dim_<key>` form fields. An empty string means "not scored" and is
dropped, never coerced to 0.

### 2.3 Read path and form

`src/services/assessment_detail.py` adds one context key,
`review_rubric` — the live `Rubric`'s dimensions (key, title, weight, anchors)
plus the version string. The assessment-detail routes on **both** surfaces
splat `**detail` (`src/routers/admin.py:875`, `src/routers/manager.py:381`),
so a new key from the detail service reaches both. (The *list* route does not —
see A2.)

The Human-review card gains, above the existing overall-merit select:

- An instruction paragraph: score the **proposal** against Blackbird's rubric,
  naming the live version, and stating that the six dimensions below are the
  rubric's own and that a dimension you cannot judge should be left blank.
- One row per dimension: title, weight, a disclosure carrying the dimension's
  `anchors` text verbatim from the document, BlackbirdBot's own score for that
  dimension (muted, labelled *"BlackbirdBot scored N"*, or *"not scored"*), and
  an optional `1–5` select named `dim_<key>`.

The edit form mirrors it, pre-selected from the stored row. Existing feedback
rows render their stored dimension scores, resolved through
`src/services/rubric_revisions.py` against the review's own stamp — the same
machinery the assessment's dimension bars already use. A stamp matching no
registry entry renders the keys as stored with no titles and no weights, never
silently remapped onto today's dimensions.

### 2.4 `review_bot` consumer

`src/services/review_bot.py`:

- `feedback_snapshot` entries gain `"dimension_scores"` (`src/services/review_bot.py:444`), so a
  suggestion's provenance records what the human actually scored.
- **The content-conditional `consumed_at` UPDATE (`:511-512`) must compare it
  too.** Today the WHERE names `score` and `comment` only. See A1 — this is a
  correctness change, not a nicety.
- The system prompt's description of the review payload gains one sentence
  naming the per-dimension scores and their meaning.

---

## 3. Narrative fields on the assessment

### 3.1 Schema (`opportunity_assessments`, migration `0043`)

| column | type | nullability | content |
|---|---|---|---|
| `headline` | `Text` | NULL | one sentence naming the mechanism or tool, the target disease or population, and what makes it fundable |
| `key_points` | `JSONB(none_as_null=True)` | NULL | 3–5 short strings |
| `elevator_pitch` | `Text` | NULL | 3–5 plain-language sentences |

`company_or_project` keeps its current meaning and stays the short label.
All three are NULL on every pre-`0043` row and are **deliberately never
backfilled** (N5) — the standing rule on this table is stamp-and-keep, and a
generated headline would be indistinguishable from one the hub actually wrote.

### 3.2 Sidecar contract

`prompts/roles/scout_hub/phase4-thread-reply.md`:

Three new keys in the `<assessment_json>` skeleton, placed immediately after
`company_or_project`:

```
"headline": "",
"key_points": [],
"elevator_pitch": "",
```

Three new numbered contract items, written to the same standard as the
existing ten:

1. **Headline.** One sentence, ≤ 200 characters. It must name the mechanism or
   tool, the target disease or population, and the reason this is fundable.
   Not the project label — `company_or_project` already carries that.
2. **Key points.** Three to five bullets, each ≤ 160 characters, each a
   complete claim rather than a topic. Together they must let a reviewer who
   reads nothing else state what the idea is, what is known, and what the
   decisive risk is.
3. **Elevator pitch.** Three to five sentences of plain language for a
   scientifically literate non-specialist. It carries the same confidentiality
   licence as the rest of the sidecar — see §6 for where it is published.

Two existing fields get tightened formatting rules:

- **`rationale`.** Every paragraph opens with a short run-in label of two to
  four words in bold, then a bolded one-sentence summary of the paragraph:
  `**Scientific panel.** **The circadian confound is the biggest threat to this
  biomarker.** Ordering (discovery-set analytics, then …)`. Reading only the
  bold text must give the whole argument.
- **`recommended_next_experiment`.** Must open with a bolded one-line ask
  naming cost and duration:
  `**$100–175K · 4–6 months — pre-registered analytical-validity package on the
  existing 124-patient cohort.**` This is what gives the detail page's "The
  ask" line without the page parsing cost and duration out of prose, which
  would be fragile in exactly the way that produces a confidently wrong number.

`prompts/roles/scout_hub/role.toml`: `version = "1.0.0"` → `"1.1.0"`. Required,
not cosmetic — `prompt_set_stamp` (`src/agent/roles.py`) records the version
plus a content hash in every run-start announcement, and a hash change without
a version bump is by definition an unrecorded edit.

**No rubric version bump.** No weight, threshold, dimension, gating key, band
semantic or red flag moves. `tests/unit/test_rubric_prompt_sync.py` pins only
the skeleton's `scores` and `gating` key sets (`:84`, `:97`), both untouched.

`scripts/sync_prompt_set_docs.py` must run in the same commit —
`docs/specs/2026-08-07-hub-bot-prompts.md` embeds the prompt verbatim and
`tests/unit/test_doc_prompt_sync.py` asserts it.

### 3.3 Persistence

`SimulationEngine._persist_assessment` (`src/agent/simulation.py:4519` region)
adds three kwargs, degrading exactly like their existing siblings:

```python
headline=_str_or_none(verdict.get("headline")),
key_points=(kp if isinstance(kp := verdict.get("key_points"), list) else None),
elevator_pitch=_str_or_none(verdict.get("elevator_pitch")),
```

A wrong type degrades to `None`; `raw_verdict` keeps the original either way. A
malformed narrative field must never cost the verdict — the row is the archive.

Shape validation is a WARNING, never a drop: log once naming the assessment and
the field when `headline` exceeds its bound or `key_points` is outside 3–5
entries. The value is stored as emitted regardless.

---

## 4. List page: card list on both surfaces

`templates/admin/_assessments_body.html`'s `<table>` becomes a stacked card
list. Everything above the table is unchanged: run/lab/sort controls, the five
recommendation tiles, the dropped-verdict banner, the unvetted-panel banner,
the dimension-distribution disclosure. Sorting is already a dropdown
(`sort_options`), not column headers, so it survives the layout change intact.

Per card:

```
┌───────────────────────────────────────────────────────────────┐
│ <headline, or company_or_project when NULL>          3.40      │
│ <company_or_project, muted — only when headline present>       │
│ Lab: <pi_link>  ·  screened <date>  ·  rubric 3.4.0   ADVANCE  │
│                                                    [ advance ] │
│ • key point 1                                                  │
│ • key point 2                                                  │
│ • key point 3                                                  │
│                                                                │
│ ✅ ❌ ❓ gating   ⚠ 2 red flags   ⚑ panel unrecorded            │
│ Assigned: —   Reviewed by: —   ● Unreviewed        detail →    │
└───────────────────────────────────────────────────────────────┘
```

Degradation, which is the common case today: a row with `headline IS NULL`
shows `company_or_project` as the heading and no subtitle; a row with
`key_points IS NULL` renders no bullet block at all, not an empty-state.

The `assessment_link(a)` / `pi_link(a)` macro contract in the two wrappers is
unchanged, and the body stays free of absolute `/admin/` and `/manager/` paths
— `tests/unit/test_reachability.py`'s `_link_credits` only accepts a Jinja
expression in a path-param slot.

### 4.1 The scan-only pin

`test_admin_assessments_page_renders_no_inline_detail_rows`
(`tests/integration/test_opportunity_assessment_persistence.py:1460`) encodes a
2026-08-27 decision that the triage page is scan-only. Putting `key_points` on
the card face **narrows that policy deliberately**: `key_points` is a
purpose-built summary field, not the dense evidence the pin was written about.
The three fields it names — `rationale`, `red_flags` text, `derisking_milestones`
— stay off the list page, and the flag *count* remains their only trace. The
test is kept, its docstring corrected to say so, and a positive assertion added
that `key_points` **does** render. It is not deleted.

---

## 5. Detail page: brief first, evidence collapsed

New top card, above everything else:

- `headline` as the page heading (falling back to `company_or_project`), with
  `company_or_project` beneath it as a muted subtitle when both exist.
- Lab · recommendation chip · score + band — the existing header content,
  folded in rather than duplicated.
- **In one minute** — `elevator_pitch`.
- **Key points** — `key_points`.
- **The ask** — the existing "Recommended next experiment" card, moved up to
  sit directly under the brief.

Below the brief, as collapsed `<details>`:

- Full rationale — summary labelled with its paragraph count, so the reader can
  see how much is behind the click.
- Gating criteria.
- Dimension scores.
- Interview timeline.

**Two things stay uncollapsed, deliberately.** The specialist-panel status
banner, and any non-empty red-flag list. Both are warnings rather than
evidence, and this repo already treats rendering an unvetted panel as
unremarkable as a named failure mode — `panel_state`'s terminal `{% else %}` is
the neutral box for exactly that reason, pinned by
`test_an_unknown_panel_state_never_renders_green`. A disqualifier-grade red flag
behind a disclosure is the same error in a different place.

A sticky in-page nav follows the scroll: Brief · Rationale · Panel · Scores ·
Review · Timeline. The Human-review card keeps its current position, after the
evidence sections and before the timeline, and gains an anchor.

Everything already gated on `admin_view` stays gated on it: the raw verdict
JSON and the LLM drill-down remain admin-wrapper-only, and the manager render
still receives no tool activity and no verbatim specialist opinion from the
service.

---

## 6. Slack headline widening

`src/services/assessment_headline.py::render_assessment_headline` gains an
`elevator_pitch` parameter and renders it as a second line beneath the existing
one. The project line keeps using `company_or_project` — unchanged.

The segment is **omitted entirely when the pitch is absent**, exactly as the
band/score segment already is for an empty `scores` map. Every one of the 12
existing rows has `elevator_pitch IS NULL`, so
`scripts/backfill_assessment_headlines.py` — which shares this renderer, by
design, so a repaired headline cannot read unlike a live one — posts precisely
what it posts today for them.

This is a deliberate widening of design D12, whose current text says exactly
five fields are ever rendered. It requires, in the same commit:

- the D12 field list in `CLAUDE.md` updated to six;
- `tests/unit/test_claude_md_disclosure_sync.py` updated — it asserts CLAUDE.md's
  claims about what a PI can see, and the current text becomes wrong the moment
  the pitch is posted;
- the module docstring's content-policy paragraph updated, since it states the
  five-field rule as the reason the function exists in this shape.

---

## 7. Migration `0043`

`0043_assessment_narrative_and_review_dimension_scores`, `down_revision = "0042"`.

Six additive nullable columns across two tables. No DDL rewrites, no FK
changes, no data repair, no backfill.

```
opportunity_assessments : headline (Text), key_points (JSONB), elevator_pitch (Text)
assessment_reviews      : dimension_scores (JSONB), rubric_version (String(20)),
                          rubric_content_hash (String(20))
```

### 7.1 Deploy order — migrate BEFORE the new code serves

Old code against the new schema is safe. The reverse is not, in both
directions at once:

- **Read side.** The new code maps all six columns, so against a pre-`0043`
  database every `select(OpportunityAssessment)` — both assessment list pages,
  both detail pages — and every `select(AssessmentReview)` — the detail pages'
  feedback list, and `review_bot`'s own load — raises `UndefinedColumn`.
- **Write side.** `_persist_assessment`'s INSERT will name `headline`,
  `key_points` and `elevator_pitch`, so **every verdict write of the run
  fails**. That write is best-effort and its failure is swallowed, which makes
  this the silent half: Slack replies keep looking normal while the archive
  takes nothing. This is the same failure the `0028`/`0030`/`0036` boxes exist
  to prevent, and it is why step 3 of the restart runbook is build-only.

```
DC="docker compose -f docker-compose.prod.yml"
$DC build blackbird-app worker
$DC --profile agent build agent
$DC run --rm blackbird-app alembic upgrade head
$DC run --rm blackbird-app alembic current      # must equal `alembic heads`
$DC up -d blackbird-app worker
$DC up -d agent                                  # supervisor comes back IDLE
```

The agent image rebuild is **not optional here and not interchangeable with the
prompt mount**. `prompts/` is bind-mounted, `src/` is baked. Shipping the
prompt edit without the image means the hub emits the three new keys and
`_persist_assessment` discards them — they survive only inside `raw_verdict`.
Shipping the image without the prompt means every new row writes NULL. They go
together.

Production is stamped `0042`, so this box applies to the next deploy.

---

## 8. Testing

New:

- Reviewer and manager can submit feedback and set status; reviewer is 403 on
  assign/unassign (§1.2).
- Dimension-score validation: unknown key rejected; out-of-range value
  rejected; empty dict normalizes to `None`; a valid sparse dict round-trips.
- A review row is stamped with the live rubric version and content hash.
- A stored review whose stamp matches no registry entry renders its keys
  without titles and is never remapped onto today's dimensions.
- `review_bot`: an edit that changes **only** `dimension_scores` is not stamped
  consumed by an in-flight job (A1).
- All three narrative fields NULL → both surfaces render today's content with
  no empty-state artefacts.
- `key_points` renders on the list page while `rationale` / red-flag text /
  milestones still do not (§4.1).
- The panel banner and a non-empty red-flag list are never inside a collapsed
  `<details>` on the detail page.
- Slack headline: pitch present → six segments; pitch NULL → byte-identical to
  today's output.

Existing tests, split by what this change actually does to them:

- **Must be updated in the same commit:** `test_doc_prompt_sync` (regenerate via
  `scripts/sync_prompt_set_docs.py`), `test_claude_md_disclosure_sync` (the D12
  field list moves from five to six), and
  `test_admin_assessments_page_renders_no_inline_detail_rows` (docstring
  corrected, positive `key_points` assertion added — §4.1).
- **Re-run, expected to pass unchanged, and a failure means a real defect:**
  `test_rubric_prompt_sync` — it pins only the skeleton's `scores` and `gating`
  key sets (`:84`, `:97`), neither of which this change touches, so a failure
  means a new key landed in the wrong place. `test_json_none_as_null` — a
  failure means one of the two new JSONB columns omitted `none_as_null=True`.
  `test_reachability` — the card list keeps the wrappers' existing
  `assessment_link`/`pi_link` macros, so a failure means a route lost its only
  literal reference.

Gate: `./scripts/ci.sh` before commit — alembic single-head and round trip,
ruff, full pytest with the branch-coverage floor.

---

## 9. Adversarial analysis

Findings are ordered by what they would cost if missed. Every one names its
mitigation; the ones marked **ACCEPTED** have no mitigation and are being taken
on knowingly.

### A1 — `review_bot` silently swallows a dimension-score-only edit · **HIGH, must fix**

`execute_review_analysis` stamps `consumed_at` with a content-conditional
UPDATE whose WHERE compares `score` and `comment`
(`src/services/review_bot.py:511-512`). Adding `dimension_scores` creates a hole:
a reviewer who edits **only** the per-dimension scores triggers
`edit_feedback`, which resets `consumed_at = None` and enqueues a fresh job —
but an in-flight job's UPDATE still matches on the unchanged `(score, comment)`
pair and re-stamps `consumed_at`. The newly enqueued job then finds zero
unconsumed rows and completes as a no-op. Net effect: the reviewer's edit never
reaches the model, silently, with no WARNING (the `stamped != len(reviews)`
warning does not fire — the stamp *succeeded*).

**Mitigation.** Add a null-safe `dimension_scores` comparison to the WHERE:
`.is_(None)` when the snapshot value is `None`, `== snap["dimension_scores"]`
otherwise (JSONB equality). Pinned by a test that edits only the dimension
scores mid-flight.

### A2 — a new list-page context key reaches only one of the two surfaces · **HIGH, must fix**

`admin_assessments` allowlists every context key it forwards
(`src/routers/admin.py:814-840`, and its own comment says so); `manager_assessments`
splats `**view`. A new top-level key added to `list_assessments` and not added
to the allowlist reaches the manager page and is Jinja `Undefined` — silently
falsy, never an error — on the admin page. `list_assessments` already documents
this trap twice and works around it by attaching `panel_state` and `review_cols`
to the row objects rather than returning them as keys.

**Mitigation.** Anything the card needs rides on the `assessments` rows. If a
genuine top-level key is unavoidable, add it to the admin allowlist *and* assert
its presence on **both** surfaces in the same test.
(The assessment *detail* routes both splat `**detail`, so §2.3's `review_rubric`
key is safe on that page.)

### A3 — the change ships and appears to do nothing · **HIGH, accepted with eyes open**

Per N5 there is no backfill, and the narrative fields only exist for verdicts
written after the agent image is rebuilt. All 12 rows currently in production —
the only assessments any reviewer can look at today — will show no headline, no
bullets and no pitch. The card list and the brief will render, but with today's
content in a new shape. The reviewer-facing benefit of request 3 arrives only
when the next simulation run concludes an interview.

**Mitigation:** none, by decision. Stated here so nobody reads the empty briefs
as a bug. The named follow-up is a one-off
`scripts/backfill_assessment_summaries.py` doing 12 `claude-opus-5` calls over
`rationale` + `raw_verdict`, with `--dry-run` and `--apply`, on the precedent of
`scripts/backfill_assessment_headlines.py`. It is out of scope for this change.

### A4 — prompt compliance is the only enforcement · **MEDIUM**

No code can check that a headline "tells the whole story", that the bullets are
the *right* three concepts, or that each rationale paragraph opens with a
bolded summary. The failure mode is silent and degrades to today's rendering.
This is the same class of unenforced invariant as the existing confidentiality
rule binding `<slack_message>` to publicly-disclosed detail — which CLAUDE.md
already records as checked by no code and no test.

**Mitigation.** Shape checks that *are* mechanizable run at persist time as
WARNINGs (headline length, `key_points` cardinality and element type), never as
drops. Content quality is a prompt-tuning loop, and the review bot is the
existing channel for it.

### A5 — doubled bold erodes the skim line · **LOW**

Per N6 each paragraph carries two adjacent bold runs. If the label grows, the
bolded fraction approaches a third of the text and bolding stops signalling
anything.

**Mitigation.** The prompt caps the label at two to four words. Residual risk is
real and unenforceable; if it degrades, the fix is a prompt edit, not a
schema change.

### A6 — the Slack widening is unretractable, and rests on an unverified premise · **MEDIUM**

A `#assessments-summary` post cannot be recalled. Per N12 the pitch may carry
the PI's unpublished disclosures, and the safety of publishing it rests
entirely on the operator's assertion that PIs cannot join the workspace — a
fact this codebase does not enforce and this design could not verify. The repo
actively renders a workspace join link to every PI
(`templates/agent/dashboard.html:71`, `:296`). If that invite is ever
re-enabled, one PI joining `#assessments-summary` can read another lab's
unpublished results, undisclosed compounds and volunteered limitations. Today's
measured baseline is 0 leaks across all 1,354 messages of run `8b64a0e0`.

**Mitigations, all partial:** the pitch segment is omitted when NULL, so nothing
retroactive is posted and the 12 existing rows are unaffected; the widening
only affects verdicts written after the rebuild; the premise is recorded in
§0.2 so a future reader knows what the decision depended on. **A cheap
follow-up worth doing separately:** either remove the PI-facing join link or
convert `#assessments-summary` to a private channel the hub is invited to —
either one turns the premise into an enforced property.

### A7 — a deploy that starts the agent before migrating loses every verdict of the run · **MEDIUM, procedural**

`_persist_assessment`'s INSERT will name three columns that do not exist on a
pre-`0043` database, and that write is best-effort: the failure is caught,
logged once, and the Slack reply goes out looking completely normal. This is
the exact shape of the 2026-08-06 near-miss recorded in CLAUDE.md.

**Mitigation.** §7.1's build-then-migrate-then-start ordering, the
`alembic current` confirmation step, and a new deploy box in CLAUDE.md matching
the `0037`/`0038`/`0040`/`0041` house style.

### A8 — a reviewer may score six dimensions from a summary the bot wrote · **MEDIUM, accepted**

Collapsing the rationale makes it possible to score the proposal having read
only the pitch and three bullets — which were written by the same model whose
verdict is being reviewed. That is a measurement-integrity cost: the human
review exists partly to catch the bot being wrong, and a human anchored on the
bot's own summary catches less.

**Mitigation.** The dimension dropdowns sit *after* the evidence sections, not
in the brief; the review card's intro says the score must reflect the full
rationale; the "Full rationale" disclosure is labelled with its paragraph count
so its size is visible rather than hidden. Residual risk **ACCEPTED** — it is
the direct cost of the digestibility the request asks for.

### A9 — showing the bot's score anchors the human · **MEDIUM, ACCEPTED**

Per N11. Presenting BlackbirdBot's 1–5 beside the reviewer's own dropdown makes
disagreement visible, and also makes agreement cheap. Published anchoring
effects on exactly this kind of side-by-side rating are large.

**Mitigation.** Muted styling and an explicit *"BlackbirdBot scored"* label, so
the number is legible as the bot's claim rather than as a default. A
reveal-after-scoring interaction is the obvious future fix and is out of scope.

### A10 — 500 cards is a much longer page than 500 rows · **LOW**

`ASSESSMENTS_LIMIT` is 500 and cards are several times taller than table rows.
With 12 rows today this is theoretical; with a full run it is not.

**Mitigation.** Keep the existing limit and keep the "top N of TOTAL" note
prominent above the list. Pagination is a separate change if the corpus grows.

### A11 — the scan-only pin encodes a policy this change narrows · **LOW, handled**

See §4.1. The pin is narrowed and re-documented, never deleted — the
distinction is that `key_points` is a summary field authored for this purpose,
while `rationale` / red-flag text / milestones remain detail-page content.

### A12 — the reviewer role has never run in production · **LOW, procedural**

Zero reviewer accounts exist. Every claim about the reviewer path is currently
backed by tests alone.

**Mitigation.** §1.3 — provision one and walk the path signed in, not
impersonating. Impersonating a reviewer will show no write forms, which is the
very confusion that started this.

### A13 — stale dimension keys across rubric revisions · **LOW, handled by design**

Rubric v3.0.0 replaced a 13-dimension dual-scale regime with six single-scale
dimensions. A human review scored under one revision must not be rendered
against another.

**Mitigation.** §2.1's stamp plus §2.3's resolution through
`src/services/rubric_revisions.py`; an unknown stamp renders keys as stored.

### A14 — a third `none_as_null` regression · **LOW, alarmed**

Two new JSONB columns. This bug has shipped twice on this schema.

**Mitigation.** Explicit in §2.1 and §3.1; `tests/unit/test_json_none_as_null.py`
walks `Base.metadata` and fails the gate if either column omits it.

### A15 — the impersonation notice does not restore the admin's ability to review as themselves in context · **LOW, ACCEPTED**

An admin who wants both the manager viewpoint and the ability to review must
switch back and forth. That is the correct trade: attribution beats
convenience.

### A16 — prompt and image must ship together · **LOW, procedural**

`prompts/` is bind-mounted; `src/` is baked. Either half alone produces a
silent partial: emitted-and-discarded, or written-as-NULL. Covered by §7.1.

### A17 — an unrecorded prompt-set edit · **LOW, handled**

`prompt_set_stamp` hashes the prompt set into every run-start announcement. A
content change without a `role.toml` version bump is by definition unrecorded.
Covered by §3.2.

### A18 — doc/prompt drift fails late · **LOW, handled**

`scripts/sync_prompt_set_docs.py` in the same commit, per CLAUDE.md's standing
instruction; otherwise one prompt edit becomes a fistful of CI failures on the
next full run.

### A19 — CLAUDE.md's disclosure claim becomes false · **MEDIUM, must fix**

`tests/unit/test_claude_md_disclosure_sync.py` exists specifically because a
2026-08-22 edit left CLAUDE.md asserting a confidentiality boundary the code did
not have, and sent an audit chasing a leak that was in fact prompt compliance.
Publishing the pitch makes the current five-field claim wrong.

**Mitigation.** CLAUDE.md, the `assessment_headline` docstring and the test's
field list all move together, in the commit that widens the post.

### A20 — malformed `key_points` · **LOW, handled**

A model that answers `key_points` with a string instead of a list must not
DataError the row out of existence.

**Mitigation.** §3.3's `isinstance(..., list)` degrade-to-`None`, matching
`red_flags` and `derisking_milestones`; `raw_verdict` keeps the original.

### What would make this design wrong

- If the operator premise behind N12 is false — if any PI has in fact joined
  the workspace — §6 must be reverted before deploy, not after. It is the only
  part of this change that is unretractable once shipped.
- If the corpus grows past a few hundred rows before this ships, A10 stops
  being theoretical and the list needs pagination in the same change.
- If a future rubric revision changes the dimension count, §2.3's form and
  §2.1's stamp are what keep old reviews readable. Removing either one
  reintroduces A13 in a form that cannot be repaired from the data.

---

## 10. Out of scope

- Backfilling the 12 existing rows (A3), including the named follow-up script.
- Any change to `weighted_score`, the band thresholds, or the rubric document.
- Any change to what the hub writes in `<slack_message>`.
- Making `#assessments-summary` private, or removing the PI-facing Slack join
  link (A6) — both are worth doing and both are separate changes.
- A reveal-after-scoring interaction for the bot's dimension scores (A9).
- Pagination of the assessments list (A10).

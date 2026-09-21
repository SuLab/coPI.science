# Review-driven assessment contract changes — design

**Date:** 2026-09-21
**Status:** approved, not implemented
**Touches:** scout_hub prompt set 1.6.0 → 1.7.0, migration `0050`,
`src/agent/simulation.py`, `templates/admin/_assessment_detail_body.html`
**Does not touch:** `prompts/rubric/blackbird-rubric.toml`, `#assessments-summary`,
the assessment queue card, any existing row

**Revision:** adversarially audited 2026-09-21 against the repository; 13 findings,
6 of them deterministic CI failures or design defects, all folded in. What the audit
changed: §3.1's explanatory note (it contained the literal `Write:` and quoted the
replaced headline, breaking two of §8's own tests); §3.2's budget (the original bound
sentence 4's *start*, which `_clip_at_sentence` does not honour); §4.1 (four
migration-bookkeeping sites, none previously named); §5 edit 3
(`_HEADLINE_SOFT_LIMIT`, a write-path alarm the original silently retired); §6 (card
retitle and citation renderer); §7 (a pre-deploy ordering precondition and two more
`UndefinedColumn` surfaces); §8 (six unnamed tests); §9 (risk 5, and 107 for a string
previously given as both 103 and 107). The audit confirmed §A's anchors, the
engine-extensibility analysis, the read-path analysis, the doc-sync claim and the
`#assessments-summary` scope claim; those needed no rework.

---

## 1. Where this came from

Two human reviews exist in production, both written 2026-09-18 by Virginia Burger
(`virginia@blackbirdlab.org`, `user_role = manager`, ORCID 0000-0002-8612-4797),
both `feedback_mode = 'learn'`, both scoring the proposal **3/5**, both stamped
rubric **3.4.0 / b7b0a1d6a4a5**, both with `recorded_by_user_id IS NULL` (she acted
in person, not under impersonation) and `dimension_scores IS NULL` (no per-dimension
signal).

| assessment | bot verdict | bot score / band | her score |
| --- | --- | --- | --- |
| `a4dcc53f` ME3BP-7 — MCT1-gated 3-bromopyruvate, pancreatic cancer | conditional | 3.10 / conditional | 3 |
| `6d637430` Oral NAT8L inhibitor for Canavan disease | conditional | 2.95 / conditional | 3 |

She disputes no fact and no recommendation, and on Canavan explicitly ratifies the
score ("it could be a 3 given that they do have some answers on both sides"). The
critique is about the *brief* — what it says, in what order, and what it leaves the
reader to infer — plus one disagreement about experimental sequencing.

### 1.1 Four findings that reframe the feedback

These were established from the production database and the repository, not from the
review text alone. They are recorded here because three of them contradict the
obvious reading of the reviews.

**F1 — the reviewed output is two prompt-set versions stale.** Run
`0e41ab0f`'s `run_start_announcement` records **hub prompts v1.4.0** (hash
`6e2bfd126a13`). `1.5.0` (strengths/risks, migration `0049`) landed at commit
`8d6672c`, 2026-09-14 23:08 UTC — after that run ended at 19:51 UTC, which is why
both rows have `strengths IS NULL` and `risks IS NULL`. `1.6.0` landed at `7f546c9`
on 2026-09-21, three days *after* her review.

**F2 — the live headline contract enshrines a headline she rejected.** `7f546c9`
added a second positive example to sidecar item 6:

> Write: "An oral drug that blocks the enzyme making a brain metabolite that builds
> up to toxic levels in children with Canavan disease."

That string is byte-identical to `6d637430`'s stored `headline`, which her review
calls *"Title challenging to read"*. The commit body states the new examples are
"real output of the prompt as it stood, which is the point"; it makes no reference to
the review, which predates it. The example is now pinned by
`tests/unit/test_headline_contract.py::test_the_headline_item_carries_two_positive_examples`.
The detail page renders `headline` as the `text-2xl` page title
(`templates/admin/_assessment_detail_body.html:73`), so "title" is unambiguous.

**F3 — "competition is missing" is mostly a surfacing failure, not a diligence
failure.** The panel produced the material and it is in the database:

- *Canavan* — the stored `rationale`'s "Commercial and legal." paragraph names
  Myrtelle (rAAV-Olig001-ASPA) and BridgeBio (BBP-812) as clinical-stage AAV,
  Contera's `US20260103714A1` (published 2026-04-16) for the ASO, and the lapsed
  Toledo/Viola filing. That answers her "who else is in this space / what stage is
  the ASO" question in full — in paragraph 5 of 6.
- *ME3BP-7* — the clinical specialist named NALIRIFOX, KRAS G12D and pan-RAS
  inhibitors and claudin-18.2 across four separate consults; the commercial
  specialist supplied her stage-matched benchmark outright (AZD3965 — AstraZeneca,
  Phase I, on-mechanism retinal toxicity, not carried forward; CPI-613/devimistat —
  Rafael/Cornerstone, failed). **None of it reached the assessment row.** The
  `rationale`'s commercial paragraph covers only the mechanism lane
  (AZD3965/BAY-8002/AR-C); `key_points.commercial_potential` got two bullets naming
  none of the above.

So the hub *does* commission and receive deep competitive diligence. It loses it in
compression: `commercial_potential` is capped at two bullets of 160 characters, and
the rationale paragraph is one of six.

**F4 — three mechanisms bury the panel's own text.** The Panel card renders domain
chips only, no consult content. Full consult text lives in the Interview timeline,
a `<details>` with no `open` attribute. The Strengths-and-risks card quotes only the
*latest* consult per domain (`_latest_consult_per_domain`,
`src/services/assessment_detail.py:702`) — for ME3BP-7's `commercial` domain that is
the 19:47 consult, not the 19:24 one carrying the comparables.

### 1.2 Operational state at time of writing

Zero `review_feedback_analysis` jobs have ever run in production;
`prompt_change_suggestions` is empty; both reviews are `consumed_at IS NULL`. One
stale artifact: Jason Zavras self-assigned assessment `74051d91` (Konig 9G4, advance,
3.7) on 2026-09-01 and never wrote a review. Coverage is 2 of 22 assessments.

---

## 2. Decisions

| # | Decision | Rationale |
| --- | --- | --- |
| **D1** | Treat all four review themes as contract, not as unconfirmed taste. | Operator decision. Accepts that this reverses part of the `1.6.0` headline decision on one reviewer's evidence, and that n = 2 from one reviewer is a thin base for a contract. |
| **D2** | Headline: tighten the constraint, leave the register free. | Her complaint is readability, and the measurable defect is chained relative clauses at length — not sentence-vs-noun-phrase. Mandating her noun-phrase register would contradict item 6's own opening words and force rewriting an example she never saw. |
| **D3** | Competitive landscape: new sidecar key and column. | The repo's established pattern (`0043`, `0048`, `0049`). The only option with room for named programs + stage + crowdedness + expansion; `commercial_potential`'s 320-character budget cannot hold them. |
| **D4** | Pitch: adopt her order, with a budget that keeps provenance inside the published excerpt. | Her order moves the citation from sentence two to sentence four, and only the first 600 characters reach `#assessments-summary`. `_clip_at_sentence` publishes only **complete** sentences, so a late citation is all-or-nothing: the budget must bound where sentence 4 *ends*, not where it begins (§3.2). Accepted as unverifiable — see §9. |
| **D5** | Evidence maturity: a second new column, not a sixth `key_points` group. | `normalize_key_points` rejects an unknown key outright (`assessment_detail.py:148`), so a sixth group inverts the deploy hazard: prompt-without-image would store `key_points = NULL` and lose all five existing groups. A new column loses only itself. |
| **D6** | Sequencing: require the ordering be justified; do not mandate a direction. | ME3BP-7 says work package A is "first, and gating on everything else" but never says why chemistry gates biology rather than the reverse. That silence is the defect; the ordering itself may be right, and one reviewer on one asset is not grounds to invert a hub's argued reasoning. |
| **D7** | Both new fields render inside the existing Strengths-and-risks card. | Operator decision, against the recommendation. Keeps the brief at its current density; the card is the second block on the page, so the cost is smaller than "below the fold" suggests. |
| **D8** | No rubric edit. | None of the four themes is a scoring change. `[meta].version` stays `3.4.0` and `prompts/rubric/revisions.toml` gains nothing. |

---

## 3. Prompt contract: scout_hub 1.6.0 → 1.7.0

All edits are to `prompts/roles/scout_hub/phase4-thread-reply.md` unless stated.

### 3.1 E1 — item 6, headline

Three changes; everything else in item 6 survives verbatim, including both mandatory
elements, the noun-stack ban, the slash ban, the lab-suffix ban, the one-abbreviation
rule and both `Not:` examples.

1. **Cap `140` → `110` characters**, in the item's opening sentence and in the
   element-3 parenthetical that currently reads "rarely fit 140 characters".
2. **Add a rule**, in the paragraph that already bans noun stacks:

   > **At most one embedded relative clause** — a headline that chains "that … that
   > …" makes the reader hold two unresolved clauses at once, and is the commonest
   > way a headline that satisfies every rule above is still hard to read.

3. **Replace the Canavan `Write:` example** with the reviewer's own rewrite, and
   append a note explaining the swap:

   ```
       Write: "Enzyme-blocking drug for prevention of toxic metabolite build
       up in brains of children with Canavan disease"
   ```

   > The second positive example above is a human reviewer's rewrite of a headline
   > this prompt produced under 1.6.0 — the one this item used to hold up as a
   > model. It satisfied every rule above and was still reported as hard to read:
   > two chained relative clauses at 126 characters.

   **The note must not contain the literal string `Write:` and must not quote the
   replaced headline verbatim.** `_item_six()`
   (`tests/unit/test_headline_contract.py:21-26`) slices from `6. **Headline.**`
   to `7. **Key points.**`, and `test_the_headline_item_carries_two_positive_examples`
   is a plain substring count over that slice — a third occurrence fails it. The
   verbatim sentence is excluded for the same reason: §8's new absence test asserts
   it is gone from item 6.

Measured lengths, which is why the cap is set at 110:

| string | chars | ≤ 110 |
| --- | --- | --- |
| existing `Write:` (blood test) | 108 | yes |
| new `Write:` (reviewer's rewrite) | 107 | yes |
| rejected 1.6.0 example | 126 | no |
| ME3BP-7 stored headline | 138 | no |

The cap alone rejects both headlines on record that drew criticism, and neither
surviving example needs rewording.

### 3.2 E2 — item 8, elevator pitch

Replace the sentence plan wholesale. Current text (verbatim):

> Three to four sentences … Sentence one names what the thing is and who it is for;
> sentence two names **where the work comes from** … The remaining sentences state
> what exists today, what the money would buy, and why the answer matters …

Replacement plan, in this order:

1. **The problem** — the disease, the patient population and its size, and what
   those patients get today. Open here, not with the asset: a reader who meets the
   asset name first has no context to put it in.
2. **The solution** — what the thing is and what it does.
3. **How it differs**, and whether it actually solves the problem sentence one
   named. If the mechanism addresses the stated liability, say so; if it does not,
   say that instead.
4. **Where the work comes from** — the published paper, preprint or dataset, cited
   the way the lab's own public profile cites it (DOI or PubMed link), or
   "unpublished" plainly when there is none.
5. **What exists today and what the money would buy.**
6. **What a clean read-out would enable** — the sentence that says why the answer
   matters. This closes the pitch.

Plus: `Three to four sentences` → `Four to six sentences`; the 900-character cap is
unchanged; and a new budget sentence —

> Sentences 1–4 together must **end** within approximately 550 characters, so the
> citation sentence completes inside the first 600 characters that are posted
> publicly. If something has to go, cut from 5, which survives in the app-only tail.
> At four sentences, elements 4 and 6 are the two that must survive: merge 1 with 3
> and 2 with 5 before dropping either.

**Why 550, and why "end" rather than "begin".** `_clip_at_sentence`
(`src/services/assessment_headline.py:88-161`) does **not** truncate at offset 600.
It cuts after the last sentence terminator found *inside* `value[:600]` that leaves
at least half the budget, so a sentence is published whole or not at all. A budget
of "sentences 1–3 ≤ 400" would guarantee only that sentence 4 *begins* before 600 —
and a citation sentence carrying a DOI URL plus context routinely runs past 200
characters, in which case the chosen boundary is the end of sentence 3 and the
citation is dropped entirely. Since today's contract puts the citation in sentence
two, comfortably inside the window, getting this bound wrong would make the reorder
a net regression against current behaviour rather than a neutral change.

Retained unchanged: the "at most 900 characters / first 600 posted publicly" framing,
the minimal-jargon rule, the abbreviation rule, and "Do not reason about the score
here — item 10 is for that."

This plan is derived from the reviewer's own rewrite of the ME3BP-7 pitch, which she
supplied in full, and from her instruction on the Canavan review to apply "the same
comments on re-arranging elevator pitch as for previous abstract".

### 3.3 E3 — item 7, key points

One added sentence to the `commercial_potential` bullet, to stop it duplicating the
new item 13:

> `commercial_potential` covers the path to a product, the IP position, the market
> and the partner — **not** the competitive landscape, which item 13 owns.

No change to the five group keys, their order, their per-group bullet counts, or
`KEY_POINT_GROUPS`.

### 3.4 E4 — new items 13 and 14

```
13. **Competitive landscape.** Two to four bullets, each at most 200
    characters. Name the competing and adjacent programs and, for each, **its
    development stage** — clinical, filing, preclinical, abandoned — or state
    plainly that a search found none. Together the bullets must answer whether
    the field is crowded, how this compares with the clinical-stage programs
    in it when THEY were at this stage, and what expansion indications exist.
    This is your own diligence and the commercial and clinical panels' —
    never sourced from the lab agent. Record them in `competitive_landscape`
    as an array of strings. **Staff-only: like the score rationale, this field
    is never posted to Slack.** Never state a number for the weighted score or
    the band.
14. **Evidence maturity.** Two to four bullets, each at most 200 characters,
    one per axis the verdict rests on — the biology, and the enabling
    chemistry, assay or platform. Each names what IS settled and what is NOT,
    in those terms. State the biology axis even when the chemistry is the
    obvious risk: a reader must never have to infer how well understood the
    biology is from the absence of a complaint about it. Record them in
    `evidence_maturity` as an array of strings. **Staff-only**, same rule as
    item 13.
```

Item 14's closing instruction is the literal form of the reviewer's generalisation:
*"These intro sections should always highlight the degree that the biology is
understood."*

### 3.5 E5 — item 5, recommended next experiment

Append to the end of item 5's prose:

> Where the work has more than one package, name which package **gates** the others
> and say in one line why that order rather than the reverse — a reviewer who would
> sequence it differently must be able to see what you traded off.

### 3.6 Mechanical companions to the five edits

- The bare-`~` warning list gains `competitive_landscape` and `evidence_maturity`.
- The `<assessment_json>` skeleton gains two keys after `"risks": []`:

  ```json
  "competitive_landscape": [],
  "evidence_maturity": [],
  ```

- `prompts/roles/scout_hub/role.toml`: `version = "1.6.0"` → `"1.7.0"`.
- Run `.venv-test/bin/python scripts/sync_prompt_set_docs.py` — the prompt file is
  embedded verbatim in `docs/specs/2026-08-07-hub-bot-prompts.md` and
  `tests/unit/test_doc_prompt_sync.py` asserts it.

---

## 4. Schema: migration `0050_assessment_landscape_and_evidence_maturity`

Two additive nullable JSONB columns on `opportunity_assessments`, mirroring `0049`:

| column | type | null |
| --- | --- | --- |
| `competitive_landscape` | `JSONB` | yes |
| `evidence_maturity` | `JSONB` | yes |

Model (`src/models/opportunity.py`) declares both as
`Mapped[list | None] = mapped_column(JSONB(none_as_null=True), nullable=True)`,
matching `strengths`/`risks` at lines 103–104. `none_as_null=True` is what makes an
absent value a real SQL NULL rather than the JSON scalar `null` — the same property
migration `0031` had to repair retroactively for eleven other columns.

**NULL on every one of the 22 existing rows, deliberately never backfilled.** Those
verdicts were never asked for either field; a generated one would be
indistinguishable from one the hub wrote. Both read paths render nothing when NULL.

The revision file declares `revision = "0050"` and `down_revision = "0049"`, and
spells the type `postgresql.JSONB(astext_type=sa.Text())` to match
`alembic/versions/0049_assessment_strengths_risks.py:40-43` so the DDL and the ORM
agree. Downgrade drops both columns in reverse-add order, as `0049` does. No data
migration, no repair step, no ordering constraint inside the migration.

### 4.1 Migration bookkeeping — four sites outside the revision file

Adding a revision is not self-contained in this repo. All four must move in the same
commit or `./scripts/ci.sh` fails deterministically:

| site | change |
| --- | --- |
| `tests/integration/test_harness_smoke.py:65` | `assert v == "0049"` → `"0050"`, and extend the per-revision comment block above it |
| `scripts/migrate/preflight.py:74` | `DEFAULT_TARGET = "0049"` → `"0050"` — the guarded production path silently will not apply `0050` otherwise |
| `scripts/migrate/preflight.py:135`, `:224`, `:428` | append `"0049"` to `SUPPORTED_START_REVISIONS`, `"0050"` to `REVISION_ORDER`, and two `PlannedObject("0050", "column", …)` entries to `PLANNED_OBJECTS` |
| `tests/unit/test_migration_checks.py:230-236` | the literal pins of both structures; `:1010-1012` asserts `REVISION_ORDER[-1] == DEFAULT_TARGET` and `:870-915` re-derives `PLANNED_OBJECTS` from the migration files |

---

## 5. Engine

No new normaliser. `normalize_bullets` (`src/services/assessment_detail.py:155`)
already has the exact contract — a non-empty list of non-blank strings, stripped;
anything else `None` so a malformed narrative field never costs the verdict (A20) —
and `_HUB_BULLETS_MIN` / `_HUB_BULLETS_MAX` / `_HUB_BULLET_CHARS` (2 / 4 / 200,
`simulation.py:9274-9276` — note the third is singular) already match the bounds
both new items ask for.

Three edits in `_persist_assessment` and its constants:

1. `simulation.py:4620` — extend the soft-bound loop from
   `for _field_name in ("strengths", "risks")` to all four field names. Shape
   violations (wrong bullet count, over-long bullet) warn and store; only a genuine
   type violation drops the field to NULL, with `raw_verdict` keeping the original.
2. `simulation.py:4691` — two kwargs beside `strengths=`/`risks=`:

   ```python
   competitive_landscape=normalize_bullets(verdict.get("competitive_landscape")),
   evidence_maturity=normalize_bullets(verdict.get("evidence_maturity")),
   ```

3. `simulation.py:9251` — **`_HEADLINE_SOFT_LIMIT` 140 → 110.** Not optional and
   not cosmetic: it is the write-path drift alarm that mirrors the prose bound, and
   leaving it at 140 means a 126-character headline — exactly the class §3.1 exists
   to eliminate — is emitted, stored and warned about by nothing. The pairing is
   documented at `tests/unit/test_rubric_prompt_sync.py:313-316`: "the prose and the
   drift alarms cannot part company." `_PROJECT_SOFT_LIMIT` (70) and
   `_PITCH_SOFT_LIMIT` (900) keep their numbers.

`normalize_key_points` is untouched, so no `key_points` shape risk is introduced.
Top-level sidecar keys the parser does not know are already ignored
(`_extract_assessment_json` does a bare `json.loads` with no allowlist, and every
retry and recovery path — `_flush_pending_assessments`, `_recover_rows_individually`,
`_record_unwritable_assessment`, `_mark_summary_posted`, `_retire_superseded_verdict`
— is `**kwargs`/`.get`-based and key-set agnostic), so the skeleton addition is safe
in both deploy directions (§7).

Two comments go stale with §3.2's sentence-count change and must be corrected in the
same commit: `_PITCH_SOFT_LIMIT`'s "Generous enough for the 3-4 sentences the
contract asks for" (`simulation.py:9258-9262`) and `PITCH_DISPLAY_CHARS`' "3-5
sentences" (`src/services/assessment_headline.py:66-71`). The 900 cap is deliberately
**not** re-derived for six sentences — it stays a tightening, and the comment should
say so rather than claiming a fit it no longer has.

---

## 6. Read path

Two new stacked sections in the `#signals` card
(`templates/admin/_assessment_detail_body.html`), placed after **Risks** and before
**Not established**, following the `hub_strengths` pattern at line 216:

```jinja
{% set hub_landscape = a.competitive_landscape if (viewer_is_staff
     and a.competitive_landscape is iterable
     and a.competitive_landscape is not string
     and a.competitive_landscape is not mapping) else [] %}
{% set hub_maturity = a.evidence_maturity if (viewer_is_staff
     and a.evidence_maturity is iterable
     and a.evidence_maturity is not string
     and a.evidence_maturity is not mapping) else [] %}
```

The `viewer_is_staff` gate is deliberate and matches `strengths`/`risks`: the prompt
promises the model both fields are staff-only, and a reviewer account reaches the
manager route. The same iterable/not-string/not-mapping guard applies — a raw JSONB
column holding a string would otherwise render one bullet per character.

`templates/manager/assessment_detail.html:114` and
`templates/admin/assessment_detail.html:139` both `include` this partial, so admin and
manager surfaces are updated by the one edit.

Each bullet renders as `{{ plain_citations(bullet) }}`, matching lines `:298` and
`:323` — not bare `{{ bullet }}`. Without this, a URL in a new bullet renders as
plain text four lines below a `strengths` bullet whose URL renders as a blue "cited
paper" link.

**The card must be retitled.** It currently reads `Strengths and risks`
(`<h2>` at `:271`) with the jump-nav entry `Strengths &amp; risks` (`:55`), and the
page's only navigation is that nav. Filing two sections under a heading that names
neither makes them unreachable. Both become:

| site | from | to |
| --- | --- | --- |
| `<h2>` `:271` | `Strengths and risks` | `Evidence summary` |
| nav `:55` | `Strengths &amp; risks` | `Evidence` |

The five sections under it are then Strengths, Risks, Not established, Competitive
landscape and Evidence maturity. The cost is losing a very legible title; the
alternative is a title that lies about the card's contents.

The card's provenance sentence gains a clause naming the two new fields as the hub's
own text, alongside the existing "In the hub's words" note.

No service-layer change: both fields are plain columns on the mapped
`OpportunityAssessment` the template already receives as `a`.

---

## 7. Deploy

Migrate before the new code serves; rebuild **both** images.

```
DC="docker compose -f docker-compose.prod.yml"
$DC build blackbird-app worker
$DC --profile agent build agent
$DC run --rm blackbird-app alembic upgrade head
$DC run --rm blackbird-app alembic current      # must equal `alembic heads` (0050)
$DC up -d blackbird-app worker
$DC up -d agent                                 # supervisor returns IDLE
```

**Ordering precondition, before the working tree is updated at all.** Confirm
`/admin/simulation` shows no running engine. `prompts/` is the host working tree and
`Agent._load_prompt` → `_load_file` does a `read_text()` **per use**, so checking out
1.7.0 reaches a live agent immediately — before `$DC build`, before the migration.
Worse, `prompt_set_stamp` is read once at run start (`simulation.py:3927-3928`), so a
run in flight would permanently record 1.6.0 for output produced under 1.7.0 — the
unrecorded-edit condition `tests/unit/test_headline_contract.py:69-76` exists to
prevent. The "no run is started" note below covers only what happens after the
deploy; this covers before it.

**Old code against the new schema is safe** — both columns are additive and
nullable, nothing is backfilled. **New code against the old schema is not:** the
code maps both columns, so every `select(OpportunityAssessment)` raises
`UndefinedColumn` across **four** surfaces, not two —

* both assessment list pages and both detail pages;
* `src/services/review_bot.py:536-539`, which runs on the **worker**, so every
  `review_feedback_analysis` job fails;
* `src/routers/reviews.py:192-195`, the review router's own load, so feedback
  submit and edit 500 out of the handler;

— and `_persist_assessment` names both in its INSERT, a best-effort write, so every
verdict of a running simulation would be lost to one ERROR line in a log nobody is
tailing while the Slack replies keep looking normal. Same shape as the
`0043`/`0048`/`0049` boxes, which enumerate all four surfaces; the CLAUDE.md `0050`
box must do the same.

**Pairing hazard, both directions.** `prompts/` is bind-mounted and re-read per use;
`src/` is baked into the agent image.

- *Prompt without image* — the old parser does not know the two keys, so
  `_persist_assessment` never assigns them and both columns stay NULL forever; the
  hub's bullets survive only inside `raw_verdict`. The 110-character headline
  contract and the new pitch order still reach the model, so this half is lossy but
  not corrupting.
- *Image without prompt* — fully benign. An old sidecar emits neither key,
  `verdict.get(...)` is `None`, `normalize_bullets(None)` is `None`, and both
  columns store NULL exactly as before the migration.

A CLAUDE.md deploy box for `0050` ships in the same commit, following the `0049` box's
structure.

**No simulation run is started.** `$DC up -d agent` returns the supervisor IDLE;
starting a run is a separate, explicit operator action from `/admin/simulation`.

---

## 8. Testing

| test | change |
| --- | --- |
| `tests/unit/test_headline_contract.py::test_the_headline_cap_is_unchanged` | rename and assert `at most 110 characters` |
| `tests/unit/test_headline_contract.py` | new: the relative-clause rule is present |
| `tests/unit/test_headline_contract.py` | new: the rejected Canavan sentence is **absent** from item 6 |
| `tests/unit/test_headline_contract.py::test_the_prompt_set_version_was_bumped` | `1.6.0` → `1.7.0` |
| `tests/unit/test_headline_contract.py::test_the_headline_item_carries_two_positive_examples` | unchanged — still two `Write:` examples |
| `tests/unit/test_rubric_prompt_sync.py::test_skeleton_carries_the_narrative_fields` | add both new keys to the required-key loop |
| `tests/unit/test_rubric_prompt_sync.py::test_phase4_bounds_the_headline_and_the_project_label` | **second, independent `at most 140 characters` assertion** — asserts over the whole phase-4 text, not item 6, in a different module. Re-assert at 110 and correct its docstring's `_HEADLINE_SOFT_LIMIT` mirroring claim |
| `tests/integration/test_harness_smoke.py:65` | `assert v == "0049"` → `"0050"` + comment block |
| `tests/unit/test_migration_checks.py:230-236`, `:1010-1012` | the `preflight.py` structure pins (§4.1) |
| new unit test | a 120-character headline logs the `contract asks for <=110` warning — `_HEADLINE_SOFT_LIMIT` currently has no test pinning its number, and `tests/integration/test_assessment_narrative_fields.py:241` uses a 300-char headline so it passes either way |
| `tests/unit/test_assessment_headline_render.py` | a pitch shaped to the new plan (S1–S3 = 400, S4 = 210) — assert whether the citation survives `_clip_at_sentence(..., 600)`; this is the only executable check on §3.2's budget |
| `tests/integration/test_assessment_detail_page.py` | both new section headings present on the staff path, absent on the reviewer path, mirroring `test_a_reviewer_never_sees_the_hubs_own_bullets` at `:2274` |
| `tests/unit/test_pitch_contract.py` (new) | prompt-text assertions for the six-step order and the ~400-character budget |
| `tests/integration/test_assessment_narrative_fields.py` (extend — this is where the `0049` persist cases live, using the module's `engine` fixture and an unbound `SimulationEngine._persist_assessment(stub, …)` call) | both fields: normal store; wrong type → NULL + one warning naming the field; over-long bullet and out-of-range bullet count → warn but store |
| `tests/unit/test_doc_prompt_sync.py` | passes once `scripts/sync_prompt_set_docs.py` has run |

Gate: `./scripts/ci.sh` — alembic single-head plus the upgrade→downgrade→upgrade
round trip against a throwaway Postgres, `ruff check`, then the full pytest run with
the branch-coverage floor.

Note the standing limit on all prompt-side tests: the suite drives
`tests/fakes.py`'s `FakeAnthropic` and never reaches a real model, so every assertion
in §8 about prompt content checks that a requirement *exists and has not been
silently softened*, never that the hub obeys it.

---

## 9. Non-goals and accepted risks

**Accepted risks**

1. **110 is tighter than `1.6.0` argued for.** That commit kept the 140 cap
   explicitly because all three elements "rarely fit" otherwise. At 110, element 3
   ("why it is fundable") will almost always be implicit. The reviewer's own 107 and
   the existing 108-character example prove the two *mandatory* elements fit — but
   her rewrite drops "oral", a real loss of information that the cap makes
   systematic.
2. **The 550-character pitch budget is unverifiable.** It ships as a prompt-text
   assertion only. Nothing in the suite can observe whether the model honours it, and
   the first evidence either way will be the next run's stored pitches.
3. **n = 2, one reviewer, stale prompt.** Every change here rests on two reviews of
   `1.4.0` output. `1.5.0` and `1.6.0` are live and unmeasured, so some of what she
   criticised may already read differently.
4. **This reverses part of a decision made three days after her review**, without
   the author of that decision having seen the review. F2 records the conflict.
5. **The review bot cannot see either new field, so the loop this spec is built on
   will not observe the change.** `_assessment_fields`
   (`src/services/review_bot.py:303-335`) is an explicit thirteen-key dict that
   already omits `headline`, `key_points`, `elevator_pitch`, `score_rationale`,
   `strengths` and `risks`; the two new columns would be the seventh and eighth
   omissions. A reviewer's next round of `learn` feedback on a 1.7.0 verdict
   therefore reaches `claude-opus-5` without the fields that feedback is about.
   Pre-existing, not introduced here, and deliberately left out of scope under D1 —
   which covers the four review themes, not the pipeline. Worth closing next, and
   cheap: it is one dict.
6. **Part of the change duplicates work already done.** Canavan's competitive answer
   already exists in `rationale`. `competitive_landscape` promotes it to a legible
   place; it generates no new diligence. The genuine gap was ME3BP-7 (F3).

**Explicitly out of scope**

- `prompts/rubric/blackbird-rubric.toml` and `revisions.toml` (D8).
- `#assessments-summary`. Both new fields are app-only, like `score_rationale`,
  `strengths` and `risks`. `render_assessment_headline` renders
  `company_or_project` + clipped `elevator_pitch`; the `headline` column is not
  published, which is why §3.1 can change its cap freely.
- `_latest_consult_per_domain` and the collapsed Interview timeline (F4). The
  42,600-character cost of expanding consult quoting was measured on 2026-09-14, and
  `signal_row` already discloses the collapse with a `latest of N consults` badge.
- The assessment queue card.
- The 22 existing rows — no backfill, no regeneration.
- The two reviews stay `consumed_at IS NULL`; the `review_feedback_analysis` job is
  not run as part of this work.

**Observed, not fixed.** `score_rationale` renders ungated while `strengths`/`risks`
are `viewer_is_staff`-gated, so a reviewer account sees the hub's reasoning about the
score but not its bullets. Pre-existing inconsistency, recorded here rather than
changed under this design.

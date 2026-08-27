# Rubric v3.0.0 — complexity consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the screening rubric from 13 dual-scale dimensions to 6 single-scale
dimensions, fold the target-level scientific checklist into per-dimension evidence lists,
fold the six dimension-shadowing red flags into low-score anchors, and re-scope the
`red_flags` sidecar field to disqualifier-grade items — regime `3.0.0`.

**Architecture:** The TOML document changes shape (6 `[[dimension]]` entries, one
`weight`/`anchors` pair each, optional `evidence` lists, no `[checklist]`, no
`[banding.incubation]`); `src/services/blackbird_rubric.py` parses/validates/renders the
new shape, scores on the single scale regardless of funnel stage, and keeps FROZEN v1/v2
weight+banding tables so `display_scale_for` can still render the 77 historical rows
against the scale that actually scored them. The sidecar contract
(`phase4-thread-reply.md`) shrinks to 6 score keys. Nothing needs a DB migration:
`scores`, `red_flags`, `gating` are JSONB and historical rows keep their old keys.

**Tech Stack:** Python 3.11, tomllib, pytest (host `.venv-test`, run via ssh — never
through the sshfs mount), Jinja2 templates.

**Spec:** the 2026-08-27 conversation analysis (adversarial audit + literature review),
recorded in §Evidence below. Design decisions D1–D10 in §Decisions.

## Global Constraints

- `[meta].version = "3.0.0"` (≤20 chars), `date = "2026-08-24"` stays historical — new date `2026-08-27`.
- The three gating keys are UNCHANGED: `life_sciences_domain`, `credible_tech_source`, `translational_potential`.
- Weights are integers summing to exactly 100; thresholds on the 0.01 grid; `advance_min > conditional_min`.
- Historical rows are never rewritten; `display_scale_for` must render pre-v3 rows on frozen v1/v2 scales.
- pi_lab prompts and `_PI_LAB` thread-guidance strings are untouchable (GM-pinned).
- `tests/unit/test_doc_prompt_sync.py` requires docs/specs/2026-08-07-hub-bot-prompts.md's embedded blocks to byte-match the prompt files — regenerate blocks after any prompt edit.
- Full gate: `./scripts/ci.sh` on the host.

## Amendment (2026-08-27, mid-implementation, operator directive)

> "We will not be carrying over any legacy verdicts, so we should not include
> legacy or deprecated references in the rubric."

This supersedes D7 and the frozen-scale halves of D1/D10 below. Applied as:
no `_FROZEN_V2_*` tables and no `display_scale_for`/`scored_stage_aware`/
`is_incubation_stage` in `blackbird_rubric.py` (read paths render every row
against the live document); `weighted_score(scores)`/`band(score)` drop the
`stage` parameter outright; `required_domains_for` keeps no `fto_achievable`
gate trigger and no `ip_fto`/`platform` score triggers (legal and technologic
are cue-required only); the gating key `credible_tech_source` — kept in v2.1.0
purely for storage continuity — renames to **`credible_science`** across the
TOML, the sidecar skeleton, the validator tuple, and the tests; the TOML's
changelog carries only the 3.0.0 entry and its comments carry no
prior-regime genealogy. Pre-v3 rows in any database are not carried over; if
present they render without per-dimension weights and their scores are not
re-derived.

## Decisions (all argued from production data in the 2026-08-27 audit)

| # | Decision |
|---|---|
| D1 | Single scale. The investment scale scored 0 of 51 v2.x verdicts (100% incubation-stage); it is retired from scoring and frozen for legacy display only. (v3-design D2, approved 2026-08-21.) |
| D2 | Six dimensions: differentiation_unmet_need 25, scientific_credibility 20, translational_path 15, fundable_experiment 15, venture_potential 15, team_executability 10. Science block = 35%. Back-test on all 51 v2.x verdicts: 6-dim score correlates 0.985 with the 13-dim score; every merged dimension discriminates positively (Δ +0.50…+0.99). |
| D3 | Bands: advance ≥ 3.4, conditional ≥ 2.8. Back-test: captures 4/4 positives with 2/47 passes above the conditional line; advance stays above the observed max (3.30) with headroom. PROVISIONAL — re-check after ≥20 v3-stamped verdicts. |
| D4 | Checklist folded into `evidence` lists on scientific_credibility (5 items) and translational_path (6 items), phrasing kept binary (CheckEval evidence). The standalone section and `[checklist]` table are deleted. |
| D5 | Red flags: the 6 dimension-shadowing flags become explicit low-anchor content; `[red_flags]` keeps an intro (disqualifier-grade only, max 3, technical concerns → `rationale`) plus the one orthogonal disqualifier (genuinely unresolvable IP blockade). |
| D6 | Gating unchanged — 3 tri-state keys are cheap and `"unconfirmed"` carries information a score cannot. |
| D7 | Consult-floor score triggers (`ip_fto>=4`, `platform>=4`) are kept as LEGACY-verdict triggers only (they fired 0 times in 51 v2.x verdicts; cue tiers carry the load). No new score trigger. |
| D8 | `maps_to_dimensions`: scientific→scientific_credibility; chemistry→translational_path; commercial→(differentiation_unmet_need, venture_potential); talent→team_executability; budget→fundable_experiment; clinical, legal, technologic → () (inform judgement, no owned dimension). |
| D9 | Milestones capped in the prompt at 5 (production mean was 7.8, max 11; staff act on recommended_next_experiment). |
| D10 | `weighted_score`/`band` keep their `(scores, stage)` signatures; `stage` no longer selects a scale (documented, not removed — every engine call site passes it). |

## Evidence (summary; full numbers in the conversation record)

51 v2.x production verdicts: mean inter-dimension r=0.131 (no halo — consolidation is
justified by dead variance, not redundancy); external_signals 76% ≤1 with negative
discrimination; exit_thesis 61% ≤1; ip_fto and platform never scored ≥4 (their floor
triggers never fired); experimental_rigor Δ=0.01 across two regimes; red_flags averaged
10.2 bespoke entries/verdict (taxonomy lost control); gating life_sciences_domain met
51/51; fto_achievable was unconfirmed 38/38 before its demotion. External grounding: NIH
2025 simplified review framework (5 criteria → 3 factors), Dawes 1979 (unit-weight
robustness), Jonsson & Svingby 2007 (analytic rubrics, fewer criteria), CheckEval
(EMNLP 2025, binary decomposition reliability).

---

### Task 1: Rewrite `prompts/rubric/blackbird-rubric.toml` as regime 3.0.0

**Files:** Modify: `prompts/rubric/blackbird-rubric.toml` (full rewrite)

- 6 `[[dimension]]` entries per D2, each with `key`, `weight`, `title`, `anchors`,
  optional `specialist`, optional `evidence` (list). Anchors absorb the red-flag
  content per D5 and the incubation anchor language from v2.
- `[banding]`: advance_min 3.4, conditional_min 2.8, semantics, pass_label,
  vocabulary_note, conditional_note, pass_note, advisory_note (kept verbatim from v2.2.0).
- `[intro]`: five steps (checklist step removed); interview-conduct + confidentiality
  paragraphs kept.
- `[scoring].preamble`: 35%/65% split stated; forward-looking commercial guidance kept;
  "score zero when left out" replaced by "every dimension applies at this grain —
  judge on what applies, note the rest in rationale"; platform/diagnostic adaptation
  note folded in (was the checklist intro).
- `[red_flags]`: new `intro` key + 1 disqualifier item per D5.
- No `[checklist]`, no `weight_incubation`, no `anchors_incubation`, no
  `[banding.incubation]`.
- `[meta]`: version 3.0.0, date 2026-08-27, source extended, changelog entry prepended.
- Header comment rewritten (single scale, evidence lists, validator summary).

### Task 2: `src/services/blackbird_rubric.py` — parse/validate/score/render v3, freeze v1/v2 for display

**Files:** Modify: `src/services/blackbird_rubric.py`

- `_EXPECTED_DIMENSION_COUNT = 6`; RubricDimension: `key, weight, title, anchors,
  evidence: tuple[str, ...] = (), specialist: str | None = None` (drop the incubation pair).
- Rubric dataclass: drop `advance_min_incubation`, `conditional_min_incubation`,
  `checklist_intro`, `checklist`; rename `banding_incubation_semantics` →
  `banding_semantics` (now on `[banding]`); add `red_flags_intro`.
- parse_rubric: validate as before minus incubation/checklist; `evidence` optional
  list of non-empty strings; `[red_flags].intro` required; weights sum to 100 (one scale).
- Frozen legacy tables (module constants, with provenance comments):
  `_FROZEN_V2_WEIGHTS`, `_FROZEN_V2_WEIGHTS_INCUBATION` (the 13-dim dicts, display
  order), `_FROZEN_V2_BANDING` (4.0/3.0), `_FROZEN_V2_BANDING_INCUBATION` (3.4/2.7).
- `weighted_score(scores, stage=None)` / `band(score, stage=None)`: single scale;
  `stage` accepted and ignored for scale selection (D10); docstrings updated;
  `_round_for_band` single threshold pair.
- Delete exports `RUBRIC_WEIGHTS_INCUBATION`, `BANDING_INCUBATION` (update importers).
- `display_scale_for(rubric_version, funnel_stage)`: major ≥3 → live scale, label
  "rubric v3 scale"; major 2 + incubation stage → frozen v2 incubation; everything
  else → frozen v2 investment. `scored_stage_aware` unchanged; add `_major_version`.
- `render_rubric_markdown()`: 4-column table; one banding line + semantics; advisory
  note; per-dimension "**Evidence to look for — {title}:**" bullet blocks after the
  table; red flags section = intro + items; sections renumbered 1–5.

### Task 3: `src/agent/specialists.py` — retarget ownership, mark legacy triggers

**Files:** Modify: `src/agent/specialists.py`

- `maps_to_dimensions` per D8; docstrings/comments updated (the 2026-08-22 census note
  becomes history; the "no dimension twice" rule still holds).
- `required_domains_for`: keep `ip_fto`/`platform` score reads, commented as
  legacy-verdict-only (D7); cue tiers unchanged.

### Task 4: sidecar + prompts + guidance

**Files:** Modify: `prompts/roles/scout_hub/phase4-thread-reply.md`,
`src/agent/thread_guidance.py` (scout_hub DECIDE string only),
`docs/specs/2026-08-07-hub-bot-prompts.md` (regenerate embedded blocks)

- Skeleton `scores` = the 6 new keys; "thirteen keys" → "six keys"; the 40%/34%
  sentence → single-scale sentence.
- Numbered list: fold old items 3–6 (market / external signals / platform / capital
  efficiency) into the dimension-scoring item; red-flags item rewritten per D5
  (max 3, disqualifier-grade, concerns → rationale); milestones item capped at 5 (D9).
- thread_guidance scout_hub DECIDE: replace "target-level scientific checklist in your
  rubric — clinical genetic evidence, …" with the evidence-lists phrasing.
- Regenerate the hub doc's verbatim blocks (test_doc_prompt_sync is the gate).
- Check `agent-system.md` for stale references (grep: checklist/thirteen/40%) — none
  found in recon; verify.

### Task 5: read-path consumers

**Files:** Modify: `src/services/directory.py`, `src/services/assessment_detail.py`
(only if it names removed exports), `templates/admin/_assessments_body.html`,
`templates/admin/_assessment_detail_body.html`

- directory.py: drop `RUBRIC_WEIGHTS_INCUBATION`/`BANDING_INCUBATION` imports;
  `dimension_stats` single `weight` (drop `weight_incubation`); context keys
  `rubric_weights_incubation`/`banding_incubation` removed; `row_scales` unchanged.
- Templates: remove dual-weight chips/legend branches; keep per-row `row_scales`
  rendering (it now serves frozen legacy scales); comments updated ("thirteen" → "the
  document's dimensions").

### Task 6: `scripts/render_rubric_review_doc.py` + regenerate `docs/rubric-review/`

- Single-scale rendering; evidence lists; red-flags intro; STRUCTURAL tags updated
  (six dimension keys, four thresholds → two).
- Regenerate the review doc (new filename carries the new hash).

### Task 7: tests

**Files:** Modify: `tests/unit/test_rubric_document.py`, `tests/unit/test_rubric_prompt_sync.py`,
`tests/unit/test_blackbird_rubric.py`, `tests/unit/test_specialists.py`,
`tests/unit/test_thread_guidance.py` (if it pins the DECIDE string),
`tests/integration/test_stage_aware_scoring.py` (becomes the frozen-display +
stage-ignored-scoring test), plus any fixture fallout in
`tests/integration/test_opportunity_assessment_persistence.py`,
`test_assessment_detail_page.py`, `test_hub_assessment_capture_gate.py`,
`test_specialist_consult_capture.py`, `test_missing_domains_null_storage.py`,
`test_manager_views.py`, `tests/unit/test_specialist_floor.py`,
`tests/unit/test_assessment_sidecar.py`, `tests/unit/test_panel_state.py`.

- New characterization pins: 6-dim weights/order, 3.4/2.8, version "3.0.0", gating keys.
- Validator tests: wrong-count now 6, evidence validation, red_flags intro, single-sum.
- Sync tests: skeleton==6 keys, "six" phrase, science block == 35 and prose says "35%".
- Stage-aware tests: `weighted_score(scores, "incubation") == weighted_score(scores)`;
  `display_scale_for("2.0.0", "incubation")` returns frozen 3.4/2.7 incubation table;
  `display_scale_for(None/"1.0.0", *)` returns frozen 4.0/3.0 investment table;
  `display_scale_for("3.0.0", anything)` returns the live scale.

### Task 8: docs + gate

**Files:** Modify: `CLAUDE.md` (rubric bullet: checklist mention → evidence lists;
regime note), run `./scripts/ci.sh` on the host, fix all fallout.

- CI must be fully green (ruff + alembic sanity + full pytest with coverage floor).
- NOT in scope: committing (operator decision), image rebuild/restart (deploy step —
  the running agent keeps v2.2.0 until rebuilt+restarted; a blackbird-app restart
  before the images are rebuilt is SAFE only after this lands consistently, since the
  bind-mounted document and the baked-in code must change together — deploy both or
  neither).

---

## Addendum: v3.1.0 — funnel-stage classification removed (2026-08-27, operator-directed)

The four-way funnel classification (Incubation/Grant, Pre-Seed/Formation, Seed,
Follow-on — Blackbird's instrument/memo classes, PDF Part C.2) is removed from the
rubric, the sidecar, and every hub prompt. Justification: zero measured entropy
(51/51 v2.x production verdicts = "incubation"; three of four values unreachable from
a PI interview by construction) and, since 3.0.0, no arithmetic role. Its two live
functions survive without it: the incubation-grain evidence bar moved into
`[scoring].preamble`, and the already-a-company escape hatch is carried by the
instrument question (non-dilutive incubation grant vs equity) the concluding contract
already asks. Touched: TOML (`[funnel]` deleted, intro 4 steps), `blackbird_rubric.py`
(dataclass/parse/renderer), phase-4 contract (item deleted, skeleton field deleted,
renumbered 6 items), `agent-system.md` (6 rewrites), `_SCOUT_HUB` EXPLORE + both
CONCLUDE strings (inline verdict is now four things), CLAUDE.md bullet +
`test_claude_md_disclosure_sync.INLINE_FIELDS`, hub-doc mirrors regenerated,
`test_stage_aware_scoring.py` → `test_verdict_scoring.py`. Deliberately kept: the
`opportunity_assessments.funnel_stage` column (nullable; engine still passes through
whatever a verdict carries; nothing reads it), the templates' Stage cells (77 legacy
rows still show theirs), and the GM-pinned `_PI_LAB` EXPLORE sentence asking PIs to
guess a stage — benign residual, requires a deliberate golden-master regeneration to
remove. Removal of stakeholder taxonomy is flagged in the changelog and visible in the
v3.1.0 review copy.

---

## Addendum: v3.2.0 — content-audit cuts (2026-08-27, operator-directed)

Four ceremonial elements removed: (1) `weighted_score` out of the sidecar skeleton
(existed only to be ignored; production models filled it in anyway); (2)
`suggested_derisking_milestones` out of the sidecar and contract (overlapped
`recommended_next_experiment`, production mean 7.8 entries; go/no-go criteria beyond
the funded experiment now live in `rationale` as experiment/readout/threshold); (3)
`[banding].pass_note` (redundant with the advisory note + recommendation semantics);
(4) `[banding].vocabulary_note` (never rendered; folded into a comment).
`[Speculative]` was a WATCH item, not a cut candidate: it is the only low-confidence
channel and has non-zero production use (3/77) — kept, deliberately. Engine keeps
tolerant passthrough writes for stray `funnel_stage` / `suggested_derisking_milestones`
fields (test-pinned); `weighted_score`/`band` columns are computed and load-bearing,
not residuals.

## Residuals plan (all known residuals, with the adversarial audit that verified it)

**R1 — pi_lab EXPLORE still asks PIs to guess "which stage of Blackbird's funnel".**
GM-pinned (CLAUDE.md: do not reword; never `--snapshot-update` to hide a mismatch), so
this is a deliberate, reviewed change, not a quiet edit:
1. Reword the one sentence in `_PI_LAB[EXPLORE]` to the instrument framing ("which
   Blackbird instrument you think it could be a candidate for — a de-risking grant or
   equity").
2. Regenerate `tests/characterization/__snapshots__/test_agent_turn_gm.ambr` with a
   targeted `--snapshot-update`, then review the diff: the sentence appears at exactly
   **9** pinned sites (audited: `grep -c "Blackbird's funnel"` = 9, single source
   string); ANY other hunk in the diff aborts the change.
3. Regenerate `docs/specs/2026-08-07-pi-bot-prompts.md` §4 (test_doc_prompt_sync pins
   it to `thread_guidance`).
4. Update CLAUDE.md's "byte-identical / do not reword" bullet to record the reviewed
   regeneration.
Until executed, the residual is benign: the hub ignores any stage talk, so a PI's
guess is inert prose.

**R2 — the 80 stored verdict rows (77 pre-v3 + 3 v3.0.0).** Ready to execute on
operator go, in one transaction after a table backup
(`pg_dump -t opportunity_assessments`):
- `DELETE FROM opportunity_assessments WHERE rubric_version IS NULL OR
  rubric_version LIKE '1.%' OR rubric_version LIKE '2.%';`  (77 rows)
- The 3 `3.0.0` rows are the operator's call: same weights/thresholds as 3.2.0
  (comparable), but their sidecars carried since-removed fields.
Audited safe: `pg_constraint` shows **zero** inbound foreign keys to
`opportunity_assessments`; `assessment_drops` (23) and `specialist_consults` (996) are
keyed by run/thread, not FK — KEEP them as independent history (consults orphaned by a
purge are simply unreferenced; optionally purge by pre-v3 `simulation_run_id` in the
same transaction). `_rehydrate_assessed_threads` is run-scoped to the CURRENT run —
unaffected. Expected page effects: "all runs" views show only v3 rows; the
unvetted-panel banner count drops to v3-only.

**R3 — residual columns and template cells.** `funnel_stage` and
`derisking_milestones` columns stay (nullable, zero cost, tolerant passthrough is
test-pinned). The three reader sites (audited: exactly `_assessments_body.html:237`,
`_assessment_detail_body.html:117` Stage cells, `_assessment_detail_body.html:318-322`
milestones block; no service code reads either column) become permanently empty only
once R2 removes every row that carries values — delete the three template blocks THEN,
not before. Dropping the columns (a 0038) is NOT recommended: it requires the
reverse deploy order (ship unmapping code first, then migrate — old code against the
post-drop schema raises UndefinedColumn on every `select(OpportunityAssessment)`) for
zero storage benefit.

**R4 — historical mentions in `[meta].changelog` and this plan.** Deliberate keeps:
they are the record Blackbird's reviewers see of what was removed and why.

Adversarial audit performed (2026-08-27): reference sweep over src/prompts/scripts/
templates finds removed names only in the changelog, the two documented passthrough
reads (`simulation.py:3659`, `backfill_dropped_verdicts.py:419` — a one-shot
historical script), and an unrelated chemistry-cue phrase ("development-candidate
milestone"); the rendered rubric and composed hub prompt contain none of them
(asserted by tests); skeleton keys = 6 scores + 3 gating + 5 scalars, all load-bearing;
stray-field tolerance is pinned by test_verdict_scoring and the persistence recompute
test.

## Residuals plan: EXECUTED 2026-08-27 (operator-directed), with audit corrections

**R1 (funnel language in pi_lab surfaces)** — executed, and the plan's own audit claim
was FALSIFIED and corrected in the process: the 9 GM-snapshot occurrences were NOT one
source string. Actual sources, all now rewritten to the instrument framing (de-risking
grant vs equity): `_PI_LAB[EXPLORE]` in thread_guidance.py (1), the global pi_lab
`prompts/agent-system.md` ("### The funnel" section → "### The two instruments", core
rule 3, the EXPLORE bullet, the retrieve_profile note), `prompts/phase5-new-post.md`
(the stage bullet), `prompts/phase4-thread-reply.md` (the retrieve_profile note), and —
missed entirely by the original plan — the LIVE `profiles/public/blackbird.md` (root-
owned; "## The funnel (the single most important structural fact)" → the two-instrument
table; replaced via sudo cp with ownership/mode preserved). GM snapshot regenerated
(4 snapshots updated, 259 changed lines) and audited: every changed line belongs to the
funnel→instrument rewrite; zero "funnel" occurrences remain in the .ambr. Both prompt-set
doc mirrors regenerated; CLAUDE.md's pi_lab-pin bullet now records the reviewed
regeneration.

**R2 (row purge)** — executed: table backed up first
(`backups/opportunity_assessments_pre_purge_1787862739.dump`, pg_restore-verified),
then one transaction deleted all 82 pre-3.2.0 rows (NULL/1.x/2.x/3.0.0; no 3.1.0 or
3.2.0 rows existed). opportunity_assessments now holds 0 rows; assessment_drops (24)
and specialist_consults (1029) kept as independent history per the plan.

**R3 (readers of the residual columns)** — executed: the list page's Stage column
(th+td), the detail page's "· Stage:" line, and the detail page's de-risking-milestones
block are removed; the columns and the engine's tolerant passthrough stay (test-pinned
by test_verdict_scoring and the persistence recompute test). No 0038; columns retained
per the plan's recommendation.

**R4 (changelog history)** — kept, as planned.

Post-execution sweep: the only remaining "funnel" tokens in live code/prompts/templates/
profiles are the retained column mapping (src/models/opportunity.py), the documented
passthrough (simulation.py), the TOML changelog record, an unrelated English idiom
("funnel through", simulation.py:3340), the Sankey chart script's name, and the
historical backfill script. The rubric document was unchanged by this execution
(version 3.2.0, hash 42aec0479ac6); the review copy was regenerated anyway and the hash
confirmed identical.

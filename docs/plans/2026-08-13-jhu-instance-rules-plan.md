# JHU Instance Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the pipeline itself enforce the JHU instance policies that are currently
live only at the data level, so that a new signup, an onboarding retry, or a
`regenerate-profiles` run *preserves* JHU-IP-scoped, individually-authored,
consortium-free profiles instead of silently reverting them.

**Spec:** `docs/specs/2026-08-13-jhu-instance-rules-design.md`
**Prerequisite:** the parent plan's Tasks 1–8
(`docs/plans/2026-08-13-pi-profile-coverage-plan.md`) — this plan extends
`src/services/corpus.py` and the pipeline seams created there. Its Global Constraints
(ruff ratchet headroom, imports at top, snapshot rules, host pytest) apply verbatim.

**Reference implementations (session-proven, 2026-08-13):** the verification matcher,
candidate classifier, and tenure filter below are lifted from the scripts that produced
the current prod state (they ran against all 2,729 stored pairs and the 51+4-paper
refills with zero attribution errors). The code blocks in this plan are those proven
versions — port them, don't rewrite them.

---

### Task J1: Tenure-map accessor + affiliation data fix

**Files:**
- Create: `src/services/jhu_rules.py`
- Test: `tests/unit/test_jhu_tenure_map.py` (create)
- Data (one-off, via psql): populate `users.institution = 'Johns Hopkins University'`
  for the two NULL-affiliation PIs (`mukherjeeclavin`, `pearce`) — verified correct from
  their own papers' affiliation strings.

**Interfaces:**
- `async get_tenure_map(db) -> dict[str, int]` — reads `app_settings` key
  `jhu_tenure_start` (JSON `{agent_id: year}`; already populated with 62 entries on
  prod). Returns `{}` when the key is absent (a non-JHU deployment must behave exactly
  as before — every rule in this plan is a no-op without the map).
- `tenure_filter(pubs: list[dict], start: int | None) -> list[dict]` — pure; keeps
  `p["year"] >= start`; **undated papers are excluded** when a start is set (IP
  presumption requires a date); identity when `start is None`.

- [ ] Failing tests: map parse, missing-key → `{}`, filter drops pre-tenure and undated,
      `start=None` identity.
- [ ] Implement; `.venv-test/bin/python -m pytest tests/unit/test_jhu_tenure_map.py -v`.
- [ ] Apply the two-row `users.institution` update (prod + local copy), then commit.

### Task J2: Author-verification module (R1) shared by resolver and refill

**Files:**
- Modify: `src/services/corpus.py` (after parent Task 4)
- Test: `tests/unit/test_author_verification.py` (create)

**Interfaces:**
- `fold_name(s) -> str` — NFKD→ASCII, lowercase, `ue`→`u`, strip `- ' `/spaces.
- `classify_authorship(rec, name, affiliations) -> str` returning
  `INDIVIDUAL_STRONG | INDIVIDUAL_NAME | CONSORTIUM | NO_MATCH`, where `rec` carries
  `authors=[(last, fore, initials, [affs])]` and `collectives=[...]` parsed from efetch.
  Surname legs: equality after folding, or ≥4-char containment either direction
  (`vandang`⊇`dang`, `mariehardwick`⊇`hardwick`). First-name leg treats 1–2-letter
  `ForeName` as initials. Affiliation leg reuses `INSTITUTION_STOPWORDS` distinctive
  tokens.

Test fixtures must include the session's proven hard cases: `Müller U` vs "Ulrich
Mueller", `Van Dang C` vs "Chi Dang", `Marie Hardwick J` vs "J. Marie Hardwick",
`Kavran J M` (1998 record) vs "Jennifer Kavran", a GTEx-style collective-only record
(→ CONSORTIUM), and `R Lara Green` vs "Rachel Green" (→ NO_MATCH).

- [ ] Failing tests → implement → pass.

### Task J3: Enforce R1 + R3 in corpus selection

**Files:**
- Modify: `src/services/corpus.py` — `resolve_corpus` gains a verification stage
- Test: `tests/unit/test_corpus_jhu_rules.py` (create)

Behaviour (mirrors the proven refill walk): after dedupe/dating/ranking and **before**
the cap, efetch candidate records most-recent-first in batches and admit only
candidates that (a) are not `EXCLUDED_TYPES` (pubtypes from the same efetch — do not
let editorials take slots), (b) classify as `INDIVIDUAL_*`; `CONSORTIUM` is skipped and
counted; `NO_MATCH` is skipped and **reported on `CorpusResult.flagged`** (new field)
for the review queue — never stored, never silently dropped from the report. Walk until
the cap is filled or candidates are exhausted.

- [ ] Failing tests with a fake efetch: consortium candidate skipped, editorial
      skipped, NO_MATCH lands in `flagged`, cap fills from the next verified candidate.
- [ ] Implement; full suite; commit.

### Task J4: Tenure scoping in profile synthesis (R2)

**Files:**
- Modify: `src/services/profile_pipeline.py` (after parent Task 5)
- Test: `tests/unit/test_pipeline_tenure_scope.py` (create)

At step 7, when `get_tenure_map(db)` has an entry for the user's agent:
`pubs_for_synthesis = tenure_filter(pubs_for_synthesis, start)` and the evidence counts
are computed from the filtered set. **Storage is not filtered** — the full verified
corpus continues to be persisted (flag-not-delete). The GM fake for `resolve_corpus`
gains a fake tenure map seam the same way (`profile_pipeline.get_tenure_map`).

**The export list must be filtered too — this was missed once already.** The agent
runtime embeds the entire `profiles/public/*.md` in every system prompt, including the
"Recent Publications" top-20, and the simulation shares that section across labs. The
first R2 deployment filtered synthesis but exported the full-corpus list; 9 of 62
agents (recent recruits — `oneal` 17/20, `hart` 15/20, `pombo` 11/20) carried
pre-tenure papers in their prompts until re-exported on 2026-08-14. The pipeline's
step-9 export call must pass the tenure-filtered publication list whenever the map has
an entry, and a test must pin it (export for a mapped user contains no pre-tenure
year).

- [ ] Failing test: with a map entry, pre-tenure abstracts never reach the synthesis
      context and `evidence_pub_count` reflects the filtered set; without a map entry,
      behaviour is byte-identical to parent-plan behaviour.
- [ ] Implement; deliberate GM regeneration if the snapshot legitimately changes
      (scoped, reviewed — per parent plan's rules); commit.

### Task J5: Distinctive-surname S4 exception (R4) — gated

**Files:**
- Modify: `src/services/corpus.py`
- Test: `tests/unit/test_distinctive_surname.py` (create)

When affiliations are empty OR the affiliation search returns nothing, probe
`esearch("{Last}[Author]", retmax=0)` for the total count; if `count < 100`, run the
name-only search as an S5 source. Every S5 hit passes Task J3 verification
individually — the exception widens *discovery*, never *admission*.

- [ ] Tests: common surname (count ≥ 100) → S5 skipped; distinctive surname → S5 hits
      flow through verification; verification still rejects a homonym record.
- [ ] Implement; commit.
- [ ] **Operational (requires explicit sign-off):** run the refill for
      `mukherjeeclavin`, `srinivasan`, `markham` with S5 enabled; audit every addition;
      regenerate their profiles (tenure-scoped); ship via a rehearsed, fingerprint-gated
      apply per the runbook below.

### Task J6: Coverage-gate awareness (ties into parent P2/P3)

- `coverage_suspect` assessment treats NULL `users.institution` as a flag condition
  (missing affiliation silently halves discovery — the Mukherjee-Clavin failure mode).
- Admin assessment view surfaces per-PI: tenure_start + source (orcid/paper), JHU-era
  evidence count vs stored count, and any `CorpusResult.flagged` items.

---

## Deployment runbook (the pattern that shipped all six 2026-08-13 applies)

Any future data change to prod follows this, no exceptions:

1. Branch-point template locally: `CREATE DATABASE simN TEMPLATE copi` **before**
   changing the local copy.
2. Apply the change to the local `copi`; audit it there (attribution checks on every
   added row; regen validation).
3. Compute pre/post fingerprints (publications: id|user|pmid|doi|title|abstract|journal|year;
   profiles: the 13-column digest over active `pi_lab` rows, ORDER BY fixed).
4. Generate a single-transaction `applyN.sql`: `ON_ERROR_STOP`, `EXCLUSIVE` locks,
   gates = pre-fingerprints (drift → abort), staging sanity, scoped DML, **post-fingerprint
   equality with the audited local state**, semantic invariants. All failures RAISE →
   rollback.
5. Rehearse on the template; then run it a second time and confirm it **aborts** at
   GATE1 leaving the state unchanged.
6. Prod: fresh table-scoped `pg_dump` → load staging → run applyN → export affected
   `profiles/public/*.md` + `create_revision` via the app container → verify → drop
   staging.

Never: whole-database restores onto prod, DML outside the gated transaction, profile
columns beyond the regenerated set (PI-authored fields are untouchable), or starting
the simulation as part of a data deploy.

## Self-review

R1→J2+J3. R2→J1+J4. R3→J3. R4→J1 (data fix) + J5 + J6. R5 lives in the parent plan
(Tasks 1,4,8 as revised). Nothing in this plan mutates stored publications except J5's
gated operational refill; everything else is selection/synthesis-time behaviour keyed
on `app_settings`, so a deployment without the key runs the parent-plan behaviour
unchanged.

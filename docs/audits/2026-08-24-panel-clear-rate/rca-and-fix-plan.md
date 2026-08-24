# RCA and fix plan for the adversarial-review findings

**Scope:** the four defects found by `adversarial-review.md` in this directory's
README. Each root cause below was verified against the working tree at `3cdb7f5`,
the production database, and `logs/blackbird_run_1787589543.log` — not inferred.
**Status:** analysis only; none of the fixes below has been applied.

The meta-pattern first, because it is the actual root cause of the document-level
defects: **every one of the four is "measured one representation, reasoned about
another."** Recommendation vs band (B), stored scores vs as-scored values (D),
in-process tally vs table rows (C), exact rubric version vs version family (A).
The README's own §9 discipline — no number without its query printed — prevented
this everywhere it was applied; both substantive errors sit exactly at the two
claims that entered the document *without* a printed query.

---

## A. §7's rebaseline-checkpoint claim ("this run contributed the first 9")

**Defect.** README §7 says the §7.3 re-check (≥20 v2-stamped verdicts) is "now
within reach" because this run contributed "the first 9". Actual: 47 v2-stamped
verdicts exist (38 × rubric 2.0.0 from 2026-08-21/22, 9 × 2.1.0), the threshold was
crossed on 2026-08-22, and the v2.1.0 commit states "no weight, threshold or scale
changes" — so 2.0.0 rows are v2 data in exactly §7.3's sense.

**Root cause chain.**
1. *Proximate:* "v2-stamped" was read as "v2.1.0-stamped". The run's startup banner
   (`Screening rubric: version 2.1.0`) and the README's own framing ("first run on
   rubric v2.1.0") anchored "v2" to the version this run happened to use.
2. *Contributing:* §7 is the only quantitative claim in the README with no printed
   SQL. It was imported from another document (the proposal) rather than measured,
   and the §9 discipline was applied only to measured claims. One
   `GROUP BY rubric_version` falsifies it.
3. *Latent:* version-family ambiguity. The checkpoint is written in family terms
   ("v2") while every surface an operator sees — banner, `rubric_version` stamps —
   shows exact versions, and nothing anywhere records "2.0.0 went live 2026-08-21
   and counts toward §7.3".

**Fix.**
1. README §7: correct the paragraph; print the query and the 22/4/38/9 version
   table; change the implied action from "wait for the checkpoint" to "the
   checkpoint is passed — the §7.3 back-test is due."
2. Actually run the §7.3 back-test (the real action this unblocks): re-run the
   proposal's separation analysis on the 47 v2 rows, stamp-split 2.0.0 vs 2.1.0
   (2.1.0 reworded some anchors), dedup to latest-per-thread per proposal §1.
   Whether the band lines then move is the owner's tuning decision, not part of
   this fix.
3. Do **not** pre-annotate the rubric toml comment; update it when the re-check is
   actually run, so it can say what happened rather than predict.

## B. §5's "advance and conditional have never been written, in any run, ever"

**Defect.** False for the `band` column: six rubric-2.0.0 verdicts banded
`conditional` (2 recommendation=route-to-incubation, 4 recommendation=pass, scores
2.7–3.29). True only of the `recommendation` column and of band `advance`.

**Root cause chain.**
1. *Proximate:* §5's query selects `recommendation` only; the prose then reasons
   about *bands* ("Incubation banding is advance ≥3.4 … leung misses conditional by
   0.10") without `band` ever appearing in a SELECT.
2. *Latent:* a deliberate vocabulary collision. The sidecar's recommendation enum
   (`"advance | conditional | pass | route-to-incubation"`,
   `phase4-thread-reply.md:261`) reuses the band names, so an unqualified "advance
   and conditional have never been written" is ambiguous by construction. The
   schema keeps the two columns separate precisely because they diverge — and they
   do, in 4 production rows (recommendation `pass`, band `conditional`).

**Fix.**
1. README §5: restate precisely — "the model has never *recommended* advance or
   conditional; no verdict has ever *banded* advance; six 2.0.0 verdicts banded
   conditional" — add `band` to the printed query, and note the 2.0.0 (6/38) vs
   2.1.0 (0/9) conditional-band delta as an input to fix A.2's stamp-split (n=9 is
   far too small to call a regression on its own).
2. Writing rule, not code: when a sentence uses band-vocabulary words, name the
   column. No schema or enum change — the shared vocabulary is a design choice
   (bands map to actions; the recommendation *is* an action) with a recorded
   rationale.

## C. The 228 vs 229 discrepancy (§8.1), and the alarm that can't explain itself

**The discrepancy itself is by design, not a bug.** A truncated consult fires
`on_consult_record` (durable row, `truncated=TRUE`; `tools.py:728-750`) but skips
`on_consult` → `_note_consult` → `_consult_signal_counts` (the `elif` at
`tools.py:721-722`). Run `ee419dd3` had exactly one truncated consult (legal,
16:26:42 — the same event as the defaulted parse), so 229 rows = 228 counted + 1
recorded-but-uncounted. The denominator semantics are **correct** and must not
change: an unread specialist has cleared nothing, the floor refuses the row
(`_seed_consults_from_db` excludes `truncated IS TRUE`), and the alarm should
count exactly what the floor credits.

**Root cause of the doc gap.**
1. *Proximate:* a targeted grep (`DEFAULTED to`) quoted log line 1957 and never
   read line 1958 — the ERROR line that states the answer verbatim ("recorded, but
   NOT counted as consulted"). Separately, §2.3 had already recorded "1 truncated"
   and the two facts were never joined (gathered for different sections).
2. *Latent:* the alarm message reports a bare denominator ("2 of 228 consults")
   with nothing marking that its counting basis differs from the table's row
   count. The reconciliation knowledge lives only in a code comment and one ERROR
   log line, so every operator who cross-references the DB re-derives it.

**Fix.**
1. README §8.1: replace the open question with the resolved mechanism and the log
   line.
2. Code, one word: `clear_rate_warning`'s message says "counted consults" instead
   of "consults". The existing tests assert only the substring `"clear"` and the
   denominator digits (`tests/unit/test_specialists.py:779-784`), so they survive;
   add one assertion pinning the new word so it can't silently revert.
3. Comment at the emit site (`simulation.py:1178`): "`specialist_consults` rows
   for the run may exceed this tally by the number of truncated consults —
   recorded, deliberately never counted (tools.py)."
4. Anti-fix, named so nobody does it: do not add truncated rows to the tally.

## D. Out-of-scale zeros and the silent [1,5] clamp (§5.1)

**Defect (doc).** The README's per-dimension means average *stored* JSONB values,
but `weighted_score` clamps every finite score to the rubric scale [1,5]
(`scale_min = 1`; pinned by
`test_out_of_range_scores_are_clamped_to_one_through_five`). The three stored 0s
(huganir `chemistry_dc_path` + `toxicity_selectivity`, egeblad `chemistry_dc_path`)
therefore *scored* as 1: as-scored means are ≈1.67 (not 1.44) and ≈2.44 (not 2.33).

**Root cause of the zeros existing at all: the prompt contract teaches 0 three
ways while forbidding it once.** In `phase4-thread-reply.md`: the
`<assessment_json>` skeleton pre-fills **all 13 scores with `0`** as the
placeholder; the text says "leave [`weighted_score`] at 0"; and "a key you omit
scores zero" frames 0 as the no-evidence value — against a single "Score each
dimension 1–5". A model with genuinely no evidence for a dimension (chemistry in a
pitch with no compound — huganir, egeblad) emits the placeholder/no-evidence 0.
The code then treats that explicit 0 as an ordinary out-of-range number and clamps
it **up** to 1, silently — the opposite direction of both plausible model intents,
with the paradox that writing 0 outscores omitting the key (1×weight vs 0×weight).

**Fix (layered, smallest first).**
1. README §5.1: caption the table as means of stored values; name the three
   clamped cells and the two corrected means; note the inference actually
   *strengthens* the section (a present-0 drags less than an omitted key).
2. Prompt, one sentence next to the skeleton: "`0` is a placeholder, not a score —
   every dimension must be 1–5; if you truly cannot assess one, score it 1 and say
   why in `rationale`." This aligns the model with the clamp's existing, pinned
   behavior instead of changing scoring. Deploy note: `_load_prompt` →
   `_load_file` → `read_text` per call, no cache (`agent.py:274`, `:612`), and
   `prompts/` is bind-mounted — effective on the hub's next phase-4 turn, no
   rebuild, no restart. Confirm `test_rubric_prompt_sync.py` still passes.
3. Observability, small code change: warn when a finite numeric dimension score is
   out of scale. Safe to put inside `weighted_score` itself — its only callers
   outside `blackbird_rubric.py` are the engine's write/capture paths
   (`simulation.py:3372`, `:4741`); every display page reads the stored column
   (sole other reference: `directory.py:109`, an ORDER BY) — so it fires at most a
   few times per verdict, never per page view. Add a test alongside the clamp test.
4. Explicitly deferred to the owner (scoring semantics = tuning): whether an
   explicit 0 should count as 0 like an omission, rather than clamp to 1. Both
   choices are defensible; changing it re-scores history's edge cases and belongs
   with the §7.3 re-baseline (fix A.2), not here.

## Also fold into the README pass (no separate RCA needed)

* §8.4 → answered: 24 blocking signals spread over 14 of 17 consulted subjects
  (max 4); the "it discriminated, just downward onto a few" escape hatch is closed.
* §3.1 → one sentence: the defaulted row "agrees" with its raw JSON only
  coincidentally (the truncated text happens to begin with the caution prefix);
  the check cannot distinguish parsed from defaulted-with-surviving-prefix.
* §1 → column order corrected (`…, raw_opinion, created_at, truncated`).
* §9 → extend the discipline one clause: "…including claims imported from other
  documents", which is the process fix for A and the class it belongs to.

## Execution order

| step | change | risk / deploy consequence |
|---|---|---|
| 1 | README corrections (A.1, B.1, C.1, D.1, the fold-ins) | none — docs only |
| 2 | `clear_rate_warning` wording + emit-site comment + test (C.2, C.3) | `src/` change → `./scripts/ci.sh`, agent image rebuild before next run; flag restart to owner |
| 3 | out-of-scale score warning + test (D.3) | same deploy batch as step 2 |
| 4 | phase4 prompt sentence (D.2) | bind-mounted, effective next turn; run prompt-sync tests |
| 5 | run the §7.3 back-test (A.2) | analysis task; any band re-cut is the owner's call |

Steps 2–4 are independent of each other; step 5 is the only one that changes what
anyone decides, and steps 1–4 exist so it is decided on correct premises.

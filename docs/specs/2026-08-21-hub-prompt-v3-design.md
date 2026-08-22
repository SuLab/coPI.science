# Design: hub prompt-set v3 — incubation-only scoring, N/A dimensions, and commercial diligence tools

**Date:** 2026-08-21 · **Status:** DESIGN, approved section-by-section in conversation;
§7 and §8 were written after approval and have not been separately reviewed.
**Ground truth:** the 2026-08-21 hub prompt set supplied by the user supersedes the
prompt text in the repo. Where that document and this design disagree, this design
records a deliberate, agreed deviation and says so.
**Precondition:** the simulation is **stopped** and stays stopped until this lands, so no
further verdicts accrue under the regime being retired.
**Baseline for comparison:** the frozen v2.0.0 sample, n=16 — scores 1.94–3.29 (median
2.41), 0 advance / 3 conditional / 13 decline under 3.4/2.7, model verdicts 15 `pass` +
1 `route-to-incubation`, `external_signals` ≤1 in 15/16, `exit_thesis` ≤1 in 8/16,
`chemistry_dc_path` ≤1 in 2/16, `toxicity_selectivity` ≤1 in 0/16.

---

## 1. Decisions (all agreed in conversation)

| # | Decision |
|---|---|
| D1 | The v2 rubric (TOML) supersedes the weight table printed in the prompt document. Focus is incubation and de-risking |
| D2 | Collapse to a **single incubation scale**; retire the investment scale from scoring; keep its weights frozen for rendering legacy rows |
| D3 | Explicit **N/A** on an eligible subset with renormalization; `exit_thesis` N/A-by-default at incubation |
| D4 | Gating keeps **three keys**; translational potential lives in `rationale`; `fto_achievable` becomes a diligence flag |
| D5 | **Panel notes stay** — PIs do not read the threads, Blackbird assessors do. The prompt sentence is corrected, not the code |
| D6 | `commercial` **always** required; `legal` cue-triggered on IP-claim language / `ip_fto >= 4` |
| D7 | `recommended_next_experiment` required for advance/conditional/route-to-incubation, enforced by **flagging, never refusing** |
| D8 | N/A renormalization and the new anchors land together as one regime, `3.0.0`, with one re-baseline afterward |
| D9 | Consult cap: correct the prompt sentence; do not raise `max_tool_rounds` (never hit in production) |
| D10 | `band` demoted to an advisory secondary in the UI; `recommendation` stays primary |
| D11 | Add ClinicalTrials.gov now **and** a vendor-agnostic web-search interface, inert until configured |
| D12 | Update the **lab-facing** funding figures too, via a reviewed golden-master regeneration |
| D13 | Keep the document's four-domain split: commercial, legal, clinical, budget are hub diligence; scientific, chemistry, technologic, talent generate lab questions |

## 2. Scoring regime `3.0.0`

### 2.1 Single scale
`RUBRIC_WEIGHTS` becomes the former incubation set, unchanged in value:

```
differentiation 16 · market_unmet_need 14 · team 12 · mechanism_validation 10
workplan_capital_efficiency 8 · chemistry_dc_path 8 · experimental_rigor 8
toxicity_selectivity 8 · platform 5 · ip_fto 4 · dev_regulatory_feasibility 3
external_signals 2 · exit_thesis 2                                    (= 100)
```

Deleted: `weight_incubation`/`anchors_incubation` as a *second* set (they become the only
set), `[banding.incubation]` as a second table, `weighted_score(scores, stage)` /
`band(score, stage)` stage parameters, `is_incubation_stage`, `display_scale_for`, and
the dual-column anchor rendering. Thresholds collapse to **advance ≥ 3.4, conditional
≥ 2.7**. Justification: 42 of 42 stored assessments are `incubation`; the investment
branch has never been exercised.

Retained: `LEGACY_INVESTMENT_WEIGHTS`, a frozen module constant (15/12/10/8/6/4/3/1/1 +
12/10/10/8), used **only** to render pre-`2.0.0` rows' score bars and weight tooltips.
Not part of the TOML, not used for scoring, never renormalized.

**Weights are deliberately NOT re-derived in this change.** `external_signals` at 2% and
`ip_fto` at 4% were set because their old anchors were unreachable; §2.4 makes them
reachable. Changing anchors and weights simultaneously would make the next sample
uninterpretable, so weights are re-derived from v3 data after ~20 v3 verdicts.

### 2.2 N/A with bounded renormalization

Eligible dimensions and their rules:

| dimension | `"n/a"` accepted | omission means | justification |
|---|---|---|---|
| `exit_thesis` | yes | **N/A** (default at incubation) | not required |
| `chemistry_dc_path` | yes | 0 | required in `rationale` |
| `toxicity_selectivity` | yes | 0 | required in `rationale` |
| other ten | **no** — logged as a warning and scored 0 | 0 | n/a |

Score = `Σ(wᵢ·sᵢ) / Σ(wᵢ)` over applicable dimensions only. Eligible weight totals 18, so
the denominator is always ≥ 82 and the maximum inflation N/A can produce is bounded. The
anti-gaming rule (missing counts as zero) is unchanged for the other ten dimensions —
that rule exists so a verdict cannot raise its mean by omitting its weakest dimensions,
and it is not relaxed. If every dimension were somehow N/A, `weighted_score` is `None`,
matching today's empty-`scores` behaviour rather than dividing by zero.

Representation is the string `"n/a"` inside the existing `scores` JSONB, so no schema
change is needed and the N/A set is inherently auditable per row. **The scorer must
handle it explicitly**: today a non-numeric value silently counts as zero, which would
turn every N/A into a stealth zero and defeat the change.

The N/A rule applies **at scoring time only**. Stored `weighted_score` and `band` values
are never recomputed, so the 42 existing rows keep the numbers they were scored with; the
back-test in §8 Window 2 is an offline comparison, not a rewrite.

Accepted limitation: the code cannot verify that a justification is *semantically* about
the dimension, so the prompt requires it and the stored `"n/a"` makes it auditable after
the fact. No validator rule attempts to police prose.

### 2.3 Gating semantics (three keys, D4)

| key | new meaning |
|---|---|
| `life_sciences_domain` | unchanged — therapeutic, diagnostic, or platform |
| `credible_tech_source` | **credible science**: the data can be believed; results internally consistent, methods described, each key claim's provenance statable. Institutional prestige is not the test and IP is not required |
| `fto_achievable` | **no longer a gate** — a diligence flag. `unconfirmed` by default; `not_met` only on a genuinely unresolvable blockade; `met` essentially unreachable by design |

Translational potential — the actual third gate — is recorded in `rationale` only.
Accepted, with the consequence stated: it is unqueryable, so its absence from the data
must not later be mistaken for absence of the judgement.

### 2.4 Semantic layer (all from the ground-truth document)

Anchors rewritten to score commercial dimensions **forward** against target class,
indication and modality rather than against today's snapshot of one unpublished result —
explicitly, absence of VC interest in a single academic result scores nothing down.
`ip_fto` becomes "what IP exists or is filable; encumbrances mapped; exclusivity where
relevant — observed, not required at this stage." The eight red flags are rewritten,
including the new "data that cannot be readily replicated for under $200K and a
reasonable timeline," the softened single-asset flag (only where a clean result would
still leave nothing worth building), and the explicit statements that unresolved FTO on
unpublished academic science and absent VC interest in one result are *not*
disqualifiers. `[funnel]` gains the almost-always-incubation note and the incubation bar
(scientific robustness + translational potential). The target-level checklist gains the
platform/diagnostic/device caveat directing that work to the technologic specialist. The
one-line heuristic is replaced with the ground-truth version. Banding carries the
advisory note. Funding bands become $100K–$1M / $300K–$1M / $1–5M and the
Maryland-specific programme list is generalised to "state and regional programmes
wherever the lab's institution is eligible."

### 2.5 Version and comparability
`[meta].version = "3.0.0"`, new content hash. New `changelog` line pointing at this
document. Every assessment continues to be stamped with version and hash; the frozen
v2 sample above is the before-picture for the post-v3 re-baseline.

## 3. Sidecar and schema — migration `0033` (additive, nullable)

- `opportunity_assessments.recommended_next_experiment` (Text, nullable) — the actionable
  line. Carries an experiment for `advance`/`conditional`; carries what must be resolved
  first for `route-to-incubation`. One column; semantics keyed by `recommendation` and
  documented on the model. Its cost/duration content is **the hub's own estimate**, not a
  lab commitment (D13 removes budget questions from the interview), and is labelled as
  such in the sidecar and the UI.
- `opportunity_assessments.thread_id` (String(50), indexed, nullable) — retires the
  fragile `slack_ts` → `agent_messages` join and makes "does a `blocking` signal predict a
  lower score on its mapped dimension?" answerable; that question is currently
  undetermined because the join resolved only 2 of 29 rows.

**The missing-experiment flag is derived, never stored:**
`recommendation IN ('advance','conditional','route-to-incubation') AND
recommended_next_experiment IS NULL`. Computed in the service so it cannot drift from its
source. Surfaced as a triage badge exactly like `panel_incomplete`. **Nothing is ever
refused** — this codebase has twice destroyed the verdicts a guard was meant to protect
(the specialist floor, fixed in `1a32e43`; the sidecar gate, fixed in `56b4fdc`), both
after the concluding reply had already been posted with no later turn to recover from.

Accepted limitation: diligence citations (NCT IDs, patent IDs, URLs) live in `rationale`
as prose, not a structured field, so "how often did diligence cite a source" needs a
later schema change to chart.

## 4. Specialist floor and personas

**`_ALWAYS` = {scientific, talent, commercial}.** Commercial joins because D13 makes
commercial diligence owed on every verdict and `differentiation` is both always scored
and the heaviest weight. Cost is near zero — the hub already consults commercial 67 times
voluntarily; the floor merely becomes able to enforce it.

**`legal` trigger** replaces the dead `gating.fto_achievable == "met"` (unreachable by
design after §2.3) with: `ip_fto >= 4`, **or** explicit encumbrance/licensing language —
`license`, `licensing`, `encumbrance`, `encumbered`, `co-inventor`, `co-owner`,
`co-ownership`, `material transfer`, `mta`, `exclusivity`.

**Deliberately excluded cues: `patent`, `fto`, `freedom to operate`.** The hub runs
`search_prior_art` routinely and reports the result in `rationale` ("a title-only US
search found nothing"), and §2.3 moves FTO reporting *into* `rationale` — so those tokens
appear in the hub's own boilerplate and would make `legal` always-required, which D6
explicitly rejects. A cue must not match the tool's own self-description. Short tokens
(`mta`) go in the word-anchored tier: this repo has already been bitten by `aso` matching
"reasons", `hit` matching "architecture" and `als` matching "also", each falsely
requiring a specialist and refusing a verdict. A test must assert that a verdict whose
only IP mention is a routine empty prior-art search does **not** require `legal`.

`_haystack` gains `recommended_next_experiment` (it names the modality, which is useful
for the chemistry and clinical cues); the excluded legal cues above keep that safe.

**Personas.** Commercial and legal adopt the ground-truth diligence framing verbatim,
including legal's carve-out that plain facts the lab would simply know — which reagents
came in under an MTA, who the co-inventors are — may still be asked. Per D13,
`clinical.md` and `budget.md` are **updated** to the diligence framing to match the
phase-4 four-domain split. Chemistry gains the DC-path, selectivity-margin and modality
bullets; clinical gains standard-of-care drift. The shared sections stay byte-identical
across all eight files; the `clear` bullet's anti-degeneracy line ("a panel that never
clears anything is noise") is retained, and the JSON example's populated `concerns` array
gains an explicit "may be empty" affordance — the measured problem is the example, not
the wording.

**Panel notes (D5).** The ground-truth sentence "never posted, never seen by the PI" is
factually wrong about this deployment and is rewritten: consults are posted in-thread as
signal-level notes for Blackbird assessors, while the specialists' opinions stay internal.
Residual recorded honestly: the threads are lab-visible in principle, so the protection is
operational rather than structural.

## 5. New diligence tools

### 5.1 `search_clinical_trials` — `src/services/trials.py`
Modeled on `patents.py`, including its central convention: **`None` = the search could not
run and must never read as "found nothing"**; an empty list = ran and matched nothing.
ClinicalTrials.gov v2 public API, no key.

Returned per study: NCT ID, brief title, phase, overall status, start date, conditions,
interventions, and **lead sponsor class**. `INDUSTRY` sponsorship against a target class
or indication is the forward-looking external signal §2.4 asks for — and the dimension the
baseline shows is broken (`external_signals` ≤1 in 15/16). Phase distribution indicates
crowding and maturity; intervention names indicate competing modality.

Caveats travel with every result: registry-only, registration is not activity, absence is
not absence of competition (unregistered preclinical programmes are invisible),
international coverage is partial. Role-gated to `scout_hub`; per-thread call cap beside
the existing abstract/full-text counters on `ThreadState`.

### 5.2 `web_search` — vendor-agnostic, inert until configured
`WebSearchProvider` protocol: `search(query, *, limit) -> list[WebResult] | None`, `None`
again meaning unavailable. Provider selected by `web_search_provider` (default `none`)
plus a key from the environment. Adapters (Brave, Serper, Tavily, Bing) drop in later
without touching the tool, the prompts or the engine.

1. **Unconfigured ⇒ not offered.** `tools_for_role` filters the tool out entirely rather
   than advertising a capability that always errors, so the prompt stays honest and costs
   no tokens.
2. **Results are untrusted input.** They pass through `prompt_safety.delimit()` with an
   explicit instruction that any directives inside the content are to be ignored. This is
   the only new attack surface in the design; the fencing helper exists for exactly this.
3. **No unsourced market claims.** Every result carries URL, title and date, and the
   prompt rule is that a market size, comparable or competitor claim must cite its source
   or be stated as unestablished — the same discipline as "an empty title search is never
   novelty." This is what makes adding the tool safer than the status quo, in which the
   hub can assert a TAM from recall with no trace.

Per-thread caps live on `ThreadState` beside the existing retrieval counters; the per-run
cap is engine-level (`SimulationEngine`), since `ThreadState` cannot see run totals.

## 6. Prompt and doc files that change

`prompts/rubric/blackbird-rubric.toml` (weights collapse, gating, anchors, red flags,
funnel, checklist, heuristic, banding note, version 3.0.0) ·
`prompts/roles/scout_hub/agent-system.md` (quality standards, division of labour, funding
bands, interview structure, tools) · `prompts/roles/scout_hub/phase4-thread-reply.md`
(panel paragraph incl. the corrected visibility sentence and the consult-cap wording, the
sidecar contract incl. `recommended_next_experiment`, capital-efficiency line) ·
`src/agent/thread_guidance.py` `_SCOUT_HUB` (EXPLORE clarification-first, DECIDE
division of labour, CONCLUDE next-experiment) · all eight `prompts/specialists/*.md` ·
`prompts/agent-system.md` (**lab-facing** funding table, D12).

Paired, mandatory: `docs/specs/2026-08-07-hub-bot-prompts.md` and
`docs/specs/2026-08-07-pi-bot-prompts.md` are verbatim-pinned by
`tests/unit/test_doc_prompt_sync.py` and must be mirrored in the same change.

**Golden master.** Changing `prompts/agent-system.md` changes the composed lab prompt, so
`tests/characterization/__snapshots__/test_agent_turn_gm.ambr` must be regenerated — and
the regeneration is only legitimate if reviewed as a diff showing the **only** changes are
the funding figures. Using `--snapshot-update` to silence an unexplained mismatch is
forbidden by CLAUDE.md and remains so.

Frozen historical documents that quote the old figures (the 2026-08-07 design docs, the
2026-08-20 proposal, the remediation plan) are records of what was true then and are not
edited.

## 7. UI (written post-approval — review this section)

Recommendation stays primary (already shipped). `band` is relabelled as an advisory
"rubric score" with its thresholds stated, not deleted — it remains the only comparable
number across verdicts. N/A dimensions render as `n/a` distinctly from both a low score
and an unscored `—`, with the renormalized denominator shown on hover.
`recommended_next_experiment` gets a prominent block on the detail page and appears in the
list's expandable detail row, labelled as the hub's estimate where it states cost or
duration. The derived missing-experiment badge sits beside `panel_incomplete`.

**The stated thresholds follow the row's own version, not the current ones.** A pre-v2 row
was banded at 4.0/3.0 and a v2+ row at 3.4/2.7, so a single global legend would misdescribe
half the table — the legend and the score tooltip read from the same version check that
selects the weight set.
`fto_achievable` is relabelled "FTO (diligence)" and the gating legend is rewritten to
describe two gates plus one diligence flag, so a red mark there is not read as a failed
gate. Legacy rows continue to render against `LEGACY_INVESTMENT_WEIGHTS` via the existing
`scored_stage_aware`-style version check, now simply "scored under v2+ or not."

## 8. Sequencing and verification (written post-approval — review this section)

**Window 0 — E1, the P0, first and alone.** Unrelated to this design but blocking:
`llm.py:617` returns `""` for any non-`max_tokens` stop reason, the turn is skipped, and
no drop row is recorded — 13 occurrences in 90 minutes on the last run. Branch on every
terminal stop reason, log ERROR, record a drop row, concatenate text blocks instead of
taking `[0]`.

**Window 1 — schema + floor + tools (code):** migration 0033, the derived flag, the floor
triggers with the excluded-cue test, `trials.py` + tool, the `WebSearchProvider` interface
(no adapter), role gating and caps.

**Window 2 — scoring regime (code + TOML):** the single-scale collapse, N/A
renormalization, `LEGACY_INVESTMENT_WEIGHTS`, version 3.0.0. Back-test the 42 stored rows
under renormalization **before** shipping and record the score deltas.

**Window 3 — prompts and docs:** every file in §6, the doc-sync mirrors, and the reviewed
GM regeneration. Prompts and the TOML are bind-mounted, so this is restart-only except
`thread_guidance.py`.

**Then** restart the run and let v3 verdicts accrue; re-baseline thresholds and re-derive
`external_signals`/`ip_fto` weights after ~20.

Verification per item: the N/A rule needs unit tests for each eligible dimension, each
omission rule, and a non-eligible `"n/a"` (must warn and score 0); the floor needs the
routine-prior-art-mention test; `trials.py` needs the `None`-vs-empty distinction tested;
`web_search` needs an unconfigured-⇒-absent-from-`tools_for_role` test and a fencing test;
the prompt window needs a render diff plus green doc-sync; `./scripts/ci.sh` gates every
window, and every `src/` window ends with rebuild-both-images, migrate, `docker stop -t
420`, restart, banner check.

**Scope note.** This spec covers five distinguishable pieces of work — the scoring regime,
the schema, the floor triggers, two new external tools, and a full prompt rewrite. Each
window in §8 is independently shippable and independently revertible, and the
implementation plan should be written per-window rather than as one pass. The one hard
dependency is that D13's division of labour (commercial/legal/clinical/budget diligence is
the hub's) is only honest once §5's tools exist — so the prompt window should not land
before the tools window.

## 9. Accepted limitations

Translational potential is unqueryable (§2.3). N/A justification is not machine-verified
(§2.2). Diligence citations are prose (§3). Web-search results are untrusted text; fencing
mitigates but does not eliminate prompt injection (§5.2). The `anthropic` image/venv skew
(1.0.0 vs 0.120.2) still means perf-harness measurements are taken against a different SDK
than production runs — out of scope here, tracked in the remediation plan.

# Rubric v2.0.0 proposal: re-baseline for incubation, with back-test results

**Date:** 2026-08-20 · **Status:** PROPOSAL — nothing here is live; adopting it is an
edit to `prompts/rubric/blackbird-rubric.toml` (+ one small code change) after review.
**Context:** the 2026-08-20 RCA (`docs/plans/2026-08-20-assessments-rca-ux-specialist-visibility.md`
§1) established that every assessment bands "pass" because the v1 scale anchors and
band thresholds are investment-grade while the screened population is 100%
incubation-stage academic pitches. Calibration was deliberately deferred until the
rubric existed as a reviewable document. It now does; this is the calibration proposal.

---

## 1. Evidence base

34 production verdicts (2026-08-17 → 2026-08-20): 29 pre-extraction + 5 stamped
`rubric_version=1.0.0` from the run started 2026-08-20 20:48 UTC. All 34 were scored
under byte-identical weights and anchors, so they form one back-test corpus.
Recommendations: 18 `pass` (decline), 16 `route-to-incubation` (the designed positive
outcome at this stage), 0 advance/conditional persisted (the only 2 `conditional`
verdicts ever emitted were destroyed by the pre-2026-08-19 floor bug — see RCA §1.1).

**Corpus-integrity note (added 2026-08-20, after the duplication remediation):** the
pre-gate engine could write more than one verdict per interview
(`docs/audits/2026-08-20-assessment-duplication/`); as of this note the corpus holds
two interview pairs with 2 verdicts each (huganir/drug-repurposing: one rti + one
pass; hart/general: two rti) — ≤4 of 34 rows, at most a marginal effect on the §6
concordance figures. The `duplicate_thread_verdict` gate now guarantees one verdict
per interview, so the §7.3 re-baseline on v2 data should additionally dedup any
remaining historical rows to latest-per-thread.

**Ground-truth caveat (honest limitation):** the back-tests below measure how well a
candidate score separates the *model's own* recommendations. That is internal
consistency — the recommendation and the dimension scores come from the same model
reading the same rubric — not human ground truth. No staff adjudications exist yet.
§7 makes collecting them part of the rollout. Second limitation: most of the corpus
was scored on Sonnet 4.6; the 5 newest on Opus 5 (the consult/agent model changed
2026-08-19). Third: n=34, one population, one instance.

## 2. What the data says dimension-by-dimension

Mean score among route-to-incubation vs pass verdicts (Δ = discrimination):

| dimension | v1 wt | rti mean | pass mean | Δ | note |
|---|---|---|---|---|---|
| workplan_capital_efficiency | **1** | 3.92 | 2.25 | **+1.67** | best discriminator in the entire rubric, at 1% weight |
| exit_thesis | 1 | 2.46 | 1.25 | +1.21 | discriminates even though floor-pinned overall |
| market_unmet_need | 12 | 3.69 | 2.56 | +1.13 | |
| differentiation | 15 | 4.54 | 3.50 | +1.04 | |
| chemistry_dc_path | 8 | 2.54 | 1.50 | +1.04 | |
| team | 10 | 4.46 | 3.50 | +0.96 | |
| dev_regulatory_feasibility | 3 | 2.85 | 2.06 | +0.78 | |
| ip_fto | 6 | 2.08 | 1.31 | +0.76 | |
| platform | 4 | 3.15 | 2.62 | +0.53 | |
| toxicity_selectivity | 10 | 2.77 | 2.31 | +0.46 | |
| external_signals | 8 | 1.54 | 1.19 | +0.35 | anchor unachievable at this stage (≥2 VCs) |
| mechanism_validation | 12 | 2.85 | 2.56 | +0.28 | weak — plausibly an anchor artifact |
| experimental_rigor | 10 | 2.92 | 3.00 | **−0.08** | does not discriminate at all |

Two surprises that shaped the proposal: the single question that most separates the
model's yes from its no — *"could a grant buy a decisive result?"* — carries 1% of the
weight; and the pure-rigor dimension separates nothing (declines are mostly *shaped*
wrong for Blackbird, not sloppy science).

## 3. Proposed change 1 — incubation-scoped anchors (highest leverage)

The anchors define the scale the model scores against; today they make 1–2 the
*correct* score for any unformed academic idea. v2 adds an `anchors_incubation` per
dimension (investment anchors unchanged — they remain right for later stages).
Proposed text, for review:

| dimension | proposed incubation anchor |
|---|---|
| differentiation | First/best-in-class *thesis*; a clear killer application; not incremental. Judged on what the idea could become, not its current evidence. |
| market_unmet_need | A real clinical decision point with a downstream intervention. Order-of-magnitude prevalence/TAM suffices — actionability over precision. |
| team | PI credibility and lab capability to execute the de-risking plan in 12–24 months; complementary expertise identified, not necessarily hired. |
| mechanism_validation | A credible, *testable* mechanism hypothesis with supporting data (genetic, functional, or published). Animal rescue is a 5, not the bar for a 4. Contradictory literature acknowledged. |
| experimental_rigor | Are the key *existing* results believable (controls, replication, interpretation)? Judge the evidence they have, not evidence the stage hasn't produced. |
| toxicity_selectivity | On-target liabilities and selectivity risks *identified, with a plan to test them early*. Ignorance of the risk scores low; absence of data at this stage does not. |
| chemistry_dc_path | A plausible modality and starting point (tool compound, series, format) with an articulable route toward a development candidate — tractability, not progress. |
| workplan_capital_efficiency | Could a $300K–$847K grant over 12–24 months buy a *decisive* de-risking result? 5 = a crisp, quantified killer experiment within budget; 1 = no experiment articulable, or scope far beyond an incubation grant. Maryland non-dilutive leverage (TEDCO MII, MSCRF, BIITC/QOF) adds. |
| platform | Reusable platform generating a pipeline vs one shot on goal. (Unchanged.) |
| ip_fto | A clean *path* to ownable IP: disclosure filed or filable, no known encumbrance or hostile co-ownership, plausible university license path. "FTO secured" is not the bar; an unencumbered, disclosure-ready position scores 4–5. |
| dev_regulatory_feasibility | If de-risking succeeds, is there a precedented modality/endpoint path? A plausibility check, not a development plan. |
| external_signals | Any *independent* validation: a KOL endorsement, non-dilutive funder interest (MII/MSCRF), informal pharma-scientist interest, or competitive activity validating the space. 5 = multiple independent signals; 3 = one; 1 = none. |
| exit_thesis | Venture-scale potential in one sentence: if the science works, is there a company or license a VC or pharma would want? Comps optional. Grant-only science with no commercial endpoint = 1. |

## 4. Proposed change 2 — incubation weights (W-C)

Principle (what an incubation decision turns on) blended with the measured
discrimination in §2:

| dimension | v1 | **v2 incubation** | rationale |
|---|---|---|---|
| differentiation | 15 | **16** | top-line thesis; strong discriminator |
| market_unmet_need | 12 | **14** | +1.13 Δ; actionable need is stage-appropriate |
| team | 10 | **12** | execution probability is the grant's real risk |
| mechanism_validation | 12 | **10** | science core kept meaningful; weak Δ likely anchor artifact — re-anchored in §3 |
| workplan_capital_efficiency | 1 | **8** | the best discriminator (+1.67) *is* the incubation question |
| chemistry_dc_path | 8 | **8** | unchanged; +1.04 Δ |
| experimental_rigor | 10 | **8** | non-discriminating; specialists' rigor concerns also land via signals |
| toxicity_selectivity | 10 | **8** | early-stage plan-awareness, per re-anchor |
| platform | 4 | **5** | |
| ip_fto | 6 | **4** | re-anchored to "IP path"; gate still carries the hard failure |
| dev_regulatory_feasibility | 3 | **3** | |
| external_signals | 8 | **2** | achievable re-anchor, but signals are scarce at this stage by nature |
| exit_thesis | 1 | **2** | kept small; the one-sentence venture story |

Sums to 100; all positive integers (validator unchanged).

## 5. Proposed change 3 — incubation band thresholds + semantics

Bands must map to actions, not vibes:

| band | provisional line | meaning at incubation |
|---|---|---|
| advance | ≥ **3.4** | staff opens grant diligence now |
| conditional | ≥ **2.7** | name the de-risking result that would move it, revisit on delivery |
| pass | < 2.7 | decline, with the named condition for coming back |

Lines are **provisional by construction**: the §3 anchor rewrite will lift future
scores on the four re-anchored floor dimensions, so thresholds set purely from the
back-test would be too low. These are placed at the top of today's distribution with
headroom, and §7 schedules the re-check. (Keeping the band *names* means no schema,
sidecar, or template changes.)

## 6. Back-test results

Method: re-score all 34 stored dimension vectors under candidate weight sets
(same clamp/missing-key rules as production `weighted_score`), compare against the
model's own recommendation. Concordance = share of (route-to-incubation, pass) pairs
ordered correctly.

| weight set | rti range (mean) | pass range (mean) | concordance | best single split |
|---|---|---|---|---|
| v1 (current) | 2.38–2.89 (2.61) | 1.85–2.57 (2.24) | 91.7% | 29/34 @ 2.38 |
| W-A science-forward | 2.53–3.07 (2.81) | 1.93–2.82 (2.44) | 89.2% | 28/34 |
| W-B moderate | 2.49–3.04 (2.78) | 1.95–2.75 (2.39) | 91.0% | 29/34 |
| **W-C (proposed §4)** | **2.56–3.01 (2.80)** | **2.00–2.69 (2.32)** | **96.5%** | **31/34 @ 2.56** |

Notable: a "science-forward" re-cut (mechanism/rigor-heavy) *worsens* discrimination —
the data killed the intuitive first candidate. W-C's gains come mostly from
workplan_capital_efficiency (1→8) and the external_signals cut (8→2).

Under W-C with the §5 provisional lines applied to the *current-anchor* corpus:
conditional captures 11 rti / 0 pass; decline holds all 18 pass + the 5 lowest rti;
advance is empty — expected pre-anchor-lift, deliberately conservative. At a 2.6
conditional line instead: 14 rti / 3 pass in conditional. Reviewer choice.

## 7. Rollout & calibration checkpoints

1. Review/adjust §3 anchors and §4 weights (the anchors are the part that most needs
   a human domain read).
2. Implement: TOML gains `anchors_incubation`, `weight_incubation`,
   `[banding.incubation]`; `weighted_score(scores, stage)` / `band(score, stage)`
   select by the verdict's `funnel_stage` (non-incubation stages keep v1 behavior);
   renderer emits both anchor columns; validator asserts each stage's weights sum to
   100; characterization pin test updated as a reviewed diff; `[meta].version = "2.0.0"`.
   Note: unlike a pure document edit, this needs an **image rebuild** (scoring code
   changes), then restart; banner must show v2.0.0.
3. After ≥20 v2-stamped verdicts: re-run this back-test on v2 data, adjust §5 lines,
   and — the real calibration — have staff adjudicate a sample of verdicts per band
   and compare. `rubric_version`/`rubric_content_hash` stamps keep the eras separable.
4. Success criteria: bands spread across the population; band agrees with
   recommendation and staff judgment; no grade inflation (declines still decline).

## 8. Decisions for the reviewer

1. Adopt W-C, or adjust (esp. workplan 8 / external_signals 2 / rigor 8)?
2. Conditional line at 2.7 (conservative) or 2.6 (captures more, 3 false positives
   on today's corpus)?
3. Companion prompt changes (separate sign-off per CLAUDE.md): reframe the CONCLUDE
   prior ("route-to-incubation is the expected positive outcome at this stage") and
   equalize consult costs (scientific+talent before *any* sidecar verdict)?
4. `baltimore_commitment` (currently excluded, see
   `docs/audits/2026-08-20-rubric-extraction/blackbird-private-diff.md`): leave out,
   add as a weighted dimension, or add as a red flag? (Not proposed as a fourth gate —
   that breaks the sidecar/DB contract.)

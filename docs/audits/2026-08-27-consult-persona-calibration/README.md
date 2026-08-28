# RCA: "the specialist panel cannot discriminate — check persona calibration"

**Run 61ccad6d-eb1e-4023-81ba-adcea726a196 (2026-08-27/28) ended with:**

```
[specialists] 4 of 162 counted consults this run returned 'clear' (2.5%, floor 5%).
A panel that clears almost nothing cannot discriminate — check persona calibration.
```

> ## ⚠️ STATISTICAL CORRECTIONS (2026-08-28, from `audit-evidence.md`)
>
> The core conclusion below — that the panel discriminates and the clear-rate alarm's
> diagnosis is wrong — **survives**. Three of the numbers used to argue it do not, and
> two of them would license the wrong next action if taken at face value.
>
> 1. **`p = 5.1e-07` is roughly 240x too small.** Fisher's exact test there treats 16
>    domain-matched pairs as independent observations. The paired test the design calls
>    for (McNemar) gives **p = 1.22e-4**. Still decisive; not what was claimed.
> 2. **"Framing does nothing (p = 1.00)" is an artefact, not a null result.** At n = 8
>    the comparison has essentially no power: even TOTAL abolition of the top label
>    would only reach p = 0.20. A paired sign test over all 24 pairs runs **4-0 toward
>    the NEUTRAL framing being harsher** (p = 0.125) — a one-directional signal the
>    reported test cannot see. Framing is **unmeasured**, not inert. This matters
>    because "framing is inert" was the stated reason not to reword the personas.
> 3. **"Only `legal` is flat" is overstated.** `legal` (2 of 8 WEAK→STRONG changes) is
>    statistically indistinguishable from `clinical` (4 of 8), Fisher p = 0.61. The
>    "0 of 91 clear" property is shared by **6 of 8** domains, and production entropy
>    ranks `legal` **4th**, behind technologic, clinical and budget. The "changed 0 of
>    2 tiers" figure was one run's noise — the committed baseline shows 1 of 2.
>
> Since these were written, the fix built on them has been measured, and the result is
> recorded in `05-isolation-series-design.md` and the SDD ledger: the interventions moved
> the panel's operating point rather than its resolution, exactly as this review's own
> "prompting moves the criterion, not the resolution" citation predicts.

## Verdict

**The alarm's diagnosis is wrong, and its metric measures the wrong thing.** The
panel discriminates strongly. Its `clear` rate is low because Blackbird's deal
flow is early-stage, which is what the panel is correctly reporting.

Under a controlled quality ladder run through the production code path, the panel
moved from **87.5% `blocking` / 0% `clear`** on a weak record to **0% `blocking` /
31.2% `clear`** on a strong one, monotonically. Its construct sensitivity — the
literature's own measure of whether a judge notices when quality actually changes
— is **roughly twice the published average for LLM judges** (R = 0.594 at one
quality rung and 0.875 at two, against a published average of 0.319). The
instrument is fine.

The main effect is not a small-sample artifact: `blocking` at 14/16 on the weak
record versus 0/16 on the strong one is **p = 5.1 × 10⁻⁷** (Fisher exact). The
framing manipulation, by contrast, gives **p = 1.00**.

Three further facts settle it:

- The tier built to mirror real deal flow is statistically consistent with
  production: **87.5% caution / 12.5% blocking** (Wilson CIs [52.9, 97.8] and
  [2.2, 47.1]) against production's **85.7% / 13.5%** ([83.6, 87.6] and
  [11.7, 15.6]). At n=8 that is an overlap check, not a precise reproduction.
- **`chemistry` is the most informative domain in the whole panel** (0.914 bits of
  a possible 1.585, 57.7% of maximum) and has **never cleared once in 82
  consults.** It discriminates entirely through `blocking`. Clear rate and
  discriminative power are close to unrelated in this data.
- The instruction the alarm is asking for — *"Say this when it is true; a panel
  that never clears anything is noise"* — **is already in all eight persona files**
  and has produced 0.76% across 1,192 consults. The fix the alarm implies has
  already been tested at n=1192 and failed.

**There is one real defect, and it is narrow:** `legal` is genuinely insensitive,
and `clear` is mislabelled when it does fire. Neither is what the alarm says.

## What is actually wrong

### 1. `legal` is flat — one domain, one file

`legal` returned `caution` in **all six** of its probe cells (three quality tiers ×
two question framings). It did not move between a record with nothing filed and a
record carrying an issued composition-of-matter patent, a verified single
assignment chain, and a written freedom-to-operate opinion from outside counsel.
Seven of eight domains changed verdict across the ladder; `legal` changed in 0 of
2. Production agrees: **0 of 91 `clear`**, and a mean concern count that is
*inverted* (9.01 on `caution` versus 8.94 on `blocking`).

The in-repo working example is `budget` — the only persona with **external numeric
thresholds** ($100K–$1M bands, 12–24 month horizons) and the only one instructed to
render an **affirmative determination** ("which band the proposed scope actually
fits"). It has the panel's highest clear rate (7.46%), never blocks spuriously
(0/67), and was fully quality-sensitive. `legal` has neither property.

### 2. `clear` does not mean what it renders as

**Not one of the nine `clear` opinions in this system's history has an empty
concerns list.** They carry 4, 5, 5, 6, 6, 6, 7, 7 and 9 concerns. The
nine-concern one lists *"Succession risk is high as described"* and *"Conflicts of
interest are undeclared"* — under a label that renders in Slack as **✅**.

The mechanism is structural: the response schema has two content fields and both
are negative (`concerns`, `questions_to_ask`). There is nowhere to record what a
record *establishes*, so specialists file positives inside `concerns` with a hedge
appended — *"this is the strongest positive signal in my domain and I have no
counter-evidence against it, but…"*. That inflates the concern count on exactly
the opinions whose label is most favourable.

### 3. The information loss is in the middle of the scale, not at the `clear` end

`caution` share is **68.8% at the `MEDIUM` tier and 68.8% at the `STRONG` tier** —
identical. Its definition, *"a real weakness that changes how much weight the
result carries"*, has no materiality threshold, and every early-stage academic
record contains a real weakness, so the category absorbs 85.7% of all consults. A
single `caution` cannot distinguish a mediocre opportunity from an excellent one.

The panel *as a whole* still separates them (5 blocking / 0 clear at `MEDIUM`
versus 0 blocking / 5 clear at `STRONG`). The defect is in the per-consult label.
**The alarm is watching the end of the scale where nothing is wrong.**

### 4. The floor is set above the population's expected value

The measured clear rate on a population-faithful case is **0%**; production is
**0.76%**. A 5% floor therefore sits above what a correctly-functioning panel will
produce on this deal flow, making it a **permanent false alarm**. That was open
question #2 of the 2026-08-24 audit ("if the true population clear rate is
genuinely ~1%, the floor is a permanent false alarm"); it is now answered
empirically.

**And there is no correct number to replace it with, for a principled reason.** The
optimal threshold for a screening instrument is a likelihood ratio — a function of
the base rate of the population screened — so a fixed floor on the output rate is
the wrong shape of constraint whatever number is chosen. The one organisation that
ran the definitive experiment (NeurIPS 2021, two committees over the same papers)
concluded that *"making the conference more selective would increase the
arbitrariness of the process."* And human expert panels show our asymmetry exactly:
across **18,043 individual NIH reviewer records**, a top score goes to **3–3.5%** of
applications on the rigor criterion versus **30–40%** on team and environment —
mirroring our 0% for `scientific`/`chemistry` against 7.46%/2.13% for
`budget`/`talent`. NIH's own instruction to *"use the full range"* does not stop it
either; observed scoring centres sit two to three points off policy on four of five
dimensions. **Replace the ratio with the ladder.**

## Hypotheses tested

The 2026-08-24 audit (`docs/audits/2026-08-24-panel-clear-rate/`) framed four
candidate explanations and prescribed a positive control to separate them, with an
explicit instruction not to edit personas first. It has now been run — 48 real Opus
consults, 2×3 factorial, no database writes — and it resolves all four.

**One correction to this audit, recorded rather than quietly fixed.** A smaller
version of the diagnosis had already been run on **2026-08-18**
(`scripts/diagnose_specialist_calibration.py`, commit `84fa1aa`, recorded in
`docs/specs/2026-08-18-specialist-panel-remediation-design.md` §5): three synthetic
strong cases, which returned `scientific` → `clear`, `chemistry` → `caution`,
`commercial` → `caution`, and concluded that *"`clear` is not structurally
unreachable."* The 2026-08-24 audit did not cite it and called the experiment
un-run; this audit repeated that. What had never been run was the *factorial with a
population control, a framing factor, all eight domains and significance testing*.
**The omission cuts in favour of the conclusion:** those three domains reproduce
exactly here, ten days and a full rubric regime apart, on a different code path —
which is the strongest available evidence that the one-sample-per-cell results below
are not noise.

| hypothesis | verdict |
|---|---|
| **H1** personas are miscalibrated risk-finders | **REFUTED.** They clear 31% and block 0% on a strong record. |
| **H2** the population really is early | **CONFIRMED, dominant.** The population-faithful tier is statistically consistent with production, and the external evidence agrees: of inventions US universities *actually licensed*, over 75% were no further than lab scale (Jensen & Thursby 2001), and independent replication attempts confirm only 11–22% of published target-validation claims (Begley & Ellis 2012; Prinz et al. 2011). If the underlying claims in the pool hold up 11–22% of the time, a low clear rate is what a correct instrument produces. |
| **H3** the hub asks unclearable questions | **REFUTED.** Rewriting every question from adversarial to symmetric moved the clear count by one consult; the neutral framing was *harsher* at the medium tier. |
| **H4** panel scope mismatch | **PARTIALLY CONFIRMED, re-specified.** Not "six domains can't clear" — seven of eight are quality-sensitive. The residual is `legal` alone. |

Also refuted along the way:

- **Parsing artifacts.** 1,020 of 1,022 stored `caution` rows have the model's own
  reply literally saying `caution`. The exact `parse_opinion` default fingerprint
  matches 15 rows all-time (1.26%); removing all of them changes the clear rate by
  0.01 pp.
- **A confidence-enum mismatch.** Suspected and checked: `_CONFIDENCES` is
  `{high, moderate, low}` and all eight personas specify exactly those. No defect.
- **A recent regression.** The personas were created 2026-08-07 and touched once
  since; the verdict-signal definitions have never changed. Four runs have zero
  clears. The rubric v3.x work did not cause this, and **run 61ccad6d is the
  best-performing run ever recorded on this metric** (2.45% against a 0.60–0.87%
  ceiling everywhere else).

## Severity

**Low, and bounded.** `verdict_signal` feeds no decision. Every reader outside the
persistence layer is a display surface; the specialist floor counts consults **by
domain, not by signal**, and `weighted_score` comes from the hub's own sidecar. The
signal reaches a verdict only textually, because the hub reads the full opinion
body — and the opinion bodies are demonstrably specific and useful. No funding
decision has been mis-scored by this.

What it has cost: staff reading ✅ on an opinion that says succession risk is high,
and a run-level alarm asserting a conclusion the evidence does not support.

## The claim this audit cannot make

Everything here establishes that the panel is **sensitive** — that its verdicts
move when real quality moves. **Nothing here establishes that its labels are
right.** Only human-labelled anchors can do that (~100 would suffice per
arXiv:2506.02945 and arXiv:2605.09227), and they do not exist. Until they do, every
statement about the panel's accuracy — including the reassuring ones above — is an
inference from sensitivity, not a measurement of validity.

Two further limits worth stating plainly:

- **Predictive validity is unmeasurable right now.** The v3.2.0 residuals purge left
  6 assessments against 1,192 consults, so 1,186 consults are orphaned from the
  verdicts they informed. At n=6 the panel-to-verdict correlation is noise
  (ρ = −0.371 overall, +0.10 among the five `pass` rows).
- **Differentiated marginal rates are not independent errors.** Per-domain
  `blocking` propensity spans 0.0% (`budget`) to 32.9% (`chemistry`) — a 32.9 pp
  spread from the persona alone — and 62.5% of production interviews show
  specialists disagreeing with each other. That is real differentiation, and it is
  a novel measurement: the persona literature names inter-agent correlation for
  *explicitly role-differentiated agents on one base model* as unmeasured. But all
  eight domains still share the `caution` attractor, and nothing here shows their
  mistakes are uncorrelated.

## Addendum (2026-08-28): a deeper root cause, found while designing the fix

This audit concluded that the personas **lack** a stage bar. Reading the Blackbird
document during design showed something sharper: **the bars exist, and the personas
have never been shown them.**

`render_rubric_markdown()` fills the `{rubric}` placeholder in
`prompts/roles/scout_hub/agent-system.md` (`agent.py:322`) — a mechanism that exists
specifically so the hub's judgment and the code's arithmetic cannot drift apart.
`_execute_consult_specialist` reads the persona file raw (`tools.py:593`). **The
specialists were never included.** Consequently the panel has spent its entire
history judging against standards the document explicitly disclaims:

- `[scoring].preamble`: *"Screen at the incubation grain: the bar is scientific
  robustness plus translational potential — **never the replicated data, filed IP, or
  identified syndicate a later-stage deal would show.**"*
- `legal`, stated three times over: *"Freedom-to-operate is diligence, not a gate"*
  (`gating.translational_potential`) · *"'FTO secured' is not the bar"*
  (`venture_potential.anchors`) · *"Unresolved FTO on unpublished academic science is
  the normal starting condition, not a disqualifier"* (`red_flags`).
- `technologic`: *"a single asset is the normal shape of a de-risking grant"* — yet in
  the probe it objected that a platform claim was untested on a fourth target.
- `clinical`: *"order-of-magnitude prevalence/TAM suffices, actionability over
  precision"* — yet in production it pressed repeatedly for precise incidence numbers.
- `scientific`: *"Judge the evidence they have, not evidence the stage hasn't
  produced: animal rescue is a 5, not the bar for a 4."*

**This does not change any measurement in this audit, and it strengthens the
`legal` finding** — a domain given a standard the document disclaims three times is
exactly a domain that would caution on everything.

**It also exposes a limitation of this audit's own experiment.** The `STRONG` case in
`02` was built with replicated data, filed IP and named syndicate interest — the
precise three things `[scoring].preamble` says are *never* the bar. So it was
constructed against a later-stage standard than Blackbird screens at, which makes its
31.2% top-label rate an upper bound on an unusually favourable record rather than a
target. Consistent with Jensen & Thursby (2001): only 28% of inventions universities
actually license hold an issued patent. A future ladder should include a tier built
to the document's *stated* bar, not above it.

Design: `docs/specs/2026-08-28-specialist-verdict-vocabulary-design.md`.

## What to do

`04-recommended-modifications.md`, in short: **do not loosen the personas.** Fix
the alarm's assertion and its floor; stop rendering `clear` without its concern
count; move the verdict field to the *end* of the response schema so evidence
precedes the label; give `legal` a stage-relative bar modelled on `budget`; and
promote the probe to a maintained calibration harness so every further change is an
experiment rather than a guess. The two highest-value items are the harness and
~100 human-labelled anchors.

**Nothing has been applied.** Persona and threshold changes are tuning decisions.

## Contents

| file | what it holds |
|---|---|
| `01-production-evidence.md` | Everything measured from the production database, with the SQL |
| `02-positive-control-experiment.md` | The 48-consult quality-ladder experiment, protocol and results |
| `03-literature-review.md` | ~100 citations, organised by the decision each bears on, with evidence grades |
| `04-recommended-modifications.md` | Tiered recommendations, including a Tier 0 of things not to do |
| `panel_probe_results.json` · `panel_probe_medium.json` | Raw per-consult records, including every full opinion |

## Provenance

Measured 2026-08-28 by Claude Fable 5 against production `copi` (Postgres 15) on
ec2-3-21-33-147 and the run's own logs. **Database access was read-only
throughout**; no row was written, no prompt was changed, and no persona was edited
in service of this report. The 48 experimental consults ran through
`_execute_consult_specialist` with both persistence callbacks set to `None`, so
they wrote no `specialist_consults` rows and credited no specialist floor. Every
table in `01` is reproducible from the SQL as printed; every number in `02` is
recoverable from the two JSON files.

Prior work this builds on and does not duplicate:
`docs/audits/2026-08-24-panel-clear-rate/` (which framed the hypotheses and
prescribed the experiment), `docs/audits/2026-08-22-run-8b64a0e0/` (the
laundered-consult parsing history), and
`docs/audits/2026-08-26-specialist-truncation-rca/` (truncation, a separate
failure mode).

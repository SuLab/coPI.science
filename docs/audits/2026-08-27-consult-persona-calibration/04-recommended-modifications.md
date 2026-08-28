# Recommended modifications

**Nothing in this document has been applied.** Persona and alarm changes are
tuning decisions, and `prompts/specialists/*` edits take effect only on an agent
restart (`prompts/` is bind-mounted but personas are read per call via
`persona_path().read_text()`, so a *running* engine picks up a persona edit on its
next consult — unlike the rubric, which is read once at import). Each item states
its evidence grade, its cost, and how it would be validated.

Ordered by (evidence strength × cheapness), not by appeal.

---

## Tier 0 — Do not do these

Recorded because each is the intuitive response to the alarm and each is
contraindicated.

| tempting fix | why not |
|---|---|
| **Loosen the personas so more consults clear** | The instruction is already there in all eight files and produced 0.76% over 1,192 consults. Threshold instructions move the criterion, not the resolution (arXiv:2606.15610); a generic "be strict/lenient" instruction shifts the whole distribution uniformly rather than sharpening it (arXiv:2606.15474). And spread is not validity — a 3.4× widening left the evaluation axis unmoved at 87°–88° (arXiv:2606.03043). |
| **Tell the personas an expected clear rate ("~1 in 5 should clear")** | No literature measures this at all. Every adjacent result is discouraging: anchoring is driven by the anchor's *presence* more than its value, is not removable by instruction (Cohen's d = 0.71, arXiv:2608.25869), and numeric anchoring inflated a headline metric ~5× more than the underlying skill (arXiv:2607.01240). This would manufacture the metric, not the discrimination. |
| **Make the personas stricter to "sharpen" them** | Requiring a rationale drove GPT-4o's rejection of *correct* code from 26.2% to 73.2% (arXiv:2603.00539); self-verification false-negative rates reach 95.8%–97.09% (arXiv:2402.08115). The knob that fixes all-pass produces all-fail without passing through discriminating. |
| **Add free-form chain-of-thought before the signal** | Narrows the judgment distribution 1.7–2.8× (arXiv:2503.03064, STRONG). It is a candidate cause of compression, not a fix. Structured evidence is a different intervention and *is* supported — see R4. |
| **Ask for a fixed number of concerns** | CriticGPT measured the mechanism: more claims buys recall and nitpicks together, on a curve with no principled operating point (arXiv:2407.00215). The personas already over-produce at ~7.5 concerns per opinion. |
| **Raise temperature, add debate rounds, or enlarge the panel** | Temperature buys incoherence faster than variation (arXiv:2603.28304). Homogeneous debate is provably a martingale, majority voting beat all nine debate configurations, and conformity rises with rounds (arXiv:2508.17536; arXiv:2501.13381). This system already collects independent verdicts and aggregates outside the model, which is the supported design. |
| **Regenerate the `.ambr` snapshots to make anything match** | Standing prohibition in CLAUDE.md; unrelated to this defect. |

---

## Tier 1 — High confidence, low cost, no change to model behaviour

### R1. Correct the alarm's assertion, and retire its numeric floor as a calibration verdict

**What.** `clear_rate_warning` (`src/agent/specialists.py:434`) currently states:

> A panel that clears almost nothing cannot discriminate — check persona
> calibration.

**That assertion is now falsified.** The panel discriminates: 87.5% → 0% blocking
and 0% → 31% clear across a controlled quality ladder, with construct sensitivity
roughly twice the published average for LLM judges. The clear rate measures the
*population*, not the instrument — and `chemistry`, the most informative domain in
the panel (0.914 bits, 57.7% of maximum), has **never cleared once**.

The floor is also mis-set. The measured clear rate for a population-faithful case
is 0%, and for production 0.76% — so a 5% floor sits **above** the population's
expected value and is a permanent false alarm. That was open question #2 in the
2026-08-24 audit; it is now answered.

**Proposed replacement:** keep the tripwire, drop the diagnosis. Report the
distribution, name the population as the expected cause, and point at the harness
rather than at the personas. Something in the shape of:

> `[specialists] signal mix this run: clear 4 (2.5%), caution 143 (87.7%),
> blocking 16 (9.8%) of 163 counted. A low clear share is EXPECTED for an
> early-stage population and is not evidence of miscalibration — the panel's
> discrimination is measured by scripts/panel_probe.py, not by this ratio. See
> docs/audits/2026-08-27-consult-persona-calibration.`

**Evidence.** arXiv:2606.19544 (*Reliability without Validity*, ~541,000
judgments); arXiv:2606.03043; arXiv:2606.15610; plus `02`'s local measurement.

**And there is no target rate to replace it with — for a principled reason, not for
want of searching.** The optimal operating threshold for a screening instrument is a
**likelihood ratio**, a function of the base rate of the population being screened,
so a fixed floor on the output rate is the wrong *shape* of constraint whatever
number is chosen. Four measured results converge on this:

- The one organisation that ran the definitive experiment — NeurIPS 2021, two
  independent committees over the same papers — concluded that ***"making the
  conference more selective would increase the arbitrariness of the process."***
  Disagreement on orals and spotlights was **5.8%, barely 3 points better than
  random**, and more than half the spotlights recommended by either committee were
  rejected by the other.
- **Human expert reviewers show the same asymmetry we do.** Lindner et al. (2016,
  *Am J Eval* 37(2):238-249, **N = 18,043 individual reviewer records**) measured
  NIH's per-criterion distributions: a top score of "1" goes to **3–3.5%** of
  applications on **Approach** (the rigor criterion — closest analogue of our
  `scientific`/`chemistry`, which clear at 0%) versus **30–40%** on
  Environment/Investigators (closest analogue of our `budget`/`talent`, which clear
  at 7.46%/2.13%). Lindner's own words: *"four of the five scored criteria exhibited
  moderate to severe restriction of range."*
- **And instruction does not fix it in humans either.** NIH policy says *"5 is
  considered an average score"* and instructs reviewers to *"use the full range"*;
  observed centres are **2, 2, 3, 3, 4** — two to three scale points off design on
  four of five dimensions. This is the human analogue of our own already-present,
  already-failed exhortation.
- **Even the commercial gatekeepers transact on unproven work.** Of inventions US
  universities actually licensed, **over 75% were no further than lab scale** and only
  **28% held an issued patent** at licensing (Jensen & Thursby 2001, *AER*
  91(1):240-259) — which also means the probe's `STRONG` scenario, carrying an issued
  patent, sits **above** the real ceiling, so its 31.2% clear rate is an upper bound
  no real pitch should be expected to reach.

Pushing a screening instrument toward finer discrimination at the top of its range
buys noise, not accuracy. See `03` Decision 8.
**Cost and test surface.** Seven tests in `tests/unit/test_specialists.py:803-860`
cover this alarm. The wording proposed above survives all of them: they assert the
substring `"clear"`, the denominator digits (`"168"`), the word `"counted"`, the
n≥50 sample floor, and the divide-by-zero guard. **One test must be changed
deliberately, not incidentally:** `test_the_clear_rate_floor_is_pinned` (`:842`)
asserts `MIN_CLEAR_RATE == 0.05` literally, with the docstring *"pinned literally
so a future loosening is a diff, not a drift."* That is working as designed — any
threshold change should show up as an explicit diff to that assertion, with the
new derivation recorded in its docstring.
**Owner decision:** whether to keep any numeric threshold at all. If kept,
re-derive it from the ladder, not from a guess. Note that
`test_a_discriminating_panel_is_silent` encodes the assumption under review — it
treats a 25% clear rate as "a discriminating panel" — and the ladder now gives a
measured alternative: 31.2% is what this panel produces on a genuinely strong
record, which is the first empirical support that constant has ever had.

### R2. Stop rendering `clear` as a bare ✅

**What.** `format_panel_note` (`specialists.py:172`) renders `clear` as `✅ clear`.
**All nine `clear` opinions ever emitted carry 4–9 concerns**; the most recent
(talent/yarchoan) lists *"Succession risk is high as described"* and *"Conflicts of
interest are undeclared"* under that ✅. The same decoupling appears in the
assessment detail pages and `thread_panel` cards.

**Proposed:** carry the concern count alongside the signal — `✅ clear (6 concerns
noted)` — in the panel note and on both read surfaces. Signal-level only, so it
does not breach the confidentiality contract `format_panel_note`'s docstring
enforces (a count is not an opinion body).

**Evidence.** Local measurement (`01` §4). This is the one place where the defect
has a concrete, currently-live harm: staff read ✅ and are told something the
opinion does not say.
**Cost:** one function plus two templates. No model change, no migration.

### R3. Log the per-domain signal distribution at run end

**What.** The engine tallies `_consult_signal_counts` globally
(`simulation.py:411`). Per-*domain* flatness — the one real persona defect found
here — is invisible without a database query.

**Proposed:** extend the shutdown log line to a per-domain breakdown, and warn
when a domain's modal share exceeds ~95% over ≥20 consults in a run. `legal` and
`technologic` are the current candidates (81.3% and 97.9% all-time).
**Caveat that must be coded in:** `technologic` was fully quality-sensitive in the
probe (2 of 2), so a modal-share warning is a *prompt to run the ladder*, not a
verdict. Word it that way or it becomes the same mistake as R1.

### R4. Reorder the response schema so evidence precedes the verdict

**What.** All eight personas specify:

```
{
  "verdict_signal": "blocking | caution | clear",   <-- FIRST
  "concerns": [...],
  "questions_to_ask": [...],
  "confidence": "high | moderate | low"
}
```

A model generates left to right, so **the label is committed before a single piece
of evidence is written.** This is score-first ordering, and the system then
anchors on its own just-emitted token for the rest of the reply.

**Proposed:** move `verdict_signal` (and `confidence`) to the end, after
`concerns` and `questions_to_ask`.

**Evidence.** Multiple Evidence Calibration — generate evidence, *then* rate — is
worth **+6 to +11 accuracy points and +0.06 to +0.21 κ** (arXiv:2305.17926,
STRONG). Anchoring on a score already in context is large and not instructable
away (arXiv:2608.25869). **Honest limit:** the clean A/B of "score then justify"
versus "justify then score" is unpublished, so this is MODERATE, not STRONG.
**Cost:** eight one-block edits. **Safety:** `parse_opinion` uses `data.get(...)`,
so key order is irrelevant to parsing; `extract_json` is order-agnostic; no test
pins the order (checked). Validate with the ladder before and after.

---

## Tier 2 — Moderate confidence, prompt-level, each must be A/B'd against the ladder

### R5. Add one positive-evidence field to the specialist contract

**What.** The output schema has two content fields and both are negative-valence
(`concerns`, `questions_to_ask`). There is nowhere to record what the record
*establishes*. The specialists visibly work around this — three of the nine
`clear` opinions file a **positive finding inside the `concerns` array** with a
hedge appended (*"this is the strongest positive signal in my domain and I have no
counter-evidence against it, but…"*).

**Proposed:** add `"established": ["what the record does support in your domain"]`.
Staged so it costs nothing to try: **add it to the persona prompts only.** It then
lands in `raw_opinion` and is visible on the assessment detail page with no
migration and no model change. Persist it as a column later only if it proves
useful.

**Evidence.** The workaround is locally measured. Bannò, Knill & Gales (BEA 2026,
arXiv:2605.04298, up to 80 trained raters per essay) found **LLMs outperform single
human raters at identifying relative weaknesses while humans remain stronger at
identifying relative strengths** — strengths need an explicit slot or they do not
get reported. And the asymmetry is **not** an artifact of being a language model:
Kaatz et al. 2022 (*PLOS ONE* 17(9):e0273813), a controlled grant-review
experiment, found **proposal *risks* predicted human reviewers' scores more
strongly than proposal *strengths***. The response in that literature is an
instrument change — an explicit strengths facet — not reviewer retraining, which is
precisely what this recommendation is. Binary/decomposed reporting is the only intervention with measured
evidence of *widening* a distribution (arXiv:2606.27226; arXiv:2403.18771).
**Risk to watch:** an `established` field could become a leniency lever. The
ladder must show `WEAK` stays at ~87% blocking after the change.

### R6. Give `legal` a stage-relative bar, modelled on `budget`

**What.** `legal` is the only genuinely insensitive domain: `caution` in all six
probe cells, unmoved between "nothing filed" and "issued composition-of-matter
patent plus written FTO opinion", and 0 of 91 `clear` in production with its mean
concern count *inverted* (9.01 on caution vs 8.94 on blocking).

**The in-repo working example is `budget`.** It is the only persona with **external
numeric thresholds** (incubation $100K–$1M, pre-seed $300K–$1M, seed ~$1M–$5M,
12–24 month horizon) and the only one whose "You do not decide" section asks for an
**affirmative determination** — *"including, when relevant, which band the proposed
scope actually fits."* It has the highest clear rate in the panel (7.46%), never
blocks spuriously (0/67), and was fully quality-sensitive in the probe.

**Proposed:** give `legal` the same two properties — a stated bar for what
constitutes an adequate IP position *at incubation stage* (as distinct from at
seed), and an instruction to name which of those states the record actually
reaches. Do not soften anything; add a decidable target.

**Evidence.** Explicit threshold language demonstrably moves behaviour — a 28.7-point
swing on a 39-point risk scale from framing alone (arXiv:2509.23058), and personas
changed decisions **only** when a mediator converted them into explicit heuristic
values (IJCNLP-AACL 2025, arXiv:2512.06867). Persona *assertion* does not improve
judgment (Findings of EMNLP 2024, STRONG) — which is exactly why the change should
be a stated bar, not more expertise or disposition prose.
**Validation:** required. `legal` must move on the ladder without `WEAK`-tier
legal becoming permissive.

### R7. Consider splitting the middle of the scale — the actual information loss

**What.** The measured defect is not at the `clear` end. `caution` share is
**68.8% at `MEDIUM` and 68.8% at `STRONG`** — identical. A single `caution` cannot
distinguish a mediocre opportunity from an excellent one, and its definition
(*"a real weakness that changes how much weight the result carries"*) has no
materiality threshold, so it absorbs 85.7% of all consults.

**Options, in ascending cost:**

1. Add a materiality qualifier to `caution` so the label distinguishes "a weakness
   that is expected at this stage" from "a weakness that must be closed before
   funding". Cheapest; closest to R6's mechanism.
2. Add a fourth level between `caution` and `clear`. Note Yamauchi et al.
   (arXiv:2506.13639, MODERATE) found describing **only the endpoints** gave the
   best human correlation, with intermediate descriptors unhelpful — so a fourth
   level should be added with rich endpoint anchors, not a paragraph per band.
3. Decompose each domain's checklist into binary sub-answers and let code
   aggregate. Strongest measured support for widening (arXiv:2606.27226) and for
   external aggregation (arXiv:2405.15329), and it is the largest change.

**Evidence and its limit.** Decomposition is MODERATE-supported for reliability and
WEAK-MODERATE for distribution widening. **Separating "a concern exists" from "it
disqualifies" is specifically UNMEASURED** — flagged as a gap by two independent
searches. Treat option 1 as a hypothesis to test, not a finding to rely on.
**Owner decision:** this changes the panel's vocabulary and every stored row's
comparability. It belongs with a version stamp, the way rubric changes do.

---

## Tier 3 — Strongest long-run evidence, highest cost

### R8. Promote the probe to a maintained calibration harness

**What.** `panel_probe.py` (this audit) is a one-off in the session scratchpad and
on the host at `/home/ubuntu/probe/`. It should live in the repo — `scripts/` or a
`tests/manual/` target — with the three case texts, and be run on **any** persona
edit, rubric change, or model change, recording R (construct sensitivity) and S
(invariance) each time.

**Evidence.** This is the literature's single highest-value recommendation for
exactly this problem (arXiv:2606.15610 for the ladder; arXiv:2608.24419 for the
S/R pair), and it is the only thing that can tell a discriminating panel from a
compressed one. **It also converts every item in Tier 2 from a guess into an
experiment**, which is the reason to do it first.
**Cost:** ~48 Opus calls per run, ~13% of a one-hour simulation. Must not be part
of `ci.sh` — it makes real API calls.

### R9. Collect ~100 human-labelled anchor consults

**What.** Have a human — the operator, or a Blackbird reviewer — label ~100 stored
consults on the same three-level scale, then measure agreement and the systematic
offset.

**Why this dominates everything above.** A small regression head on a **frozen**
judge, explicitly motivated by score compression and leniency bias, doubled Pearson
(0.298 → 0.634) and cut MSE 6.346 → 2.626 at 1% of the fine-tuning data
(arXiv:2506.02945, MODERATE-STRONG); ~**100 anchors** sufficed to close a
0.71-point systematic offset to within ±0.08 (arXiv:2605.09227). Multi-faceted
Rasch modelling is the established instrument for separating true quality from
**rater severity and centrality bias** — precisely this pathology
(arXiv:2602.22585; arXiv:2602.00521).

**And it is the only way to answer the question this audit cannot.** Everything
measured here establishes that the panel is *sensitive*. Nothing establishes that
its labels are *right*. Only labelled anchors can, and until they exist, every
statement about the panel's accuracy — including the reassuring ones in this
audit — is an inference from sensitivity, not a measurement of validity.

### R10. Protect the no-cross-anchoring invariant with a test

**What.** No consult in 1,192 carries another specialist's verdict or a numeric
score in its `question` or `context` (`01` §10). This is the literature's
strongest single prohibition for a critic panel — d = 0.71, blocks 48% of error
corrections, not instructable away (arXiv:2608.25869) — and it currently holds by
accident, protected by nothing.

**Proposed:** a unit test asserting that the string assembled for a consult
contains no `verdict_signal`, no signal word in a verdict-bearing position, and no
sidecar score. Cheap insurance against a future change that pastes the panel's
running state into a later consult's context.

---

## Two things to record rather than fix

**A. Prestige and identity leakage is live and cannot be removed.** Every consult
names the PI, lab and institution in both `question` and `context_excerpt`.
Revealing author identity lowered LLM rejection recommendations by **~25% of the
mean rejection rate on identical content**, with low-prestige institutions
penalised in every field (arXiv:2509.15122, MODERATE-STRONG). In a simulation where
the hub interviews a named PI this is intrinsic. It should be stated in the docs so
it is not discovered later as a surprise: **the panel's verdicts carry a prestige
component of unknown size.** Related: "paper laundering" — LLM-polished text scores
better with no substantive change (arXiv:2605.03202).

**B. The `confidence` field should not be trusted.** Across nine models and 13
biomedical datasets, LLMs were overconfident in **84.3% of scenarios**, four models
in **100%**, only **4.8%** well-calibrated, and **fine-tuning did not fix it**
(JAMIA Open 2025, DOI 10.1093/jamiaopen/ooaf058, STRONG). The local pattern is the
signature: `blocking` is **91.9% high-confidence** while `clear` is **8 of 9
moderate**. The panel is confident when condemning and hedged when clearing. Do
not build a gate on this field.

---

## Suggested execution order

| step | items | why here | deploy consequence |
|---|---|---|---|
| 1 | **R8** (harness into the repo) | Makes every later step an experiment instead of a guess | none — new script, not in `ci.sh` |
| 2 | **R1, R2, R3** | Correct a false assertion and a live misreading; no model behaviour changes | `src/` change → `./scripts/ci.sh`, agent image rebuild, flag restart to operator |
| 3 | **R10** | Cheap, protects a property already true | test only |
| 4 | **R4** (schema reorder) | Single highest-value prompt change with real supporting evidence | persona files only; picked up on the next consult, no rebuild |
| 5 | **R6** (`legal`), then **R5** (`established`) | One at a time, ladder run before and after each | persona files only |
| 6 | **R9** (human anchors) | The only route to a validity claim | operator time, no code |
| 7 | **R7** (scale change) | Largest change, weakest evidence, breaks row comparability | needs a version stamp and an operator decision |

**Steps 4 and 5 must be done one at a time with a ladder run between**, or the
attribution is lost — which is the same discipline the rest of this audit was
conducted under.

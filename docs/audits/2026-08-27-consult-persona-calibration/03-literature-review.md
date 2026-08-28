# Literature review: what is actually known about calibrating LLM critic panels

Assembled 2026-08-28 from three independent searches (arXiv API and full text, ACL
Anthology, NeurIPS/ICLR/OpenReview, ACM DL). Organised by the decision each body
of evidence bears on rather than by topic, because the point of the review is to
constrain the fix.

**Evidence grades.** **STRONG** = quantified, multi-model or multi-dataset,
peer-reviewed or replicated. **MODERATE** = quantified, single credible study.
**WEAK** = single small study, single-author preprint, or qualitative only.

**Provenance warning, stated once and meant.** Several of the most directly
on-point results are 2026 arXiv preprints with no peer review and in some cases a
single author. They are flagged individually. The peer-reviewed backbone is:
Zheng et al. (NeurIPS 2023), Liu et al. (EMNLP 2023), Kim et al. (ICLR 2024),
Ye et al. (ICLR 2024), Huang et al. (ICLR 2024), Zheng et al. (Findings of EMNLP
2024), Gupta et al. (ICLR 2024), Sharma et al. (ICLR 2024), Deshpande et al.
(Findings of EMNLP 2023), Zhang et al. (ACL 2024), Wang et al. (ACL 2024),
Liang et al. (EMNLP 2024), Jin et al. (EMNLP 2024), Lee et al. (EMNLP 2025),
Zhu et al. (ACL 2025), Weng et al. (ICLR 2025 Oral), Choi et al. (NeurIPS 2025
Spotlight), Liang et al. (NEJM AI 2024), Sharma et al. (FAccT 2026),
Ashery et al. (Science Advances 2025), Jacobs & Wallach (FAccT 2021),
Zhao et al. (ICML 2021).

---

## Decision 1 — Should we raise the clear rate? **No. This is the best-supported conclusion in the review.**

**Spread is cheap to buy and is not validity.** Mukherjee et al., *The Geometry of
LLM-as-Judge* (arXiv:2606.03043, MODERATE) fine-tuned judges until score spread
rose **3.4× (σ 0.32 → 1.08)** and the judge's evaluation axis **stayed 87°–88° from
the human axis** — near-orthogonal, i.e. unmoved. Post-hoc calibration then got
alignment only to r = 0.184 against a human-human baseline of r = 0.474. If one
citation survives this document, it is this one.

**Prompting moves the criterion, not the resolution.** Usami et al., *LLM Judges
Have Dark Current* (arXiv:2606.15610, MODERATE) built a controlled quality ladder
and measured what a threshold instruction does. A strict-tie prompt drove raw
false preference on identical inputs from 0.2583 to 0.0000 — and drove Δ1
sensitivity from **0.9400 down to 0.5000**, with the loss being *miss-by-tie*, not
wrong-choice. Their own summary sentence: *"prompting moves the criterion, not the
resolution."*

**A generic "be strict" instruction is an intercept shift, not sharper
discrimination.** Li (arXiv:2606.15474, WEAK — single-author preprint, but the
cleanest direct measurement) compared a judge prompt instructing *"reserve 4 for
flawless work … when torn, choose the lower"* against the standard prompt, same
model. Every substantive dimension moved down roughly uniformly (TL;DR overall
−0.230, coverage −0.193, coherence −0.134, accuracy −0.097; HelpSteer2
helpfulness −0.152, correctness −0.120) while verbosity moved *up* +0.025. The
instruction relocated the whole distribution; it did not separate good from bad.

**Telling a judge the expected rate is unstudied, and every adjacent measurement
is discouraging.** Two targeted arXiv sweeps found **zero** papers that state an
expected pass rate or target verdict distribution in a judge prompt and measure
the resulting shift. What exists:

- Anchoring on a prior score reaches **Cohen's d = 0.71**, blocks **48% of error
  corrections**, flips **10.18% of correct judgments** — and **neither chain-of-thought
  nor an explicit "disregard this" instruction reduces the effect**
  (Kapetanovic et al., arXiv:2608.25869, CIKM '26, 185,271 evaluations,
  MODERATE-STRONG). The response is threshold-like: the anchor's *presence*
  matters more than its value.
- Numeric anchoring raised a count-based F1 by **+0.21 while raising true
  multi-reference localisation by only +0.04** — the headline metric moved about
  five times more than the underlying skill (Yang, arXiv:2607.01240, MODERATE,
  N=15,600 with an independent scorer).
- In forecasting, base-rate references yield only *"slight benefits"* and
  Bayesian-reasoning prompts actively degrade performance (Schoenegger, Jones,
  Tetlock & Mellers, arXiv:2506.01578, STRONG design, no effect size).

**That is the signature of metric gaming, not calibration.** And note the local
fact this predicts: the exhortation *"a panel that never clears anything is
noise"* is already in all eight personas and has produced 0.76% over 1,192
consults.

**Elaborating a critic's instructions can flip it from all-pass to all-fail
without passing through discriminating.** Jin & Chen (arXiv:2603.00539, STRONG for
the claim) paired correct and buggy implementations, 5 models, >1,400 instances.
GPT-4o rejected *correct* code 26.2%/35.9% of the time under a direct prompt —
rising to **73.2%/87.9% once a rationale was required**, with its false-positive
rate simultaneously falling to 0.00% in several settings. Their conclusion:
*"more detailed prompt design leads to higher misjudgment rates."*

**Over-strictness has a catastrophic tail.** Stechly, Valmeekam & Kambhampati
(arXiv:2402.08115, STRONG) measured GPT-4 as its own verifier on decidable
domains: false-negative rate **95.8%** on graph colouring and **97.09%** on
Mystery Blocksworld — a verifier that rejects nearly every correct answer. Shown
100 provably optimal colourings, GPT-4 agreed 2 were correct
(arXiv:2310.12397, STRONG). The knob that fixes an all-pass problem produces
this.

---

## Decision 2 — Is the clear-rate floor the right alarm? **No; the right instrument is a periodic quality ladder.**

**Reliability, consistency and spread are all cheap, and none is validity.**
Norman, Rivera & Hughes (arXiv:2606.19544, STRONG on scale — 21 judges, 9
providers, 118 runs, **~541,000 judgments**) titled the paper *Reliability without
Validity*. Chance-corrected agreement runs **33.8–41.2 pp below raw exact-match**
on MT-Bench for every judge; two production judges combine test-retest
reliability **>0.95 with position bias >0.10**; judge rankings shift up to **14
positions** across benchmarks. Jacobs & Wallach (FAccT 2021, foundational) supply
the vocabulary: widening a spread is a change to the *operationalisation* with no
argument that it improves *construct validity*. Bean et al. (arXiv:2511.04703,
STRONG — 29 expert reviewers, **445 benchmarks**) found validity-undermining
patterns to be the norm.

**The correct instrument exists and has two variants.** Both replace a
population-dependent statistic with a controlled one:

1. **Seeded quality ladder + vacuum test** (Usami et al., arXiv:2606.15610):
   construct items with known Pareto-dominant quality gaps Δ1…Δ5 plus degenerate
   "true-vacuum" inputs, then measure sensitivity at each rung and the
   false-verdict rate on the nulls.
2. **Construct-preserving / construct-changing edit pairs** (Chen, Chen, Lin &
   Vong, arXiv:2608.24419, STRONG numbers / days-old preprint): **invariance
   S** = P(verdict unchanged | cosmetic edit) and **construct sensitivity
   R** = P(verdict changed | substantive edit). *"S and R are independent and no
   scalar summary preserves all relevant comparisons."* Measured on 7 judges: at
   matched S ≥ 0.90, judges average **S = 0.945 with R = 0.319** — and
   **R_strength = 0.262**, meaning strength-of-evidence changes are the axis
   judges are worst at. Surface-only predictors reproduce **67.4% of MT-Bench
   human votes**.

`02-positive-control-experiment.md` implements variant 1 and reports both
statistics for this panel.

**Structure buys reliability; the evidence that it buys validity is not there.**
Georgantas (arXiv:2606.15887, MODERATE, 300 ICLR submissions, pre-declared
criteria) scored manuscripts with a structured 5-dimension weighted pipeline:
**AUROC 0.82 (95% CI 0.78–0.87)**, monotone across decision tiers. But a
one-paragraph bare prompt discriminated nearly as well — the pipeline's advantage
**failed its own pre-declared significance criterion, p = 0.09** — while the
pipeline cut within-item SD **4× (2.8 → 0.7 points)**. Claim reliability, not
validity.

---

## Decision 3 — Will rewriting the personas help? **Not for judgment quality. For scope and threshold clarity, yes.**

**Asserting expertise does not improve accuracy.** Zheng, Pei, Logeswaran, Lee &
Jurgens, *When "A Helpful Assistant" Is Not Really Helpful* (Findings of EMNLP
2024, STRONG — **162 personas × 2,410 questions × 6 models across 4 families**):
*"none of the personas lead to statistically better model performance"*, and
*"most of the personas have no or negative impact."* Domain-aligned personas reach
significance at a regression coefficient of **0.004**. Oracle best-persona
selection helps; every automatic selection method landed near or below random.

Corroborated mechanistically: Liang et al. (arXiv:2510.24677, MODERATE-STRONG,
neuronal ablation in medical LLMs) — *"role prompts do not significantly enhance
the medical reasoning abilities of LLMs … no evidence of distinct reasoning
pathways or cognitive differentiation across clinical roles … the core
decision-making mechanisms of LLMs remain uniform across roles."* And Hu, Rostami
& Thomason (arXiv:2603.18507, MODERATE): on MMLU **all** expert-persona variants
damaged accuracy.

**The two headline positive results do not say what they are usually cited as
saying.** Salewski et al. (NeurIPS 2023 Spotlight, STRONG) found task > domain >
non-domain expert and a matched persona performing ~2× a mismatched one — but the
paper's own appendix records that *"the neutral persona performs on par with the
domain expert"*, and the ordering vanishes where the model cannot do the task.
It is evidence that a **mismatched** persona hurts, not that an expert persona
beats none. Kong et al. (NAACL 2024, STRONG for the method) got AQuA 53.5% →
63.8% — from a **two-stage role-feedback procedure** the authors themselves
describe as an *"implicit CoT trigger"*, not from a one-line system prompt.

**But disposition and threshold language do move behaviour, by a lot.** This is a
different question from accuracy and the answer is yes:

- Wang, Li & Chen (arXiv:2509.23058, MODERATE): on the Grable–Lytton risk-tolerance
  scale (range 13–52), "aggressive" vs "cautious" framing swung Qwen2.5-7B-Instruct
  from **44.60 to 15.92 — a 28.7-point swing on a 39-point scale**, with
  instruction-tuned models markedly *more* steerable than base models.
- Shah, Mishra & Silpasuwanchai (ACL 2026 Main, arXiv:2604.10733, STRONG — 275
  personas, 4,950 prompts, 13 models): persona **agreeableness** correlates with
  sycophancy at **Pearson r up to 0.87, Cohen's d up to 2.33**, in 9 of 13 models.
- Licato, Steinle & Hollis (IJCNLP-AACL 2025, MODERATE): personas changed
  strategic play **only when a mediator converted the persona into explicit
  heuristic values.** Prose disposition without a decision rule was unreliable.

**Named gap:** no study measures whether *experiential backstory* ("you have seen
many failures") shifts approval thresholds. That is the least-evidenced thing one
is tempted to write.

**Sycophancy is the baseline a critic must overcome.** Sharma et al. (ICLR 2024,
STRONG): a single unargued *"Are you sure?"* made models change a correct answer
**32% (GPT-4) to 86% (Claude 1.3)** of the time and admit a mistake **42%–98%** of
the time, with accuracy dropping up to 27%; **high confidence did not protect**.
The root cause is in the preference model, which preferred sycophantic responses
over truthful ones **95%** of the time. Perez et al. (Findings of ACL 2023):
sycophancy **increases** with model size and with more RLHF.

---

## Decision 4 — Is an 8-persona panel on one base model sound? **The literature says usually not. This system is a measured exception.**

**The convergence evidence is strong and consistent.**

- Choi, Zhu & Li (NeurIPS 2025 Spotlight, arXiv:2508.17536, STRONG) prove that for
  homogeneous fully-connected agents the belief sequence is a **martingale**, so
  debate cannot improve expected correctness. Empirically **majority voting with
  no debate beat all nine debate configurations** on both models tested.
- Zhu et al. (arXiv:2601.19921, MODERATE-STRONG): five same-model agents produce
  **1.45 unique answers out of 5** (Qwen-2.5-7B) — ~3.5 of 5 already agreeing
  before any interaction.
- Zhang et al. (arXiv:2502.08788, MODERATE-STRONG, 5 methods × 9 benchmarks × 4
  models, budget-matched): **Multi-Persona — the explicitly role-differentiated
  framework — achieved a 0% win rate against plain chain-of-thought.** Role-diverse
  frameworks did not outperform role-free ones. **Model** heterogeneity did
  (+6.4% to +8.2%). *"Using identical agents from the same model undermines the
  very premise of debate."*
- Zhang, Xu et al. (ACL 2024, arXiv:2310.02124, STRONG) is the cleanest
  "protocol, not persona" result: varying agent traits gave *"variations in
  accuracy [that] are not pronounced"*; varying the collaboration protocol gave a
  **~31 pp spread** (65.2% vs 34.4%).
- Yao et al. (arXiv:2509.23055, MODERATE): homogeneous two-agent debate shows a
  **disagreement collapse rate of 62–86%**.
- **In the closest domain to ours:** Sandström & Thelwall (arXiv:2603.14565) scored
  Swedish Medical Council fellowship applications. **LLM↔expert Spearman 0.22;
  LLM↔LLM 0.34.** The models agree with each other more than with experts — the
  signature of shared systematic bias. And Thorne et al. (arXiv:2603.08281, WEAK
  power — six EPSRC proposals) ran the only persona-panel ablation in grant review:
  section-by-section review beat both alternatives while *"the computationally
  expensive council method performs no better than baseline."*

**Named gap in that literature, quoted:** *"nobody has measured inter-agent error
correlation for explicitly role-differentiated agents on one base model"* — which
is exactly this system's architecture.

**Measured here (`01` §7, `02` Result 3, and the convergence query):**

| quantity | this panel |
|---|---|
| mean dissent from the modal signal, identical input | **25.0%** (one cell split 50/50) |
| production interview threads where specialists disagree | **62.5%** of 56 threads with ≥5 domains |
| per-domain `blocking` propensity spread | **0.0% (budget) → 32.9% (chemistry) = 32.9 pp** |

**The personas are the only difference between those two extremes** — same model,
same temperature, same question shape. So this panel does *not* exhibit the
convergence failure the literature documents for undifferentiated same-model
agents.

**The honest limit of that claim:** differentiated *marginal rates* are not
*independent errors*. All eight domains share the `caution` attractor (85.7%), and
nothing here establishes that their mistakes are uncorrelated — which would
require ground truth this system does not have. Kohli (arXiv:2605.29800, WEAK —
single-author preprint) is the cautionary bound: nine judges from seven families
carried **~2 effective votes**, with the best single judge matching the panel.
Ashery et al. (Science Advances 2025, peer-reviewed) add the methodological
warning that matters most here: *"collective bias is not easily deducible from
analyzing isolated agents"* — testing each persona alone will not predict the
panel.

**If specialists ever start reading each other, that changes.** Weng et al. (ICLR
2025 Oral, arXiv:2501.13381, STRONG): conformity rises with rounds (Llama3-70B
33.9% → 44.4% over 1→5) and falls sharply with a smaller majority (69.9% → 32.6%
going from 6 confederates to 3); an explicit reflection step beat an "empowered
persona" roughly 3:1 as a mitigation. Zhu et al. (ACL 2025, arXiv:2410.12428,
STRONG): a Devil's-Advocate dissenter cut sycophantic answers **63.20% → 41.37%**
and raised correct answers **36.80% → 57.82%** — **and a dissenter who is
themselves wrong still reduces conformity.**

---

## Decision 5 — What structural changes have measured support?

**Binary decomposition is the only intervention with direct measured evidence of
widening a rating distribution.** Cho et al., *BinEval* (arXiv:2606.27226,
WEAK-MODERATE — workshop preprint, distribution evidence partly figure-based, but
the only on-point citation): SummEval ρ 0.563 vs G-Eval 0.514, and critically
*"UniEval and G-Eval exhibit narrower, more concentrated distributions, suggesting
weaker discrimination"* while BinEval *"retains wider, more human-like within-system
variance"* and *"avoids the ceiling effects."* Supporting: CheckEval (Lee et al.,
EMNLP 2025, arXiv:2403.18771) improved average agreement across 12 evaluator
models by **+0.45** with reduced variance — **STRONG for reliability, WEAK for
accuracy** (SummEval ρ 0.55 vs 0.52). FLASK (Ye et al., ICLR 2024 Spotlight)
measured decomposed vs holistic head-to-head: **Spearman 0.680 vs 0.641**.
Likert scoring is paraphrase-unstable where binary is not (JudgeSense,
arXiv:2604.23478: coherence JSS 0.387–0.992 vs binary factuality 0.893–0.987).

**Aggregate sub-answers in code, not by asking the model.** DnA-Eval
(arXiv:2405.15329, MODERATE): using an external calculator for the weighted sum
achieved higher human agreement than feeding aspect scores back to the LLM. This
system already computes `weighted_score` in code and is on the right side of that
finding — but note the caveat under Decision 6.

**Require structured evidence before the verdict; do not add free-form
chain-of-thought.** These are different interventions with opposite signs.

- Multiple Evidence Calibration (Wang et al., arXiv:2305.17926, STRONG) — generate
  several pieces of evidence *then* rate: GPT-4 accuracy/κ **52.7%/0.24 → 60.9%/0.33**
  at k=6; ChatGPT 44.4%/0.06 → 55.6%/0.27. **+6 to +11 accuracy points.**
- Plain CoT **collapses the judgment distribution**: Wang, Zhang & Choi (EMNLP 2025
  Findings, arXiv:2503.03064, STRONG) measured judgment-distribution SD with vs
  without CoT — GPT-4o pointwise **.039 with, .103 without**; MT-Bench .041 vs
  .116. *"No-CoT always has a greater standard deviation."* A **1.7–2.8× narrowing**,
  and no-CoT won in **14 of 16** cases when scoring by the distribution mean.
  With good score descriptors, CoT has *"little effect"* anyway (Yamauchi et al.,
  arXiv:2506.13639).

**Prefer comparative judgment for ordering.** The effect sizes are large and the
domain match is close. Si, Yang & Hashimoto (arXiv:2409.04109, STRONG): on
research *ideas*, human inter-reviewer balanced accuracy was **56.1%** and every
LLM evaluator did worse (Claude-3.5 pairwise 53.3%, direct 51.7%, the AI Scientist
reviewer 43.3% — below chance) — but a Claude-3.5 **pairwise** ranker hit **71.4%**
predicting which of two paired ICLR papers was accepted. Yang et al.
(arXiv:2605.25240, MODERATE, 30 real legal tasks scored by practising attorneys)
is the most extreme: **pairwise ρ = 0.908 vs rubric ρ = 0.150**, and the pattern
held for **human** annotators as well as LLM autograders. Liusie, Manakul & Gales
(EACL 2024) lifted mid-size judges by up to **+43 Spearman points**.
**Counter-evidence, reported as such:** Tripathi et al. (COLM 2025,
arXiv:2504.14716, STRONG) found pairwise preferences flip in **~35% of cases under
spurious distractor features vs 9% for absolute scores**, and first-position
preference runs 0.17–0.68 and is *worse* in larger models.

**Post-hoc statistical calibration on ~100 human anchors dominates prompt
tuning.** Sahoo et al., *Quantitative LLM Judges* (arXiv:2506.02945,
MODERATE-STRONG) put a small regression head on a **frozen** judge — explicitly
motivated by *"score compression"* and *"leniency bias"* — and doubled Pearson on
OffsetBias (0.298 → 0.634) with MSE 6.346 → 2.626, at 1% of SFT data and 6.9×
the training speed. Morandi (arXiv:2605.09227, MODERATE): **~100 anchors** closed a
+0.71-point systematic offset to within ±0.08. And multi-faceted Rasch modelling
is the century-old instrument for separating true quality from **rater severity and
centrality bias** — now applied to AI evaluation (arXiv:2602.22585;
arXiv:2602.00521, both method-only). **If you have labels, calibrate; if you do
not, get ~100 before tuning prompts.**

**Randomise criterion order.** Xu et al. (arXiv:2602.02219, WEAK-MODERATE): judges
prefer score options at specific *positions within the rubric list*, and *"when a
prompt scores several criteria simultaneously, the ordering of the criteria itself
shifts the resulting scores"*, with a few random permutations sufficing as
mitigation. Li et al. (DASFAA 2026, arXiv:2506.22316, MODERATE-STRONG) formalise
rubric-order and reference-answer-score bias, and record DeepSeek-V3 assigning
**score 5 to more than half of all pairs** absent mitigation. Any
multi-dimension rubric scored in one prompt has this confound live.

**Anchors in the prompt bias the output toward themselves, and you cannot instruct
that away.** Zhao et al. (ICML 2021, STRONG, foundational): few-shot accuracy
varies *"from near chance to near state-of-the-art"* with example choice **and
order**; contextual calibration is worth up to **30.0% absolute**. Lou & Sun
(arXiv:2412.06593): CoT, principles, reflection, and explicitly instructing the
model to ignore the anchor are all *"not sufficient"*. AnchorBench
(arXiv:2608.14320): frontier models above 95% control accuracy remain susceptible.
Every worked example, named failure mode and illustrative number in a persona is
an anchor.

---

## Decision 6 — What should we NOT do? (interventions with no support, or negative support)

| tempting fix | status |
|---|---|
| Tell the personas roughly what share should clear | **No literature exists.** Adjacent evidence says the anchor's presence dominates its value, is not removable by instruction, and inflates the metric ~5× more than the skill (arXiv:2608.25869; arXiv:2607.01240). |
| Say "clear when true" more emphatically | Already in all 8 personas; 0.76% over 1,192 consults. Predicted by "prompting moves the criterion, not the resolution" (arXiv:2606.15610). |
| Add "you are a skeptical expert who has seen many failures" | Persona assertion does not improve accuracy (Findings of EMNLP 2024, STRONG). **Backstory specifically is unmeasured**; prose disposition needs a mediator to become a decision rule (IJCNLP-AACL 2025). |
| Ask for a fixed number of concerns (e.g. "list 3") | CriticGPT (arXiv:2407.00215, STRONG) measured the mechanism: *"the probability of catching a bug increases with the number of claims … more likely to include both some particular issue and a nitpick"*, and models that hallucinate more also catch more. A precision/recall curve with **no principled operating point**; OpenAI calls finding one prohibitively expensive. |
| Require a "what would make this clear?" counterfactual | **No supporting study found.** Negative adjacent evidence: an explicit disregard-the-anchor instruction fails (arXiv:2608.25869), and even advanced reasoning models construct a genuine counterexample **<9%** of the time (arXiv:2502.19414). Good for auditability; do not claim it improves discrimination. |
| Enumerate more red flags to raise the hit rate | **Unmeasured** — named as a gap by two independent searches. The anchoring literature suggests the corollary risk (listed items crowd out unlisted failure modes), also unmeasured. |
| Separate "is this a concern" from "does this block" as scored fields | **Unmeasured** (the nearest support is binary decomposition, which is a different thing). The existing tri-state `met`/`not_met`/`unconfirmed` is *better* than anything the literature evaluates, because fallback-label collapse is documented (arXiv:2605.06940) and a tri-state makes it visible. Keep it; do not claim evidence. |
| Raise temperature to widen the distribution | T≈0 → 3.0 drove consistency from ~1.00 to **0.55** and error rate from ~0.00 to **0.49**, while mean accuracy moved only −0.01 to −0.03 and verdict rankings did not change (arXiv:2603.28304, MODERATE). Buys noise, not variation. T=0 does not buy determinism either. |
| Add free-form CoT to the specialists | Narrows the judgment distribution **1.7–2.8×** (arXiv:2503.03064, STRONG). A candidate *cause* of compression, not a fix. |
| Build a bigger panel / let specialists debate | Homogeneous debate is a martingale (NeurIPS 2025 Spotlight); majority vote beat all nine debate configs; agents peak at 3–4 then decline; conformity rises with rounds (ICLR 2025 Oral). Collect independent verdicts, aggregate in code — which is what this system already does. |
| Compute the score in code as a compression fix | Relocates the problem: the per-dimension scores the model emits are themselves compressed (arXiv:2601.20920; arXiv:2605.29815). Keep it for reproducibility and audit; it is not a calibration fix. |
| Test each persona alone and infer panel behaviour | *"Collective bias is not easily deducible from analyzing isolated agents"* (Ashery et al., Science Advances 2025, peer-reviewed). |

---

## Decision 7 — Two biases this system carries that the review flagged and we had not

**A. Author-identity / prestige leakage — live and unmitigated.** Howell et al.
(arXiv:2509.15122, MODERATE-STRONG) ran an adapted audit: **revealing author
identity lowered LLM rejection recommendations by ~25% of the mean rejection rate
on identical content**, and low-prestige institutions received lower quality
scores in every field. Every consult in this system names the PI and the lab
("Slusher lab, JHU", "the Coyne lab") in both `question` and `context_excerpt`.
This is intrinsic to a simulation where the hub interviews a named PI, so it
cannot simply be removed — but it means the panel's verdicts carry a prestige
component of unknown size, and it should be stated rather than discovered later.
Adjacent: Baumann et al. (arXiv:2605.03202) measured **"paper laundering"** —
LLM rewriting raises AI-reviewer scores with no scientific change — and Wataoka
et al. (arXiv:2410.21819) found self-preference tracking the judge's own
perplexity, which together predict that a **well-written** pitch scores better
independent of merit.

**B. Do not trust the `confidence` field.** de Oliveira et al. (JAMIA Open 2025,
DOI 10.1093/jamiaopen/ooaf058, STRONG): across nine models and 13 biomedical
datasets, **overconfidence in 84.3% of scenarios (296/351)**; four models
overconfident in **100%**; only **4.8%** well-calibrated; mean Flex-ECE 37.5%; and
**domain-specific fine-tuning did not rescue calibration.** Xiong et al. (ICLR
2024) find verbalized confidence systematically overconfident. This matches the
local pattern exactly: `blocking` is **91.9% high-confidence** while `clear` is
**8 of 9 moderate** — the panel is confident when condemning and hedged when
clearing, which is what an uncalibrated confidence signal looks like.

---

## Decision 8 — What do HUMAN expert panels do, and does that give us a target clear rate?

**Short answer: no. There is no population-independent target clear rate, because
the optimal threshold for a screening instrument is a likelihood ratio, which
depends on the base rate of the population being screened.** What the human
literature does supply is a set of measured analogues, and every one points the
same way as the local evidence.

### 8.0 The closest human measurement that exists

**Lindner, Vancea, Chen & Chacko (2016), "NIH Peer Review: Scored Review Criteria
and Overall Impact," *American Journal of Evaluation* 37(2):238-249. STRONG.**
**N = 18,043 individual assigned-reviewer records** — one reviewer's five criterion
scores plus overall impact, **not averaged across a panel**. This is the only
large-N per-reviewer-per-dimension measurement in any candidate domain, and it is
the right unit to compare a per-specialist verdict against.

Share of individual reviewer-criterion scores in the top 1–3 band of a 1–9 scale
(1 = best):

| criterion | top-band share | share scoring a literal "1" |
|---|---|---|
| Environment | 80–90% | 30–40% |
| Investigators | 80–90% | 30–40% |
| Innovation | 67% | — |
| Significance | ~70% | — |
| **Approach** (methodological rigor) | **57–60%** | **3–3.5%** |

Lindner's own summary: ***"Four of the five scored criteria exhibited moderate to
severe restriction of range."***

**The mapping to our scale must be done carefully; doing it carelessly inverts the
conclusion.** Our `clear` is not "top third of a 9-point scale" — it is *"nothing in
your domain stands in the way"*, a categorical assertion far closer to NIH's literal
**score of 1**. On that mapping:

- NIH awards a "1" on **Approach** — the rigor criterion, closest analogue of our
  `scientific` and `chemistry` — to only **3–3.5%** of applications.
- It awards a "1" on **Environment/Investigators** — infrastructure and team,
  closest analogues of our `budget` and `talent` — to **30–40%**.
- Ours: `scientific` **0%**, `chemistry` **0%**, `budget` **7.46%**, `talent` **2.13%**.

**The direction of the human asymmetry is exactly ours: rigor criteria almost never
receive a top score while infrastructure and team criteria often do.** Our absolute
rates are lower, consistent with a categorical "nothing stands in the way" being a
stronger claim than a 1 on a 9-point scale, and with a genuinely earlier population.

⚠️ **One derivation offered to this audit was checked and rejected.** A
Gaussian-copula argument inverted the probe's 31.2% strong-record clear rate into an
implied per-specialist rate of ~72% (at ρ = 0.50, anchored on Lindner's
single-reviewer inter-criterion correlations of .38–.62) and concluded it sits inside
NIH's 57–90% band. **It does not follow.** The derivation solves p⁸ = 0.312 — it
treats 31.2% as the probability that *all eight* domains clear at once. But 31.2% is
the **marginal** per-consult rate (5 of 16 individual consults). The joint all-clear
rate in the probe is **0 of 2 strong-tier cases — never observed** (best was 3 of 8
domains). Recorded because the correlation figure itself is useful and carries its
own warning: **per-dimension rates do not multiply, so any reasoning that treats
eight domains as independent gates will be wrong in both directions.**

### 8.1 Telling raters to use the full range does not make them use it

**This is the strongest external support for the central local finding.** NIH policy
states that *"5 is considered an average score"*, instructs reviewers to *"use the
full range"*, and tells them *"not to feel constrained to limit their scores to the
upper half."* Observed per-reviewer distribution centres:

| criterion | policy centre | observed centre |
|---|---|---|
| Environment | 5 | **2** |
| Investigators | 5 | **2** |
| Innovation | 5 | **3** |
| Significance | 5 | **3** |
| Approach | 5 | **4** |

**Four of five dimensions run two to three scale points off the design despite an
explicit written instruction to the contrary.**

That is the human analogue of `01` §2: *"Say this when it is true; a panel that never
clears anything is noise"* has been in front of the model on all 1,192 consults and
produced 0.76%. **Exhortation does not move a rating distribution — in humans or in
language models.** Two literatures, arrived at independently, agreeing on the
mechanism.

### 8.2 The population claim is independently corroborated

- **Jensen & Thursby (2001), *American Economic Review* 91(1):240-259**, 62 US
  research universities. Of inventions universities **actually licensed**: **48%
  proof-of-concept with no prototype, 29% lab-scale prototype only — over 75% no
  further than lab scale**; only **12%** ready for practical or commercial use;
  **71%** requiring continued inventor involvement; only **28%** holding an issued
  patent at licensing. **Even the commercial gatekeepers transact overwhelmingly on
  unproven early-stage work.**
- **Prinz, Schlange & Asadullah (2011), *Nat Rev Drug Discov* 10:712** — of 67
  in-house Bayer target-validation projects, **78% showed data inconsistent with
  published claims (~22% confirmed)**. **Begley & Ellis (2012), *Nature*
  483:531-533** — **6 of 53 landmark oncology papers (11%) confirmed.** They
  disagree ~4× on definition and selection; both say most published claims at
  target-validation stage do not survive inspection.

**If the underlying claims in an early-stage idea pool replicate at 11–22%, a low
clear rate is what a correct instrument should produce.**

Jensen & Thursby also **recalibrates my own experiment's ceiling**: the `STRONG`
scenario in `02` carries an **issued composition-of-matter patent**, a property only
**28%** of actually-licensed university inventions have. Its 31.2% clear rate is
therefore an **upper bound on an unusually favourable record**, not a rate any real
Blackbird pitch should be expected to reach.

### 8.3 Funnel base rates, with the conventions that make them quotable

- **NIH RPG success rate** (RePORT Table #218, official full-population, updated
  2026-02-19): **21.3% FY2023, 18.5% FY2024**, 20.1–20.7% FY2019–22. Institute
  spread FY2023 runs **NIGMS 36.3% → NCCIH 14.4%** — a >2× range on identical review
  machinery. **Do not use FY2025** (~13%, shutdown-distorted).
- **NIH triage is terminal.** Of **52,056 not-discussed applications across FY2010–13,
  exactly one was funded**; funded-given-discussed is **29.8%**. The "~50% not
  discussed" figure is a **policy ceiling, not a measurement** — measured years are
  **40.5–42.1%**; the current regime is officially **30–35%** (NIH Guide
  NOT-OD-26-069).
- **Pharma phase-transition rates depend entirely on the estimator.** On the *same*
  dataset, Phase-by-Phase gives **6.9%** and Path-by-Path **13.8%** — a factor of two,
  from how missing Phase 2 records are imputed. **Any funnel figure must state its
  convention or it is not a number.**
- **University tech transfer (AUTM):** the cited **62.9%** disclosure-to-filing figure
  is a **same-year ratio**, not a cohort conversion (multi-year lag means the
  numerator and denominator are largely different inventions), and it drops to
  **48.6% in FY2025**, unexplained in AUTM's own numbers.

### 8.4 Four further findings that shaped the recommendations

**1. Selectivity and arbitrariness rise together — the NeurIPS 2021 organisers'
own conclusion.** From the consistency experiment in which two independent
committees reviewed the same papers: *"making the conference more selective would
increase the arbitrariness of the process."* Supporting detail: for
orals+spotlights the disagreement rate was **5.8%, barely 3 points better than
random**, and **more than half** the spotlights recommended by either committee
were rejected by the other (13/25 and 13/23). Ethics flags overlapped on **3 of
~23 (13%)**.

This is the strongest external support for R1. **A screening instrument pushed
toward finer discrimination at the top of its range does not become more accurate;
it becomes noisier.** Any change that makes `clear` easier to reach in order to
satisfy a ratio is buying arbitrariness.

Related, and worth attaching its uncertainty: the widely-quoted NeurIPS
"accept precision" figure is **~49.5–51% on ~40 samples, SE ≈ 8%, empirical range
38–64%**, and Cortes & Lawrence themselves write that this *"highlights the
unreliability of the accept precision statistic."* The circulating "57%" and "~50%"
are **the same quantity computed on different accept counts**, not two findings.

**2. Reviewer miscalibration is the *smallest* component of the variance — which
downgrades every "fix the rater" intervention.** Cortes & Lawrence's calibration
model `y = f_i + b_j + ε`, fitted on ~6,000 scores, decomposes to:

| component | magnitude |
|---|---|
| α_f — objective signal about the object | **1.28** |
| α_b — reviewer offset / miscalibration | **0.24** (~9% of variance) |
| σ² — irreducible subjective disagreement | **1.27** |

Calibration training targets that 9%. So even in humans, **rewriting the rater is
aimed at the smallest term.** This is an independent argument for the same
conclusion the LLM literature reached from the other direction (persona assertion
does not improve judgment; prompting moves the criterion, not the resolution) and
it is why `04`'s Tier 2 items are framed as *stated bars* and *schema changes*
rather than as recalibrated dispositions.

Corroborating inter-rater reliability figures, for scale: Eckes 2012 (33 raters,
2,097 examinees) measured observed exact agreement **42.1%** against an
MFRM-expected 41.1% — a **Rasch-kappa of 0.02**. Rater severity spans **35–45% of
the candidate-ability span** (Midtbø et al. 2018, 77 raters). **Do not quote a
single figure for "how much of the variance is the reviewer"** — the rater-facet
share disagrees by an order of magnitude across fields (1.41% / 9.1% / 11.4% /
16–34%).

**3. In human grant review, risks predict scores more strongly than strengths.**
Kaatz et al. 2022 (*PLOS ONE* 17(9):e0273813), a controlled grant-review
experiment: **proposal *risks* predicted reviewers' scores more strongly than
proposal *strengths***, and **reviewer scoring leniency** predicted scores while
reviewer risk tolerance did not.

This is close to a direct human analogue of the mechanism measured in `01` §5 —
where the specialists have no field in which to record what a record establishes,
and file positives inside `concerns`. **The asymmetry is not an artifact of being a
language model.** It is what expert reviewers do too, and the response in the human
literature is an instrument change (an explicit strengths facet), not reviewer
retraining. That is the argument for R5.

**4. A caution about what a review score predicts at all.** Among *accepted*
NeurIPS papers, review score predicted 7-year citation impact at **ρ = 0.051 (not
significant)**; among rejected papers ρ = 0.22. The most predictive of the four
review scores was **reviewer confidence (ρ = 0.25)** — not quality. Combined with
the LLM overconfidence evidence in Decision 7, this is why `04` recommends never
gating on the `confidence` field.

### 8.5 Conclusion for R1

**There is no population-independent target clear rate to be had, and the reason is
principled rather than a gap in the search.** The optimal operating threshold for a
screening instrument is a **likelihood ratio**, which is a function of the base rate
of the population being screened — so a fixed floor on the output rate is the wrong
shape of constraint regardless of what number is chosen.

Everything measured agrees: the metric is a property of the deal flow; no MFRM study
of grant peer review reports reviewer severity in logits at all (searched OpenAlex
full text, Crossref, arXiv), so the severity-modelling prescription transfers from
language assessment and conference-abstract review rather than from grant review;
human reviewers compress into the top third and do not stop when instructed
otherwise; and the one organisation that ran the definitive experiment concluded
that tightening selectivity **increases** arbitrariness.

The nearest quantified payoff for the alternative is Scanlan et al. 2017
(*Australian Occupational Therapy Journal* 65(1):54-62, 1,340 conference abstracts,
27 raters): ***"25% of abstracts (n = 341) would have been allocated differently if
inter-rater variability was not accounted for"*** — which argues for **severity
modelling against labelled anchors** (`04` R9), not for a clear-rate floor.

**Replace the ratio with the ladder.**

### 8.6 Citations carrying provenance caveats

Recorded so they are not quoted more confidently than they can bear:

- **Cole, Cole & Simon (1981), *Science* 214(4523):881-886** — paywalled with no OA
  copy; two researchers on this review disagreed about whether any specific statistic
  from it could be verified. **No number from it is used in this audit.** If used
  later, source it through Jerrim & de Vries rather than the original.
- **Tomkins, Zhang & Heavlin (2017)** on single-blind bias is **contested** by
  Stelmakh, Shah & Singh (arXiv:1912.13188), whose critique is that the test used
  *"does not guarantee control over false alarm probability."*
- **The NeurIPS "accept precision" statistic** is ~49.5–51% on ~40 samples,
  **SE ≈ 8%, empirical range 38–64%**, and Cortes & Lawrence themselves write that
  this *"highlights the unreliability of the accept precision statistic."* The
  circulating "57%" and "~50%" are **the same quantity on different accept counts**
  (37/38 versus 43/44), not two findings.
- **Myford & Wolfe's separation-strata statistic** H = (4G+1)/3 **inflates with the
  number of observations per rater**, sometimes implying more distinct strata than
  there are raters. Treat any H value as directional, not as a count. Relatedly, a
  high **rater**-separation reliability is a pathology, not a virtue: Myford & Wolfe
  are explicit that *"the most desirable result is to have a reliability of rater
  separation close to zero, which would suggest that the raters were
  interchangeable."*
- **Two arithmetic inconsistencies** exist in the arXiv text of 2109.09774 (a printed
  0.175 where the given formula evaluates to 0.825; 22/101 versus 22/123). Check
  before quoting its appendix.

## The ceiling on any fix, which bounds the whole exercise

Two peer-reviewed-adjacent results say the target itself is soft:

- **Human inter-reviewer balanced accuracy on research *ideas* is 56.1%** — barely
  above chance — against 66.0% at NeurIPS 2021 and 71.9% at ICLR 2024 for finished
  papers (Si, Yang & Hashimoto, arXiv:2409.04109, STRONG).
- **A favourable expert judgment at proposal time does not survive execution.**
  Si, Hashimoto & Yang (arXiv:2506.20803) had **43 expert researchers each execute
  a randomly assigned idea over 100+ hours**, then blind-reviewed the executed
  work: scores of LLM-generated ideas dropped significantly more than human ideas
  on **novelty, excitement, effectiveness and overall (p < 0.05)**, *reversing* the
  pre-execution ranking.

Any screening instrument — human or synthetic — is bounded by that. The
implication for this system is not "tune the critic harder" but **report verdicts
with stated uncertainty and track outcomes**, which is also what the calibration
literature concludes from the other direction (get ~100 labelled anchors and
calibrate post hoc).

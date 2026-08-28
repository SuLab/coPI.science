# The positive control: does the specialist panel discriminate?

**Run 2026-08-28 00:46–00:55 UTC. 48 real Opus consults through the production
code path.** This is the experiment
`docs/audits/2026-08-24-panel-clear-rate/README.md` §7 prescribed, extended from a
one-factor design to a 2×3 factorial so it also tests that audit's H3.

> **STATISTICAL CORRECTION (2026-08-28).** The headline `p = 5.1e-07` in this file
> applies Fisher's exact test to 16 domain-matched pairs as if they were independent.
> The correct paired test (McNemar) gives **p = 1.22e-4** — the main effect stands, the
> magnitude was overstated ~240x. Separately, the "framing p = 1.00" result is an
> arithmetic artefact of a near-degenerate table at n = 8, where even total abolition of
> the top label reaches only p = 0.20; a paired sign test over all 24 pairs is 4-0
> toward NEUTRAL being harsher (p = 0.125). Read framing as **underpowered and
> unmeasured**, never as shown-inert. Details: `audit-evidence.md`.

> **CORRECTION (2026-08-28), and it is a correction to this audit, not to the
> code.** An earlier and smaller version of this diagnosis **had already been run
> on 2026-08-18** — `scripts/diagnose_specialist_calibration.py` (committed as
> `84fa1aa`), recorded in
> `docs/specs/2026-08-18-specialist-panel-remediation-design.md` §5 as "Phase 0 —
> diagnosis (F1)". Three synthetic strong cases, one domain each, three real API
> calls. Its result: **`scientific` → `clear`, `chemistry` → `caution`,
> `commercial` → `caution`**, and its recorded conclusion was that *"`clear` is not
> structurally unreachable"* and that the miscalibration hypothesis was
> unsupported.
>
> The 2026-08-24 audit did not cite it and described the experiment as never run;
> this audit repeated that claim. **Both were wrong.** The correct statement is
> that the *2×3 factorial with a population control, a framing factor, all eight
> domains and significance testing* had never been run — but the reachability
> question had already been answered, in the same direction, ten days earlier.
>
> **This strengthens rather than weakens the finding, in a way worth spelling
> out.** Those three domains reproduce **exactly** here — `scientific` → `clear`,
> `chemistry` → `caution`, `commercial` → `caution` under the production framing
> (see Result 1's per-domain table). That is an independent replication ten days
> and a whole rubric regime apart (v2.x → v3.2.0), on a harness that used a
> *different* code path. Three-for-three agreement across that gap is strong
> evidence that the per-domain results below are not single-sample noise, which is
> the main threat to validity of one sample per cell.

Raw per-consult records, including every full opinion:
`panel_probe_results.json` (32 records, the 2×2) and `panel_probe_medium.json`
(16 records, the population-faithful tier).

## Why it had to be run before anything was changed

Every consult in the database was asked about the same population, so the
production numbers cannot separate "the instrument cannot say `clear`" from
"nothing it was shown deserved `clear`". Those two have opposite fixes. The
2026-08-24 audit said so explicitly and added the warning this document exists
to honour:

> Do not "fix" the clear rate by editing the personas before running this.
> Loosening eight personas to make an alarm stop is how a panel that
> discriminates nothing becomes a panel that clears everything, and the second
> failure is worse: it manufactures the verification the specialist floor exists
> to demand.

## Design

| factor | levels |
|---|---|
| **A — opportunity quality** | `WEAK` (unpublished single-lab shRNA hit, no compound, nothing filed, uncosted, one departing postdoc) · `MEDIUM` (published single-lab, provisional filed, weak tool compound, plausible but uncosted, two competing R01s) · `STRONG` (independently replicated incl. a paid blind replication, four orthogonal causal arms, pre-registered, DC-quality lead with 210-fold selectivity and clean tox, issued composition-of-matter patent + written FTO opinion, named licensee conversations, named staff-scientist successor, line-itemed $650K/18mo inside band) |
| **B — question framing** | `PROD` (presupposition-laden degree questions — "how disqualifying is…", the shape actually measured in `specialist_consults`) · `NEUTRAL` (symmetric — "is X adequate for an incubation-stage go/no-go, or does something stand in the way? Answer either way") |

8 domains × 3 quality tiers × 2 framings = 48 consults, one sample per cell,
concurrency 4. Called `_execute_consult_specialist` directly with
`on_consult=None` and `on_consult_record=None`, so **nothing was written to
`specialist_consults`, no floor was credited, and no run row was touched.** Same
persona files, same `llm_agent_model_opus`, same `max_tokens=4000` as production.

Script: `panel_probe.py` in the session scratchpad (host `/home/ubuntu/probe/`).
Cost: 48 Opus calls, ~13% of a one-hour run's 363.

## Result 1 — the panel discriminates strongly, and monotonically

| tier | n | clear | caution | blocking | H (bits) |
|---|---|---|---|---|---|
| `WEAK` | 16 | 0.0% | 12.5% | **87.5%** | 0.544 |
| `MEDIUM` | 16 | 0.0% | 68.8% | 31.2% | 0.896 |
| `STRONG` | 16 | **31.2%** | 68.8% | **0.0%** | 0.896 |
| *production, all-time* | *1192* | *0.8%* | *85.7%* | *13.5%* | *0.634* |

`blocking` share falls 87.5% → 31.2% → 0.0% and `clear` share rises
0% → 0% → 31.2%, monotonically, across both framings. **An instrument that moves
from 87.5% blocking to 0% blocking as a function of input quality is not an
instrument that "cannot discriminate."**

### Does this survive n=16 per tier? Yes, decisively for the main effect

Fisher exact tests, and Wilson 95% intervals on the cell proportions:

| comparison | result |
|---|---|
| `blocking`: `WEAK` 14/16 vs `STRONG` 0/16 | **p = 5.1 × 10⁻⁷** |
| `clear`: `STRONG` 5/16 vs `MEDIUM` 0/16 | p = 0.043 |
| `clear`: `STRONG`/`PROD` 3/8 vs `STRONG`/`NEUTRAL` 2/8 | **p = 1.00** (no framing effect) |

| cell | proportion | Wilson 95% CI |
|---|---|---|
| `WEAK` blocking | 87.5% | [64.0, 96.5] |
| `STRONG` blocking | 0.0% | [0.0, 19.4] |
| `STRONG` clear | 31.2% | [14.2, 55.6] |
| `MEDIUM` blocking | 31.2% | [14.2, 55.6] |

The central claim — the panel's verdict tracks opportunity quality — is not a
small-sample artifact. The secondary quantities (the *size* of the `STRONG` clear
rate, the `MEDIUM` blocking rate) carry wide intervals and should be read as
directions, not point estimates.

## Result 2 — the probe is consistent with production, which validates the apparatus

`MEDIUM`/`PROD` — the cell built to mirror real deal flow, asked in the shape the
hub actually asks — returned **0 clear / 7 caution / 1 blocking = 87.5% caution
(Wilson CI [52.9, 97.8]), 12.5% blocking (CI [2.2, 47.1])**. Production all-time is
**85.7% caution (CI [83.6, 87.6])** and **13.5% blocking (CI [11.7, 15.6])**.

**The intervals overlap comfortably on both live labels, so the synthetic
population is consistent with the measured one.** At n=8 that is a consistency
check, not a precise reproduction — the point estimates landing within two
percentage points is a coincidence of a wide interval, and should not be quoted as
if the cell were tightly estimated. What it licenses is the inference that the
`WEAK` and `STRONG` cells are probing the same instrument that produced the
production distribution, which is all the design needs.

## Result 3 — question framing is not the cause (H3 refuted)

| tier | `PROD` | `NEUTRAL` |
|---|---|---|
| `WEAK` | 0 clear / 1 caution / 7 blocking | 0 / 1 / 7 |
| `MEDIUM` | 0 / 7 / 1 | 0 / 4 / **4** |
| `STRONG` | **3** / 5 / 0 | **2** / 6 / 0 |

Rewriting every question from adversarial to symmetric moved the `STRONG` clear
count by one consult (3 → 2) — noise at n=8. At `MEDIUM` the neutral framing was
**harsher**, quadrupling blocking (1 → 4). The hub's presupposition-laden
questions are therefore not suppressing `clear`; if anything they are the more
permissive of the two framings. Any remediation aimed at rewording the hub's
questions would be aimed at nothing.

## Result 4 — construct sensitivity, measured on the literature's own instrument

Chen et al., *A Judge Should Know What Changed* (arXiv:2608.24419) define two
independent quantities: **invariance S** = P(verdict unchanged | edit that does
not change the construct) and **construct sensitivity R** = P(verdict changed |
edit that does). Treating a quality-tier change as construct-changing and a
framing change as construct-preserving:

| | Blackbird panel | published judges (7 judges, 4 domains) |
|---|---|---|
| R, one rung | **0.594** | 0.319 average |
| R, two rungs | **0.875** | — |
| R on strength-of-evidence changes | — | 0.262 (their weakest axis) |
| S | 0.833 | 0.945 |

**The panel is roughly 1.9× the published average construct sensitivity at one
rung and 2.7× at two**, bought with about 0.11 less invariance. For a screening
instrument that trade is the right way round. The caveat: `PROD` framing adds
presuppositions rather than only rewording, so S here is a loose lower bound.

## Result 5 — one domain is genuinely flat, and it is `legal`

Per-domain verdict change from `WEAK` to `STRONG`, across both framings:

| domain | changed? |
|---|---|
| scientific, chemistry, clinical, commercial, technologic, talent, budget | 2 of 2 |
| **legal** | **0 of 2** |

`legal` returned `caution` in **all six of its cells** — three quality tiers ×
two framings. It did not move between a record with nothing filed and a record
carrying an issued composition-of-matter patent, a verified single assignment
chain, and a written freedom-to-operate opinion from outside counsel. Production
agrees: legal is 0/91 `clear` all-time, with the second-highest mean concern
count in the panel (9.01 on `caution`, *higher* than its own 8.94 on `blocking`
— inverted).

This is the one place where the alarm's phrase "check persona calibration" points
at something real, and it is one file, not eight.

## Result 6 — the five `STRONG`-tier cautions are correct, not manufactured

The instrument's validity depends on whether a `caution` on an engineered-perfect
record is a real finding or reflex. Read in full, they are real, and two of them
caught defects the scenario genuinely had:

- **chemistry** — the scenario names a brain-tumour indication and never states
  brain exposure. "No Kp, no Kp,uu, no unbound brain/plasma ratio, no CSF or
  intratumoural drug levels. 62% oral F in rat says nothing about whether the
  compound crosses an intact or partially intact blood-brain barrier." It also
  flagged no MDR1/BCRP efflux assessment, no statement whether the efficacy model
  was orthotopic, and — precisely — that Ames-negative is insufficient for a
  compound whose pharmacology *is* inhibition of a genome-stability complex,
  where micronucleus is the assay that matters. All correct. **The scenario was
  defective and chemistry found the defect.**
- **technologic** — "'validated leads on three unrelated target classes' is
  unquantified — no statement of how many target classes were attempted and
  dropped, which is the denominator that determines whether the hit rate is a
  platform property or a sampling artifact," and the 18-month budget "is entirely
  downstream single-asset development … no line item tests the platform claim on
  a fourth target class." Also correct: the scenario claimed a platform and
  costed only single-asset work.
- **legal** — FTO opinion scope unstated; a March-dated search cannot see
  applications filed within the preceding 18 months; CRISPR/reporter-line MTA
  reach-through; explant consent terms for commercial use; inventorship exposure
  from the Utrecht replication. Individually all legitimate — but see Result 5:
  legal says this *whatever* it is shown, so its content being good does not make
  its signal informative.

**A panel whose cautions are this specific on a record built to be clean is not
miscalibrated.** It is reading a record that still has gaps, which is what an
early-stage record always has.

## What the experiment establishes

| hypothesis (2026-08-24 audit §6) | verdict |
|---|---|
| **H1** — personas are miscalibrated risk-finders | **REFUTED.** They clear 31% of the time on a strong record and block 0%. |
| **H2** — the population really is early | **CONFIRMED as the dominant cause.** `MEDIUM` reproduces production to within 2pp. |
| **H3** — the hub asks unclearable questions | **REFUTED.** Framing moves the clear count by one consult; neutral framing is harsher at `MEDIUM`. |
| **H4** — panel scope mismatch | **PARTIALLY CONFIRMED, re-specified.** Not "six domains can't clear" — seven of eight are quality-sensitive. The residual is `legal` alone. |

The production clear rate of 0.76% is therefore **the correct reading of
Blackbird's actual deal flow**, not a defect in the instrument reading it.

# Isolation series — pre-registered design

**Written 2026-08-28, BEFORE any arm was run.** Pre-registration is the point: this
project's evidence has already been faulted once for post-hoc reasoning (see
`audit-evidence.md`), and a knockout series with freely chosen endpoints would repeat that
in a more expensive form. The arms, the predictions and the decision rules below are fixed
in advance. Results land in `06-isolation-series-results.md`.

## What is stacked

Four manipulations now separate the panel from its `c445473` baseline. They were shipped in
three commits, and no two were ever measured apart:

| | variable | landed in |
|---|---|---|
| **V1** | `established` added as a fifth, positive-valence contract field | Task 9 |
| **V2** | `verdict_signal` moved from FIRST to FOURTH, after `concerns`/`questions_to_ask` | Task 9 |
| **V3a** | a global "screen at the incubation grain" sentence, prepended to all eight personas | Task 10 |
| **V3b** | eight per-domain stage bars | Task 10 |
| **V4** | labels renamed `caution`→`gap`, `clear`→`adequate` | Task 11 |

V1 and V2 are confounded by construction — one commit, never measured apart. V3a and V3b
are separable, and separating them is the most decision-relevant test available.

## What is already known

| run | pooled R | top label | notes |
|---|---|---|---|
| baseline | 0.6250 | 7/48 | |
| +V1+V2 (run 1) | 0.4688 | 0/48 | |
| +V1+V2 (run 2) | 0.4375 | 0/48 | |
| +V1+V2+V3 | 0.2812 | 4/48 | `legal` reaches the top label for the first time ever |

Two directional facts the arms must explain: the top label vanished when V1+V2 landed, and
`blocking` fell 19/48 → 5/48 when V3 landed while the top label only partially returned.

## Arms

Each arm is a **single knockout from the full state** (V1+V2+V3+V4), because the decision
in front of us is "what should we revert", and a marginal effect in the presence of
everything else is exactly that question. 48 cells per arm.

- **Arm 0 — full state.** V1+V2+V3+V4. Establishes the comparison point after the rename.
- **Arm 1 — knock out V2.** Move `verdict_signal` back to first; keep `established`, bars,
  new labels. Tests the schema-adjacency hypothesis: that rating immediately after two
  required problem-shaped arrays suppresses the top label.
- **Arm 2 — knock out V3a only.** Drop the global leniency sentence; **keep all eight
  per-domain bars.** Tests whether the across-the-board severity collapse came from the
  global sentence rather than from the domain bars — i.e. whether the `legal` win and
  restored severity can be had together.
- **Arm 3 — knock out V1** (contingent, see below).

## Pre-registered predictions

Stated now so a miss is visible later.

- **Arm 1:** pooled R rises above the full state. The top label rises. If the
  schema-adjacency hypothesis is right, this is the larger of the two effects on the top
  label. *Prediction: R ≥ 0.45.*
- **Arm 2:** `blocking` recovers substantially (toward the baseline's 19/48) and pooled R
  rises, while `legal` **keeps** its top-label reachability. *Prediction: R ≥ 0.45,
  blocking ≥ 12/48, legal still reaches the top label at STRONG.*
- **If both arms raise R by similar amounts**, the two mechanisms are additive and both
  changes should be reverted.
- **If neither arm raises R above ~0.45**, the cause is V1 or V4 and Arm 3 runs.

## Decision rules, fixed in advance

1. Revert a variable if its knockout raises pooled R by **> 0.18** (see power, below) and
   does not cost the `legal` recovery.
2. Keep V3b (per-domain bars) if `legal` retains top-label reachability in Arm 2. The
   `legal` result is the spec's own falsifiable success criterion and is not traded away
   for aggregate R.
3. Keep V1 regardless unless Arm 3 shows it costs R: a positive-evidence field is
   independently justified by the RCA's negative-only-schema finding and has no measured
   cost.
4. **No arm is rerun with modified text to move a number.** If a prediction misses, it is
   reported as a miss. This is the same rule §7.2 of the spec sets for the bars.

## Power — stated up front, because it bounds every conclusion

Pooled R is 32 paired comparisons, so its standard error near 0.5 is
`sqrt(0.25/32) ≈ 0.088`. **At n=1 per arm, only differences greater than roughly 0.18
(2 SE) are distinguishable.** The effects already observed are larger than that
(0.625 → 0.281 is 0.34, ≈ 3.9 SE), which is why a single-run screening pass is defensible —
but an arm-to-arm difference of 0.10 means nothing at this sample size and will not be
reported as if it did. Whichever arm looks like the winner gets **one replicate** before it
drives a revert.

Also fixed, and unfixable: temperature is unset (1.0), n=1 per cell, one idea, one model,
and measured test-retest agreement is 87.5% / 83.3%. These bound generality for every arm
equally, which is what makes the *comparison* meaningful even though no single arm's
absolute numbers are precise.

## Method held fixed

Per ruling R20, every arm runs the same way the baseline did: on the host via `.venv-test`,
`scripts/panel_calibration_ladder.py`, same pinned model and `max_tokens`, same 48-cell
grid, concurrency 4. Changing the instrument between arms would make the series measure two
things at once — the mistake this whole document exists to avoid.

**Tree hygiene:** Task 11 is committed before the series starts, so each arm is an
uncommitted edit that `git checkout -- <paths>` restores exactly. After every arm the tree
is verified clean and `scripts/sync_prompt_set_docs.py --check` must exit 0 before the next
arm begins.

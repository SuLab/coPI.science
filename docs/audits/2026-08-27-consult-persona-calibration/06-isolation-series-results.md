# Isolation series — results

Pre-registered design: `05-isolation-series-design.md`, written before any arm ran.
Eight 48-consult runs, **384 real Opus consults**. Method held fixed across every run
(same host, same venv, same pinned model and `max_tokens`, 48-cell grid, concurrency 4).
All 384 cells parsed; no run contained an ERROR, UNPARSED or defaulted cell.

| run | configuration | pooled R | blocking | top label | domains |
|---|---|---|---|---|---|
| baseline (pre-change) | verdict FIRST, no bars, clear/caution | 20/32 = **0.6250** | 19/48 | 7/48 | 4/8 |
| post-T9 run 1 | verdict LAST, no bars | 15/32 = **0.4688** | 21/48 | 0/48 | 0/8 |
| post-T9 run 2 | verdict LAST, no bars | 14/32 = **0.4375** | 19/48 | 0/48 | 0/8 |
| post-T10 | verdict LAST, + bars | (missing) | | | |
| Arm 0 (post-rename) | verdict LAST, + bars, + rename | 10/32 = **0.3125** | 0/48 | 10/48 | 5/8 |
| Arm 2 (-global bar) | verdict LAST, per-domain bars only | 9/32 = **0.2812** | 0/48 | 9/48 | 5/8 |
| Arm 1 run 1 | verdict FIRST, + bars, + rename | 19/32 = **0.5938** | 3/48 | 20/48 | 8/8 |
| Arm 1 run 2 | verdict FIRST, + bars, + rename | 17/32 = **0.5312** | 1/48 | 20/48 | 8/8 |

## Verdict on the pre-registered predictions

**Arm 1 (predicted R >= 0.45): PASSED.** Two runs, 19/32 = 0.5938 and 17/32 = 0.5312; pooled over both, **36/64 = 0.5625**.

**Arm 2 (predicted R >= 0.45, blocking >= 12/48, legal keeps the top label): FAILED on the first two.** R = 0.2812 and blocking = 0/48. Dropping the global leniency sentence changed nothing. The prediction was mine and it was wrong; the reorder was the cause all along, which is what the FIRST hypothesis (ledger R24) had said before I revised away from it on cross-sectional evidence.

## The comparison that drove the revert

Verdict FIRST, pooled over 2 runs: **36/64 = 0.5625**  
Verdict LAST (same bars, same vocabulary), pooled over 2 runs: **19/64 = 0.2969**  
Difference: **0.2656**, against a pre-set decision threshold of 0.18 and a two-group SE of about 0.088 — roughly 3 SE. The revert is justified on the evidence, not on preference.

## Three honest caveats

1. **Verdict-first does not reliably clear the 0.594 tripwire.** It cleared it in one run (0.5938) and missed in the other (0.5312); the two-run pooled figure, 0.5625, is below both the threshold and the baseline's 0.625. The revert is well supported RELATIVE to verdict-last; the claim that it fully restores baseline discrimination is NOT supported. The commit message and the spec box quoted only the first run and have been corrected.
2. **`blocking` stays suppressed** — 3/48 and 1/48 with verdict first, against the baseline's 19/48. Arm 2 rules out the global sentence, so the per-domain bars are the remaining suspect. Discrimination recovered without it, because the gap/adequate boundary carries both tier steps. Acceptable, not ideal.
3. **The top label is the unambiguous win.** 20/48 in BOTH verdict-first runs with all eight domains reaching it, against a baseline of 7/48 and four domains, and 0/48 for two runs with the verdict last. `legal` reaches it in both runs, having never reached it once in 91 production consults.

## Limits that bound every row

n = 1 per cell; temperature unset (1.0); one idea; one model; tier confounded with target; measured test-retest 87.5% / 83.3%. Absolute values are imprecise. The comparisons are meaningful because the instrument was held fixed, which is the only reason this series concludes anything at all.

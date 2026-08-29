# Consolidated verdict: specialist verdict vocabulary, stage bars, and the 2026-08-28 prompt redlines

**Scope.** Everything executed on the `blackbird` branch on 2026-08-28 under the
specialist-verdict-vocabulary plan, plus the integration of two human prompt redlines.
Thirteen commits, from `c445473` (the ladder baseline) to `28c0c66` (the coherence fixes).
Phase A's eleven earlier commits (`fd12e5e..b648be5`) are in scope for the audits but were
delivered previously.

**Verdict: the engineering is complete and verified. The science contradicted the plan's
central hypothesis, and the plan was changed to follow the measurement rather than the
other way round.** Four independent review passes ran; every finding was triaged, and the
ones not fixed are listed below with reasons rather than omitted.

**Final gate:** `./scripts/ci.sh` — **3067 passed, 93 skipped**, single alembic head `0038`,
13 characterization snapshots intact, `ruff` zero on `tests/`, `src/` at 223 against a 231
ceiling.

---

## 1. What was audited, and by whom

Four independent passes, none of them the implementer of the work they reviewed:

| pass | scope | verdict |
|---|---|---|
| Phase A code audit | 11 commits, metrics/read-state/migration | no CRITICAL; 8 IMPORTANT, 10 MINOR |
| Prompt & doc integrity audit | 5 commits, prompts + snapshot + sync tool | no v3 revert, no confidentiality regression; 9 findings |
| Evidence & methodology audit | the RCA, the spec, three result files | core conclusion survives; **5 of 7 claims overstated** |
| Tasks 10+11 review | stage bars + vocabulary rename | spec **FAIL** (one blocking), quality GOOD |
| PI-redline review | the wholesale redline integration | **CHANGES REQUESTED**; fidelity exhaustively verified |

Plus per-task reviews on Tasks 1–9 and a whole-branch review during Phase A.

## 2. The three findings that mattered most

**2.1 A live specialist reply could post an undefined verdict label into a PI's Slack
thread.** `parse_opinion` was widened to accept retired labels so that stored text stayed
readable — but it is also the LIVE write path, so a reply saying `clear` was accepted as
`parsed`, stored, and posted as a green tick. The proof was already in the tree: a test
drove the live path with `"clear"` and asserted the result, and it passed. Fixed by making
`parse_opinion` strict and moving historical acceptance behind an explicit
`allow_historical=True` used only by the two genuine retro readers. The test that encoded
the defect now asserts the refusal, and a second test pins the retro direction.

*This is the reason a green CI was not treated as sufficient: the suite asserted the bug.*

**2.2 The acceptance criteria did not gate the thing they existed to gate.** The stated
tripwire "construct sensitivity R must not fall below 0.594" was read as the maximum of
four per-rung values — a reading that passes 26.5% of the time even at the published
comparison figure. Read correctly, 0.594 is the POOLED figure (19/32), and Task 9 **failed
it**: 0.625 → 0.469/0.438. The failure was reported as a pass until the audit caught it.
Both tripwires also read only the bottom of the scale or the best rung, so neither could
see a top-of-scale collapse — which is exactly what happened.

**2.3 The plan's central design hypothesis was wrong.** See §3.

## 3. What the measurement actually said

Eight 48-consult ladder runs, **384 real Opus consults**, instrument held fixed, one
variable isolated per arm against a design pre-registered *before any arm ran*
(`../2026-08-27-consult-persona-calibration/05-isolation-series-design.md`).

| configuration | pooled R | bottom label | top label | domains |
|---|---|---|---|---|
| baseline, before any change | 0.6250 | 19/48 | 7/48 | 4 |
| + `established`, verdict moved LAST | 0.469 / 0.438 | ~20/48 | **0/48** | 0 |
| + stage bars | 0.2812 | 5/48 | 4/48 | 3 |
| + vocabulary rename | 0.3125 | **0/48** | 10/48 | 5 |
| − global bar (Arm 2) | 0.2812 | 0/48 | 9/48 | 5 |
| **− the reorder (Arm 1, shipped)** | **0.594 / 0.531** | 3/48, 1/48 | **20/48 both** | **8/8** |

**The reorder was the harm.** Undoing it alone moved pooled R by +0.27 (≈3 SE against a
pre-set 0.18 threshold) with every other change left in place. The rename is a clear win —
`clear` ("nothing in your domain stands in the way") was close to unsayable at 9 of 1,192
production consults; `adequate` reaches 20 of 48. `legal` reached the top label for the
first time in the project's recorded history, which was the spec's own falsifiable success
criterion.

**Why the design was wrong.** The verdict was moved last on an evidence-before-rating
rationale whose warrant does not transfer: the cited "+6 to +11 accuracy points" is k=6
ensembling on PAIRWISE judging, applied here to a k=1 reorder of one key, and the same
review grades the opposite result as STRONG. The mechanism, as far as it is understood:
`concerns` is a REQUIRED negative-valence array, so a verdict written after it is chosen
with a freshly-authored list of problems adjacent in context. Ordering the rating after the
evidence ordered it after *negative* evidence.

**The spec worked as designed.** §3.2 stated its own honest limit — "This is MODERATE
evidence, and the ladder must validate it" — and the ladder invalidated it. The section is
left standing with a REVERSED box beneath it rather than rewritten.

## 4. Errors made in this work that the audits caught

Recorded because the audit trail is worth more than the appearance of a clean run.

1. **Reported Task 9's gate as passing when it failed** (§2.2), then compounded it by
   asserting the gate was "uncomputable" when the pooled form lands on it exactly.
2. **Revised a correct hypothesis into a wrong one.** After one cross-sectional result I
   moved from "the reorder is the harm" (right) to "the global leniency sentence is the
   harm" (wrong), one message after writing the pre-registration that existed to prevent
   exactly that. Arm 2 refuted it: 2 of my 3 predictions in the series failed.
3. **Concluded the hub redline contained no reviewer edits.** It contained four, saved with
   change-tracking off. Two mistakes: reading absent markup as absent edits, and running a
   comparison whose own confound (~1,300 words of rendered rubric) swamped a 40-word
   insertion.
4. **Cleared hub edit E3 against only half the evidence** — checked the hub's rules, never
   read what the lab is told. Then **overclaimed** that the reviewer's own edit closed it,
   when it closed it only at the rule level and not at turn level.
5. **Corrupted a commit message** by passing backticks through a remote shell, and
   **swept ~2,600 lines of another session's untracked work** into a docs commit with a
   glob. Both reverted and redone.
6. **Left my own stage-bar narrowing pinned by nothing** — reverting it passed the whole
   suite until a test was added.
7. **Quoted a single ladder run as the result** (0.594) when the replicate gave 0.531;
   corrected in the spec, the test docstring and a follow-up commit.

## 5. Accepted, not fixed — with reasons

- **`blocking` is 1–3/48 against baseline's 19/48.** Discrimination recovered without it
  (the gap/adequate boundary carries both tier steps). Arm 2 rules out the global sentence,
  so the per-domain bars are the remaining suspect. This is a new measurement cycle, not a
  repair, and guessing at it would repeat the error in §4.2.
- **The hub-context trailer (Phase A Task 6) was never isolated.** Same class of change as
  the reverted reorder, justified by the same two citations. The most likely remaining
  instance of the same mistake; not touched without evidence.
- **`rubric_version` does not move when the PERSONA CONTRACT changes**, so pre/post-reorder
  consults are indistinguishable by the stamp. Real observability gap; overloading that
  column with contract identity is a design change.
- **Three commits in one stretch fail a bisect** (prompt edits landed before the
  regenerated specs). Unpushed branch; the sync tool is now named in CLAUDE.md so it cannot
  recur.
- **`[]` in `established` cannot distinguish "named no positives" from "ignored the key"**
  — `_str_tuple` yields empty for a missing key. Documented as accepted ambiguity rather
  than asserted precision.
- **Six load-bearing 2026 preprints could not be verified.** Every figure they support is
  now qualified in place; the one that was hard-coded in a source comment as a comparison
  benchmark was removed and replaced with the reason it is not like-for-like.
- **No instrument measures pi-bot behaviour.** The specialist panel has a ladder that
  caught a bad hypothesis here; the PI prompt set has nothing equivalent, so its ~40%
  expansion is unfalsifiable at the point of commit.

## 6. Open decisions for the operator

1. **A confidentiality contradiction, and it is the one to look at first.** The lab is told
   "The interview is confidential and is never repeated to another lab", used explicitly as
   an inducement to disclose unpublished work; the hub is told "this thread is visible to
   every lab in the workspace". One is false. Pre-existing, untouched here, and the right
   resolution may be channel topology rather than either prompt.
2. **`_PI_LAB` sign-off.** `thread_guidance.py`'s docstring requires sign-off (andrewsu) for
   rewording those strings. This work rewrote them at the operator's direction.
3. **The instrument-vs-maturity slot.** The "say which instrument fits" ask was removed in
   `28c0c66` in favour of the maturity framing, reversing an earlier adjudication, because
   the redline's own new text made the ask vacuous. One-line revert either way.
4. **A co-authorship offer** in `phase5-new-post.md` with no hub counterpart, in a prompt
   that elsewhere forbids asking what the hub contributes. Reviewer's text; worth
   confirming with them.
5. **Deployment.** The agent image is not rebuilt and migration `0038` is not applied to
   production. `0038` must be applied before any run, or every `specialist_consults` write
   fails — and the failure is best-effort and therefore quiet.

## 7. Verification state

- `ci.sh`: 3067 passed, 93 skipped, single head `0038`.
- Three `.ambr` regenerations, all operator-directed. The two large ones were audited
  **programmatically**, not by eye: every added line traced to the new source files and
  every removed line to the old, with Python string-literal scaffolding normalised so
  concatenated `_PI_LAB` literals compare correctly. 0 untraceable in either direction on
  both. Nothing else in the 164 KB snapshot moved.
- Redline fidelity: all 229 markers accounted for, machine-checked, 61 applied / 1
  already-satisfied / 0 not-applied.
- Retired v2.x vocabulary: zero occurrences introduced on any added line, verified per term.
- Rubric registry consistency across the concurrent assessment-archive workstream: live
  `3.3.0/f13be750ac8d` → `live`, `3.2.0/42aec0479ac6` → `archived`, stale intermediate hash
  → `unknown`, unstamped → `unstamped`.

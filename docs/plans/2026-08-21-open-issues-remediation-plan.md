# Remediation plan — v3 (2026-08-21): open issues + the hub prompt-set update

**Status:** PLAN. Nothing implemented. v3 = v2 (three adversarial audits incorporated)
**plus** the 2026-08-21 hub prompt-set update, which changes screening *semantics* and
collides with two things already shipped.
**Deployed:** HEAD `f6f436b`; schema `0032`; run `076e80b6` live since 15:47 UTC on
rubric v2.0.0 (`e3ef75f84c48`), ~4.5 verdicts/h, 10/20 toward the v2 calibration check.
**§9 records what v1 got wrong** rather than hiding it.

---

## 1. P0 — an ACTIVE data-loss path (falsifies v1's "no open P0")

**E1 — a non-`max_tokens` stop reason silently yields an empty reply; the turn is
abandoned with no trace.** `src/services/llm.py:617` is
`response_text = text_blocks[0].text if text_blocks else ""`, and only
`stop_reason == "max_tokens"` is handled. A refusal, or a response whose only block is
thinking, returns `""`; `simulation.py:1879-1890` logs
`Phase 4: Empty/unparseable response … skipping`, posts nothing, records **no**
`assessment_drops` row, and abandons the thread after the second occurrence. Same
outcome as the ValueError defect `f6f436b` fixed, different route, unfixed.
**Measured live: 13 occurrences in 90 minutes, all `[blackbird]`, one thread at
count=2.** Zero drops since restart is consistent with the loss being invisible.
Related: `_first_text` (`llm.py:209-227`) keeps only the FIRST text block;
`generate_agent_response` handles no non-truncation stop reason at all.

**Fix:** branch on every terminal stop reason; on refusal/empty-text-blocks log ERROR
with the reason and record a drop row; concatenate text blocks instead of `[0]`.
**Do it first, alone, its own restart.**

---

## 2. The hub prompt-set update — what it changes, and what it collides with

The update is not a wording pass. It changes what the hub screens on, who answers which
question, how scores are formed, and the sidecar contract. Mapped to owning files:

### 2.1 Schema / code changes (not prompt-only)

| # | Change | Owner | Work |
|---|---|---|---|
| **PU1** | **New sidecar field `recommended_next_experiment`** — "exactly one experiment Blackbird should fund next", with readout, pass threshold, rough cost and duration. Verified absent from the codebase today | `opportunity_assessments`, `_persist_assessment`, phase4 skeleton, list + detail templates | migration **0033** (nullable Text), persistence, display; it is "the line Blackbird staff act on", so it belongs on the detail page prominently and in the list's detail row |
| **PU2** | **Banding becomes advisory; inapplicable dimensions must not force a `pass`.** "chemistry-to-DC or toxicity on a non-therapeutic, exit thesis at incubation stage — currently score zero and drag the total down … never let a total depressed only by inapplicable dimensions produce a pass" | `blackbird_rubric.py`, the TOML, the sidecar contract | **Design decision required.** The scorer deliberately counts missing/non-numeric as 0 (`blackbird_rubric.py:50`) precisely so a verdict cannot game the total by omitting its weakest dimensions. "Never let inapplicable dimensions produce a pass" cannot be met by removing that floor. Principled option: an explicit **N/A** value per dimension (distinct from a low score and from absent), with the weighted mean **renormalized over applicable weights only**, plus a validator rule that N/A must be justified in `rationale`. This changes `weighted_score` semantics → **rubric version bump** and a new stamp cohort |
| **PU3** | **Gating semantics rewritten.** #2 becomes "credible science — the data can be believed; prestige is not the test, IP not required" (recorded under the existing `credible_tech_source` key); #3 becomes **translational potential**, and **FTO is no longer a gate** — it is diligence, with `gating.fto_achievable` left `unconfirmed` unless a genuinely unresolvable blockade is found. The schema has no key for translational potential | TOML `[gating.*]`, phase4 contract, `specialists.py` | Retitle/redescribe the two gating entries. Then decide: add a `translational_potential` key (schema + validator + UI, cleanest) or leave it rationale-only as the update permits (cheap, but the sidecar then has a key whose meaning no longer matches its name). **Also forces F5/F6a**: `required_domains_for` triggers `legal` on `fto_achievable == "met"`, which the update makes rarer still — that trigger must be rebuilt |
| **PU4** | **Division of labour: commercial/market/IP diligence is the hub's, never the PI's.** New core principle #7; DECIDE guidance forbids asking the lab about market size, comps, competing programs, investor sentiment, FTO, encumbrances | scout_hub prompts, `thread_guidance.py` `_SCOUT_HUB`, 4 persona files | `_SCOUT_HUB` strings are doc-sync pinned (not GM-pinned — GM covers `_PI_LAB` only), so this needs the paired doc update in `docs/specs/2026-08-07-hub-bot-prompts.md` §4 |
| **PU6** | **Funding bands changed and de-Marylandized**: incubation $300K–$847K → **$100K–$1M**; pre-seed $300K–$750K → **$300K–$1M**; seed ~$2M → **$1–5M**; "Maryland non-dilutive stack (TEDCO MII, MSCRF, BIITC/QOF)" → "state and regional programs wherever the lab's institution is eligible" | 5 places: `prompts/specialists/budget.md`, `prompts/roles/scout_hub/agent-system.md` (×2), `prompts/roles/scout_hub/phase4-thread-reply.md`, the TOML's `external_signals` incubation anchor (cites MII/MSCRF) — **and `prompts/agent-system.md`, the LAB-facing prompt, which carries the same figures in a table** | ⚠️ the lab-facing prompt is **golden-master pinned**; changing it requires a deliberate, reviewed GM regeneration (CLAUDE.md permits this for intentional `_PI_LAB` changes; it forbids `--snapshot-update` to silence a mismatch). Leaving it stale creates a hub/lab contradiction on the same numbers |
| **PU7** | **Red flags rewritten**: new "data that cannot be readily replicated for <$200K and a reasonable timeline"; single-asset flag only where a clean result still leaves nothing worth building; unresolved FTO on unpublished academic science is explicitly *not* a disqualifier; absent VC interest in one unpublished result is *not* a flag | TOML `red_flags` | direct list edit |
| **PU8** | **Commercial dimensions scored forward, against the target class/indication/modality** — not against today's snapshot of one unpublished result; "absence of VC interest … scores nothing down". IP dimension becomes "what IP exists or is filable … observed, not required at this stage" | TOML anchors (`external_signals`, `ip_fto`, `market_unmet_need`, `exit_thesis`) | **Interacts with rubric v2**: W-C cut `external_signals` to 2% and `ip_fto` to 4% *because* their anchors were unreachable. PU8 makes them reachable, so the weight cut may now be wrong — re-derive after the first post-change run rather than guessing |
| **PU10** | Funnel: "in practice almost every PI interview lands at Incubation/Grant … the later stages are here to catch the rare exception"; at incubation the bar is scientific robustness + translational potential | TOML `[funnel]` | aligns with v2's incubation scale — supports it, no conflict |
| **PU9/PU11/PU12** | EXPLORE must clarify before screening ("you cannot screen what you have not understood"); "the proposal is input, not the unit of approval"; two worked archetypes; `retrieve_full_text` loosened from "sparingly" to "not your default read"; chemistry persona gains DC-path/selectivity-margin/modality bullets; clinical gains standard-of-care drift; commercial + legal `questions_to_ask` reframed as **hub diligence, not PI questions** | scout_hub prompts, `thread_guidance.py`, 8 persona files | mechanical but must be applied per-file; the 8 personas are otherwise byte-identical in their shared sections |

### 2.2 ⚠️ Two collisions with already-shipped work

- **PU5 vs panel notes (deployed, live).** The update states consults are "internal —
  **never posted, never seen by the PI**". Panel notes post `🧪 Panel · <domain> —
  <signal> — asked: "<question>"` into the interview thread, which every lab in the
  workspace can read. That is a **direct contradiction of a shipped feature**. Options:
  (a) disable via the existing `PANEL_NOTES_IN_THREAD=false` (env + container recreate,
  no rebuild); (b) mirror notes to a staff-only channel instead; (c) amend the prompt
  line if in-thread visibility is wanted. **Decision required — this is the one item
  where the update contradicts production behaviour rather than extending it.**
  (The other half is consistent: notes already do not count against the 12-message
  budget — verified in production.)
- **PU2/PU8 vs the in-flight calibration sample.** The update changes how scores are
  *formed* (forward-looking commercial, N/A dimensions). The comparability stamp records
  the **rubric document only**, so prompt-side changes land inside the same
  v2.0.0/`e3ef75f84c48` cohort and are invisible in the data. Landing the update before
  the 20th verdict **destroys the calibration sample**. Either hold the update ~2h, or
  bump `[meta].version` / extend the stamp with a prompt-bundle hash.

---

## 3. Inventory (audit items — corrections marked ▲)

| # | Issue | Class | Sev |
|---|---|---|---|
| A1 ▲ | `_close_thread` runs two billed LLM calls inside both agent locks + a reply-lane slot; every close convoys on the hub | TP | HIGH |
| F5/F6a ▲ | `required_domains_for` has **no branch for `commercial`** (mapped to `differentiation`, 15% — the heaviest dimension), and `legal` fires only on `fto_achievable=="met"`, which has **never occurred** (28 unconfirmed / 1 not_met / 0 met). Two of eight domains unreachable. **PU3 makes this worse and PU4 makes `commercial` diligence mandatory — so this is now a prerequisite, not a nice-to-have** | IQ | MED-HIGH |
| S1 ▲ | Signal-parse divergence is **bidirectional**: write path less tolerant (10/411 degrade, losing concerns/questions); read path (`assessment_detail.py:228-238`) discards the authoritative regex match and re-parses `text[brace:]`, failing on fenced replies — the page can contradict the stored row, both rendered together | IQ/LC | MED |
| A2 ▲ | Sync Slack HTTP + `time.sleep` backoff on the loop (3 paths). v1 dropped the audit's safer half: cache `users.info` | TP | MED-HIGH |
| A3–A6 | O(n) `MessageLog` scans; per-call Anthropic client; unreleased per-interview state; uncapped `_prior_threads` in every Phase-5 prompt | TP/CO | LOW-MED |
| §7-1..7 ▲ | **The perf audit's §7 backlog, omitted from v1 while citing that document**: waitlist_submit race, sync httpx in admin Slack provisioning, web DB pool, `AgentBadgeMiddleware` N+1, `profile_version` RMW, unthrottled pubmed `Semaphore(8)`, budget-debit-before-fetch | mixed | see audit |
| D1 ▲ | `opportunity_assessments` has **no `thread_id`** — the fragile `slack_ts` join is why the specialist RCA resolved only 2 of 29 rows and left "does blocking predict a lower dimension score?" undetermined. Fold into PU1's migration | LC | MED (unlocks measurement) |
| D2 ▲ | 0031's `none_as_null` fix is unchecked on `scores`, `gating`, `red_flags`, `derisking_milestones`, `raw_verdict`, `specialist_consults.concerns`/`.questions_to_ask` | LC | LOW-MED |
| S3 ▲ | **No 2000-char cap on what specialists receive** — that constant caps only the stored column (`tools.py:643`); the wire is verbatim and uncapped (`tools.py:300`→`:560`). The hub simply sends little. PU4 makes richer context load-bearing | IQ | MED |
| S2 ▲ | **Refuted as a fix**: 65 (thread, domain) pairs already consulted 2–4× produced **zero** `clear`. Demoted below S4. PU4/PU12 partly supersede it by redefining what the commercial/legal panels are *for* | IQ | LOW |
| S4 ▲ | Persona schema shows a populated `concerns` with no "[] if none" affordance and bills `questions_to_ask` as the most valuable field. (v1's citation of the `clear` bullet was backwards — that bullet argues *against* the bias.) The update retains the "a panel that never clears anything is noise" line, so the fix is the example, not the wording | IQ | MED |
| S5 ▲ | **STALE** — `f6f436b` already raised the consult ceiling 2500→4000, and `call_stats` now answers "what truncated" directly | — | closed |
| G1 ▲ | Non-⏸️ verdict at ordinal 11 unpersistable (5 rows). **Cheap option v1 missed:** stash the raw sidecar in the existing nullable `AssessmentDrop.detail` — recoverable, no schema, no prompt | DL | MED |
| G3 ▲ | Retire-on-flush, **not** narrowing `_persist_assessment`'s return — True-on-queue is load-bearing for duplicate suppression | DL | LOW now / HIGH if cap raised |
| G4 | No `superseded.slack_ts != slack_ts` guard before the DELETE | DL | LOW |
| G2 | **Measured non-biting**: 0 rows ≥3900 chars, conversation rows cap at 12, notes excluded | LC | monitor |
| G5, O1, O3–O5, C1–C3, R1–R3, R5, R6 | Docs/tooling/UI polish (O3 may also cover E1's empty-content log path — verify) | DX | LOW |
| #33, #29, #37 ▲ | Open GitHub issues absent from v1: dormant Phase-2 prompts cost 1 LLM call/turn/agent for a no-op (in scope); fabricated co-authorship; signup writes pending users into `users` | mixed | see issues |
| P1 ▲ | Sidecar section conflates "no verdict I believe" with "a negative verdict" — the premature-sidecar cause. **The update does not fix this**; fold the wording into the prompt batch | IQ | MED |
| V1 | v2 threshold re-check — 10/20 verdicts, ~2h to close | IQ | scheduled |

---

## 4. Corrected approaches for the two hardest items

**A1 — v1 got the shape and the evidence wrong.** The audit's "control run" *deletes*
the memory calls (`harness_a_close_convoy.py:131-134` substitutes a no-op), so
**8.02s → 0.20s measures deletion, not relocation**; the honest criterion is the other
line (unrelated reply starts at 0.00s instead of 6.01s). The hazard is not a stale read:
`_update_agent_memory` is a whole-file read-modify-write of working memory across an
await (`agent.py:187` → LLM → `agent.py:570` full `write_text` + a revision row),
serialized **only** by the `acquire_all` v1 proposed removing. The hub is in every close,
so bare relocation gives concurrent hub syntheses off one pre-state, last-write-wins —
and v1's "capture inside, synthesize outside" makes that *deterministic*.
**Correct shape:** a per-agent memory queue drained by one long-lived task, coalescing
events into a single synthesis — keeps per-agent serialization, frees the thread lock,
agent lock and semaphore, and *reduces* calls (memory calls are booked into the rate
limiter but never gated, so bursts can make the hub refuse its own replies). Requires a
task registry drained by `stop()` **before** the log callback is nulled, plus harness
arms that actually relocate.

**W2's lever is structural, and PU4 promotes it to a prerequisite.** F5/F6a means
`commercial` can never be required and `legal` effectively never — yet PU4 makes
commercial/legal diligence *the hub's job* and PU3 removes the FTO gate that was
`legal`'s only trigger. Fixing S2 alone would move the metric while leaving the hole.
Order: **F5/F6a (code) + S4's example fix + PU4/PU12's persona reframing first, then
measure, then S2 only if the clear rate has not moved.** Note S2 also weakens the floor
(which credits distinct domains) and risks pushing turns onto the forced-final path
(`max_tool_rounds=5` is already reached in production).

---

## 5. Execution order (lowest-risk-first; five windows, not two)

0. **Now, no restart:** W5 polish (G5, O1, O4, O5, C1, C2, R1, R2, R3, R5, R6).
1. **Window 1 — E1 alone.** Live data loss; narrow; own restart so a regression is
   attributable. Rollback: revert one commit.
2. **Window 2 — narrow, independently attributable:** S1 (both directions), A4, O3, C3,
   G4's one-liner, plus migration **0033** carrying **PU1's `recommended_next_experiment`
   AND D1's `thread_id`** (both additive/nullable; one migration, one window), and the
   D2 audit. Resume, not `--fresh`.
3. **HOLD ~2h for the 20th v2 verdict; run V1 on an unpolluted sample.** Nothing that
   changes scoring behaviour may land before this — see §2.2. If the prompt update
   cannot wait, bump `[meta].version` first so the cohorts separate.
4. **Window 3 — the prompt-set update (sign-off), as one reviewed change:** PU3, PU4,
   PU6, PU7, PU8, PU9, PU10, PU11, PU12, plus P1's wording and S4's example. Bump
   `[meta].version` → 3.0.0 (new anchors + new gating semantics + new red flags = a new
   scoring regime; the stamp is what makes before/after comparable). Prompts and the TOML
   are bind-mounted → restart, **no rebuild**, except where `thread_guidance.py` changes
   (that is `src/`). Paired edits: `docs/specs/2026-08-07-hub-bot-prompts.md` §4
   (doc-sync pinned) and, if PU6's figures change the **lab-facing** prompt, a reviewed
   golden-master regeneration.
5. **Window 4 — PU2 (N/A dimensions) alone**, because it changes `weighted_score`
   arithmetic: explicit N/A per dimension, renormalized over applicable weights, validator
   requiring justification in `rationale`, version bump, and a back-test of the stored
   corpus under the new rule before it ships.
6. **Window 5 — F5/F6a's required-domain rebuild, A1 (corrected shape, corrected
   harness, shutdown-drain + same-agent-concurrent-close tests), then A2/A3/A6, then G3
   (retire-on-flush) and G1 (`AssessmentDrop.detail`).** Then the §7 backlog on its own
   severities. S2 only if still justified after Window 3's measurement.

**Rollback:** each window is one revertible commit and the prior agent image is still
tagged locally, so a bad window is revert + rebuild, not a restore.

---

## 6. Rejected

Row-per-call `llm_call_logs` (would inflate both restart-rebuild ledgers and
over-throttle every agent); a fourth `verdict_signal` value (three-value contract across
sidecar/DB/UI — use a separate boolean); deferred sidecar capture for G1 (moot —
`AssessmentDrop.detail` is cheaper); thread-reply `max_tokens` above 16000 (the retry
clamps at 21,333 — the answer would be streaming). ▲ **Reconsidered:** aligning the
`anthropic` pin — the image/venv skew (1.0.0 vs 0.120.2) means every perf harness
measurement is taken against a different SDK than production runs; correctness is
covered, *measurement validity* is not. Moves to a decision (§8.7).

## 7. Verification

`ci.sh` is necessary but caught none of this wave's real defects — per-workstream checks
matter more. E1: drive a fake refusal/thinking-only response through the real path,
assert a drop row + ERROR. S1: assert stored and rendered signals agree for fenced,
prose and truncated replies. F5/F6a: unit-test `required_domains_for` on a
commercial-only idea. PU1: round-trip the new field and assert it renders where staff
act on it. PU2: back-test the stored corpus under renormalization before shipping.
Prompt window: render-diff the composed prompt before/after (the technique the rubric
extraction established) and keep the doc-sync tests green. A1: the audit's harnesses with
new relocate arms, plus `test_concurrent_thread_safety.py`. Every `src/` window ends
with: rebuild both images, migrate if needed, `docker stop -t 420`, restart, confirm the
banner.

## 8. Open decisions

1. **E1 now** (recommended) or batched?
2. **PU5 / panel notes** — disable via the existing env flag, mirror to a staff-only
   channel, or amend the prompt line? The update and production currently contradict
   each other.
3. **PU2** — adopt explicit N/A + renormalization (recommended, principled) or a weaker
   "don't let inapplicable dimensions force a pass" heuristic? The scorer's
   missing-counts-as-zero rule exists to stop verdicts gaming the total, so it cannot
   simply be relaxed.
4. **PU3** — add a `translational_potential` gating key (clean, schema+UI) or leave it
   rationale-only (cheap, but `fto_achievable` then keeps a name its meaning no longer
   matches)?
5. **PU6** — update the **lab-facing** prompt's funding table too (requires a reviewed
   golden-master regeneration), or accept a hub/lab contradiction on the same figures?
6. **F5/F6a** — `commercial` always-required, or cue-triggered? `legal` on any IP claim
   rather than on `fto=="met"`?
7. Align the `anthropic` pin so harness measurements match production?
8. **Hold the prompt update ~2h for the calibration sample** (recommended, nearly free),
   or bump the version and land it now?
9. G1 via `AssessmentDrop.detail`; O1 sum-vs-document `latency_ms`; V3
   `baltimore_commitment` — note the update's de-Marylandization argues for leaving it
   out permanently.

## 9. What v1 got wrong (recorded, not hidden)

1. Claimed "no open P0" — **false**; E1 is an active data-loss path.
2. A1: adopted a success criterion measured from a no-op control, and proposed a
   mitigation that makes the real hazard (a lost update) deterministic.
3. Omitted the perf audit's §7 backlog entirely while citing that document as a source.
4. S3 pointed at a wire cap that does not exist; S5 was stale against the plan's own
   declared HEAD; S2 was prioritized above the free fix on an inference from absence
   that live data refutes; S1 captured only one direction of the divergence.

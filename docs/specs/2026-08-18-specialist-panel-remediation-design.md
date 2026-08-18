# Specialist panel remediation

**Date:** 2026-08-18
**Status:** Design, approved. Not implemented.
**Branch:** `blackbird`
**Amends:** `docs/specs/2026-08-07-nine-evaluator-panel-design.md`
**Evidence:** production run `1787010946` (2026-08-17 22:48–23:55 UTC), 142 consults
and 18 persisted assessments — the complete production record of the panel.

## 1. What the audit found

The panel's *plumbing* works. Its *function* does not.

Every mechanical part behaves as designed: 142/142 consults parsed, zero LLM
failures, zero missing persona files, the `floor_armed` latch is correctly placed
against the two-lane concurrent scheduler, and the prompt's mandatory-consult list
matches the code exactly. What fails is everything the mechanism was built to
deliver — the panel produces no usable signal, and the one enforcement it performs
destroyed 100% of the verdicts it ever gated.

### Measured, not inferred

| Observation | Value |
|---|---|
| Consults, run `1787010946` | 142 (all succeeded, all parsed) |
| `verdict_signal` distribution | 114 `caution`, 28 `blocking`, **0 `clear`** |
| Persisted assessments | 18 — **0 `advance`, 0 `conditional`** (11 `pass`, 7 `route-to-incubation`) |
| `band` distribution | **18/18 `pass`**, `weighted_score` 2.06–2.89 |
| Verdicts the floor gated | 2 (both `gordy`, both `conditional`) — **both refused and discarded** |
| `gating.fto_achievable` | 17 `unconfirmed`, 1 `not_met`, **0 `met`** |
| Consults within 60s of a verdict | 26/142 (18%) |
| Consult input size vs hub turn | ~890 tokens vs ~30,700 tokens |

### The twelve findings

| # | Severity | Finding |
|---|---|---|
| F1 | Critical | The panel has never returned `clear`. 142/142 were `caution` or `blocking`, with zero parse failures — genuine model output. The personas warn against this in their own words ("a panel that never clears anything is noise", `prompts/specialists/scientific.md:48`). If `clear` had a 10% base rate, P(0 in 142) ≈ 3×10⁻⁷. |
| F2 | Critical | Every verdict the floor gates has been refused. The success path has never executed outside tests. |
| F3 | Critical | Refusal destroys the verdict *after* the Slack reply is posted (`simulation.py:1699` posts, `:1743` captures). The PI receives the verdict; Blackbird keeps only an `assessment_drops` row. |
| F4 | High | Substring cues manufacture requirements: `"aso"`→"reasons" (7/18), `"hit"`→"architecture" (6/18), `"als"`→"also"/"signals"/"animals"/"journals" (9/18). **3/18 verdicts had a domain required solely by a false positive.** |
| F5 | High | `commercial` and `budget` can never be required, proven exhaustively over the cue/gating/score space. `commercial` maps to `differentiation` — the heaviest dimension at 15%. |
| F6 | High | The `legal` trigger (`fto_achievable == "met"`) is structurally unreachable, because `search_prior_art` is title-only/US-only and the prompt correctly forbids reading an empty title search as FTO. This also pins `ip_fto` at mean 1.33 / max 2. |
| F7 | High | Specialist opinions do not survive to the turn that writes the verdict. `build_phase4_prompt` rebuilds messages from Slack history only; tool blocks are within-turn. |
| F8 | Medium | The band output has zero variance. Four dimensions are effectively constant (`external_signals`, `ip_fto`, `exit_thesis`, `chemistry_dc_path` all max at 2) = 23 of 100 weight points pinned near minimum. |
| F9 | Medium | A degenerate response satisfies the floor. `""`, `"   "`, `null`, `[]` all parse to `caution`/`low` and still fire `on_consult`. |
| F10 | Medium | Consults are cumulative per PI for the process lifetime, never cleared. A second interview inherits the first's consults. |
| F11 | Low | `maps_to_dimension`, `owns`, `consult_when` are never read at runtime. Domain descriptions are duplicated between `specialists.py` and `tools.py:113-131` with nothing pinning them together. |
| F12 | Low | `specialist_floor` drop rows carry `NULL thread_id`, so a refused verdict cannot be traced to its interview. |

### The one-sentence finding

**The floor's cost model is inverted.** `specialists.py:181` reasons that "a false
positive costs one consult, a false negative costs the whole point of the floor."
But the floor is evaluated *after* the interview is over and the reply is posted, so
a false positive costs the **entire verdict**, not one consult. F4 and F3 compose
into a verdict-destroying bug: on `gordy` — a DNA/protein TB vaccine with no chemical
matter — `chemistry` was required by `"peptide"` (antigen peptides for T-cell
recognition) and `"hits"` (from "*no on-point hits*", a patent-search result).

## 2. Decisions taken

Five decisions were resolved with the stakeholder before this design was written.

| # | Question | Decision |
|---|---|---|
| D1 | What happens on a floor gap? | **Flag now, gate later.** Phase 1 persists with a `panel_incomplete` flag; Phase 4 adds a pre-post gate that auto-convenes. The flag remains as a backstop. |
| D2 | Is F1 a prompt bug? | **Diagnose first.** Build a replay harness before rewriting eight personas on a hypothesis. |
| D3 | Consult record unit and lifetime? | **Per-interview, domains only.** Key by `(pi, thread_id)`. Fixes F10; **deliberately leaves F7 open.** |
| D4 | Trigger table redesign? | **Add `commercial`, retarget `legal`, leave `budget` advisory.** Word boundaries assumed throughout. |
| D5 | How far on the rubric? | **FTO interlock + instrumentation only.** No `RUBRIC_WEIGHTS` change, so the 18 existing scores stay comparable. |

## 3. Scope

### Fixing

F1 (diagnose), F2, F3, F4, F5, F6, F9, F10, F11, F12, the F8 FTO-interlock slice, and
instrumentation.

### Deferred, deliberately

- **F7 — panel opinions still will not reach the CONCLUDE turn.** Per D3. The
  consequence must be stated plainly rather than buried: the 2026-08-07 design's §3
  ("give the specialists somewhere to land") **remains unrealised**. The panel will
  keep steering the hub's *questions* — which is real value, and is what
  `questions_to_ask` was for — but its findings still will not reach the *score*
  except through whatever the hub chose to restate in its public reply. Anyone
  reading a `panel_incomplete = false` row should understand it as "the required
  domains were asked", not "the panel's findings were weighed."
- **F8 full reweighting.** Weights and the 3.0/4.0 thresholds are untouched. n=18
  from a single run is too thin a base, and changing weights silently reorders the
  triage queue and makes historical `weighted_score` values incomparable.
- **`budget` requirability.** Stays advisory; it maps to a 1% dimension.

## 4. Phasing, and why the order is load-bearing

**Phase 3 makes the floor strictly stricter.** Today a repeat interview inherits the
PI's earlier consults (F10); keying by `(pi, thread_id)` means every interview must
convene its own panel. Against the hub's observed behaviour — `chemistry` consulted
6 times across 18 assessments, `budget` once — that would refuse most verdicts.

**So Phase 1 must land before Phase 3**, or the remediation amplifies F3 instead of
fixing it. This is the single ordering constraint in the plan.

| Phase | Fixes | Deliverable |
|---|---|---|
| 0 — Diagnosis | F1 | Replay harness. No production change. |
| 1 — Stop the bleeding | F3, F2 | Migration + `panel_incomplete`. Verdicts stop being destroyed. |
| 2 — Trigger accuracy | F4, F5, F6a | Word boundaries, `commercial` added, `legal` retargeted. |
| 3 — Record scoping | F10 | `(pi, thread_id)` keying. **Requires Phase 1.** |
| 4 — Pre-post gate | F3, F2 | Gate before `_post_message`, auto-convene. |
| 5 — Hardening | F9, F11, F12 | Degenerate responses, dead data, drop provenance. |
| 6 — Interlock + instrumentation | F6b, F8 | FTO scoring guidance, admin surfaces. |

Phases 1–3 are independently shippable and each leaves the system in a better state
than it found it. Phase 4 is the only one that changes control flow in
`_reply_to_thread`.

## 5. Phase 0 — diagnosis (F1)

A throwaway harness, not production code. It replays consults against the existing
personas:

- **5 recorded consults** drawn from `llm_call_logs` where `phase LIKE 'consult%'`,
  as a control — these are known-weak ideas and should reproduce `caution`.
- **3 synthetic strong ideas** written to be clean in the relevant domain: adequate
  controls and power with a pre-specified analysis (`scientific`), a tractable lead
  series with a clean in-family profile (`chemistry`), an uncrowded landscape with
  named comparables (`commercial`).

**Decision rule.** If `clear` fires on the synthetic strong ideas, the personas are
calibrated and the 18 assessments really were weak — record that and change nothing.
If `clear` never fires even then, it is a prompt defect, and Phase 0 gains a step:
add a worked `clear` exemplar and a "what would move this to clear" field to all
eight personas.

Output is a written finding appended to this document, not a code change.

## 6. Phase 1 — the flag (F3, F2)

### Migration

Head is `0028`. This takes **`0029`**; CLAUDE.md's reservation of `0029` for the
deferred `users.is_admin` drop moves to `0030` in the same change (that migration was
never written, so nothing is renumbered in the chain).

Adds to `opportunity_assessments`:

```
panel_incomplete  BOOLEAN NOT NULL DEFAULT false
missing_domains   JSONB   NULL
```

Additive with a server default, so old code against the new schema is safe. The
reverse is not — see §11.

### Behaviour change

`_persist_assessment` (`simulation.py:2718-2740`) stops returning early on a gap. It
persists the verdict with `panel_incomplete = true` and `missing_domains` recording
the gap, then continues down the existing path unchanged.

The `specialist_floor` `AssessmentDrop` reason becomes unused for new rows. It is
retained, not removed, so the three historical drop rows still read correctly.

`/admin/assessments` renders the flag prominently. This is not cosmetic: an
incomplete-panel verdict must never be mistaken for a vetted one, which is the exact
failure the original floor existed to prevent.

## 7. Phase 2 — triggers (F4, F5, F6a)

### Word boundaries

Substring `in` tests are replaced by word-boundary matching. The implementation must
handle three cases the current cue list already contains:

- **multiword cues** — `lead series`, `chemical matter`, `medicinal chem`,
  `small molecule`, `multiple shots`
- **hyphenation** — forms observed in the run itself (`known-compound`,
  `aso-based`, `clinical-stage`, `patient-derived`, `rare-disease`,
  `cross-platform`) must still match their component cues
- **prefix cues** — `medicinal chem` and `neurodegener` are deliberate stems and must
  keep matching `medicinal chemistry` / `neurodegeneration`, so the rule is
  "boundary at the start, stem-permissive at the end", not `\b...\b` on both sides.

### The trigger table

```
always      : scientific, talent
commercial  : differentiation / first-in-class / competitive claim   [NEW]
legal       : any IP or third-party-materials claim                  [RETARGETED]
chemistry   : chemical matter                       (word-bounded)
clinical    : disease / indication                  (word-bounded)
technologic : platform claim, or scores.platform >= 4
budget      : advisory only — never required
```

`legal` moves off `gating.fto_achievable == "met"` (unreachable, F6) and onto an IP /
third-party-materials claim in the verdict text, which is what the Legal Specialist
actually owns.

### Tests

Three regression tests pinning the verified false positives — `reasons` must not
match `aso`, `architecture` must not match `hit`, and `also`/`signals`/`animals`/
`journals` must not match `als`. Plus a **reachability test** asserting exactly which
domains the floor can ever require, so F5 cannot silently return. That test is the
one that would have caught F5 originally.

## 8. Phase 3 — record scoping (F10)

`_specialist_consults` becomes keyed by `(pi_agent_id, thread_id)`:

- `_record_consult` gains the thread id. The call site already has it — the closure
  at `simulation.py:1627` captures `thread.other_agent_id` and can capture
  `thread.thread_id` the same way.
- `_consulted_domains` and `_specialist_floor_gap` join on the pair.
- `thread is None` (direct callers, pre-existing tests) keeps the current fail-open
  behaviour.

The PI-keyed rationale in `_record_consult`'s docstring — that one PI's consults are
"naturally cumulative across however many interview threads that PI has open" — is
the behaviour being removed, and that docstring must be rewritten rather than left
contradicting the code.

## 9. Phase 4 — the pre-post gate (F3, F2)

Sidecar extraction moves ahead of `_post_message`. New order in `_reply_to_thread`:

```
compose reply
  -> extract sidecar
  -> compute gap
  -> gap? -> run the missing consults inline
          -> one targeted call: revise ONLY the sidecar, given the new opinions
          -> recompute gap
  -> POST to Slack
  -> persist
```

**Bounded to one repair attempt.** If a gap survives the repair, the reply posts and
the verdict persists with Phase 1's flag. The bound is what guarantees termination
and caps worst-case cost at one extra generation plus at most 7 consults (the
maximum requirable set once Phase 2 adds `commercial`; `budget` is never required).

Two constraints this must respect:

- **Consults are billed.** The repair path books each consult through
  `agent.record_api_call`, exactly as the tool executor does today, or the limiter
  and `SimulationRun.total_api_calls` lose sight of them.
- **The reply must still be posted if the repair fails.** The existing invariant —
  a persistence problem must never cost the reply — is preserved in the new order by
  making every repair failure fall through to post-then-flag.

## 10. Phases 5 and 6

### F9 — degenerate responses

`parse_opinion` keeps treating prose as a valid opinion; that was a deliberate and
correct design decision. What changes is the narrower case the design's §7 already
intended to exclude: a response with **no usable content at all** (empty, whitespace,
or a JSON `null`/`[]` that yields no fields) must not fire `on_consult`. "The
specialist was unreachable" must not become "the specialist approved."

### F11 — dead data

`maps_to_dimension` is put to work in the Phase 6 admin view, tying each specialist
to the dimension its concerns belong in. A test pins the `tools.py` tool-description
domain list against `SPECIALIST_DOMAINS` so the two sources of truth cannot drift.

### F12 — drop provenance

`_persist_assessment` passes `thread_id` through to `_record_assessment_drop`.

### F6b — the FTO interlock

Worth stating precisely, because it determines *where* the fix goes: `ip_fto` is
scored by the **model**, not by rubric code. So the fix is prompt guidance in
`prompts/roles/scout_hub/` — an `unconfirmed` FTO reflects a tooling limitation
(`search_prior_art` is title-only and US-only and can never confirm FTO), not a
defect in the idea, and should score neutral rather than punitive.

**No `RUBRIC_WEIGHTS` change**, which is what keeps the 18 existing rows comparable
and honours D5.

### F8 — instrumentation

`/admin` gains a per-dimension score distribution, a band histogram, and a panel
clear-rate monitor that warns when the clear-rate stays at 0 across ≥50 consults —
the check that would have surfaced F1 on its own.

## 11. Testing and deployment

### Testing

TDD throughout; `./scripts/ci.sh` is the whole gate (alembic sanity, an
upgrade→downgrade→upgrade round trip, ruff, and the pytest run with a branch-coverage
floor). Two hazards:

- **The `pi_lab` guidance strings are pinned** by
  `tests/characterization/__snapshots__/test_agent_turn_gm.ambr` and must stay
  byte-identical. Never run `pytest --snapshot-update` to clear a mismatch — a
  mismatch there means a `pi_lab` regression, which this work must not cause.
- **The migration needs a downgrade** that survives the round trip.

### Deployment

Per the `0028` lesson: the new code maps the new columns, so a `select(User)`-style
`UndefinedColumn` failure applies here too. Migrate **before** the new code serves.

```
DC="docker compose -f docker-compose.prod.yml"
$DC build blackbird-app worker
$DC run --rm blackbird-app alembic upgrade head
$DC run --rm blackbird-app alembic current      # must equal `alembic heads`
$DC up -d blackbird-app worker
$DC --profile agent build agent                 # src/ is baked in; this is not optional
```

The simulation container is currently `Exited (137)` (SIGKILL, consistent with the
host OOM profile). Restarting it is an operator decision, not part of this change.

## 12. Risks

| Risk | Mitigation |
|---|---|
| Phase 3 shipped before Phase 1 refuses most verdicts | The ordering constraint in §4 is explicit and Phase 3's plan step depends on Phase 1's |
| Phase 4 adds cost on gapped verdicts | Bounded to one repair attempt; consults booked through the limiter |
| Word-boundary rewrite silently loses a legitimate cue | The stem-permissive rule in §7 is specified, and the reachability test pins the outcome |
| Phase 0 concludes the personas are fine and F1 is real | That is a valid outcome — it converts F1 from a bug into a recorded calibration fact |
| F7 stays open and the panel keeps not reaching the score | Recorded in §3 rather than left implicit; `panel_incomplete = false` is documented as "asked", not "weighed" |

## 13. What this does not change

`pi_lab` behaviour, the cohort gate, the post-type allow-list, the roster, the Slack
topology, `RUBRIC_WEIGHTS`, the band thresholds, and the eight persona files (unless
Phase 0's diagnosis says otherwise).

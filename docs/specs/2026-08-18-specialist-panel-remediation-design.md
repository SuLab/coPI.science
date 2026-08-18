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
| D1 | What happens on a floor gap? | **Flag now, gate later.** Phase 1 persists with a `panel_incomplete` flag; a pre-post gate then auto-convenes. *(Gate half since deferred by D6 — see §3.)* |
| D2 | Is F1 a prompt bug? | **Diagnose first.** Build a replay harness before rewriting eight personas on a hypothesis. |
| D3 | Consult record unit and lifetime? | **Per-interview, domains only.** Key by `(pi, thread_id)`. Fixes F10; **deliberately leaves F7 open.** |
| D4 | Trigger table redesign? | **Add `commercial`, retarget `legal`, leave `budget` advisory.** Word boundaries throughout. *(`commercial` and `legal` halves since deferred by D6 — only the word-boundary fix survives; see §7.)* |
| D5 | How far on the rubric? | **FTO interlock + instrumentation only.** No `RUBRIC_WEIGHTS` change, so the 18 existing scores stay comparable. |
| D6 | May the fixes touch prompts? | **No.** PI bot prompts and hub bot prompts are byte-frozen, including new hub-facing prompt text. Specialist personas are exempt. |

### D6 — the prompt freeze, and what it costs

Added after the design was first approved. The frozen surface is:

- **PI bot prompts** — `prompts/agent-system.md`, `prompts/phase4-thread-reply.md`,
  `prompts/phase5-new-post.md`, `prompts/identity.md`
- **Hub bot prompts** — `prompts/roles/scout_hub/{agent-system,identity,phase4-thread-reply}.md`
- **Injected guidance** — the `pi_lab` *and* `scout_hub` strings in
  `src/agent/thread_guidance.py`
- **New hub-facing prompt text is also forbidden**, not just edits to existing files.

**Exempt:** `prompts/specialists/*.md`. These are the panel's own prompts, not the PI
or hub bot's, and Phase 0 may change them if it proves miscalibration.

This is not a free constraint, and the cost is concentrated in one place: **any floor
change that makes the code require more than the prompt tells the hub to consult is
now forbidden.** `prompts/roles/scout_hub/phase4-thread-reply.md:65-82` documents the
mandatory-consult list, and the audit's own finding was that this list matches the
code exactly. Tightening the code against a frozen prompt would punish the hub for a
rule it was never given, and would introduce precisely the drift that is currently
absent. Three fixes are blocked by this and are moved to §3's deferred list: F5, F6a
and F6b, plus the whole of the former Phase 4.

**F4 is unaffected**, because word boundaries only ever *remove* spurious
requirements. The frozen prompt says `chemistry` is required "whenever the idea
involves a small molecule, a compound series..." — word-bounded matching is a closer
implementation of that sentence than substring matching is. F4 makes the code agree
with the frozen prompt *better*, which is why it is the one trigger fix that survives.

## 3. Scope

### Fixing

F1 (diagnose, and fix the personas if diagnosis warrants), F3 (mitigated), F4, F9,
F10, F11, F12, and instrumentation.

### Deferred by the prompt freeze (D6)

These are blocked by the constraint, not by their difficulty. Each needs a hub prompt
change to land safely, and each should be revisited as a single follow-up that ships
code and prompt together:

- **The pre-post gate (former Phase 4), and with it the full fix for F3 and F2.**
  It requires a new hub-facing prompt asking the hub to revise its sidecar after late
  consults. Consequence to state plainly: **the panel still never gets convened when
  it was skipped.** Phase 1's flag means no verdict is destroyed, but a gap remains a
  gap. F2 — that the floor has never once let a gated verdict through — is
  *recorded*, not resolved.
- **F5 (`commercial` requirable).** The heaviest dimension at 15% keeps a specialist
  the floor never demands.
- **F6a (retarget `legal`).** The `legal` trigger stays structurally unreachable, so
  in practice only five domains are ever required: `scientific`, `talent`,
  `chemistry`, `clinical`, `technologic`.
- **F6b (the FTO interlock).** `ip_fto` is scored by the model, so the fix is hub
  prompt guidance. The only code-side alternative — special-casing `ip_fto` inside
  `weighted_score` — would break the row comparability D5 exists to protect, so it is
  rejected rather than substituted.

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
| 0 — Diagnosis | F1 | Replay harness, then personas only if it proves miscalibration. |
| 1 — Stop the bleeding | F3 (mitigated) | Migration + `panel_incomplete`. Verdicts stop being destroyed. |
| 2 — Trigger accuracy | F4 | Word boundaries only. F5/F6a deferred by D6. |
| 3 — Record scoping | F10 | `(pi, thread_id)` keying. **Requires Phase 1.** |
| 4 — Hardening | F9, F11, F12 | Degenerate responses, dead data, drop provenance. |
| 5 — Instrumentation | F8 (partial), F1 | Admin surfaces and the clear-rate monitor. |

Every phase is independently shippable and each leaves the system better than it
found it. **No phase changes control flow in `_reply_to_thread`** — the pre-post gate
that would have done so is deferred by D6 — so the blast radius of this work is
confined to the floor's decision, the consult record's key, and read-only admin
surfaces.

Net effect on how often the floor bites: F4 *narrows* requirements (fewer spurious
gaps), Phase 3 *tightens* the record (no inherited consults). These pull in opposite
directions and the net is not predictable from first principles — which is exactly
why Phase 1's flag lands first and why Phase 5's instrumentation exists to measure
it.

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

Output is a written finding appended to this document. A persona change follows only
if the diagnosis earns it — `prompts/specialists/*.md` are exempt from D6's freeze,
but "exempt" is permission to fix a proven defect, not licence to rewrite on a hunch.

### Diagnosis result (2026-08-18)

Ran `scripts/diagnose_specialist_calibration.py` against the three synthetic strong
cases (scientific, chemistry, commercial), each written to pre-empt the specific
objection its persona is built to raise. Three real API calls, zero parse failures.

Observed signals:

- `scientific` -> `clear` (moderate confidence)
- `chemistry` -> `caution` (moderate confidence)
- `commercial` -> `caution` (moderate confidence)

`clear` fired on at least one of the three deliberately-clean cases. That is enough
to answer the question Phase 0 was built to ask: `clear` is not structurally
unreachable. The `chemistry` and `commercial` personas still returned `caution` on
their strong cases, but with concerns that are substantive rather than reflexive
hedges — e.g. chemistry pressed on family-panel coverage behind "two nearest family
members," commercial pressed on discontinuation-cause attribution behind "our
chemotype does not share." Those are the kind of domain-specific pushback the
personas are supposed to produce, not evidence of a floor that cannot lift.

**Reading supported: (a)-adjacent, but not miscalibration.** The evidence does not
support "the eight personas are miscalibrated and cannot say `clear`" — one of them
said it, unprompted, on the first synthetic case tried. It also does not cleanly
support "the 18 assessed ideas really were all weak" as the *complete* explanation,
since two of three strong synthetic cases still drew `caution`. The single sample
per domain is too small to distinguish "these two personas are stricter than
`scientific`" from "these two synthetic cases were not as airtight in their domain
as the scientific one was in its."

**Decision taken:** `clear` is reachable, so the miscalibration hypothesis that would
license rewriting all eight personas is not supported by this evidence. Step 4 (the
worked-`clear`-exemplar addition to all eight `prompts/specialists/*.md` files) is
skipped per the brief's condition. No persona file is modified in this task. Whether
`chemistry` and `commercial` specifically warrant a narrower, evidence-driven look is
left to a later phase — this diagnosis was scoped to answer reachability, not to
tune two personas off a sample size of one each.

## 6. Phase 1 — the flag (F3, mitigated)

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

## 7. Phase 2 — triggers (F4 only)

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

### The trigger table — unchanged in shape, corrected in matching

D6 freezes the hub prompt that documents this table, so the table itself does not
move. Only the matching underneath it is corrected:

```
always      : scientific, talent
chemistry   : chemical matter                       (word-bounded)  [FIXED]
clinical    : disease / indication                  (word-bounded)  [FIXED]
technologic : platform claim, or scores.platform >= 4
legal       : gating.fto_achievable == "met"    (still unreachable — F6a deferred)
commercial  : never required                    (F5 deferred)
budget      : never required                    (advisory by decision D4)
```

The consequence is worth naming so nobody reads this phase as more than it is: after
Phase 2, **five domains are reachable in practice** — `scientific`, `talent`,
`chemistry`, `clinical`, `technologic` — because `legal`'s trigger is unreachable and
`commercial`/`budget` are not wired. That is a truthful floor, not a complete one.

### Tests

Three regression tests pinning the verified false positives — `reasons` must not
match `aso`, `architecture` must not match `hit`, and `also`/`signals`/`animals`/
`journals` must not match `als`. Plus a **reachability test** asserting exactly which
domains the floor can ever require. That test is the one that would have caught F5
originally, and here it does double duty: it pins the deferred state honestly, so the
five-of-eight reality is asserted in code rather than remembered in a doc.

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

## 9. Phase 4 — hardening (F9, F11, F12)

### F9 — degenerate responses

`parse_opinion` keeps treating prose as a valid opinion; that was a deliberate and
correct design decision, and it stays. What changes is the narrower case the
2026-08-07 design's §7 already intended to exclude: a response with **no usable
content at all** — empty, whitespace-only, or a JSON `null`/`[]` that yields no
fields — must not fire `on_consult`.

The distinction being drawn is between *"the specialist said something I could not
parse"* (an opinion, counts) and *"the specialist said nothing"* (not an opinion,
must not count). Only the second is being excluded. Verified reachable today: `""`,
`"   "`, `"null"` and `"[]"` all currently parse to `caution`/`low` and satisfy the
floor. Zero occurrences in production so far, which is why this is Phase 4 and not
Phase 1.

### F11 — dead data

`maps_to_dimension` is put to work in Phase 5's admin view, tying each specialist to
the dimension its concerns belong in — the first runtime read it has ever had. A test
pins the `tools.py:113-131` tool-description domain list against `SPECIALIST_DOMAINS`
so the two sources of truth cannot drift.

Note the tool description itself is **not** edited: it is hub-facing text and
therefore frozen by D6. The test pins the existing agreement rather than changing
either side.

### F12 — drop provenance

`_persist_assessment` passes `thread_id` through to `_record_assessment_drop`, so a
dropped verdict can be traced back to the interview that produced it. The three
historical rows keep their `NULL`.

## 10. Phase 5 — instrumentation (F8 partial, F1)

Read-only admin surfaces. No scoring logic changes, so the 18 existing rows stay
comparable and D5 is honoured.

- **Per-dimension score distribution** — the view that makes F8 visible: four
  dimensions (`external_signals`, `ip_fto`, `exit_thesis`, `chemistry_dc_path`) are
  effectively constant at max 2, pinning 23 of 100 weight points near minimum.
- **Band histogram** — currently a single bar. That is the point.
- **Panel clear-rate monitor** — warns when the clear-rate stays at 0 across ≥50
  consults. This is the check that would have surfaced F1 on its own, without an
  audit.
- **Panel-gap surface** — how often `panel_incomplete` is set, and which domains are
  missing. This is how the deferred F5/F6a/gate work gets prioritised later: it turns
  "the floor is incomplete" from an argument into a number.

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
| Phase 3 shipped before Phase 1 refuses most verdicts | The ordering constraint in §4 is explicit; Phase 3's plan steps depend on Phase 1's |
| Word-boundary rewrite silently loses a legitimate cue | The stem-permissive rule in §7 is specified, and the reachability test pins the outcome |
| Phase 0 concludes the personas are fine and F1 is real | A valid outcome — it converts F1 from a bug into a recorded calibration fact |
| D6 leaves the floor enforcing only 5 of 8 domains | Asserted in code by the reachability test, surfaced by Phase 5's panel-gap view, and listed in §3 rather than left implicit |
| The deferred gate means a skipped panel stays skipped | Phase 1 guarantees no verdict is lost; Phase 5 measures how often it happens so the follow-up can be justified |
| F7 stays open and the panel keeps not reaching the score | Recorded in §3; `panel_incomplete = false` is documented as "asked", not "weighed" |

## 13. What this does not change

`pi_lab` behaviour, the cohort gate, the post-type allow-list, the roster, the Slack
topology, `RUBRIC_WEIGHTS`, the band thresholds, and — per D6 — **every PI bot prompt,
every hub bot prompt, both roles' strings in `src/agent/thread_guidance.py`, and the
`consult_specialist` tool description**. The eight persona files change only if Phase
0's diagnosis proves they must.

Control flow in `_reply_to_thread` is untouched: the reply is composed, posted, and
captured in exactly the order it is today.

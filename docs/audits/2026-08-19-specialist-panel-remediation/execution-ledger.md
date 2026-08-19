# SDD ledger — plan: docs/plans/2026-08-18-specialist-panel-remediation.md

Spec: docs/specs/2026-08-18-specialist-panel-remediation-design.md (read, authority)
Branch: feat/specialist-panel-remediation (off blackbird @ a7acd72)
MERGE_BASE: a7acd72caf8ca53bd0676f37c15c6a3a61c4f94b

Ruling: work in-place on a feature branch, not a git worktree — `uv` is not
installed and `.venv-test` (the CLAUDE.md-documented test path) exists only in
this checkout; recreating it in a worktree needs a full `[dev]` install on a box
with ~1GiB free RAM, which the host's OOM profile makes risky. `blackbird` is not
main/master so the skill's hard prohibition does not apply.
Cost if wrong: edits live in the deployed checkout; a rebuild mid-execution would
pick up in-progress code. Mitigated: the simulation container is stopped
(Exited 137) and `blackbird` itself is untouched.

## Pre-flight conflict scan

### Cross-task rows (every pair sharing a file or interface)

| Pair | Produced -> Consumed | Finding |
|---|---|---|
| T2->T3 | `panel_incomplete`/`missing_domains` cols -> written by `_persist_assessment` | clean |
| T2->T4 | same cols -> counted by `list_assessments` | clean |
| T2->T9 | same cols -> read for dimension stats | clean |
| T3->T4 | flagged rows -> `incomplete_panel_count` | clean |
| T3->T6 | `_record_consult(pi,domain)` 2-arg calls in T3 tests -> T6 adds optional `thread_id` | clean (default None keeps T3 tests passing; plan says so explicitly) |
| T3->T8 | `_record_assessment_drop` call site in `_persist_assessment` | **CONFLICT** — T3 DELETES the only call site (simulation.py:2730) that T8 was going to add `thread_id` to |
| T4->T9 | `list_assessments` return dict, `_assessments_body.html`, routers | clean (T9 adds keys, does not alter T4's) |
| T5->T7 | both edit `specialists.py` + `test_specialists.py` | clean (disjoint functions; sequential) |
| T5->T6 | `required_domains_for` -> `_specialist_floor_gap` | clean (signature unchanged by T5) |
| T6->T9 | `_record_consult(pi,domain,thread_id)` -> `_note_consult` wraps it | clean (T9 wraps, does not re-sign) |
| T7->T9 | `on_consult(domain)` -> `on_consult(domain, signal)` | clean (T9 explicitly updates T7's test) |

### Per-task self-consistency rows

| Task | Own tests vs own code | Finding |
|---|---|---|
| T1 | script complete; Step 4 gated on Step 2 output | clean; `.env` has ANTHROPIC_API_KEY and config reads `.env` from CWD, so it runs on the host |
| T2 | test asserts `server_default is not None` vs `server_default=text("false")` | clean; 0028 confirmed as current head |
| T3 | "peptide...tuberculosis" -> chemistry cue; "indication"/"platform" -> clinical/technologic | clean (verified against the real cue lists) |
| T4 | `incomplete_panel_count` asserted vs computed | clean; needs `func` import (plan says so) |
| T5 | prototyped against the 18 real verdicts before the task was written | clean; expected delta = exactly 3 rows |
| T6 | thread-keyed tests vs `(pi, thread_id)` dict | clean |
| T7 | `has_usable_content` cases vs implementation | clean |
| T8 | Step 3 targets a call site T3 removes | **CONFLICT** (same as T3->T8 above) |
| T9 | "find the run-summary path with grep" | **DEFECT** — no `_log_run_summary` exists; the grep returns ambiguous hits |

### Rulings

Ruling: F12 is closed by Task 3, not Task 8 — Task 3 deletes simulation.py:2730,
the only `_record_assessment_drop` call site lacking `thread_id`; the other two
(1747, 2561) already pass it. Task 8's Step 3 therefore becomes a VERIFICATION:
assert every remaining call site passes `thread_id`, and record that F12 closed
in Task 3. Do not add a drop call back just to give it a thread_id.
Cost if wrong: none material — this is drop provenance, and no new
specialist_floor drops can exist once Task 3 lands.

Ruling: Task 9's clear-rate monitor goes in `SimulationEngine.stop()`
(simulation.py:811), immediately before `logger.info("Simulation stopping...")`.
The plan's "find it with grep" instruction has no unique target — there is no
`_log_run_summary`. `stop()` is the graceful-shutdown path, awaited from the
entry point's finally block, and already logs there.
Cost if wrong: `stop()` is idempotent and could log the warning twice in a
double-stop; it is a WARNING with no state, so a duplicate line is harmless.

## Pre-existing working-tree noise (NOT from this plan)

At branch creation the tree already carried unrelated uncommitted changes:
 M .gitignore, docker-compose.prod.yml, new_orcids.txt
 ?? SECOND_INSTANCE_SETUP.md, docs/audits/2026-08-17-*, docs/blackbird-star-topology-runbook.md,
    docs/plans/2026-08-05-*, docs/plans/2026-08-13-*, docs/specs/2026-08-05-*, docs/specs/2026-08-07-pi-pitch-reframe-design.md,
    docs/specs/2026-08-13-*, logs/, scripts/make_install_links.py, slack_install_links.md
These predate this work. Review packages are commit-range diffs so they are excluded
automatically, but do not attribute them to any task.
Baseline (pre-change, HEAD=a7acd72): 82 passed — tests/unit/{test_specialists,test_specialist_floor,test_tool_gating,test_consult_accounting,test_thread_guidance}.py

Task 1: implementer DONE (commit 84fa1aa). Diagnostic result: scientific -> CLEAR,
chemistry -> caution, commercial -> caution. `clear` IS reachable, so Step 4
(persona edits) was correctly SKIPPED. Controller independently verified the eight
prompts/specialists/*.md checksums are unchanged vs the pre-task baseline.
Consequence for the rest of the plan: F1 is a calibration FACT about the 18 assessed
ideas, not a persona defect. No persona work is needed in any later task.
Task 1: complete (commits a7acd72..84fa1aa, review clean)
Task 1: controller-resolved ⚠️ — reviewer could not reproduce the 3 live API responses
  from disk. Inherent to a live-API diagnostic; the freeze, the file list and the
  spec wording were all independently verified. No action.
Task 1: minor (deferred): chemistry + commercial returned caution on their own strong
  synthetic cases (n=1 each). Too thin to act on; Task 9's clear-rate monitor is the
  instrument that would catch it over a real run.

Task 2: implementer DONE (commit 9d8d7a3). ci.sh PASSED; full suite 2034 passed, 93 skipped.
Task 2: out-of-brief changes VERIFIED LEGITIMATE by controller — the implementer also bumped
  scripts/migrate/preflight.py + 2 test files that pin the alembic head. Precedent confirmed:
  855bec4 (the 0028 migration commit) touched preflight.py the same way. Required for ci.sh.

Ruling: revision-number collision with an UNMERGED branch is a recorded hazard, not fixed here.
  `feat/user-account-types-0029` carries alembic/versions/0029_drop_is_admin.py, also claiming
  revision 0029. It is not merged into blackbird (git branch --contains shows only that branch),
  so our 0029 is valid on this branch today. I did NOT rewrite that branch — it is not part of
  this plan and rewriting someone else's unmerged branch is an out-of-scope side effect.
  My CLAUDE.md edit (is_admin drop reserved as 0030) is the consistent resolution.
  Cost if wrong: whoever merges feat/user-account-types-0029 must renumber it to 0030.
  ci.sh's duplicate-revision-id / single-head check fails LOUDLY on collision, so this
  cannot land silently.
Task 2: complete (commits 84fa1aa..9d8d7a3, review clean)
Task 2: minor (deferred): tests/unit/test_migration_checks.py:238-242 docstring still
  narrates only the historical 0027/0028 bug though the body now covers 0028/0029.
Task 2: minor (deferred): tests/unit/test_opportunity_models.py asserts nullable +
  server_default but NOT column type — would not catch Boolean->Integer or JSONB->Text.
  This weakness is INHERITED FROM MY OWN BRIEF (plan Task 2 Step 1), not an implementer
  shortcut. Flag to final review.
Task 2: controller-resolved ⚠️ — reviewer did not re-run ci.sh (instructed not to, for
  host memory). Controller will re-run ci.sh independently in the final audit.

Task 3: implementer a5fbb27 TERMINATED mid-task by an API session limit (not a code
  failure). State on recovery, verified by controller: HEAD still 9d8d7a3 (no commit),
  uncommitted WIP in src/agent/simulation.py + tests/integration/test_opportunity_assessment_persistence.py,
  no stray ci.sh/pytest processes, no leftover testcontainers postgres.

Ruling: KEEP the WIP and dispatch a fresh implementer to finish it, rather than
  discarding and restarting. Controller inspected the diff directly: simulation.py is
  byte-correct per the brief (warning replaced, _record_assessment_drop call removed,
  bare return removed, panel_incomplete/missing_domains added to assessment_kwargs) and
  the test file contains both brief-mandated tests.
  Cost if wrong: a fresh implementer inherits a subtly wrong edit it did not write. Mitigated
  by requiring it to re-derive the diff, run the tests and run ci.sh before committing.

Ruling (PLAN GAP): the brief said "append two tests" but PRE-EXISTING tests in that file
  assert the OLD refuse-and-discard behaviour — notably
  test_a_refused_verdict_is_recorded_as_a_drop (asserted `assessments == []` and one drop row).
  Task 3 necessarily invalidates them. Updating those tests is IN SCOPE and required; the
  brief simply did not anticipate them. The replacement must assert the NEW contract
  (row stored + panel_incomplete True + no drop row), never be deleted to make red go green.
  Cost if wrong: deleting rather than converting a test would silently drop coverage of the
  drop-table path — the reviewer is instructed to check exactly this.
Task 3: controller observation to put to the reviewer — src/models/opportunity.py:98-99
  documents the `specialist_floor` drop reason as "...never convened. Nothing persisted."
  After Task 3 no NEW row can carry that reason, so the sentence is true only of the three
  historical rows. It reads as current behaviour and risks contradicting the code. Not
  fixing it myself (controller must not fix); routing to the task reviewer to judge.
Task 3: fresh implementer a4be66d launched ci.sh in background and ended its turn awaiting it. ci.sh confirmed still running (pytest). Controller waiting, then will resume the agent to finish verification + commit. No commit yet; WIP still uncommitted.
Task 3: the fresh implementer's background ci.sh COMPLETED and PASSED before its process
  died — recovered from its output file: "2036 passed, 93 skipped ... Required test coverage
  of 60% reached. Total coverage: 75.96% ... 16 snapshots passed ... ==> CI passed. [exit 0]".
  Snapshot count 16/16 passing confirms the pi_lab characterization pins were not disturbed.
  No orphaned testcontainers left behind (checked all containers; only the two deployments').
  Remaining for Task 3: freeze check + commit. Dispatching a minimal implementer for those.
Task 3: committed 1a32e43. Controller independently verified: freeze diff a7acd72..HEAD over prompts/ + thread_guidance.py + blackbird_rubric.py is EMPTY; commit touches exactly 2 files.
Task 3: implementer DONE (commit 1a32e43). 46 passed in the task's test file; ci.sh already
  green on this tree. Pre-existing tests CONVERTED (none deleted), per implementer:
    - test_engine_known_subject_overrides_the_models_guess (cleanup only)
    - test_a_refused_verdict_is_recorded_as_a_drop -> test_a_gapped_verdict_persists_with_no_drop_row (contract flip)
    - test_a_persisted_verdict_records_no_drop (cleanup only)
    - test_floor_arms_from_this_threads_own_later_consult (now asserts stored+flagged)
    - test_floor_arms_for_a_restart_rebuilt_thread_once_any_consult_is_recorded (same)
  Review dispatched on opus (highest-risk task) with 4 adversarial probes: retry-queue flag
  preservation, per-test conversion integrity, collateral loss of the other 2 drop reasons,
  and the opportunity.py:97-99 doc-vs-code contradiction.
Task 3: review verdict — spec 8/8 ✅; quality ISSUES (1 Important, 1 Minor, 0 Critical).
  Reviewer independently confirmed (a) retry path is safe: assessment_kwargs is ONE dict object
  used by both the first write (:2796) and _pending_assessments (:2816) -> _flush_pending_assessments
  (:4171), so a flagged verdict cannot lose its flag on retry; (c) the other two drop reasons
  (unparseable_sidecar, missing_sidecar) keep live code paths + tests; template coverage for the
  3 historical specialist_floor rows is preserved by test_admin_page_warns_about_dropped_verdicts.
  Important: test_engine_known_subject_overrides_the_models_guess — `assert len(rows) == 1` is now
    unfalsifiable for the reason its message names (a row is stored either way post-Task-3).
    Matters because Task 6 re-keys the consult join; this is the test that should catch it.
  Minor: opportunity.py:97-99 "Nothing persisted" — controller's finding CONFIRMED by reviewer.

Ruling: bundled the Minor doc fix into fix round 1 rather than deferring it. The skill keeps
  minors out of the fix loop, but this one is a one-line doc-vs-code contradiction in a file the
  round already touches, and the final review would only ask for it later.
  Cost if wrong: negligible — a doc caption, no runtime effect, no test asserts it.

Task 3: fix round 1/5 dispatched (resumed implementer ac6a962). Required a FALSIFIABILITY
  DEMONSTRATION: break the subject-override, show the test fails, revert, show it passes.
  An assertion that cannot be shown to fail is precisely the defect being fixed.
Task 3: fix round 1/5 (2 addressed claimed, awaiting scoped re-review; commits 1a32e43..c79bcc1). Implementer demonstrated falsifiability by stubbing _specialist_floor_gap to report a gap -> test FAILED on the new panel_incomplete assertion; reverted -> 46/46 PASSED. Re-review instructed to judge whether that stub actually models a Task-6-style consult-join break rather than merely proving the assertion is reachable.
Task 3: fix round 1/5 (2 addressed, 0 open; commits 1a32e43..c79bcc1)
  Re-reviewer went beyond the implementer's demo: independently traced _persist_assessment ->
  _specialist_floor_gap -> _consulted_domains and confirmed the new assertion is NON-VACUOUS
  (wrong key "WangBot" => gap {scientific,talent} => panel_incomplete True => test fails;
  correct key "wang" => empty gap => passes). It correctly noted the implementer's stub only
  proved reachability, not the regression class — the independent trace is what closes it.
  No leftover stub/monkeypatch in the committed tree. Freeze holds. 46/46.
Task 3: complete (commits 9d8d7a3..c79bcc1, review clean)
Task 4: implementer DONE (commit 772b287). ci.sh PASSED (2037 passed, 93 skipped, cov 75.97%,
  alembic single head 0029, ruff clean). Freeze empty.
  Implementer updated only src/routers/admin.py:656 and reports src/routers/manager.py needs no
  edit because it already spreads **view into the template context — it says it verified this by
  reading rather than assuming. Review dispatched with that claim as probe (a): if manager.py
  does NOT receive incomplete_panel_count, the shared banner silently blanks for managers, who
  are exactly the read-only audience this warning exists for.
Task 4: review verdict — spec 8/8 ✅; quality Approved with 2 Important test-coverage findings.
  (a) manager.py CLAIM CONFIRMED by reviewer reading source: manager_assessments does
      _template_context(..., **view) at manager.py:153-157, so no edit was needed. The
      implementer was right to depart from my brief here.
  (b) run-scoping on incomplete_query CONFIRMED identical to total_count's.
  (d) missing_domains NULL rendering safe: `(a.missing_domains or [])|join(', ')`.
  Important 1: no test seeds a panel_incomplete row on the MANAGER page. Jinja Undefined is
    falsy and does not raise, so losing the **view forwarding would blank the banner silently
    for the exact read-only audience the warning serves.
  Important 2: test_list_assessments_counts_incomplete_panels uses ONE SimulationRun, so a
    scoped and an unscoped count both return 1 — it cannot catch a dropped run-scope.
Task 4: fix round 1/5 dispatched (resumed implementer a7ab19a). Falsifiability demonstrations
  required for both: break the guard, show the test fails, restore, show it passes.
Task 4: fix round 1/5 (2 addressed claimed; commits 772b287..b14268c). Implementer demonstrated BOTH falsifiably: dropping the run-scope made the NEW test fail (assert 2 == 1) while the OLD single-run test still PASSED — direct evidence the original gap was real, not hypothetical; dropping incomplete_panel_count from manager.py's **view spread made the new manager test fail on absent banner text. Both production files byte-restored. Scoped re-review dispatched, instructed to confirm no leftover break survives in the committed tree.
Task 4: fix round 1/5 (2 addressed, 0 open; commits 772b287..b14268c)
  Re-reviewer confirmed both assertions bite, the second run id is genuinely distinct, and —
  importantly — NO leftover break survived the falsifiability sabotage: directory.py:196-199
  still carries the run-scope guard and manager.py:157 still spreads **view. 57 tests pass.
Task 4: complete (commits c79bcc1..b14268c, review clean)

Ruling: Task 5's Step 5 tells the implementer to query the PRODUCTION database via
  `docker compose exec postgres psql`. I overrode that: subagents get no docker access on this
  host, because a second unrelated PRODUCTION deployment (copi-python / copi.science) shares it
  and one mistyped container name reaches their live stack. I ran the read-only SELECT myself and
  handed the implementer a data file (production-verdicts.json, 18 rows) instead.
  Cost if wrong: none to correctness — the verification is identical, the data is the same rows.
  It only moves who runs the query.
Task 5: implementer DONE (commit 221f42a). ci.sh PASSED (2044 passed, 93 skipped, cov 76.01%,
  ruff src 229<=231, ruff tests 0, 16/16 snapshots, no snapshot-update). Freeze empty.
  Step 5 acceptance criterion MET EXACTLY: 3 of 18 production verdicts changed — pearce(row5)
  and hart(row8) lost `chemistry`, mcmeniman(row12) lost `clinical`; the other 15 identical,
  including the OTHER pearce/hart/huganir rows. This matches the controller's pre-plan prototype
  prediction precisely.
Task 5: PLAN DEFECT (mine) found by implementer — the brief's test snippet had a mid-file
  `import itertools` and a duplicate `required_domains_for` import, which trips ruff E402/F811
  under the repo's zero-findings test-suite gate. Implementer hoisted the import and dropped the
  duplicate. Review instructed to confirm no TEST LOGIC was altered under cover of that fix.
Task 5: review verdict — spec 7/7 ✅; quality Approved with 1 Important + 2 Minor.
  Reviewer INDEPENDENTLY reproduced the 3-of-18 result (old `in` vs new), confirmed hyphen
  boundaries work (aso-based, known-compound, clinical-stage, patient-derived all match),
  confirmed real standalone uses survive (ALS, ALS/FTD, ASOs, ADCs, hits), confirmed cue LISTS
  untouched, mutation-tested the reachability probe (killing a cue list makes it fail => not a
  tautology), and confirmed _cue_pattern's unbounded lru_cache is keyed only on the 38 fixed
  module cues, never on verdict text.
  Important: F4 SURVIVES via the PREFIX tier — production row 17 (coller) has `compound`
    matching "compounding" in "several compounding reasons" as its ONLY chemistry cue.

Ruling: FIX IT rather than defer, despite the reviewer labelling it "follow-up, not this task's
  spec". It is the same failure class Task 5 exists to close (a specialist required by a false
  positive alone), it was found in real production data, and the fix is small — move `compound`
  to the word-only tier. Deferring would leave F4 half-closed while the plan records it as done.
  Cost if wrong: the 18-row acceptance criterion moves from 3 changed rows to 4; I required the
  implementer to re-run the full production check AND the full ci.sh gate to prove no fifth row
  moves and nothing GAINS a domain.
Task 5: fix round 1/5 dispatched (resumed aff59c2) — Important + 2 Minors (ruff UP033 ratchet
  regression 0->1 finding; stale substring-era comment at specialists.py:187-188 that the new
  _cue_matches docstring already rebuts by name).
Task 5: fix round 1/5 (3 addressed claimed; commits 221f42a..d4c2901). 60 passed; ci.sh PASSED 2045/93; ruff src ratchet IMPROVED 229->228 (below the 231 ceiling and below the pre-task 229); 18-row check now 4 changed exactly as ruled — pearce(5)+hart(8) chemistry, mcmeniman(12) clinical, coller(17) chemistry; no row gained a domain. Scoped re-review dispatched, required to RE-DERIVE the 18-row comparison itself rather than trust the report.
Task 5: fix round 1/5 (3 addressed, 0 open; commits 221f42a..d4c2901)
  Re-reviewer RE-DERIVED the 18-row comparison independently: exactly 4 rows changed, all LOST a
  domain, none gained — pearce(5)/hart(8)/coller(17) chemistry, mcmeniman(12) clinical. Confirmed
  cue tuple MEMBERSHIP untouched (only tier assignment + comments moved), tests purely additive,
  required_domains_for signature unchanged, ruff clean, freeze holds.
Task 5: complete (commits b14268c..d4c2901, review clean)

ORDERING GATE SATISFIED: Task 6 tightens the floor and the plan requires Tasks 3 and 4 to land
first (Task 3 stops a tightened floor destroying verdicts; Task 4 makes the result visible).
Both are complete and reviewed. Task 6 is now safe to dispatch.
Task 6: implementer a0fe4d2 backgrounded the full pytest run and ended its turn (same trap that
  cost two earlier agents, despite an explicit foreground instruction). WIP uncommitted across 4
  files: src/agent/simulation.py, tests/unit/test_specialist_floor.py,
  tests/integration/test_opportunity_assessment_persistence.py, tests/unit/test_consult_accounting.py.
  Controller checks while waiting:
   - the Task-3 guard assertion (`panel_incomplete is False` in
     test_engine_known_subject_overrides_the_models_guess) is UNTOUCHED in the diff — the
     re-keying did not break the join, and the guard was not weakened to hide it.
   - test_consult_accounting.py was changed although my brief never named it. Inspected: it
     updates `_consulted_domains("wang")` to `_consulted_domains("wang", thread.thread_id)`.
     That is correct and STRENGTHENS the test — it is now an end-to-end proof that the tool-executor
     closure actually forwards thread_id. Weakening would have looked like asserting an empty set.
Task 6: implementer DONE (commit a38d507). ci.sh PASSED (2048 passed, 93 skipped, ratchet 228/231,
  16/16 snapshots, alembic single head 0029). Freeze empty. Guard test passes UNMODIFIED.
  Implementer also rewrote _persist_assessment's docstring unprompted (same PI-keyed contradiction)
  and fixed 4 collateral test breakages the brief never named.
  Review dispatched on opus with 6 probes, the sharpest being (c) whether each of those 4 collateral
  test edits was RE-KEYED or WEAKENED, and (d) whether any test actually fails if the code reverts
  to PI-only keying — i.e. whether the behaviour the task exists for is genuinely proven, not just
  implemented. Reviewer told it may copy the repo to /tmp and revert the keying there to find out.
Task 6: review verdict — spec 8/8 ✅; quality ISSUES (1 Important, 2 Minor, 0 Critical).
  Reviewer VERIFIED: (a) write key ≡ read key end-to-end, closure default-arg binding intact;
  (b) all 20 call sites consistent on the (pi, None) slot; (c) all four collateral test edits were
  genuine re-keys, two of them STRENGTHENINGS, no weakenings; (d) behaviour genuinely proven —
  reverting the keying in a /tmp copy fails exactly the two intended tests; (e) all three docstrings
  now match the code with no stale contradiction; (f) the Task-3 guard is UNMODIFIED (panel_incomplete
  appears 0 times in the diff).
  Important: tests/unit/test_consult_accounting.py:176 in test_a_failed_consult_is_not_booked is now
    VACUOUS — production writes ("wang","t1"), the assertion reads ("wang",None), a slot nothing
    writes. Mutation-proven by the reviewer: injecting on_consult into tools.py:449's unknown-domain
    early return FAILS at d4c2901 and PASSES at a38d507. The property "a failed consult must not
    satisfy the floor" is no longer guarded. This is a coverage regression introduced by Task 6.
  Minor: test_the_floor_reads_the_consults_of_this_interview_only's first assertion passes via the
    fail-open branch (thread_one.floor_armed is False), not via the join it means to exercise.
  Minor: two test names now half-wrong after re-keying.
Task 6: fix round 1/5 dispatched (resumed a0fe4d2). Required the same mutation demonstration the
  reviewer used, plus an audit of EVERY `_consulted_domains(` call site for the same defect class.
Task 6: fix round 1/5 (3 addressed claimed; commits a38d507..7a461df). Implementer mutation-proved Finding 1 (injected on_consult into tools.py's unknown-domain early return -> test FAILED with frozenset({'astrology'}) == frozenset(); reverted -> PASSED, git diff empty) and re-proved Finding 2 by reverting the keying. Audit: 7 _consulted_domains call sites, exactly 1 defect, now fixed. 77 passed. Scoped re-review dispatched, required to RE-RUN the mutation itself and RE-AUDIT the call sites independently, and to report git status --porcelain at the end to prove no leftover mutation.
Task 6: fix round 1/5 (3 addressed, 0 open; commits a38d507..7a461df)
  Re-reviewer RE-RAN both mutations itself: injecting on_consult into tools.py:449 made
  test_a_failed_consult_is_not_booked FAIL then PASS on revert; forcing PI-only keying in
  simulation.py made both interview-isolation tests FAIL then PASS on revert. Independent
  call-site audit reproduced the same 7 sites / 1 defect verdict. Fix diff touches ONLY the two
  test files — no leftover production mutation. git status --porcelain at end identical to the
  session's pre-existing baseline. Freeze holds.
Task 6: complete (commits d4c2901..7a461df, review clean)
Task 7: implementer DONE (commit 229fe77). ci.sh PASSED (2052 passed, 93 skipped). 46/46 in the two
  test files. Freeze empty. Falsifiability: reverting the gate made
  test_an_empty_specialist_reply_is_billed_but_not_counted FAIL (['chemistry'] == []) while the pure
  has_usable_content unit tests still passed; gate restored byte-for-byte, all 46 green.
Task 7: RECURRING PLAN DEFECT (mine, 2nd occurrence) — my brief's test snippet again used a mid-file
  import, tripping ruff E402 under the repo's zero-findings test gate. Same defect as Task 5.
  Implementer folded it into the top-of-file import block. Review told to confirm that is the ONLY
  deviation. Lesson for the remaining briefs (8, 9): warn the implementer up front that any snippet
  import must be hoisted.
Task 7: review verdict — spec 8/8 ✅; quality Approved, ZERO findings.
  Reviewer executed has_usable_content itself against all 13 cases (8 FALSE / 5 TRUE) — all correct;
  confirmed PROSE STILL COUNTS (the design decision that must not be reversed); confirmed
  parse_opinion byte-identical between 7a461df and 229fe77; confirmed on_api_call's booking at
  tools.py:469-470 is untouched and unconditional so billing/floor still disagree; confirmed the
  error string tells the hub the truth and does not read as an opinion; and judged the falsifiability
  split correct (pure-function tests unaffected by a tools.py gate revert is the expected signature).
Task 7: complete (commits 7a461df..229fe77, review clean)
Task 8: implementer DONE (commit f584254). ci.sh PASSED (2053 passed, 93 skipped, cov 76.06%,
  16 snapshots). Both freeze checks empty INCLUDING src/agent/tools.py — the frozen tool description
  was not edited into agreement. CONTROLLER RULING OBEYED: simulation.py untouched, no
  _record_assessment_drop call re-added. Drop-path verification: 2 remaining call sites
  (simulation.py:1752, :2566), both already pass thread_id=thread.thread_id — F12 confirmed closed
  by Task 3 as ruled. Drift demo in a /tmp copy: adding a 9th fake domain made the new test AND its
  pre-existing sibling fail ("Extra items in the right set: 'fake_ninth_domain'"); scratch copy deleted.
  Implementer disclosed a transient editing slip (Read truncation misplaced the test), caught by test
  failure and fixed pre-commit; review told to confirm the FINAL file is clean by reading all of
  test_tool_gating.py, not just the diff.
Task 8: review verdict — spec 5/5 ✅; quality Approved, ZERO findings. Reviewer independently
  confirmed: ruling obeyed (simulation.py untouched, no drop call re-added); both drop call sites
  (1752, 2566) pass thread_id; the ratchet is NOT a tautology (SPECIALIST_DOMAINS, the enum at
  tools.py:139-142 and the prose at :120-129 are three independently hand-typed literals);
  tools.py freeze byte-empty; and the disclosed editing slip left the final file clean (read all
  163 lines — neighbouring test undamaged, no duplication).
Task 8: complete (commits 229fe77..f584254, review clean)

Ruling UPDATED for Task 9: my earlier ruling put the clear-rate monitor at simulation.py:811.
  Line numbers have shifted across 12 commits — `stop()` is now at :816 and its
  `logger.info("Simulation stopping...")` at :828. Anchor by SYMBOL, not line number.
Ruling (PLAN GAP, mine): Task 9 widens on_consult from 1-arg to 2-arg. My plan named only
  tests/unit/test_consult_accounting.py:194 as needing the update. There are FOUR one-arg call
  sites: that one plus tests/unit/test_tool_gating.py:82, :100, :117 (all `on_consult=seen.append`).
  Missing them would break the suite. Carried into the dispatch.
  Cost if wrong: caught immediately by a red suite, not silent.
Task 9: implementer DONE (commit 529b202) — FINAL task implemented. ci.sh PASSED (2054 passed,
  93 skipped, ruff src 228/231, ruff tests clean, alembic single head 0029 + clean round trip,
  coverage 76.08%). All FOUR on_consult call sites updated (the one my plan named plus the three
  I found pre-dispatch). Freeze empty; TOOL_DEFINITIONS reported byte-identical.
  Review dispatched on opus with 8 probes; sharpest are (b) the clear-rate monitor must not be able
  to raise inside stop(), which is the graceful-shutdown path that flushes durable state, and
  (f) maps_to_dimension must be genuinely READ rather than re-hardcoded as a duplicate mapping —
  a duplicate would recreate the exact two-sources-of-truth problem Task 8 just pinned.
Task 9: review verdict — spec 10/10 ✅; quality Approved, 0 Critical, 0 Important, several Minor.
  Reviewer VERIFIED: (a) new aggregates share the run-scoped `assessments` list; (b) monitor sits
  AFTER all three flushes so an exception could not lose durable state, and cannot raise
  (sum({})==0, .get short-circuits behind total>=50, keys always normalised to VERDICT_SIGNALS);
  (c) _record_consult byte-unchanged, _note_consult delegates; (d) no stale one-arg on_consult
  anywhere (it is keyword-only, execute_tool has one caller); (e) TOOL_DEFINITIONS byte-unchanged
  and the whole freeze diff empty; (f) maps_to_dimension GENUINELY read, sole consumer, no duplicate
  mapping — F11's dead data is now alive; (g) template link-free and None-safe end-to-end;
  (h) manager.py does receive the new keys via **view -> ctx.update(kwargs).

Ruling: promoted ONE Minor into a fix round — the new instrument is itself untested (warning branch,
  _note_consult tally, and the 2-arg closure are all unasserted; the stop() line only ever runs with
  total==0). A typo'd "clear" key or an off-by-one threshold would ship green and silent. This
  monitor is the ONLY thing that would surface the original F1 finding without a human audit, so an
  unwatched watchdog is the exact failure class this plan exists to fix. Required a typo-mutation
  demonstration. Cost if wrong: a few test-only lines at the very end of the plan.

Task 9: minor (deferred, ACCEPTED LIMITATIONS — flagged to final review, deliberately not fixed):
  - the clear-rate monitor fires only on a graceful stop(); a SIGKILL skips it entirely. Note the
    production container's last exit was 137 (SIGKILL), so in practice it may rarely fire. This is a
    real efficacy limit on the F1 instrument and should be weighed before relying on it.
  - the in-memory tally under-counts across a restart (same trade-off as _specialist_consults).
  - dimension_stats is computed from the 500-row, score-ordered limited list, so a run with >500
    assessments yields a score-biased sample. Identical to the pre-existing band summary cards.
  - the `{% if dimension_stats %}` guard is dead (13 weights always present); an empty run renders
    13 all-dash rows.
  - Task 2's model test asserts nullable/server_default but not column TYPE (inherited from my brief).
  - Task 2's tests/unit/test_migration_checks.py docstring still narrates only the 0027/0028 bug.
Task 9: fix round 1/5 dispatched (resumed ac69314).
Task 9: fix round 1/5 (1 addressed claimed; commits 529b202..fff5799, test-only, 115 insertions / 0 deletions). 1411 passed, 2 skipped. Typo mutation ('clear'->'cleared') caught by the NEGATIVE test test_stop_does_not_warn_when_at_least_one_clear_is_present — correct signature: with a typo'd key the 'has a clear' case stops being recognised and the warning fires when it should not. Re-review told to re-run that mutation AND additionally probe the >=50 THRESHOLD (change it to >=40 and see whether anything catches it) — the implementer proved the key is pinned but not that the boundary is.
Task 9: fix round 1/5 (1 addressed, 0 open; commits 529b202..fff5799)
  Re-reviewer ran BOTH mutations live and reverted cleanly: "clear"->"cleared" failed test 3;
  threshold >=50 -> >=40 failed test 4 — so the BOUNDARY is genuinely pinned, not only the key.
  Test 1 asserts both halves plus accumulation-vs-overwrite. Bonus test exercises the 2-arg closure
  end-to-end through a real _reply_to_thread call rather than a hand-built closure. Diff is 2 test
  files, 115 insertions, 0 deletions. simulation.py byte-clean after the experiments.
Task 9: complete (commits f584254..fff5799, review clean)

ALL 9 TASKS COMPLETE. 14 commits, a7acd72..fff5799.

FINAL FIX WAVE: DONE (commit d92ca76, 11 files, +528/-62). ci.sh PASSED in the foreground
  (2071 passed, 93 skipped, cov 76.13%, alembic single head 0029 + clean round trip, ruff tests 0,
  ruff src 228/231, 16 snapshots). Freeze empty; TOOL_DEFINITIONS proved identical by extracting
  and diffing the block against fff5799.
  Mutation 1 (_floor_verifiable -> return True): 5 failures incl. `assert None == []` on the stored
    column; restored -> 86 passed.
  Mutation 2 (lookbehind deleted from the PREFIX branch): 1 failed / 41 passed — i.e. it was
    INVISIBLE to every pre-existing test, which was the finding itself; restored -> 42 passed.

*** INCIDENT (near-miss, user-visible) ***
  The fix-wave agent ran `git stash -u` to measure a ruff baseline. It failed partway and removed
  19 UNTRACKED files — the user's own work, not in git (docs/audits/, docs/plans/, docs/specs/,
  logs/, SECOND_INSTANCE_SETUP.md, scripts/make_install_links.py, slack_install_links.md).
  The agent recovered them from stash@{0}^3 and reported them byte-identical.
  CONTROLLER VERIFIED INDEPENDENTLY rather than trusting that claim:
   - all 18 named untracked files present with plausible sizes;
   - logs/ still holds 16 files (matches the session-start baseline exactly);
   - the 3 modified tracked files (.gitignore, docker-compose.prod.yml, new_orcids.txt) still modified;
   - `git stash list` holds exactly 2 stashes, both dated 2026-08-14/15 — i.e. the user's
     pre-existing ones, days before this session. No stray stash left behind.
  Recovery is confirmed complete. Root cause: `git stash -u` is destructive to untracked files and
  should never have been used for a lint measurement. My dispatches banned docker and background
  jobs but did not ban destructive git operations — that is a gap in MY briefing, and I am
  recording it as such rather than as purely the agent's error.

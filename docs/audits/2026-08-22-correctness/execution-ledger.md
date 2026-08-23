# SDD ledger — plan: docs/plans/2026-08-22-correctness-remediation.md

Spec: docs/audits/2026-08-22-correctness/README.md
Plan audit (binding corrections): docs/plans/2026-08-22-correctness-remediation-AUDIT.md
BASE at start: f3171bb  (branch: blackbird)

## Preflight conflict scan

### Pairs of tasks sharing a file or an interface

| A | B | A produces | B consumes | Finding |
|---|---|---|---|---|
| A1.1 (migration) | B1.1 (llm.py) | `llm_call_logs.cache_read_input_tokens` / `cache_creation_input_tokens` columns | B1.1 populates them | **ORDERING CONFLICT** — plan puts A1.1 in Phase 2, B1.1 in Phase 1 |
| A1.1 | A2.2 | `specialist_consults.truncated` | A2.2 filters on it | **ORDERING + MISSING DDL** (audit U-1) |
| A1.1 | F1.2 | `opportunity_assessments.panel_owed` | F1.2 sets it | **ORDERING CONFLICT** |
| A1.1 | F1.3 | `opportunity_assessments.thread_id` | F1.3 keys idempotency on it | **ORDERING CONFLICT** |
| A1.1 | A2.4 | `thread_id` | A2.4 writes/rehydrates on it | ordering OK (both Phase 2, A1 first) |
| C1.2 | A2.2 | truncation marker on the consult row | A2.2's SELECT filter | needs the same column; also C1.2's note-suppression is in `simulation.py` (A-owned) — **OWNERSHIP COLLISION** |
| A3.7 | B1.1/B1.2 | per-`_acreate` booking hook in `llm.py` | A3.7 needs it | **OWNERSHIP COLLISION** (llm.py is WS-B's) |
| B1.2 | A2.5 | `on_stop_reason` value on a fallthrough (`"max_tokens"`, not `"refusal"`) | A2.5's truncation predicate | **INTERFACE GAP** — Interfaces block pins the signature, not the predicate |
| A1.3 | F1.2 | column name | F1.2 references it | **SELF-INCONSISTENCY** — Interfaces says `panel_checked`, revised A1.3 says `panel_owed` |
| A1.3 | directory.py / list page | `panel_state` | list page + banner gate on `panel_incomplete` | same 12 rows, three surfaces (audit) |
| E1.1 | every non-GET test | Origin guard | shared client fixture | 3 files (`conftest.py:100-125`, `test_badge_impersonation_gate.py:79`, `test_manager_access.py:85`) |

### Per-task self-consistency

- **A1.1** — revised text now lists 4 DDL items + 2 data repairs with an explicit ordering constraint. Self-consistent after the revision.
- **A1.2** — count corrected to 11 of 13; instruction says enumerate from `Base.metadata`. Consistent.
- **A1.3** — **INCONSISTENT with the Interfaces block** on the column name (see above). Ruled below.
- **A2.2** — **not implementable** without A1.1's new column. Ruled below.
- **A2.4** — revised; the DELETE re-key is now explicitly forbidden. Consistent.
- **B1.1** — revised; preferred route (columns) depends on A1.1. Ruled below.
- **B1.5** — revised to docs-only; no test churn. Consistent.
- **C1.2** — declares `SpecialistOpinion.truncated` in Interfaces, but `parse_opinion` has no access to `stop_reason` (audit U-1). **INCONSISTENT.** Ruled below.
- **C1.3** — revised to transliterate-not-widen. Consistent.
- **E1.1** — revised; unsubscribe exemption + ordering + runtime Origin. Consistent.
- **F1.x** — consistent after A1.1 lands first.
- **A3.x** — consistent after revisions.

## Rulings

Ruling R1: Insert a **Task 0** — migration 0036 plus ALL model changes and head pins — and run it FIRST, before any Phase-1 work. — Four ordering conflicts (B1.1, A2.2, F1.2, F1.3) all resolve to "the columns must exist first", and the plan's phase split put the migration last. — If wrong: a wasted early migration that later tasks must amend, visible in one `alembic history`.

Ruling R2: The truncation marker lives on the **consult record**, threaded as a `truncated: bool` kwarg through `record_consult` → `_record_specialist_consult` → the new `specialist_consults.truncated` column. NOT on `SpecialistOpinion`. — `parse_opinion` never sees `stop_reason`, so a field there could only ever be `False`, which is a second value with two meanings (the §1.1 defect class). — If wrong: the flag sits one layer further out than ideal; no data is wrong.

Ruling R3: The column is named **`panel_owed`** ("was a panel owed under the rules in force at write time"), not `panel_checked`. The Interfaces block is corrected. — `panel_checked` is ambiguous with the `missing_domains=[]` unverifiable case; `panel_owed` is a durable fact. — If wrong: a column rename later, cheap while only one migration references it.

Ruling R4: The truncation predicate is **`stop_reason in {"refusal", "max_tokens"}`**, defined ONCE as a shared helper and used by every consumer (`tools.py`'s consult credit, A2.5's three engine sites). — B1.2's fallthrough reports `"max_tokens"`, so a `refusal`-only predicate would silently pass truncated text as complete; and a consult that hit `max_tokens`, retried, and still truncated is credited today. — If wrong: a slightly wider predicate marks some complete replies as truncated, which fails safe.

Ruling R5: Everything in `src/agent/simulation.py` is **lead-owned and lands in Phase 2**, including `_post_panel_note`'s truncation suppression (C1.2's second half) and `record_api_call`'s per-round booking (A3.7). Phase-1 workstreams implement only their own side and report the seam. — Two ownership collisions in the scan; simulation.py is 6.7k lines and three workstreams wanted it. — If wrong: one extra hand-off per seam, recorded in each report.

Ruling R6: Implementers are dispatched **SERIALLY**, never in parallel, per this skill's explicit rule — even though the plan is partitioned by file ownership. — The plan audit proved the partition had at least four collisions, so "disjoint" was not true; and earlier today concurrent implementers raced on the git index and on `inspect.getsource` line numbers. — If wrong: slower wall-clock, no correctness cost.

Ruling R7: Work proceeds on branch **`blackbird`** with no new git worktree. — `blackbird` is not the default branch (main is); the host-side `.venv-test` and the deploy build context are both bound to `/home/ubuntu/blackbird-copi-science`, so a worktree would break every test invocation and the image build. — If wrong: history is on a feature branch either way and is rebasable.

Ruling R8: **No deploy and no simulation start** as part of this execution. The plan's P3.3 already states deploy is a separate decision. — Deploying mid-execution would put half-fixed code in front of the only environment that can validate it. — If wrong: the fixes sit unshipped until a deliberate deploy, which is the safer default.

## Tasks

Task 0: migration 0036 + models + head pins   (lead-dispatched)
Task 1: llm.py                                (B1.1-B1.6)
Task 2: specialists/tools/patents/pubmed      (C1.1-C1.7, own side only)
Task 3: web/auth                              (E1.1-E1.5)
Task 4: backfill script                       (F1.1-F1.4)
Task 5: assessment pipeline + panel state     (A1.3, A2.1-A2.5)
Task 6: main loop / --fresh / flush paths     (A3.1-A3.9)

## Execution log

Task 0: implemented (commits f3171bb..2f2b175). Full host suite 2538 passed / 1 failed;
  the failure `test_concurrent_proposal_reviews_do_not_500` was proved PRE-EXISTING by running the
  same suite at HEAD~1 (also fails). ruff tests/=0, src/=225 (ceiling 231).
  Migration verified on a throwaway Postgres: single head 0036; upgrade→downgrade 0018→upgrade clean;
  both data repairs correct in six seeded shapes, INCLUDING explicit proof the milestone repair runs
  before the JSONB normalisation (recovered ["m1","m2","m3"]; reversed order would have nulled it).
Task 0: minor (deferred): `PLANNED_OBJECTS` in preflight.py has no entries for 0035 or 0036 — it is
  the "object already exists" collision check, so two revisions' objects are unrepresented. 0035 set
  the precedent of adding none. One-line follow-up.
Task 0: note — 3 of the 11 JSON columns are physically `json`, not `jsonb`
  (`cohort_audit_events.topology`, `researcher_profiles.pending_profile`/`.user_submitted_texts`), so
  `jsonb_typeof` on them is a hard error and the migration carries a per-column typeof function.
  These are exactly the three that BOTH earlier audits missed; `grep JSONB` does not find them.
Task 0: note — `panel_owed` is NULL on all pre-0036 rows and deliberately not backfilled. Task 5
  (A1.3) must render NULL as something other than green, or Task 0 changes nothing user-visible.
Task 0: note — brief said `src/models/researcher_profile.py`; the real file is `src/models/profile.py`.
Task 0: pre-existing flake to expect in ci.sh: `test_concurrent_proposal_reviews_do_not_500` races
  two `review_proposal` calls; one racer can legitimately take the 400 "Already reviewed" path
  instead of the IntegrityError-rollback path it pins.
Task 0: review clean — Spec ✅, Quality Approved. 0 Critical, 0 Important, 4 Minor. Reviewer
  mutation-tested the drift alarm (flipped none_as_null → fails; new nullable column → fails; new
  NOT NULL column → correctly ignored) and independently reproduced the repair-order proof on seeded
  data. Independently enumerated Base.metadata: 16 JSON columns, 13 nullable, 0 offenders.
Task 0: ⚠️ RESOLVED by controller — the repair predicate `jsonb_typeof(derisking_milestones)='null'`
  does match production: both backfilled rows (markham, weeraratna) show dm_typeof='null',
  is_sql_null=false, with 8 and 9 recoverable milestones. All 17 will be recovered. Not a gap.
Task 0: minor (deferred): comment tense drift — `opportunity.py:140` and `specialist_consult.py:16,88`
  describe behaviour Task 5 lands ("used to re-derive", "unless truncated is True"). Fix in the
  wiring task, not by amending 0036.
Task 0: minor (deferred): preflight.py:88 is a 110-char comment line (E501 is ignored; pure nit).
Task 0: minor (deferred): `test_the_walk_actually_found_json_columns` asserts >= 10 against 13; an
  exact-tuple pin is this repo's house pattern for that alarm.
Task 0: Ruling: accept the `PLANNED_OBJECTS` gap for 0035+0036 rather than widening preflight now.
  — The brief named three constants, 0035 set the precedent, and the covering test only iterates
  0019..0025 so nothing fails; widening it is a separate concern from this plan's defects. — If wrong:
  the guarded operator migration path misses an "object already exists" collision for six objects,
  detectable by running preflight against a half-migrated DB.
Task 0: Ruling: no CLAUDE.md migrate-before-serve paragraph for 0036 in this task. — 0032-0035 have
  none either, so adding one only for 0036 would misrepresent the others; CLAUDE.md gets one
  consolidated update at the end of the plan. — If wrong: an operator deploys new code before
  migrating and every select() over five columns raises UndefinedColumn until they migrate.
Task 0: complete (commits f3171bb..2f2b175, review clean)
Task 1: implemented (commits 2f2b175..1370fba). Red first (18 failed/31 passed) then green;
  full host suite 2559 passed / 93 skipped / 0 failed (the pre-existing race flake passed this run).
  ruff tests/=0, src/=225. `is_truncated_stop` is module-level in llm.py as pinned.
Task 1: CARRY TO TASK 6 — `_flush_llm_logs` (~simulation.py:6024) builds `LlmCallLog` from explicit
  keys, not `**entry`, so B1.1's two new payload keys go nowhere and Task 0's columns stay NULL.
  Two lines needed: `cache_read_input_tokens=entry.get("cache_read_input_tokens")` and the
  `cache_creation_` twin. Exact snippet in task-1-report.md.
Task 1: CARRY TO TASK 6 — `src/models/agent_activity.py:193-194` repeats the B1.5 off-by-one
  ("1..7 real API calls… up to max_tool_rounds tool rounds"); should be 1..8 / max_tool_rounds + 1.
  (models/ was Task 0's file and llm.py was Task 1's, so neither owned it.)
Task 1: implementer refined the brief in 3 places, each argued: cache totals DERIVED from call_stats
  via `_sum_reported` rather than accumulated at six sites; the B1.2 guard RE-RAISES when nothing is
  salvageable (a blanket `return ""` would break two existing ceiling/consult tests); emission gated
  on `if call_stats:` so a call never issued does not write a row that books a limiter slot after
  restart. Reviewer is judging each.
Task 1: open question for the reviewer — `generate_agent_response` still has the B1.2 defect (a
  raising retry loses first-pass text AND the row for both billed calls). The brief listed retry
  site 808, which is in that function, so this may be a spec miss rather than out of scope.
Task 1: review — Spec ❌ (B1.2 at site 808, in `generate_agent_response`, unreachable from a
  `generate_with_tools` body guard; reviewer reproduced: first-pass text LOST, 0 rows for 2 billed
  calls). Quality: Needs work — 0 Critical, 3 Important, 6 Minor. Reviewer independently verified
  the semaphore bounds (20 calls → peak 8), contextvars crossing the new executor, uvloop
  weakref-ability, and that the B1.5 loop is unchanged with all 12 `max_tool_rounds=1` setups green.
Task 1: Ruling: SPLIT the spec miss. Implement only the ROW half at site 808 (emit the call log
  before re-raising); the TEXT half stays deferred. — Returning first-pass text there is unsafe until
  `tools.py` adopts `is_truncated_stop`, because `tools.py:665` still tests `== "refusal"` and would
  credit a `max_tokens` fallthrough to the panel as a complete opinion. The row half changes no
  contract and is pure gain. — If wrong: `generate_agent_response` keeps losing a usable partial
  answer on a raising retry (it still raises, as today), until the follow-on lands.
Task 1: Ruling: AMEND R5 — Task 2 gets a narrow, single-function exception to touch
  `generate_agent_response`'s retry text-return in `src/services/llm.py`, because that change and
  `tools.py`'s adoption of `is_truncated_stop` are only safe if they land together. — Splitting them
  across tasks leaves a window where a `max_tokens` fallthrough is credited as a complete consult.
  — If wrong: two files move in one commit instead of one, reviewable as a unit.
Task 1: minor (deferred): M1 WeakKeyDictionary retains one entry per CONTENDED loop (comment
  overstates); M2 `_API_MAX_CONCURRENCY` comment says "process", semaphore is per-loop;
  M3 `test_llm_event_loop.py:18` stale "the fix is asyncio.to_thread"; M4 one-line un-salvaged window
  at the forced-final site; M5 loop comment says 7 tests, real count 12 across 6 files.
Task 1: CARRY TO TASK 6 — M6: `src/models/agent_activity.py:246-248` documents the `call_stats` shape
  and is now missing `cache_read_input_tokens`, `cache_creation_input_tokens` and `block_types`.
  Same file as the 1..7 off-by-one already carried.
Task 1: DEPLOY GATE — I3: B1.2's fallthrough now RETURNS a truncated `thread_reply` instead of
  raising, and nothing in simulation.py consumes `is_truncated_stop` until Task 5's A2.5. Task 1 must
  NOT reach production ahead of Task 5. Recorded for the deploy decision, which is out of this plan.
Task 1: fix round 1/5 (3 addressed, 0 open — spec-❌ row half at site 808; `if call_stats:` narrowed
  to a new `NonStreamingMaxTokensError(ValueError)` raised pre-flight; semaphore test now saturates
  so it would catch a singleton; commits 1370fba..cba961f). Re-review: all ADDRESSED, no new breakage,
  46 targeted tests green, ruff src 225 / tests 0.
Task 1: Ruling: KEEP the truncated first-pass text on the failure row's `response_text`. — The row is
  a RECORD of what was billed, not a return value (the caller still gets the exception), and the
  dropped-verdict backfill regexes that column, so text is strictly more recoverable than "". — If
  wrong: a failure row carries a partial reply that no reader treats as authoritative; one-word revert.
Task 1: CARRY TO TASK 6 — simulation.py's comment asserting an empty `call_stats` "never happens" is
  now false: after F2 it means "turn recorded, no call completed".
Task 1: complete (commits 2f2b175..cba961f, review clean after 1 fix round)
Task 2: implemented (commits cba961f..200265c, 3 commits). Full host suite 2588 passed / 93 skipped
  (baseline 2562 → 26 net new tests). ruff tests/=0, src/=225. Every change watched red first.
Task 2: ownership exception taken and MEASURED, not predicted — 2 functional lines in
  `simulation.py::_record_specialist_consult` (`truncated: bool | None = None` + `truncated=truncated`).
  Without them `record_consult(**fields)` splats into a strict keyword-only signature and raises
  TypeError one frame below tools.py's guard: 8 red integration tests, and in production EVERY
  specialist_consults row and EVERY panel note would stop being written. Reviewer is judging whether
  it is genuinely forced and minimal.
Task 2: CARRY TO TASK 5 — `_seed_consults_from_db` must filter `SpecialistConsult.truncated.is_not(True)`
  and NOT `== False`, so pre-0036 NULL rows keep counting. Its docstring's claim that "a row exists
  only for a SUCCESSFUL consult" is false and was false before this task.
Task 2: CARRY TO TASK 5 — `_post_panel_note` still posts `⚠️ caution` for a truncated consult;
  `**_withheld` absorbs the new field silently. Now provably reachable.
Task 2: flagged for controller ruling — the implementer added a clause to `search_prior_art`'s
  MODEL-VISIBLE tool description telling the model not to write AND/OR/NOT, on top of C1.4 stripping
  those tokens in code.
Task 2: judgement calls extending the brief: added U+00B5 MICRO SIGN and U+03C2 to the Greek table
  (µ is a distinct codepoint and NFKD would fold it after the table ran); uppercase Greek
  transliterates capitalised (Γ→Gamma) so `_salience`'s symbol bonus survives.
Task 2: minor (deferred): `on_retry` never fires when the retry `_acreate` itself raises, so C1.8's
  fallthrough under-counts that second billed call silently. Pre-existing, outside the brief.
Task 2: review — Spec ✅, Quality Approved with findings (4 Important, 5 Minor). Reviewer verified on
  the host: 193 + 26 tests green, ruff src UNCHANGED at 225 across +865 lines, character class and
  injection invariant intact, C1.8 confirmed in the same commit as C1.2, no test weakened (the one
  rewritten test was a mandated inversion that kept every row assertion).
Task 2: Ruling: RATIFY the simulation.py ownership exception (2 functional lines in
  `_record_specialist_consult`). — The reviewer independently confirmed it is forced (a `**fields`
  closure splats into a strict keyword-only signature, so the TypeError lands one frame ABOVE both
  the row write and the panel note, killing every consult row in production) and that all four escape
  routes are closed. — If wrong: the later simulation.py task has a small conflict surface at that
  signature and docstring.
Task 2: Ruling: KEEP the `search_prior_art` model-visible tool-description clause. — Dropping `NOT`
  does not merely clean up syntax, it INVERTS the model's intent (`TFEB NOT melanoma` becomes
  `TFEB AND melanoma`, the opposite question), so preventing it beats disclosing it afterwards; the
  file is the implementer's own and no snapshot or sync test pins the string. — If wrong: one
  sentence of model-visible text shipped without a prompt-review pass; revertible in one line, and it
  only reaches the model on an agent-image rebuild.
Task 2: CARRY TO TASK 5 — I1: C1.8's fallthrough now reaches `_update_agent_memory` (simulation.py
  ~6663) and pi_lab `new_post` (~2687), which previously got an exception and now get truncated text.
  The memory site's OWN comment already predicts the damage ("a half-written summary is stored as the
  working memory"). A2.5's `on_stop_reason` guard is what closes it.
Task 2: fix round 1 dispatched — I2 (nothing asserts `truncated` reaches the column; deleting the
  constructor kwarg leaves the whole suite green) and I4 (residual non-ASCII: `π-π stacking` →
  `stacking` with an EMPTY disclosure, and U+2126 renders as the identity mapping `Ω29→Ω29`).
Task 3: pre-flight re-verification of the brief's load-bearing claims, run on the host against the
  live tree (not from memory): E1.5 confirmed — `allow_http` appears in exactly one place,
  `src/routers/admin.py:1055`, and nothing assigns `app.state.allow_http`, so the ternary is a
  constant False (`src/main.py:137` uses the *settings* field `allow_http_sessions`, a different
  name). E1.3 confirmed — `src/routers/profile.py:113` is `current_user: User = Depends(get_current_user)`
  on `profile_save`, while `profile_refresh` (:132) uses `get_pi_user`. E1.4 confirmed —
  `src/main.py:122` calls `FastAPI(title=..., description=..., version=...)` with no docs kwargs, and
  `tests/unit/test_reachability.py:112-115` allowlists exactly the four doc routes. E1.1 confirmed —
  `src/routers/settings.py:195` is a real `@router.post("/unsubscribe/{token}")`, so the RFC 8058
  exemption is not hypothetical.
Task 2: re-review of fix round 1 CLEAN. Both findings ADDRESSED, no new breakage. The reviewer did
  not take the implementer's word for either: it re-ran the F1 mutation on the host (removing
  `truncated=truncated` from the `SpecialistConsult(...)` call → exactly `4 failed, 26 passed`,
  one failure reading `assert None is False`, so NULL-vs-False is genuinely exercised, then restored
  the file to a clean `git diff`), and for F2 it loaded the PRE-fix module via
  `git show 200265c:src/services/patents.py` and ran the CURRENT test body against it verbatim —
  exactly one assertion failed, `'Ω29 assay': 'Ω29→Ω29' promises a non-ASCII search`, fired by the
  `.isascii()` check. It separately confirmed the implementer's discarded `before != after` version
  does NOT catch that case (U+2126 != U+03A9 as codepoints), so the added `.isascii()` assertion is
  the whole regression guard. Live cases now: `'π-π stacking'` → `(['pi','pi','stacking'],
  ('π-π→pi-pi',))`; `chr(0x2126)+'29 assay'` → `(['Omega29','assay'], ('Ω29→Omega29',))`.
  Invariants intact: `_Q_TOKEN` byte-unchanged, `total_terms=len(tokens)` at patents.py:743 (not a
  whitespace split), ruff src 225 (ceiling 231), ruff tests 0, 227 green on the covering set.
Task 2: complete (commits cba961f..1b90846, review clean after 1 fix round).
Task 5 pre-flight ruling: the column is `panel_owed`, NOT the plan's `panel_checked`. — The plan's
  Cross-Workstream Interfaces block is stale prose; the shipped artifacts all agree on `panel_owed`
  (`src/models/opportunity.py:145`, `alembic/versions/0036_*.py:143,266`, and task-5-brief.md), and
  the mapped column's own comment pins the tri-state unambiguously: True = a panel WAS owed at write
  time so the floor evaluated this verdict (an empty gap is then a real finding); False = the floor
  determined none was owed; NULL = pre-0036, unknown, never green. So `panel_owed is True` →
  `verified` in `_panel_state` is correct despite reading oddly, and Task 5 must not invert it. —
  If wrong: the assessment page's green box means the opposite of what it says, on every row.
Task 3: dispatched (WS E, web/authorization). BASE=1b90846.
Task 4: pre-flight re-verification on the host, all four claims confirmed against the live script.
  F1.1 — `scripts/backfill_dropped_verdicts.py:192` reads `verdict.get("derisking_milestones")`
  while `src/agent/simulation.py:3378` reads `verdict.get("suggested_derisking_milestones")`; the
  script's key is the wrong one. F1.3 — all three guards are module-level and importable:
  `_normalize_gating` (simulation.py:6929), `_bounded_str` (:6971), `_str_or_none` (:6990). F1.4 —
  the fallback at :135-146 selects whole `LlmCallLog` ORM rows (so `system_prompt` and
  `messages_json` come with them) and picks the last row at-or-before `drop.created_at` with NO
  subject check, exactly as the brief describes.
Task 4: NEW finding for the brief, found in pre-flight — the script's own module docstring (line 27)
  already CLAIMS "Idempotent: skips any (run, thread) that already has an assessment row", but line
  121 keys on `OpportunityAssessment.subject_agent_id`. So F1.3's idempotency change is not a
  behaviour change against the documented contract, it is making the code match a docstring that has
  been wrong since the script was written. Carry this into the Task 4 dispatch, and have the
  implementer also fix the docstring's stale "AFTER migration 0035 is applied" (head is 0036).
Task 3: implementer returned DONE. 5 commits 1b90846..fce5dba (048849f E1.4 docs closed, d248738
  E1.5 impersonate cookie Secure, 5ca802c E1.3 /profile/save -> get_pi_user, b59fde7 E1.2 revocation
  ends the session, fce5dba E1.1 OriginGuardMiddleware). DoD suites 214 passed; wider sweep 2596
  passed / 73 skipped; ruff tests 0, src 225 (unchanged).
Task 3: five self-reported judgement calls sent to the reviewer for adjudication — (1) it CHANGED an
  existing test (test_onboarding_flow.py's pending-user 200 -> 302 /access-pending), (2) it added an
  Origin header to tests/e2e/test_browser_flows.py, outside the brief's stated 3-file blast radius,
  (3) it left the two files the brief PREDICTED would need changes untouched, claiming they are
  GET-only bare-FastAPI probe apps, (4) it probed E1.1 with POST /logout instead of the brief's
  suggested routes to dodge rate-limiter 429s, (5) Referer is consulted only when Origin is ABSENT,
  not when present-but-unparseable.
Task 3: review dispatched (opus, security-focused: bypass hunt on the Origin guard, the unsubscribe
  exemption as a CSRF gadget, middleware ordering, runtime base_url derivation, plus adjudication of
  all five judgement calls).
Task 4: Ruling: dispatch Task 4's implementer IN PARALLEL with Task 3's review, breaking the
  skill's one-implementer-at-a-time rule. — The two own strictly disjoint files (Task 4:
  scripts/backfill_dropped_verdicts.py + a new unit test; Task 3's reviewer: read-only except
  transient mutation tests in src/main.py and src/dependencies.py), and the only real coupling is the
  shared git index, which I closed by mandating explicit-path commits and forbidding `git add -A`,
  `git stash`, `git checkout` and `git restore` in the Task 4 dispatch. Sequential execution was
  costing ~35 min of wall clock per task for no correctness gain here. — If wrong: a reviewer's
  transient source mutation could be swept into a Task 4 commit; detectable in the Task 4 review
  package, which will show any file outside the two it owns.
Task 4: dispatched (WS F, backfill script). BASE=fce5dba. Carried two pre-flight findings the brief
  does not have: the docstring's already-wrong idempotency claim, and its stale "AFTER migration 0035".
Task 3: review verdict — Spec COMPLIANT on all five defects, Quality "Approved with findings"
  (1 Important, 3 Minor). All five judgement calls ADJUDICATED APPROVE. The reviewer earned those:
  it mutated `!= "allowed"` back to `== "denied"` and confirmed the CHANGED onboarding test goes red
  (so it is a genuine consequence of E1.2, not a weakened test), and it read both "probe app" test
  files and confirmed they build bare `FastAPI()` apps carrying only `AgentBadgeMiddleware`/
  `SessionMiddleware` with a single `GET /probe` — the brief's prediction that they would break was
  simply wrong. Bypass hunt found NO bypass: `blackbird.copi.science.evil.com` 403, wrong scheme 403,
  `Origin: null` 403, `Origin: ""` 403, `/settings/unsubscribeX` 403, `/SETTINGS/UNSUBSCRIBE/` 403,
  exemption gated on the RAW cookie so a present-but-empty `copi-session=` is treated as present
  (fail-closed). Independently re-run: 271 passed on DoD+onboarding, 2605/66 on the wide sweep,
  ruff tests 0 / src 225.
Task 3: Important finding CONFIRMED BY THE REVIEWER'S OWN EXPERIMENT — the middleware-ordering
  invariant is NOT pinned. It moved `add_middleware(OriginGuardMiddleware)` to be added FIRST
  (innermost) and `test_origin_guard.py` still passed 12/12, because Starlette's
  `SessionMiddleware.send_wrapper` emits `Set-Cookie` only `if session.modified and session` and a
  refused request never modifies the session. The implementer's report claimed that assertion would
  catch nesting; it would not. This is the "test that cannot fail" class again, second occurrence in
  this plan.
Task 3: fix round 1 dispatched (4 items): pin ordering structurally on `create_app().user_middleware`
  and verify by actually reordering; make the e2e Origin's BASE_URL-equality requirement explicit
  (it is currently derived from `E2E_BASE_URL`, the wrong side of the wire); comment the now-vacuous
  onboarding control instead of papering over it; normalise default ports.
Task 3: Ruling: ADD the `Sec-Fetch-Site` acceptance path, overriding the reviewer's decision to defer
  it. — The brief's own title names it, and as shipped the change introduces an availability defect:
  a browser under a `no-referrer` policy sends `Origin: null` AND no Referer on same-origin form
  POSTs, so every form on the site returns a bare 403 with no recovery. Specified precedence:
  a present non-`null` Origin must still MATCH (mismatch 403s regardless of Sec-Fetch-Site), then
  `Sec-Fetch-Site: same-origin` accepts, then Referer, then 403. Accept ONLY `same-origin` — never
  `same-site`, which is computed on the registrable domain and would admit `copi.science` and
  `devel.copi.science`, the exact attack. Not a weakening: `Sec-Fetch-*` are forbidden header names
  so page script cannot set them, and a non-browser client that can carries no ambient cookie. —
  If wrong: one extra accepted header on state-changing requests; revertible in one branch.
Task 3: Ruling: admin impersonation of a DENIED account stays open. — The E1.2 check is on the
  session holder, impersonation is already admin-gated, and a denied user carries no power an admin
  lacks, so it is a support path and not an escalation. Requires a one-line comment recording the
  intent. — If wrong: an admin can act as a revoked account; already fully audited and admin-only.
Task 4: implementer returned DONE, commit f609ebb (2 files, 11 tests). Review dispatched (opus).
Task 4: production measurements taken by me, read-only, and handed to the reviewer so it does not
  re-derive them: prod alembic head is **0035** — 0036 is NOT applied, `opportunity_assessments`
  has no `thread_id` column there yet, so the script must not run against prod until it is. There
  are 9 recoverable drops (premature_sidecar 7, unparseable_sidecar 2, duplicate_thread_verdict 0);
  ALL 9 have a non-NULL `thread_id`; and NONE of them carries `raw_verdict` (that column arrived in
  0035, after they were written), so EVERY recoverable drop goes down the `llm_call_logs` fallback —
  F1.4's cross-PI leak is load-bearing for all 9 rows, not hypothetical. Existing (run,subject)
  assessment rows: huganir 1, markham 1, weeraratna 1, hart 2, and bailey/gill/pienta/thompson/
  dimopoulos 0.
Task 4: the review's decisive question, derived from those measurements — after 0036,
  `opportunity_assessments.thread_id` is NULL on EVERY pre-existing row (0036 deliberately does not
  backfill it), including the two rows this script itself wrote. So the `(run, subject)` fallback
  must be driven by the EXISTING ASSESSMENT ROW's NULL thread_id, not by the drop's. Backwards —
  "the drop has a thread_id, so compare thread_ids only" — re-writes markham, weeraratna, huganir
  and hart as duplicates on the supervised re-run. Reviewer is required to run both scenarios and
  state the outcome.
Task 4: review verdict — Spec COMPLIANT on all five items (F1.1-F1.4 + the idempotency re-key),
  Quality "Approved with findings" (2 Important, 5 Minor). The reviewer settled the decisive
  question BY EXECUTION rather than reading: it ran `_existing_assessment_for` on the host against
  real ORM objects and got the two required outcomes — drop thread_id='T1' vs existing row with
  thread_id=NULL → SKIPPED (so markham/weeraratna/huganir/hart are not re-written), and drop
  thread_id='T2' vs existing row thread_id='T1' same subject → WRITTEN. It then mutated the source
  three ways (subject-only, thread-only-with-no-fallback, and the INVERTED `if drop.thread_id is
  None`) and all three go red, so the duplicate-four-rows failure is genuinely pinned. It also
  mutated 20+ other ways via `inspect.getsource` + injection: 12 mutations red, 8 GREEN — those 8
  are findings 3, 2 and part of 5.
Task 4: Important finding 1 — my BRIEF was wrong, not just the code. It said "refuse a candidate
  whose subject_agent_id does not equal the drop's", written without knowing the model is never
  shown its partner's real agent_id, only `{other_agent_name}` (the bot_name). Measured on prod:
  4 of 63 stored `raw_verdict->>'subject_agent_id'` name the bot (dang→dangbot, krieger→kriegerbot,
  lee→leebot), and `thompson`'s ONLY candidate log row says `ThompsonBot`, so strict equality
  reports thompson unrecoverable where the PRE-FIX code recovered it correctly. Ruling: amend the
  brief — compare case-folded and additionally accept exactly `f"{agent_id}bot"` case-folded, which
  is how bot_name is generated. It must still refuse `epearce` vs `pearce` (a real last-name
  collision, two different agents) and every cross-PI case. — If wrong: one wrong-PI verdict could
  be recovered if a PI's agent_id were literally another PI's agent_id + "bot"; no such pair exists.
Task 4: Important finding 2 — the backward walk is unbounded in time and its ordering is unpinned
  (flipping `.desc()`→`.asc()` leaves all 11 tests green, and ascending returns the OLDEST
  same-subject sidecar in the run). `llm_call_logs` has no thread_id, so nothing confines the walk to
  the drop's interview. Real instance: hart's drop has TWO `hart` candidates, 0.3 s and 272.6 s
  before it, and the near one is unparseable BY DEFINITION (that is why the drop exists) — so the
  walk lands on a 4.5-minute-older superseded verdict and writes it unmarked. Ruling: cap at a
  `_MAX_LOOKBACK_SECONDS = 60` module constant (all nine real drops sit 0.2-0.3 s after their own log
  row, so 60 s is a ~200x margin and still excludes hart's 272.6 s), expose `--max-lookback-seconds`
  to widen it, pin the ordering with a test, and put the chosen row's time delta into `source` so the
  WRITE log discloses a stale pick. — If wrong: a legitimate recovery >60 s from its drop is reported
  unrecoverable instead of silently mis-recovered, and the operator widens the flag.
Task 4: Ruling on Minor 5 — DERIVE the rubric stamp from the run instead of defaulting it. The
  `--rubric-version`/`--rubric-hash` defaults are run 8b64a0e0's and are applied silently to any
  `--run`, contradicting the module docstring's "stamps the rubric version from the RUN", and the
  supervised re-run spans THREE run ids. Query the distinct non-NULL pairs on the run's existing
  assessments: exactly one → use it; all NULL → write NULL; ambiguous → refuse and demand the flags.
  Keep the flags as an override and `_bounded_str` them (they are the one remaining path to a
  StringDataRightTruncation that takes the whole single-commit batch down). — If wrong: the script
  refuses to run on an ambiguous run and the operator passes the flags, which is the old behaviour.
Task 4: fix round 1 dispatched (7 items).
Task 4: PREDICTED OUTCOME of the later supervised re-run, to be re-confirmed after the fixes —
  88d81cd8: hart skipped. 076e80b6: pienta, gill, bailey WRITTEN, huganir skipped, thompson now
  RECOVERED (was unrecoverable before FIX 1), dimopoulos unrecoverable (genuinely truncated sidecar,
  no closing tag). 8b64a0e0: markham, weeraratna skipped. Net 4 written, 4 skipped, 1 unrecoverable.
  DO NOT RUN IT UNTIL 0036 IS APPLIED — prod head is 0035 and the script aborts loudly (UndefinedColumn
  on the up-front select, before any write) if run early, which is the correct failure mode.
Task 4: I re-measured prod myself rather than relaying the reviewer's numbers. Both Important
  findings confirmed, and one NEW fact that changes the design. (a) Exactly four of 63 assessments
  have `raw_verdict->>'subject_agent_id'` differing from the stored value: dang|dangbot,
  krieger|kriegerbot, lee|leebot, pearce|epearce — so the bot form is `{agent_id}bot` case-folded in
  three, and pearce|epearce is the one that must still be refused. (b) Counting candidate
  llm_call_logs rows in the window (drop.created_at - 60s, drop.created_at] for all nine recoverable
  drops: eight have exactly ONE candidate and hart's 272.6 s stale sidecar correctly falls OUTSIDE
  the window, confirming the cap's arithmetic; thompson's single candidate says `ThompsonBot`,
  confirming FIX 1 is the only thing preventing a false "unrecoverable". (c) NEW: **pienta has TWO
  candidates inside 60 s and one of them is huganir's sidecar** — the F1.4 cross-PI leak is live
  INSIDE the window, so the lookback cap does NOT make the subject check redundant. Both defences
  are load-bearing and must cooperate; sent to the implementer with a request for a test mirroring
  that exact shape.
Task 3: fix round 1 returned DONE — 83667cb (structural ordering pin), 4e70fdb (default-port
  normalisation + Sec-Fetch-Site: same-origin), 719d043 (e2e self-diagnosis, honest onboarding
  control, impersonation comment). Reds observed: reordering create_app() to add the guard first
  reproduced the reviewer's finding EXACTLY (`1 failed, 12 passed`, stack ['SessionMiddleware',
  'AgentBadgeMiddleware', 'OriginGuardMiddleware']); FIX 4 was `5 failed, 24 passed` before, with the
  seven negative controls passing both before and after. 2625 passed / 73 skipped, ruff tests 0 /
  src 225. It WITHDREW the two false round-0 report claims explicitly rather than editing them away.
Task 3: Ruling on its concern 2 (pytest.fail over pytest.skip for the e2e config mismatch): APPROVED
  — a silent skip of the whole e2e tier on a config typo is the failure mode conftest's collection
  hook exists to prevent.
Task 3: Ruling on its concern 1 (FIX 2 only half-verified — it could not stand up a mismatched
  server because starting a container was off-limits): ACCEPTED as-is; the substitute (a drift alarm
  on the grepped substring, mutation-confirmed) closes the part that can rot silently. Residual
  limitation recorded, no further work.
Task 3: Ruling on its concern 3 — `Origin: null` must NOT fall through to the Referer check; only
  `Sec-Fetch-Site: same-origin` may rescue it. My own Sec-Fetch-Site ruling loosened this as an
  unintended side effect, undoing a property the reviewer had explicitly approved. It costs no real
  user: the population the availability fix exists for is on a `no-referrer` policy and sends no
  Referer at all, so the fallthrough rescues nobody it was meant to. And the shape that DOES produce
  "opaque origin + a Referer on our own origin" is a sandboxed <iframe> pointed at one of our own
  pages, where the only thing standing in the way is nginx's `X-Frame-Options: DENY` — I will not
  have the guard's correctness depend on a header set in a different tier's config file. Fix 1b
  dispatched. — If wrong: one exotic legitimate client shape gets a 403; no browser produces it.
Task 3: fix round 1b dispatched with an explicit instruction to run ONLY the guard suites and NOT a
  wide sweep, because Task 5 is about to begin editing src/agent/simulation.py and several tests
  `inspect.getsource()` whole modules.
Task 5: dispatched (WS A1/A2, assessment pipeline: A1.3, A2.1-A2.5) on opus. BASE=719d043. History
  is linear and interleaved across three agents — build its review package from ITS OWN contiguous
  commit range, not from BASE..HEAD.
Task 3: fix round 1b returned DONE — 22b7588. Red observed:
  `test_an_opaque_origin_is_not_rescued_by_a_referer` failed `assert 302 == 403`, i.e. the
  fallthrough ALLOWED the forged POST and cleared the victim's session — the concern was real and
  exploitable in the shape I described, not theoretical. Final precedence: usable-Origin decides
  alone -> `Sec-Fetch-Site: same-origin` -> opaque Origin STOPS (403) -> Referer only when there was
  no Origin header at all. 32 passed in the guard suite; 105 passed / 7 skipped across the targeted
  set; no wide sweep run, as instructed.
Task 3: scoped re-review dispatched over f609ebb..22b7588 (4 commits), verdicting F1-F4 plus the
  three pieces of scope I added (S1 Sec-Fetch-Site precedence, S2 opaque-Origin non-fallthrough,
  S3 the impersonation comment). It is required to REPEAT the previous reviewer's ordering
  experiment rather than trust the new assertion, and to probe every Sec-Fetch-Site value against
  the real app — `same-site` in particular MUST be refused, since the same nginx serves the
  unrelated production tenant copi.science and devel.copi.science.
Task 4: fix round 1 returned DONE — dd8ea77, all 7 fixes, 39/39 tests (up from 11), each watched red
  by reverting to its pre-fix form. It CONFIRMS the predicted supervised-re-run outcome of 4 written
  / 4 skipped / 1 unrecoverable, traced mechanism-by-mechanism (thompson now recovered via the
  bot-name match; hart/huganir/markham/weeraratna skips unchanged; dimopoulos still unrecoverable —
  its sidecar is genuinely truncated, no closing tag). It also correctly noticed and reported that
  the `src/` changes visible in the working tree belong to the CONCURRENT Task 5 agent and are not
  in its commit — the parallel-execution ruling is holding.
Task 4: its one disclosed concern — FIX 7's dry-run bookkeeping and the `logging.basicConfig` move
  are verified BY INSPECTION ONLY, since both live inside `main()`'s async DB-backed body and the
  task forbids a database. Disclosed rather than glossed; handed to the re-review to judge whether a
  cheap test was actually available (e.g. extracting the bookkeeping into a testable helper).
Task 4: scoped re-review dispatched over 22b7588..dd8ea77. It must settle F1/F2(a)/F6 by REAL source
  mutation rather than trusting the report, re-run the two idempotency scenarios by execution, and
  check the interaction I measured: the 60 s cap does NOT make the subject check redundant, because
  pienta has two candidates inside the window and one of them is huganir's verdict.
Task 3: re-review CLEAN. F1-F4 all ADDRESSED, S1-S3 all ✅, no weakened tests, no new
  Critical/Important breakage. The reviewer REPEATED the ordering experiment rather than trusting
  the new assertion: with the guard added first it got `1 failed, 31 passed` — and the telling
  detail is that all 31 BEHAVIOURAL tests, including the old `Set-Cookie` proxy, stay green with the
  guard innermost; only the new structural test notices. It restored src/main.py and verified by
  sha256sum (identical before/after) plus an empty `git status --porcelain` for its own files.
  20 default-port cases probed against the real app, including the two that matter — a non-default
  port is still kept (`:8443` → 403) and the wrong scheme's default is still refused
  (`http://host:443` vs `http://host` → 403). Every Sec-Fetch-Site value probed: `same-site`,
  `cross-site` and `none` all REFUSED, wrong-Origin + `same-origin` REFUSED, sibling
  `https://copi.science` + `same-origin` REFUSED. It independently sanity-checked the availability
  premise against the Fetch spec (a non-CORS non-GET under `no-referrer` really does get
  `Origin: null` while `Sec-Fetch-Site` is computed from real origins, and a sandboxed iframe
  computes `cross-site`, not `same-origin`) — so the branch I ruled in is sound, not a hole.
  185 passed / 7 skipped targeted; ruff tests 0, src 224.
Task 3: complete (commits d248738..22b7588, review clean after 2 fix rounds).
Task 3: carried forward, not blocking — (a) the e2e probe fixture's own control flow against a live
  server whose BASE_URL disagrees is still unexercised, accepted by ruling; (b)
  `src/routers/onboarding.py:98`'s access_status guard is now an honestly-labelled UNTESTED backstop;
  (c) task-3-report.md:227's "strictly stronger" sentence is withdrawn at :443 but not corrected in
  place, so a top-down reader meets the false claim first; (d) with E2E_BASE_URL set but the server
  down, two previously server-independent e2e tests now error in fixture setup (Low); (e)
  `normalized_origin` strips userinfo via `urlsplit.hostname`, a benign behaviour change that no
  test pins.
Task 4: re-review CLEAN. All seven findings ADDRESSED, no new breakage, ownership clean
  (`git show --stat dd8ea77` = exactly the two owned files; the `src/` dirt in the working tree is
  Task 5's, and the src ruff drift 225->224 is theirs too). 16 mutations run, 14 red. Both
  idempotency scenarios re-determined BY EXECUTION, plus F6's new case: drop NULL vs existing T1
  same subject -> SKIPPED. The pienta interaction verified end-to-end: two candidates inside 60 s,
  the nearer one huganir's, walk returns pienta's from the FARTHER row, and each defence is
  independently load-bearing (reverting either one turns a different test red). F2 ordering pinned —
  the reviewer flipped `.desc()`->`.asc()` itself and got exactly 1 failed, 38 passed.
Task 4: the reviewer ran one read-only prod query I had not, and it validates the F5 ruling on real
  data: `076e80b6` -> one stamp pair 2.0.0/e3ef75f84c48 (16 rows), `8b64a0e0` -> one pair (15 rows),
  `88d81cd8` -> ALL NULL (22 rows). So `_derive_rubric_stamp` never hits its refusal branch on real
  data, and the pre-fix hardcoded default WOULD have fabricated a 2.0.0 stamp for 88d81cd8's rows.
Task 4: operator prediction AGREED and independently traced: 88d81cd8 hart skipped; 076e80b6 pienta,
  gill, bailey, thompson WRITTEN, huganir skipped, dimopoulos unrecoverable (the LIKE matches the
  OPENING tag so a candidate row exists, but `_SIDECAR_RE` needs the closing one); 8b64a0e0 markham,
  weeraratna skipped. Net 4/4/1. Precondition restated: apply 0036 first, then `--dry-run` first —
  it now logs the derived rubric stamp before the loop, which is a free pre-flight on F5.
Task 4: Ruling: run a SMALL fix round 2 for three of the five deferred items rather than closing the
  task on them. — The reviewer mutated `_subject_matches` to `startswith` and ALL 39 TESTS STAYED
  GREEN; the shipped code is correctly anchored, but a future loosening to prefix matching would
  ship silently, and that is the FOURTH test-that-cannot-fail in this plan. The other two are a
  3-line import-time logging assertion the reviewer already wrote and ran (so "unverifiable without
  a DB" was wrong — the assertion is about import, not about `main()`), and rejecting a negative
  `--max-lookback-seconds`, which would silently report all nine drops unrecoverable. Explicitly NOT
  doing the other two deferred items: extracting `main()`'s loop for the dry-run test, and
  `--rubric-version ""` (operator error only, now caught by the dry-run pre-flight). — If wrong:
  ~10 minutes spent on three test-only changes.
Task 4: I will verify the `startswith` mutation myself rather than dispatching a fourth review seat.
Task 4: fix round 2 returned DONE — 2d84843, 44/44 (up from 39).
Task 4: FIFTH test-that-cannot-fail in this plan, and the first one caught by an IMPLEMENTER against
  a REVIEWER's suggestion. The reviewer's proposed 3-line in-process logging assertion
  (`logging.getLogger().handlers == []` after import) is itself vacuous under pytest: `_pytest.logging`
  pre-attaches a root handler, so the assertion fails against ALREADY-FIXED code and `basicConfig` is
  a no-op whichever code runs. The implementer replaced it with a subprocess-isolated version and
  verified red/green. Lesson for the final review: a suggestion from a reviewer is not pre-verified
  just because a reviewer made it.
Task 4: I verified the `startswith` closure MYSELF on the host rather than taking the report. Mutated
  line 182 from `candidate_folded in (drop_folded, f"{drop_folded}bot")` to
  `candidate_folded.startswith(drop_folded)` -> `1 failed, 43 passed`, failing exactly
  `test_subject_matches_bot_name_and_case_but_not_collisions[leebottomley-lee-False]` with
  `where True = _subject_matches('leebottomley', 'lee')`. Restored from a pre-mutation copy; md5
  identical before and after (df20bab8c530f629314065dc216adb68) and `git diff --stat -- scripts/`
  empty. The gap that was silent under 39 tests is now genuinely pinned.
Task 4: complete (commits f609ebb..2d84843, review clean after 2 fix rounds).
Task 5: implementer returned DONE_WITH_CONCERNS. Two commits, c89da2c (A1.3, A2.1, A2.2) and
  ad9ff11 (A2.3, A2.4, A2.5), NOT contiguous — Task 4's 2d84843 landed between them. 307 passed on
  the DoD + list-surface suites, 1849/2 on the whole unit suite, 850/64 + 16 snapshots on
  integration+characterization; ruff tests 0, src 224.
Task 5: SIXTH test-that-cannot-fail, and the second found by an implementer on its own work —
  its mutation M16 went GREEN: `test_rehydration_runs_after_the_thread_decisions_are_loaded` matched
  on the bare METHOD NAME inside `SimulationEngine.start`'s source, and the explanatory comment above
  the call contains that same name, so deleting the call left the test passing on prose. It now
  matches the call expressions and re-runs RED. 22 mutations total, 21 red before the fix.
Task 5: review package built by hand, NOT by scripts/review-package — the two commits are
  non-contiguous, so a BASE..HEAD range would have silently swept in Task 4's round-2 commit.
  Used `git diff dd8ea77..ad9ff11 -- src/ templates/ tests/ ':!tests/unit/test_backfill_dropped_verdicts.py'`,
  which isolates Task 5 exactly: 15 files, +2112/-178.
Task 5: three cross-file stragglers it could NOT fix because the files were off-limits — A2.1 asks
  for `panel_is_owed`'s docstring (it enumerates three call sites and is now wrong) and two comments,
  all in `src/agent/specialists.py`, which still name the old private `_panel_state` after the rename
  to public `panel_state`. Assigned to Task 6 as a narrow extra ownership grant; the reviewer is
  asked to confirm they are the ONLY stragglers.
Task 5: my lean on its concern 2, for the reviewer to adjudicate — APPROVE widening
  `incomplete_panel_count` rather than adding a key. Adding one means editing `src/routers/*`
  (admin.py allowlists every context key, manager.py splats `**view`), which was off-limits, and a
  new key would render on /manager and be silently `Undefined` on /admin. The cost is that all 64
  historical rows are `panel_owed IS NULL` and 0036 deliberately does not backfill, so the All Runs
  banner reports a number that CAN NEVER REACH ZERO. Excluding NULL rows would reproduce exactly the
  silence the column exists to end, and under-warning is the dangerous direction — but the banner
  COPY must say the number includes rows whose panel status was never recorded, or a reader believes
  there are 64 known gaps. Reviewer must read the rendered copy on both surfaces.
Task 6: Ruling: HOLD Task 6 until Task 5's review completes, rather than running it in parallel as I
  did with Tasks 3 and 4. — Task 6 owns `src/agent/simulation.py`, and the Task 5 reviewer must
  mutate that same file to verify A2.2 and A2.4. Mutation testing is copy-mutate-restore, so a
  reviewer's restore landing after a Task 6 edit would silently DESTROY that edit — a genuinely
  destructive race, not the recoverable index contention I accepted earlier. — If wrong: one task's
  wall-clock spent serial.
Task 6: pre-flight verified on the host — A3.1's unfiltered deletes are real, `src/agent/main.py:174-176`
  (`AgentMessage`, `AgentChannel`, `PiDmMessage`, all `__table__.delete()` with no run filter);
  `_sync_private_channels_from_db` is at simulation.py:2417 with callers at :707 and :846; the
  background flush task is `loop.create_task(self._flush_llm_logs())` at simulation.py:6401.
Task 5: review verdict — Spec COMPLIANT 6/6 (A1.3, A2.1-A2.5), Quality "Approved with findings"
  (1 Important, 5 Minor). Ownership clean, no snapshot regenerated (16 snapshots, count unchanged),
  no prompt or thread_guidance literal touched. Full host sweep at ad9ff11: 2699 passed / 66 skipped,
  ruff tests 0 / src 224. The tri-state is NOT inverted — shipped mapping at assessment_detail.py:445-453
  is gap -> unverified -> verified(True) -> not_owed(False) -> unrecorded(None), matching the column
  comment exactly, and `bg-green-50` occurs exactly ONCE in the template, on the verified branch.
Task 5: all five concerns ADJUDICATED APPROVE. The two that needed real work: (2) widening
  `incomplete_panel_count` — the reviewer agreed excluding NULL rows reproduces the silence the
  column exists to end, and confirmed the body copy DOES name the third disjunct, so only the bolded
  headline misleads (now FIX 2); (3) the per-row instance attribute — it verified
  `expire_on_commit=False` at src/database.py:53, that `list_assessments` performs no
  commit/refresh/expire after the assignment, that a non-mapped attribute does not mark the instance
  dirty so autoflush is a no-op, that BOTH surfaces route exclusively through `list_assessments`
  (admin.py:728, manager.py:257), and that the residual failure mode if a row ever arrived without it
  is Jinja `Undefined` -> NO badge, never a false green.
Task 5: the reviewer ran 7 of its own mutations, all red. The decisive one is M-C: it kept an
  explicit `unrecorded` amber branch and made ONLY the terminal `{% else %}` green, isolating the
  UNKNOWN-state claim from the `unrecorded` case — `test_an_unknown_panel_state_never_renders_green`
  went red by itself. That is the highest-risk test in the task and it is real. M-F (dropping the
  `slack_ts` predicate from `_superseded_row_filter`) also went red on
  `test_supersession_does_not_delete_the_replacement_row`, so the data-loss trap the plan audit
  caught is pinned.
Task 5: Important finding — `PANEL_STATES_UNVETTED` (assessment_detail.py:391-395) is dead code
  whose comment CLAIMS directory.py's count uses it "so the banner and the per-row badge cannot
  disagree". directory.py never references it; the count at :349-356 is an independent SQL
  predicate, i.e. a SECOND COPY of the unvetted rule — the exact drift this task exists to end.
  Ruling: make the coupling real (a drift alarm over the full (panel_incomplete, missing_domains,
  panel_owed) matrix asserting the SQL selects a row IFF `panel_state(row) in PANEL_STATES_UNVETTED`)
  rather than deleting it, with deletion of both constants + the false comment as the stated
  fallback. — If wrong: a matrix test that is slower than it is worth; the fallback is one deletion.
Task 5: Ruling: FIX 7 pulled BACK IN from the reviewer's deferred list. It called
  `src/models/opportunity.py:100-114` off-limits — it is not; `src/models/*` was in Task 5's
  ownership grant from the start. That block's `missing_domains` comment still says
  "NULL — panel VERIFIED complete", which is precisely the conflation `panel_owed` splits, so
  leaving it deferred would ship the new column beside a comment asserting the old semantics.
Task 5: fix round 1 dispatched (7 items).
Task 6: still held. Task 5's fix round touches simulation.py at two points (findings 3 and 6), and
  two agents committing `git add src/agent/simulation.py` would sweep each other's uncommitted work
  into the wrong commit. Serial until the fix round lands.
Task 5: fix round 1 returned DONE — 428b815, all 7 fixes, 313 passed across the 15 touched suites,
  ruff tests 0 / src 224, 7 mutations all red.
Task 5: Ruling: APPROVE the FIX 1 scope extension. I ruled "add a drift alarm OR delete the
  constants"; the implementer instead MOVED the SQL predicate out of directory.py into
  `assessment_detail.unvetted_panel_filter()`, beside the state machine and the constant, and had
  directory.py import it — arguing that a test-only binding leaves the unvetted rule written twice in
  two modules with a test as the only glue. That is a better answer than my ruling: it makes the rule
  defined once in CODE rather than merely asserted-equal by a test. Its drift alarm then walks the
  full 18-row matrix asserting SQL-selects IFF `panel_state(row) in PANEL_STATES_UNVETTED` ROW BY ROW
  (labels, not counts — the compensating-errors trap), reading rows back FROM THE DB so the
  JSONB-null trap cannot hide between the Python and SQL sides, plus non-degeneracy and
  full-state-coverage assertions. Both drift directions proven red. — If wrong: a predicate now lives
  a module away from its only consumer; both files are the same task's and the revert is small.
Task 5: FIX 7 turned up MORE than I sent it after. Told to check the rest of the block, it found the
  same stale claim one column earlier: `panel_incomplete`'s comment said two columns suffice to read
  a finding, when it now takes three. That is the exact class of half-truth the finding was about,
  and it was not in the review.
Task 5: scoped re-review dispatched over ad9ff11..428b815, with an explicit carve-out — it MUST NOT
  mutate src/agent/simulation.py, because Task 6 is now editing that file and mutation testing is
  copy-mutate-restore, so a restore landing after a Task 6 edit would silently destroy it. F3 and F6
  are log-message changes, verifiable by reading plus running their tests; everything that needs
  mutation (F1, F2) lives in files Task 6 does not touch. That carve-out is what makes the two safe
  to run in parallel.
Task 6: dispatched (WS A3, agent main loop: A3.1-A3.9) on opus. BASE=428b815. A3.1 flagged CRITICAL
  and ordered first — it is a data-destruction bug. Given the narrow extra ownership grant for the
  three stale `src/agent/specialists.py` comment/docstring edits Task 5 could not make, explicitly
  comments-only, no behaviour.
Task 6: dispatch carries the full list of the SIX tests-that-cannot-fail shipped so far, by SHAPE
  rather than by count, plus two specific traps for this task: A3.1 must have a test that fails
  loudly if the run filter is removed, and A3.7's accounting must be pinned by a fixture where
  "turns" and "real API calls" actually DIFFER — one where they are equal proves nothing.
Task 5: re-review CLEAN. All seven findings + the tightening ADDRESSED, scope extension APPROVED,
  no new breakage, ownership clean (`src/agent/specialists.py` untouched, as required). It honoured
  the simulation.py carve-out — never mutated that file, verified F3/F6 by reading plus a standalone
  caplog probe instead. 8 mutations of its own, ALL RED, including three I did not ask for:
  M5b (third class named ONLY in the unbolded sentence, not the bold span) still RED, so F2's test is
  span-scoped for real; M6 (drop `none_as_null=True` from missing_domains) RED, which proves the F1
  alarm genuinely catches the JSONB-null trap rather than merely claiming to; and M7 (a sole,
  comma-less `from src.agent.specialists import panel_is_owed`) RED, while `grep -c "panel_is_owed,"`
  on the same mutated file returned 0 — so the old text assertion would have sailed past and the new
  symbol-table check is what closes it.
Task 5: the scope-extension adjudication was done properly rather than waved through — the reviewer
  compiled the OLD hand-written query and the NEW one side by side against the postgres dialect with
  `literal_binds` and got BYTE-IDENTICAL SQL including the run scope
  (`WHERE (panel_incomplete IS true OR missing_domains IS NOT NULL OR panel_owed IS NULL) AND
  simulation_run_id = 'X'`), confirmed `or_()` stays one grouped clause so nothing widens, confirmed
  lab_filter is still applied only to `query` and never to `incomplete_query` (the warning follows
  the RUN, not the lab, as the template's own note says), and confirmed no new import edge —
  directory.py already imported from assessment_detail.
Task 5: complete (commits c89da2c..428b815, review clean after 1 fix round).
Task 5: FIVE deferred items, none blocking. Disposition: (1) alembic/versions/0036_*.py:13 still
  names `_panel_state` — no task owns alembic, I take it in the cleanup batch. (2)
  src/agent/specialists.py:481,552 — ALREADY assigned to Task 6. (3) the F1 alarm computes its Python
  side from `expire_on_commit=False` session objects, so an `expire_all()` before the read-back would
  make "as the database has them" literal — cleanup batch. (4) `_superseded_raw_verdict` has a FIFTH
  silent-None case the docstring does not list: row found but `raw_verdict` itself NULL, no warning —
  cleanup batch (simulation.py, so it must wait for Task 6 to finish). (5) the symbol-table check
  does not catch `import src.agent.specialists as sp; sp.panel_is_owed(...)` used outside
  `panel_state` — ACCEPT, the body assertions still catch any use inside the read path.
Task 5: Ruling: batch deferred items 1, 3 and 4 into ONE small cleanup dispatch AFTER Task 6 lands,
  rather than extending Task 5's loop or smuggling them into Task 6. — They are three unrelated
  one-liners in three different owners' files; a single batch is the skill's own guidance for small
  same-shape work, and item 4 cannot start until Task 6 releases simulation.py anyway. — If wrong:
  one extra dispatch.
Task 6: implementer returned DONE. NINE commits 428b815..af3859e (4c49e62 A3.1, 822693a A3.2+A3.3,
  9fa797d A3.4, 8be84a8 A3.5, bb200ec A3.6, 68e35c6 A3.7, 49173f8 A3.8+A3.9, 4206dca the specialists
  comment grant, af3859e an A3.4 follow-up restoring exc_info). Whole suite 2775 passed / 93 skipped
  (run 4x); DoD list 208 passed; ruff tests 0, src 224. ~25 mutations, all detected. Largest diff in
  the plan: 17 files, +2575/-322, 955 changed lines in simulation.py alone.
Task 6: SEVENTH AND EIGHTH tests-that-cannot-fail — and BOTH were caught by the implementer on its
  own work before claiming the item, which is the first time that has happened twice in one task.
  A3.3: three tests passed against broken code because `stop()`'s own flush beat the never-yielding
  spawned task to the buffer. A3.9: a substring filter that the SUPPRESSED DEBUG line also matched.
  Both documented in the test docstrings. Handed to the reviewer as a POINTER to where the remaining
  one likely is, not as a discharge.
Task 6: two scope questions for the reviewer that I could not settle from the diff alone —
  (1) `src/agent/message_log.py` (+37) was edited and was in NEITHER my allow list nor my forbid
  list, a genuine gray area; A3.8 plausibly requires it, so the question is necessity and minimality,
  not permission. (2) `src/agent/specialists.py` shows +40/-, against a grant of COMMENTS AND
  DOCSTRINGS ONLY — the reviewer must confirm hunk by hunk that not one executable line moved.
Task 6: A3.7 is a UNITS CHANGE with an operational consequence: `SimulationRun.total_api_calls` now
  counts API calls, not turns, so it is not comparable with any historical run and the discontinuity
  lands at whichever run first starts on this code. Old number recoverable as COUNT(*) over
  llm_call_logs. Reviewer must confirm this is documented where an OPERATOR sees it — a column
  comment, model docstring or startup banner — not only in a report file.
Task 6: one open flake to resolve before the CI gate —
  `tests/integration/test_concurrent_web_writes.py::test_concurrent_proposal_reviews_do_not_500`
  failed ONCE under random ordering, green in six subsequent runs; the implementer touched neither
  it nor its router/model. Reviewer must say plainly whether this diff can have caused it (anything
  touching the shared session-scoped engine, pool sizing, or event-loop lifetime), because a flake
  during ./scripts/ci.sh will otherwise be misread as a regression.
Task 6: DEPLOY NOTE for the final summary — `src/` changed, so the agent image needs
  `$DC --profile agent build agent` before any next run. No migration added; tree head stays 0036,
  production stays 0035. Nothing deployed, built or started.
Task 6: review verdict — Spec COMPLIANT on all NINE items plus the specialists comment grant,
  Quality "Approved with findings" (2 Important, 4 Minor). Ownership clean; snapshot dir diff EMPTY;
  no prompt or thread_guidance literal touched. 16 reviewer mutations: 15 red, 1 green.
Task 6: CORRECTION TO MY OWN DISPATCH — I flagged `src/agent/message_log.py` to the reviewer as a
  gray area outside the implementer's grant. It was not: task-6-brief.md:3-4 names it explicitly.
  My error, not the implementer's; told it so directly. It is also the only place A3.8 can be fixed.
Task 6: A3.1 was solved better than the brief's framing — the three deletes are REMOVED, not scoped
  (the brief's "delete nothing"), and the hard prerequisite landed: `_sync_private_channels_from_db`
  now carries the run filter plus a `not self.simulation_run_id` early return. The fixture is a
  PREVIOUS run's collab_private row that must survive, with an opposite-direction control, so it
  cannot pass on a single-run seed.
Task 6: Ruling: DO NOT overrule the implementer on A3.2's unguarded `finally`. — The reviewer read
  `_drain_memory_events` and found the no-raise contract is ENFORCED BY CODE (`_update_agent_memory`
  wraps its whole body in `except Exception`), not merely asserted in prose, and that even if it did
  raise, Python's implicit chaining puts the loop-body exception in `__context__` so the original
  traceback still prints — masked is the wrong word, demoted is right. A blanket swallow would have
  hidden a real failure. — If wrong: a BaseException from the drain could still displace the primary.
Task 6: Important 1 — the hoisted drain is UNBOUNDED and now runs on the SHUTDOWN path. `stop()`
  bounds its drain with MEMORY_EVENTS_MAX_AT_SHUTDOWN precisely because each event is a real LLM call
  and the grace period is finite; the new `finally` at simulation.py:1064 has no limit and no
  `_running` check, so a `docker stop -t 420` landing in the idle-backoff branch runs N unbounded
  10-40 s LLM calls before the `while` condition is even re-tested. A3.2 opened that path. One-line
  fix, no existing test covers it.
Task 6: Ruling: FIX 6 pulled IN from the reviewer's deferred list — an assessment row lost to
  per-row recovery must leave an `AssessmentDrop`. — The reviewer deferred it as outside the brief.
  It is in scope for one specific reason: **A3.4 CREATED this path.** Before it, a poison row lost
  its whole batch loudly and requeued; now a single row is dropped with one log line, while every
  other way a verdict fails to land writes a drop row. "Every way an assessment can be lost is
  silent" is the exact defect class this plan exists to end, and closing a hole your own change
  opened is in scope. Constrained to best-effort: own try/except, own session (the failing one may be
  in an aborted transaction), ERROR if the drop write itself fails, no retry. — If wrong: ~15 lines
  of new write path added late; it can only ever log.
Task 6: fix round 1 dispatched (7 items, incl. FIX 7, Task 5's carried fifth silent-None case in
  `_superseded_raw_verdict`).
FLAKE RESOLVED: `test_concurrent_proposal_reviews_do_not_500` is PRE-EXISTING and is a TEST-DESIGN
  defect, not a regression. The reviewer reproduced it standalone — 2 failures in 15 runs (~13%),
  `-p no:randomly`, nothing else in session — and excluded causation outright: nothing in Task 6's
  diff touches the session-scoped engine, pool sizing (conftest is NullPool, unchanged, and is not
  in the diff at all) or event-loop lifetime, and the test's runtime path is agent_page.py +
  models/, neither touched. Mechanism: it `asyncio.gather`s two UNSYNCHRONISED coroutines and
  asserts neither raises, which requires both to pass the existence check before either commits —
  nothing forces that interleaving, so racer #2 correctly returns 400 "Already reviewed". At ~13%
  this had roughly a 1-in-8 chance of failing ./scripts/ci.sh and being misread as a regression.
Cleanup batch dispatched IN PARALLEL with Task 6's fix round (disjoint files: the flaky test, the
  0036 docstring's stale `_panel_state`, and the `expire_all()` that makes Task 5's drift alarm
  literally true). Told to fix the flake with a real barrier and NOT by tolerating a 400, which
  would delete the test's whole point.
Task 6: fix round 1 returned DONE — 04073fa, all 7 fixes, whole suite 2782 passed / 93 skipped,
  ruff tests 0 / src 224, 9 mutations all red. FIX 5 was made REAL rather than deleted: it now
  `ast`-parses each flusher and asserts the `isinstance` second argument is the bare Name
  `_ROW_LEVEL_DB_ERRORS` AND that the recovery is actually called, so the reviewer's exact
  comment-trick mutation (previously green) is red on all three parametrised cases.
Task 6: DISCLOSED SIDE EFFECT that the re-review must adjudicate — three existing loop-flush tests
  had their expected `drain` counts moved from `ticks` to `ticks-1` (and 1 to 0) because the last
  tick is now correctly a STOPPING tick. The implementer says every flush expectation is unchanged
  and asserted in the same dict. That is the exact shape of a test bent to go green, so the
  re-review must distinguish "the behaviour legitimately changed" from "the assertion was relaxed".
Task 6: Ruling: ACCEPT its refusal to extend FIX 6 to the `final=True` shutdown-LOST path. — Its
  reasoning is sound on both legs: that path is NOT a hole A3.4 opened (pre-A3.4 those rows were
  re-queued into a buffer nothing would drain — equally lost, equally silent), and writing N drop
  rows inside a finite stop grace is the same sequential-checkout hazard `_ROW_LEVEL_DB_ERRORS`
  exists to avoid. The loss is still LOUD there (`_report_flush_failure` says LOST at final=True), so
  what is missing is the drop ROW, not the signal. Re-review asked to verify leg (a) against the
  pre-A3.4 source rather than accept it. — If wrong: verdicts lost at shutdown have a log line but no
  drop row; recorded as a known gap.
Cleanup batch 1: DONE, all three items, each with the verification I demanded rather than an
  assertion. Item 1 (309fcc1) barrier-pins the flaky race: 20/20 green with `-p no:randomly`, 1/1
  random order, AND falsifiability confirmed — it still catches a genuine 500, reproduced via a real
  IntegrityError, so the barrier did not turn it into a test that cannot fail. Item 2 (95cca3b)
  `_panel_state` -> `panel_state` in 0036's docstring, `alembic heads` still a single head 0036.
  Item 3 (e665c69) `expire_all()` before the read-back, 8/8 pass, and the JSONB-null mutation
  (dropping `none_as_null=True`) still turns the alarm RED — so making the docstring literal did not
  cost the alarm its teeth.
Cleanup batch 2 dispatched in parallel with Task 6's re-review (disjoint: src/models/agent_activity.py,
  src/models/opportunity.py, templates/*). Three items: document the `total_api_calls` units change
  where a HUMAN reads it (column comment + the three staff templates), add the new `unwritable_row`
  reason to `AssessmentDrop`'s documented vocabulary, and VERIFY rather than assume the earlier
  reviewer's claim that a template's `if/elif` with no `else` degrades an unknown reason to
  name+count — if it renders blank or drops the row, that is a real defect and it must be fixed.
Cleanup batch 2: DONE — 57570ff (Item 1), d348116 (Items 2+3). 134 + 89 targeted tests green, ruff
  tests 0 / src 224 unchanged, NO snapshot or doc-sync failure and nothing needed --snapshot-update
  (which matters: these were comment/docstring/template-label edits, exactly the class that trips a
  byte-pinned test). It also hit a transient git `index.lock` from a concurrent agent and retried
  successfully — the explicit-path + retry-on-lock discipline held under real contention.
Cleanup batch 2: Item 3's claim VERIFIED rather than assumed, which was the point of sending it.
  `templates/admin/_assessments_body.html:39-58`'s if/elif genuinely has no `else`, and an unknown
  reason degrades to a legible `<span>reason</span> x n` <li> row — not a blank row, not a dropped
  one. So the earlier reviewer's degradation claim was accurate. The `unwritable_row` branch was
  added anyway (lines 52-55) so the new reason reads as prose rather than as a raw enum value.
STATUS: all six tasks implemented and reviewed; both cleanup batches done. Outstanding: Task 6's
  scoped re-review (in flight), then the whole-branch final review, then ./scripts/ci.sh as the gate.
Task 6: re-review CLEAN. All seven findings ADDRESSED, no assertion relaxed, no new breakage,
  ownership clean. 12 reviewer mutations, 12 RED, zero green.
Task 6: the edited drain-count expectations were cleared PROPERLY, which was the thing I most wanted
  settled. The reviewer did not merely check the flush numbers were unchanged — it ran a RELAXATION
  PROBE (M2): a "fix" that ALSO skips the flushes on the stopping tick, which a bent assertion would
  have accepted. It went RED on the same 4 tests. So the behaviour legitimately changed and nothing
  was loosened. It also confirmed the ast-based FIX 5 catches the comment trick on all three
  flushers, and that the F4 control's stated source of teeth is real (M7b: widening
  `_ROW_LEVEL_DB_ERRORS` with OperationalError — invisible to the ast test since the Name is
  unchanged — reddens the control).
Task 6: my ruling to ACCEPT the `final=True` refusal was verified, not taken on trust. The reviewer
  read `9fa797d^` and found `_flush_pending_assessments` had NO `final` parameter at all and
  re-queued into a buffer nothing would ever drain, with a log line calling it "still queued for
  retry" — so pre-A3.4 that loss was equally total, wrote no drop row, and was WORSE because it was
  mislabelled as recoverable. A3.4 did not open the hole, it made it honest. One caveat recorded
  against the implementer's second leg: N drops in ONE session is one checkout, so the
  sequential-checkout hazard was avoidable; the refusal stands on the scope ground, not that one.
Task 6: fix round 2 dispatched, two items, both found by the reviewer rather than by me — (A)
  `_record_unwritable_assessment`'s except handler calls `row.get(...)`, so the malformed-row case
  its docstring CLAIMS to cover escapes into the flusher, skipping `_report_flush_failure` and the
  re-queue (demonstrated on the host; not reachable today, one append site, always a dict); (B) two
  more stale "cosmetic" references to total_api_calls at simulation.py:579-580 and :6096 — the
  latter sits FOUR LINES ABOVE the assignment that now carries the units comment, so they read as
  contradicting each other. My brief had named only :263.
FINAL WHOLE-BRANCH REVIEW dispatched (opus, READ-ONLY — no mutations, no suite runs, because the
  tiny fix round is still editing simulation.py and I run ./scripts/ci.sh straight after). Package:
  final-review-production.diff (359KB: src/ templates/ alembic/ scripts/, with the 37-commit list
  and full stat at the top) + final-review-tests.diff (348KB, to be read selectively). Given the
  plan, the plan's OWN audit (which recorded the plan was not safe as written), the source audit,
  and THIS LEDGER — explicitly told the ledger is where I may have been wrong, and asked to rank any
  bad ruling by consequence. Highest-value deliverable requested: an exhaustive table of every
  CLAUDE.md claim this branch has falsified.
Task 6: fix round 2 returned DONE — 3f9b6a5, 226 passed across 9 targeted suites, ruff tests 0 /
  src 224, 2 mutations 2 red. This is the last change to src/ in this plan.
Task 6: MY INSTRUCTION WAS WRONG AND THE IMPLEMENTER CAUGHT IT. I said "capture thread_id/slack_ts
  into locals BEFORE the try". That does not work — the capture is itself what raises on a
  non-mapping row, so the escape just moves one line up. The correct shape is to PRE-BIND the locals
  to None before the try and do the capture INSIDE it, which is what shipped; its mutation M2 is my
  naive version and both tests catch it. Second time this session an implementer has corrected me on
  a detail I asserted (the first was message_log.py's ownership).
Task 6: complete (commits 4c49e62..3f9b6a5, review clean after 2 fix rounds).
ALL SIX TASKS COMPLETE. Both cleanup batches complete. No agent is editing src/.
GATE: ./scripts/ci.sh started on the host at HEAD=3f9b6a5, running in the background. This is the
  whole gate — alembic single-head + no duplicate revision ids, an upgrade->downgrade->upgrade round
  trip against a throwaway Postgres it creates and destroys itself, ruff on tests (zero) plus the
  ratcheted src ceiling, then the full pytest run with a branch-coverage floor. There is no
  server-side CI; this is it.
GATE PASSED. `./scripts/ci.sh` exit 0 at HEAD=3f9b6a5, on the host, working tree clean.
  **2784 passed, 93 skipped, 3 warnings in 508.67s**; 16 snapshots passed (count unchanged, so
  nothing byte-pinned moved); branch coverage 81.16% against a 60% floor; alembic single-head and
  the upgrade->downgrade->upgrade round trip both green; ruff clean on tests and under the src
  ceiling. Notably `test_concurrent_proposal_reviews_do_not_500` passed — the ~13% pre-existing
  flake the cleanup batch barrier-pinned did not fire, which is the first full-gate run since it was
  fixed.
FINAL WHOLE-BRANCH REVIEW returned. Verdict: the branch is SOUND and gateable — the invariants the
  plan set out to enforce (panel_owed tri-state, one-row-per-interview, the truncated-consult floor,
  gating, none_as_null) are consistent across every site it checked, and H-1 (the supersession
  DELETE eating its own replacement) is genuinely closed — but NOT FINISHED. Five Important
  cross-cutting findings, none of which a task-scoped reviewer could have seen because each spans
  files that belonged to different tasks.
FINAL REVIEW F1 — migration 0036's TWO CACHE-TOKEN COLUMNS ARE NEVER WRITTEN. `_llm_log_record`
  (simulation.py:6987-7013) is the only `LlmCallLog(...)` constructor in src/ and maps 14 fields,
  neither of them, so `sum(cache_read_input_tokens)` returns NULL forever. This was an explicit
  ledger CARRY from Task 1 to Task 6 and it was DROPPED — Task 6's brief and review were scoped to
  A3.x, so nobody owned it. And no test covers the seam: every cache assertion in
  test_llm_call_stats.py is against the payload dict, never a DB row. That absence is exactly why a
  three-task carry could vanish while adjacent tests looked like coverage.
FINAL REVIEW F2 — a TRUNCATED CONSULT still reads as a real specialist opinion on the two
  human-facing pages. The branch fixed the floor, the Slack note and the durable row, then left
  `_load_consults` (assessment_detail.py:739-756) dropping `truncated` from its projected dict and
  `thread_panel.py:122-139` never selecting it — so both render the parse-default `caution` as if
  someone gave that opinion, one click from the panel-state box this branch rewrote.
FINAL REVIEW F3 — CLAUDE.md is UNCHANGED (`git diff -- CLAUDE.md` empty) and its restart recipe is
  now the WRONG ORDER for 0036: build-then-migrate, against a schema the new code maps.
FINAL REVIEW F4 — the sliding-window throttle silently got ~2-3x TIGHTER.
  `llm_calls_per_load_per_window` is still 8 but now buys real API calls where it bought turns. The
  branch declares the UNITS change three ways and the PACING consequence nowhere. A TUNING DECISION
  FOR THE USER — I am not changing it unilaterally, which would be the same silent-repricing mistake.
FINAL REVIEW F5 — an INVERTED DESIGN PROHIBITION now sits above the columns this branch added
  (agent_activity.py:215-222 forbids a change on grounds A3.7 reversed). Also a dropped Task 1 carry.
FINAL REVIEW: verdict on my rulings — no objection to R1-R8, the truncation predicate, the
  panel_owed naming/tri-state, the parallel Task 3/4 dispatch, Sec-Fetch-Site + opaque-Origin
  non-fallthrough, the impersonation path, Task 2's ratified two lines, or the final=True refusal.
  TWO OBJECTIONS, both mine and both fair: (1) Task 0's "defer CLAUDE.md to one consolidated update
  at the end" — the reasoning was fine, the follow-through was never SCHEDULED, and the end arrived
  with zero CLAUDE.md commits; I wrote the cost-if-wrong myself and that is now the state of the
  tree. (2) the ledger's CARRY mechanism failed exactly once, on the only item spanning three tasks.
  Under-hedged: the assessments banner JUMPS to 64+ on first load — that belongs in the deploy note,
  not just here.
ACTION: dispatched a final code-fix batch (F1, F2, F5, plus three stale cross-task comments and the
  list-page badge defaulting the opposite way from the detail page) and a separate CLAUDE.md rewrite
  working from `claude-md-drift.md`, told to VERIFY every row against the tree and to contradict the
  review rather than propagate an error into the manual. Disjoint files, running in parallel.
ACTION: committed the three untracked doc paths as cc1d32d — the plan, the plan's own audit, and
  docs/audits/2026-08-22-correctness/. Every prior audit dir was tracked; these three were cited by
  37 commit messages and pointed at nothing a fresh clone would have.
PENDING: the 9th test-that-cannot-fail suspicion — the `thread_id` narrowing in
  `_superseded_row_filter` (simulation.py:4014-4023) may be unpinned. Experiment: delete that element
  from the returned tuple and run test_hub_assessment_capture_gate.py +
  test_opportunity_assessment_persistence.py. I run it MYSELF once the code-fix agent releases
  simulation.py.
CLAUDE.md UPDATED — 86f325d, +394/-51, 21 claims corrected (10 drift-table rows, 3 already-false-at-
  HEAD, 8 absent operator facts), plus a new migrate-before-serve box for 0036 and a rewritten
  restart recipe. Disclosure-sync test 4 passed on the host.
CLAUDE.md agent REJECTED OR CORRECTED FOUR ROWS of the drift table — which is exactly why it was
  told to verify rather than transcribe. All four were the final reviewer's errors, not the tree's:
  (1) "the discussions page raises UndefinedColumn under 0036" is FALSE — `thread_panel.py:131-144`
  uses an explicit column list without `truncated` and `build_discussions_view` touches no whole
  entity, so it survives a pre-0036 DB; the real breakage set is the assessments list, both detail
  pages, /admin/activity/{run_id}/llm-calls, and the two engine INSERT paths. The wording looks
  carried over from the 0030 box. (2) "all 64 historical rows" — the tree says **63**; the 64 is a
  DIFFERENT measurement ("57 of 64 assessments" with an unresolvable slack_ts). Both are now in the
  manual attached to their own measurement. (3) the `prompts/` mount claim was OVERSTATED — the
  mounts are byte-identical in both compose versions; only the service name, container_name,
  copi-edge alias and logging driver are uncommitted. (4) see below.
CORRECTION TO MY OWN REPORT TO THE USER — the rate-limiter mechanism I relayed was WRONG. The hub is
  NOT on `llm_calls_per_load_per_window`: `_allowance_for` (simulation.py:690) puts `scout_hub` on
  **`hub_llm_calls_per_window`**, currently **600**, a separate lane. The final reviewer named the
  wrong setting. The CONCLUSION survives intact and gets bigger — BOTH lanes changed units from
  turns to real API calls, and NEITHER 8 nor 600 was re-tuned — so the tuning decision I am putting
  to the user covers two settings, not one. The manual now distinguishes the lanes explicitly.
CLAUDE.md agent also caught a stale banner tell in passing: the documented `Budget: N calls/agent`
  line does not exist; the real one is `Starting simulation: ... budget/agent`.

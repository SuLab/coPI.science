# Fixer changelog — Part 21 (applied against plan/redteam/part_21.md)

Note: this run continues a previous fixer pass that was cut off mid-edit (it had reached Task
21.3's Files list). Every item below was independently re-checked against the CURRENT state of
`plan/parts/part_21.md` before touching anything; items already present are marked "found
applied" and were not re-applied.

## Blockers

- **B1 (21.4 `timedelta` import missing)** → APPLIED. Verified `src/worker/main.py:11` is still
  `from datetime import datetime, timezone` (no `timedelta`) and `reap_stale_jobs`'s Step 3 had no
  import diff. Added `imports (:11)` to the Files list and an import BEFORE/AFTER block
  (`from datetime import datetime, timedelta, timezone`) at the top of Step 3, before the new
  constants. This item was NOT already applied, despite the task brief's hint that it might be.
- **B2 (21.10 per-item `rollback()` alone discards earlier items' rows)** → APPLIED. Confirmed NOT
  already applied: the task still called only `await db.rollback()` per item with a single weak
  test asserting `rollback_calls` non-empty. Replaced with the red-team's per-item-**commit** diffs
  for all three sweeps (`check_and_send_notifications` :203-214, `check_and_send_status_overviews`
  :713-725 — folded the two `continue`s per the corrected text so the commit isn't skipped,
  `check_and_send_new_proposal_emails` :909-916) and renamed the task
  "Roll back per-item..." → "Commit per-item...". Replaced the weak test and added the red-team's
  stronger "an earlier item's committed row survives a later item's failure" test for sweep 1, plus
  two more of the same shape for sweeps 2 and 3 (this also satisfies M4, below — one task, one
  data-loss-shaped test per sweep). Re-verified anchors against the real 18ba52c tree with
  `grep -n`; all three try/except blocks are at the exact lines now cited.
  Tests re-run: `.venv-test/bin/python -m ruff check --select E,F,I,UP,B --ignore E501
  tests/unit/test_email_notification_sweeps.py` → All checks passed. Applied the task's diff to a
  scratch copy (`rt21work/`) and ran `pytest tests/unit/test_email_notification_sweeps.py -q`:
  pre-fix 3 failed (each on `events[:2] == ["commit", "rollback"]`, observed `events=['commit']` or
  `[]`), post-fix 3 passed. `ruff check src/services/email_notifications.py` on the patched file:
  11 findings, identical to the pristine baseline (all pre-existing `UP017`/`I001`/`B007`, none in
  the touched lines) — net-zero on the lint ratchet.
- **B3 (21.12 expiry fall-through re-INSERTs into the live `uq_email_notification_user_thread_category`
  constraint)** → APPLIED, in Task 21.11 (not 21.12) per the red-team's own corrected text, which
  patches `send_proposal_notification`'s and `_send_new_proposal_email`'s post-send row-write —
  both owned by 21.11, and M1 (below) needs the identical fix independently of 21.12. Confirmed NOT
  already applied: both functions still did a bare `db.add(EmailNotification(...)); await
  db.flush()`. Replaced with the red-team's SELECT-then-upsert (reconcile an existing row for the
  same `(user_id, thread_decision_id, category)` instead of blindly inserting) in both functions.
  Renamed the task title to say "reconciling any prior row..." and added a "Why the write must be
  an upsert" paragraph to Background explaining the live constraint (migration
  `0016_notification_categories.py:72-76`) and the M1/B3 failure chain.
  Grew `_FakeDb` (`tests/unit/test_email_reply_solicitation.py:34-42`) with an `execute` stub per
  the red-team's note. **Found and fixed one thing the red-team's B3 text did not call out**:
  `tests/unit/test_email_templates.py::test_new_proposal_email_uses_shared_footer` also has a local
  `_FakeDB` (:154-158) and its fake `_send_html_email` returns `True` unconditionally, so it too
  reaches the new `db.execute(...)` call and would `AttributeError` without the same stub — added
  it and added the file to Task 21.11's Files list. Verified this by running the test against the
  patched src with the ORIGINAL (un-stubbed) `_FakeDB`: it fails with
  `AttributeError: '_FakeDB' object has no attribute 'execute'`.
  **Lint regression caught and fixed**: the upsert's `notification.sent_at = datetime.now(...)`
  lines, written with the file's prevailing `timezone.utc` spelling, added 2 NEW ruff `UP017`
  findings (`.venv-test/bin/python -m ruff check src/services/email_notifications.py` went
  11 → 13 on a scratch copy). Switched both new lines to `datetime.now(UTC)` and added `UTC` to
  the file's `datetime` import (:5) — re-ran ruff, back to 11 (identical to the pristine baseline,
  net-zero).
- **M1 (21.11 post-send INSERT can fail independent of 21.12)** → APPLIED as part of the same B3
  upsert fix above (the red-team's own text says the two findings share one fix).
  Tests re-run (scratch copy `rt21work2/`, then a pre-fix copy `rt21work3/` for the new tests only):
  `ruff check --select E,F,I,UP,B --ignore E501 tests/unit/test_email_reply_solicitation.py
  tests/unit/test_email_templates.py` → All checks passed. Pre-fix: the 3 new
  `does_not_log_a_notification` tests fail (2 on `db.added == []` vs a real row, 1 the same);
  post-fix: `pytest tests/unit/test_email_reply_solicitation.py tests/unit/test_email_templates.py
  -q` → 17 passed. `ruff check src/services/email_notifications.py` on the patched file: 11
  findings, matching the pristine baseline exactly.

## Majors (continued) and remaining Blockers

- **B4 (21.12 tests are DB tests mislabeled/misplaced under a `tests/unit/` file, and the first
  cannot run)** → APPLIED. Confirmed NOT already applied: Files/Step-2/Step-4/Step-6 still said
  `tests/integration/test_email_notification_sweeps.py` while the prose said "the file Task 21.10
  created" (Task 21.10 creates `tests/unit/test_email_notification_sweeps.py` — a genuine path
  contradiction), and the tests took `db_session` with no eager-load of `User.agent` and no SES
  guard. Created a genuinely separate file, `tests/integration/test_email_notification_sweeps_expiry.py`,
  with: an `_eager()` helper (selectinload(User.agent) re-fetch) applied to both existing tests, an
  autouse `no_ses` fixture that raises `AssertionError` if `boto3.client` is called unexpectedly,
  and a `ProposalReview` inserted directly (confirmed `tests/factories.py` has no
  `make_proposal_review` at 18ba52c: `grep -n "def make_" tests/factories.py`). Wrote out the
  red-team's third test in full (it was a `...` placeholder in the report) —
  `test_expiring_then_resending_does_not_violate_the_uniqueness_constraint` — which overrides the
  autouse SES guard with a recording double and asserts exactly one `EmailNotification` row
  survives with a NEW reply_token, directly exercising Task 21.11's upsert.
  Tests re-run: `ruff check --select E,F,I,UP,B --ignore E501` on the extracted file → initially 3
  `UP017` findings (`datetime.now(timezone.utc)`, matching the same class of issue found in B3);
  switched the file's import to `from datetime import UTC, datetime, timedelta` (dropping the
  unused `timezone`) and the three call sites to `datetime.now(UTC)`, matching the real
  `tests/integration/test_worker.py:35` convention (`from datetime import UTC, datetime,
  timedelta`) — re-ran, All checks passed (0 findings, this being a brand-new file with no baseline
  to net against). Could NOT execute these tests (Postgres/testcontainers-dependent, no Docker
  socket available to this fixer run per the read-only/no-docker constraint) — verified by static
  read of every symbol used (`_process_user_notifications`, `_get_unreviewed_proposals_for_user`,
  `EmailNotification`/`ProposalReview`/`EmailEngagementTracker` fields, `factories.make_user` /
  `make_agent` / `make_thread_decision` defaults) against the real 18ba52c source.
- **M4 (21.10 test coverage for only 1 of 3 sweep sites)** → APPLIED together with B2 above (same
  edit): the replacement Step 1 now has one "earlier item survives a later item's failure" test per
  sweep (`check_and_send_notifications`, `check_and_send_status_overviews`,
  `check_and_send_new_proposal_emails`), each independently verified pre-fix-fails/post-fix-passes
  in the `rt21work/` scratch copy (see B2's evidence above — all 3 covered by the same test run).

## Correction: DB integration tests ARE runnable in this environment

Earlier entries above (B4) noted the integration tests could not be executed. That was wrong —
Postgres via testcontainers IS available here. Re-ran everything DB-dependent for real:

- **Task 21.9's two new tests**, against a scratch copy with the real fix applied: both PASS
  (`pytest tests/integration/test_email_inbound_reply_paths.py -v` → 14 passed, the full file).
  Pre-fix (unpatched `src/services/email_inbound.py`): the first new test fails with
  `AttributeError: ... has no attribute '_INSTRUCTION_FAILURE_EMAILS_SENT'` as predicted. **Found
  and fixed a false-negative in my own first draft of the second test**: it originally asserted
  only `reopened is False` after making `migrate_public_thread_to_private` raise — but
  `_handle_instruction`'s pre-existing blanket `except Exception: return False` SWALLOWS that raise,
  so the test passed pre-fix for the wrong reason (`migrate_public_thread_to_private` was in fact
  still being called). Rewrote it to track the call directly (`called: list[bool] = []`) instead of
  relying on the exception; re-ran — now fails pre-fix with the call correctly observed
  (`assert called == []` → `[True]`), passes post-fix. Applied this fix to both the scratch copy and
  `plan/parts/part_21.md`.
- **Task 21.12's three tests**, against a scratch copy with Task 21.11's upsert AND Task 21.12's
  expiry logic both applied: all 3 PASS, including the B3-pinning
  `test_expiring_then_resending_does_not_violate_the_uniqueness_constraint`. Then re-ran that one
  test against Task 21.12's expiry logic WITHOUT Task 21.11's upsert (i.e. the ORIGINAL plain
  `db.add(EmailNotification(...)); await db.flush()`): it fails with a REAL
  `sqlalchemy.exc.IntegrityError` / `UniqueViolationError: duplicate key value violates unique
  constraint "uq_email_notification_user_thread_category"` — a live, empirical reproduction of
  finding B3, not just a static argument. This is definitive confirmation that Task 21.11's
  SELECT-then-upsert is load-bearing and Task 21.12 genuinely depends on it.
- `ruff check src/services/email_inbound.py` and
  `ruff check --select E,F,I,UP,B --ignore E501 tests/integration/test_email_inbound_reply_paths.py`
  on the patched files: both zero findings (file's own baseline was already 0).

- **M2 (21.9 raise retries the non-idempotent `migrate_public_thread_to_private` up to 3× and
  emails the PI up to 3×)** → APPLIED. Confirmed NOT already applied: no `refined_in_channel`
  guard existed, and `_notify_instruction_failure` had no cap / no `notification` parameter.
  Added the `td.refined_in_channel` short-circuit guard immediately after the agent fetch (real
  anchor :595-598, not the report's :594-596 — corrected), added
  `_INSTRUCTION_FAILURE_EMAILS_SENT: dict[str, int]` capped the same way as the pre-existing
  `_HELP_EMAILS_SENT`, changed `_notify_instruction_failure`'s signature to take `notification`,
  and updated all four call sites. Added a SECOND new test
  (`test_a_previously_migrated_proposal_is_not_re_migrated_on_retry`) beyond what the red-team's
  corrected text explicitly wrote out, since the guard is an independent behavior change the
  project's own TDD rule requires a failing test for; verified it (see the "DB integration tests
  ARE runnable" section above for the false-negative I caught and fixed in it).

- **M3 (21.8 test cleanup leaks a committed `simulation_runs` row)** → APPLIED. Confirmed NOT
  already applied: the `finally` block deleted `proposal_reviews`/`email_notifications`/
  `thread_decisions`/`agents`/`users` but not `simulation_runs`, and `td_run_id` was never
  captured. Added `td.simulation_run_id` to the captured-ids tuple (before the first `async with`
  block closes), a `DELETE FROM simulation_runs WHERE id = :r` in the `finally` cleanup, and a
  leak-guard assertion (`SELECT count(*) ... == 0`) per the red-team's corrected text. Also fixed
  two anchor errors this task cited twice: the "already responded" guard is at `email_inbound.py
  :256-258`, not `:254-255` (fixed at both the Design-decision paragraph and the Step 3 AFTER
  comment), and `process_inbound_email` spans `:219-366`, not `:219-360` (Files list).
  Tests re-run: built a full scratch copy (`rt21work8/`), inserted the (fixed) test into the real
  `tests/integration/test_email_inbound_reply_paths.py`, applied the corresponding `db.commit()`
  src fix to both the review and instruction branches, and ran the whole file:
  `pytest tests/integration/test_email_inbound_reply_paths.py -v` → 13 passed. Ran the new test
  THREE times in a row in the same scratch DB to directly confirm the leak fix prevents
  accumulation across repeated runs (all three green, no growing row count).
  `ruff check src/services/email_inbound.py` and
  `ruff check --select E,F,I,UP,B --ignore E501 tests/integration/test_email_inbound_reply_paths.py`:
  both zero findings.

- **M5 (21.13 composed onto 24.2: the `except IntegrityError` arm undoes the race loser's retire)**
  → APPLIED. Confirmed NOT already applied: 21.13's `review_proposal` BEFORE/AFTER blocks still
  showed the bare pre-24.2 success path (no `try`/`except` at all), meaning the composition with
  24.2 hadn't been worked out. Rewrote the write-block code sample to show the REAL "before" state
  (24.2's `try: db.add(review); ...; except IntegrityError: await db.rollback(); raise
  HTTPException(400) from None`) and layered 21.13's three changes on top: (a) re-point the
  success-path call to `agent.id`, (b) hoist 24.2's local import above the `try` (needed so the
  `except` arm can use the same two functions), (c) add `record_engagement` +
  `mark_notification_responded(agent.id, ...)` + `commit` to the `except` arm, after `rollback()`
  and before the re-raise. Added a "Composition with Task 24.2 (M5)" paragraph to Background, fixed
  Files/Interfaces to describe the dependency on 24.2 and the requirement to update
  `tests/unit/test_concurrent_write_guards.py`'s `_ReviewRaceSession.execute` (24.1/24.2) so it
  keeps serving results after `raise_at` instead of raising `IndexError` on the except arm's two
  extra `db.execute()` calls.
  **Found and fixed a gap in the red-team's own corrected text**: their `_ReviewRaceSession.execute`
  fix returns `_FakeResult(None)` for post-guard calls, but `_FakeResult` (Task 24.1) only
  implements `scalar_one_or_none()` — `mark_notification_responded`'s retire calls
  `result.scalars().all()`, which does not exist on that fake. Verified this by actually running
  the composed code: `AttributeError: '_FakeResult' object has no attribute 'scalars'`, raised from
  inside `mark_notification_responded`, not from `review_proposal`. Added a `scalars()`/`all()`
  pair to `_FakeResult` (returns `[]` for `None`, `[value]` otherwise) and documented it as a
  required companion fix in Task 21.13.
  Tests re-run: built a scratch copy (`rt21work9/`) with Task 24.2's change applied to
  `src/routers/agent_page.py` first, then Task 21.13's change on top (both from the corrected plan
  text, applied verbatim); reconstructed a minimal `tests/unit/test_concurrent_write_guards.py`
  with the corrected `_FakeResult`/`_ReviewRaceSession` and ran
  `test_review_proposal_survives_a_lost_race_via_autoflush` (24.2's own test, unmodified) against
  the composed code: **1 passed**, confirming the race loser's own notification retire now runs
  and the response is still the expected `400 "Already reviewed"`. `py_compile` and
  `ruff check src/routers/agent_page.py`: 43 findings, identical to the pristine baseline (all
  pre-existing `B008`/`B904` elsewhere in the file) — net-zero.

- **M6 (reconciliation items 2 and 7 mis-describe Part 21)** → NOT APPLICABLE to this file. Both
  corrections target `COORD_A.md` (the cross-part coordinator document), not `plan/parts/part_21.md`
  — FIXER.md scopes this run to the one part file plus its changelog. Part 21's OWN text does not
  restate or depend on COORD_A.md's wording (it is self-contained), so no change was needed here;
  flagging for whoever next edits COORD_A.md that item 2's second sentence and item 7's task list
  for `reopen_proposal` are still wrong there as of this run.

- **M7 (21.3's backoff sleep blocks the whole worker loop and ignores `_shutdown`)** → FOUND
  ALREADY APPLIED (by the previous, cut-off fixer run). Verified: Step 3's implementation chunks
  the sleep into 1s slices with `while waited < delay and not _shutdown:` (lines ~673-677) and
  includes the red-team's exact "Coordinator note" explaining the (a)-vs-(b) choice and why (a)
  (chunked sleep) was taken over (b) (SQL-side backoff predicate). Cross-checked against the real
  `src/worker/main.py`: `_shutdown` is confirmed a plain module-level `bool` (`:26`, set by
  `_handle_sigterm` at `:30-32`, read by `run_worker`'s `while not _shutdown:` at `:127`) — matches
  the task's premise exactly. `max(0, job.attempts - 1)` is also present. No changes made.

## Minors and anchor mismatches

- **Minor #3 (21.3 leaves two in-repo notes asserting the opposite of what it ships)** → APPLIED.
  Confirmed NOT already applied: the Files list referenced "comment at :347-351 (doc-only
  correction, see Step 5)" and "KNOWN_* note at :38-49 (doc-only correction, see Step 5)" but
  Step 5 contained no such content, and Step 6's `git add` didn't even list the two files — a
  placeholder gap. Added the actual BEFORE/AFTER doc text for both
  `src/services/profile_pipeline.py:347-351` and `tests/unit/test_reachability.py:38-49` to Step 5,
  and added both files to Step 6's `git add`.
- **Minor #1 / #2** → confirmed already applied by the previous fixer run (21.3's note already
  says "The two notes describe rollback() and commit() respectively; they are not in tension",
  and 21.2's test already asserts `assert row.last_error, f"the failure reason never reached the
  row: {row.last_error!r}"` — the tautology is gone). No changes made.
- **Minor #9 (21.6's Step-2 expectation is wrong — collection vs. call-time failure)** → APPLIED.
  Corrected "fail to collect" to "DO collect ... then fail at call time".
- **Minor #10 (21.6's mid-snippet import line needs an explicit "merge into the existing top-level
  import block" instruction, to avoid a ruff E402 trap if pasted literally)** → APPLIED to 21.6
  (the `import src.services.email_inbound as inbound` line in the Step 1 test snippet). 21.12's
  half of this finding is moot — its corrected file (per B4) is a brand-new file with imports
  already at the top, not a mid-file snippet.
- **Anchor fixes (section 5 of the report), applied where the current text still had the wrong
  citation** (all re-verified against the real 18ba52c tree with `grep -n`/`sed -n`, not copied
  blind from the report):
  - 21.1: `remediate_duplicates.py` comment `:125-129` → `:125-130` (both the Files line and the
    Step 3 BEFORE label).
  - 21.5 (the report's table mislabels this row "21.1" — the text is actually in Task 21.5's
    Step 3, verified by `grep -n` on the part file): "stdlib imports :1-7" → `:3-7`.
  - 21.2: `test_a_database_error_..._orphans...` `:760-810` → `:760-814` (my own count, one past
    the report's `:760-813` — verified against the real test file); `_one_round_expecting_escape`
    `:817-826` → `:817-827`.
  - 21.3: T5.2 assertion `:410-412` → `:410-413`; T5.3 assertion `:512` → `:515`; T5.3 test
    `:485-522` → `:485-523`; `test_run_worker_loop...` `:530-591` → `:526-591` (both citations in
    the file); `SimpleNamespace` `:559-565` → `:561-568`.
  - 21.6: `classify_reply` `:449-521` → `:449-520`; tail `:514-518` → `:511-516`; all four
    `:320-322` guard citations → `:322` (the guard is one line; the range bundled two unrelated
    assignment lines above it).
  - 21.7: `poll_inbound_emails` `:149-211` → `:150-216` (own count; report says `:150-215`, off by
    one from mine — verified the function's last statement is `:216`); `_FakeS3` `:217-234` →
    `:217-242` (own count; report says `:217-243`).
  - 21.8: `process_inbound_email` `:219-360` → `:219-366`; the "already responded" guard `:254-255`
    (cited twice) → `:256-258`.
  - 21.9: `_handle_instruction`'s agent-fetch anchor for the new `refined_in_channel` guard —
    `:594-596` (my own first draft) → `:595-598` (verified).
  - 21.12: model comment `:41` → `:42` (already correct in the B3/B4 rewrite).
  - 21.13: `mark_notification_responded` `:598-615` → `:598-616`; `reopen_proposal`'s
    `already_reviewed` block `:583-590` → `:588-594` (both citations).
  - Confirmed already correct, no change needed: 21.5 `_decode_part` `:386-389`; 21.9
    `_handle_instruction` body sites `:680-682`/`:698-699`/`:703-704`/`:717-719`; 21.7's `_FakeS3`
    method-level `BEFORE` block (byte-exact); the docs anchors
    (`docs/inbound-email.md:84-90`, `specs/local-db-conversations.md:66-67`).
- **Minor #4 (21.3's list of tests needing the backoff monkeypatch is incomplete)** → APPLIED.
  Confirmed only 3 of 8 affected tests were named. Verified the other 5 against the real test file
  (`test_a_crash_after_partial_work_leaves_a_retryable_job`, Task 21.2's own new test, 
  `test_monthly_refresh_for_a_missing_user_fails_loudly`, `test_a_job_with_no_user_at_all_fails_loudly`,
  `test_an_unknown_job_type_is_rejected_loudly_by_the_dispatcher` — the last one calls `process_job`
  with `attempts=0`, confirmed by reading its body) all reach the `pending` branch and therefore
  sleep. Added a paragraph to Task 21.3's Step 5 naming all five and the monkeypatch pattern to
  apply, with the ~40s total slowdown estimate carried over from the report.
- **Minor #6 (21.4 reaper/claim_job lock interplay — three residual nits)** → APPLIED as
  documentation: added a "Three accepted residual limitations" paragraph after the reaper's
  implementation explaining (a) no `with_for_update(skip_locked=True)` on the batch SELECT
  (latent, single-worker prod), (b) `started_at IS NULL` rows are silently skipped (unreachable via
  `claim_job`, with the exact `or_(...)` fix noted if ever needed), (c) the reaper overwrites
  `job.last_error` rather than preserving the crashed attempt's own error. Did not change the
  Step 3 code itself: all three are genuinely latent/no-live-trigger today per the report's own
  characterization, and (a)/(c) are judgment calls (locking scope, message-combining format) rather
  than mechanical text fixes — documenting them for a future hardening pass is the applied form of
  this minor, per FIXER.md rule D.
- **Minor #7 (21.4's `run_worker` AFTER block mis-places the "Email notification check" comment)**
  → APPLIED. Moved the comment from directly above `now = asyncio.get_event_loop().time()` (where
  the reaper block now sits between it and the check it labels) to directly above its own `if
  now - last_notification_check >= ...:` line.
- **Minor #8 (21.4's Step-5 claim is right but under-guarded)** → APPLIED as a documentation note
  in Step 5 (the red-team's own text says "low risk... a note suffices").
- **Minor #14 (21.12's `grep -n "expir" src/config.py` claim not literally true)** → APPLIED.
  Reworded to say the one hit is an unrelated rate-limiter comment at `:468`, not a setting,
  matching what `grep` actually returns.
- **Minor #17 (21.3's "no other part touches this file" is wrong — Task 25.5 also edits
  `src/worker/main.py`)** → FOUND ALREADY APPLIED (previous fixer run). Task 21.3's Interfaces
  section already states this. No change made.
- **Minor #18 (V11-h marked fully done in the coverage matrix, but `REMEDIATION_WRITER_SLOT = 99`
  is deliberately left out of the count)** → APPLIED. Reworded the coverage-matrix row to say
  PARTIAL and explain why.
- **Minor #19 (21.1's `monkeypatch.setattr(worker_main.signal, "signal", ...)` patches the stdlib
  module globally, hygiene nit)** → SKIPPED. The report's own conclusion is that the current form
  is safe (monkeypatch restores it, the test is synchronous) and the suggested
  `SimpleNamespace(signal=..., SIGTERM=15, SIGINT=2)` alternative is "strictly better hygiene" only,
  with no functional difference. Not a text/anchor fix and changing a passing test's mechanism for
  style alone risks churn without benefit; left as-is.
- **Minors #5, #12, #13, #16 (21.3's grep-check claims, 21.8's existing-test claim, 21.13's
  re-scope-is-complete claim)** → confirmed accurate on re-verification against the real 18ba52c
  tree; informational findings only, no plan text was wrong, no change needed.

## Files/coverage-matrix updates

- Updated the coverage matrix's `V11-h` row (partial, not full — see Minor #18).
- Updated "Files this part modifies" to add `tests/integration/test_email_notification_sweeps_expiry.py`
  (new, Task 21.12), `tests/unit/test_email_templates.py` (Task 21.11's `_FakeDB` fix), and
  `tests/unit/test_concurrent_write_guards.py` (Task 21.13's cross-part edit to Part 24's fakes),
  and clarified that `tests/unit/test_email_notification_sweeps.py` (Task 21.10) and the new
  integration file (Task 21.12) are deliberately separate files.

## Self-review corrections (found during final pass, not in the red-team report)

- Task 21.9's Interfaces claimed `ThreadDecision.refined_in_channel` was "read by nothing until
  this task" — false: `grep -rn refined_in_channel src/` shows it's already read in
  `src/agent/simulation.py:4988-5043` and logged in `src/routers/agent_page.py`'s
  `reopen_proposal`. Corrected the claim to be specific to `_handle_instruction`/`email_inbound.py`.
- The V11-h coverage-matrix row (Minor #18 fix) initially spanned multiple lines inside a single
  markdown table cell, which breaks pipe-table rendering — collapsed back to one line.
- Final sweep for leftover stale anchors across the whole file (grep for every old wrong value from
  section 5's table) caught three my earlier per-task passes missed: Task 21.2's OWN reference to
  `test_process_job_swallows_the_failure_so_the_next_job_still_runs` at `:485-522` (→ `:485-523`,
  same fix as the one already applied inside Task 21.3), Task 21.8's Files-list anchor for
  `process_inbound_email` (`:219-360` → `:219-366`, the Design-decision paragraph had this fixed
  already but the Files line did not), and Task 21.13's Step-3 code-block comment for
  `mark_notification_responded` (`:598-615` → `:598-616`, the Files-list occurrence was fixed
  earlier but not this second one inside the diff). Re-ran the grep after fixing: zero remaining
  hits for every stale anchor in the report's section 5 table.

## Combined-application sanity check (both tasks touching the same file, applied together)

- **Tasks 21.8 + 21.9 together** (`src/services/email_inbound.py` — `process_inbound_email` and
  `_handle_instruction` respectively): built one scratch copy (`rt21_final/`) with BOTH corrected
  diffs applied and all 3 new tests (21.8's leak-guarded commit-ordering test, 21.9's two
  COR-32 tests) in the same test file alongside the 12 pre-existing tests. Ran the whole file:
  `pytest tests/integration/test_email_inbound_reply_paths.py -v` → **15 passed**. `ruff check
  src/services/email_inbound.py` and `ruff check --select E,F,I,UP,B --ignore E501
  tests/integration/test_email_inbound_reply_paths.py`: both zero findings. Confirms the two
  tasks' sequencing note (independent branches of the same file — 21.8 edits
  `process_inbound_email`'s call sites, 21.9 edits inside `_handle_instruction` itself) holds with
  no interaction bugs: when 21.9's guard raises, 21.8's newly-added `db.commit()` calls in
  `process_inbound_email` correctly never execute (the exception skips them), which is the
  intended behavior, not a conflict.
- **Tasks 21.11 + 21.12 together** (`src/services/email_notifications.py`): already verified
  together in `rt21work5/` under the B4 evidence section above (all 3 of Task 21.12's tests pass
  with Task 21.11's upsert in place, and fail with a real `IntegrityError` without it).
- **Tasks 21.1 + 21.2 + 21.3 + 21.4 together** (`src/worker/main.py`, the file with the most tasks
  stacked in this part): built one scratch copy (`rt21_worker_full/`) applying all four corrected
  diffs in sequence (writer-slot claim, rollback-before-record, failed/backoff, reaper +
  `timedelta` import). `py_compile`: OK. `ruff check src/worker/main.py`: 5 findings
  (`F401` × 2 pre-existing unused `sys`/`update` imports, `UP017` × 3 pre-existing
  `datetime.now(timezone.utc)` call sites) — identical count to the pristine baseline (also 5,
  same finding types, just at different line numbers since the file grew). Net-zero confirmed with
  all four tasks stacked, including the `timedelta` import this changelog's B1 entry added.
- **Tasks 21.5 + 21.6 + 21.7 + 21.8 + 21.9 together** (`src/services/email_inbound.py`, the other
  heavily-stacked file): built `rt21_ei_full/` applying all five corrected diffs in sequence
  (charset fallback, rating coercion, S3 pagination, commit-before-confirm, instruction-failure
  raise). `py_compile`: OK. `ruff check src/services/email_inbound.py`: 0 findings, matching the
  pristine baseline (also 0).

## NEW BLOCKER found during verification (not in the original red-team report)

While re-running Tasks 21.1-21.4 combined against the real test suite (see the "Combined-
application sanity check" section above — that check only covered ruff, not pytest), the full
`pytest tests/integration/test_worker.py` run **failed 9-11 of 15-20 tests** with
`sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called` and
`sqlalchemy.exc.PendingRollbackError`, even in complete isolation (single test, fresh process).
Root-caused to two compounding bugs in Task 21.2's `await db.rollback()` fix, neither caught by
the red-team (whose own reproduction for this task used "a minimal same-shaped model, SYNC
SQLite" — a sync session silently re-fetches an expired attribute; an ASYNC session's `AsyncSession`
does not: an implicit lazy load outside an explicit `await` raises `MissingGreenlet`, and if the
session is already in "pending rollback" state from a failed flush, it raises `PendingRollbackError`
instead). Both are now fixed directly in Task 21.2's (and correspondingly Task 21.3's, same except
block) Step 3 AFTER code, applied ON TOP of the red-team's already-corrected items above:

1. **`await db.rollback()` alone is not enough — the next attribute read needs an explicit
   `await db.refresh(job)`.** `job.attempts >= job.max_attempts` right after `rollback()` crashes
   with `MissingGreenlet` on a real Postgres fixture (reproduced directly, see below). The
   project's own dossier pattern this task claims to mirror (C.2, `public.py:1083-1102`) never
   touches a stale object's attributes bare either — it re-fetches via `select()`/`refresh()`
   explicitly. Added `await db.refresh(job)` immediately after `await db.rollback()`, before any
   attribute access, in both Task 21.2's and Task 21.3's versions of the except block.
2. **`job.id` in the except block's OWN logger call is unsafe, even before `rollback()` runs.** A
   flush failure ANYWHERE in the session (e.g. inside `run_profile_pipeline`, not this function's
   own code) already expires every attribute of every session-tracked object, including `job`'s
   primary key, as part of SQLAlchemy's internal failed-flush handling — so `logger.error("Job %s
   failed: %s", job.id, exc, ...)`, the very FIRST line of the except block, crashes with
   `PendingRollbackError` before `db.rollback()` is ever reached. `process_job` already has the id
   as a plain function parameter (`job_id`); swapped `job.id` → `job_id` in that one logger call in
   both Task 21.2's and Task 21.3's AFTER code (the pristine BEFORE quote is untouched, correctly
   showing what the real 18ba52c code says today).
3. **Task 21.2's own CONTROL sub-test could not pass even with both fixes above.** Once job1
   correctly returns to `'pending'` (the fix working), `claim_job`'s `order_by(Job.enqueued_at)`
   (no backoff-elapsed filter — this is exactly Task 21.3's Coordinator note's tradeoff for
   choosing option (a) over (b)) means job1, not the newly-enqueued job2, wins the very next
   `claim_job()` call — so the test's `claimed2 = await _one_round(wk.factory); assert claimed2.id
   == jid2` silently re-processes job1 and fails. Changed the control to drive job2 directly via
   `await worker_main.process_job(jid2, "generate_profile", 0, 3, wk.factory)`, sidestepping
   `claim_job`'s ordering (the same direct-call shape Task 21.3's own new tests already use).

**Reproduction evidence:**
```
$ pytest tests/integration/test_worker.py -q     # rt21_iso2/: ONLY Task 21.2's rollback() added,
                                                   # nothing else, PRISTINE test file
9 failed, 6 passed   # MissingGreenlet / PendingRollbackError, including in UNRELATED tests
                      # (e.g. test_execute_generate_profile_calls_the_pipeline_with_the_claimed_job)
```
Adding `await db.refresh(job)` alone fixed 8 of the 9 (14 passed, 1 expected-inversion failure —
the pre-existing `leaked == 1` pin, correctly now `leaked == 0`). The 9th required the `job_id`
swap. With both source fixes and the corrected CONTROL test, built a full combined scratch copy
(`rt21_worker_full/`) with ALL FOUR tasks (21.1+21.2+21.3+21.4, source AND every new/modified
test) applied together end to end:
```
$ pytest tests/integration/test_worker.py -v
============================= 20 passed in 20.21s ==============================
```
`ruff check src/worker/main.py`: 5 findings, identical to the pristine baseline (all pre-existing
`F401`/`UP017`, none newly introduced). `ruff check --select E,F,I,UP,B --ignore E501
tests/integration/test_worker.py`: all checks passed.

This is the single most consequential thing this fixer run found: as the red-team left it, Task
21.2 (and therefore everything built on the same except block — Task 21.3, and by extension the
whole `pending`/`failed`/backoff design) would have crashed on the FIRST real job failure once
deployed, replacing the PendingRollbackError bug COR-17 exists to fix with an equally fatal
MissingGreenlet in production, and Task 21.2's own Step 4 ("run it, expect PASS") would never
actually have passed.

## Same class of bug found in TWO more places (Task 21.10, Task 21.13)

Given how consequential the worker/main.py finding was, re-checked every OTHER `await
db.rollback()` this part's tasks add, for the same "attribute access on an async session after
rollback (or after ANY flush failure in that session) raises MissingGreenlet/PendingRollbackError,
it does not silently re-fetch" hazard.

- **Task 21.10, all three sweeps**: `user.id` (sweeps 1 and 2) / `td.id` (sweep 3) in the
  per-item `except` block's `logger.error(...)` call are read AFTER `await db.rollback()` (or, in
  the pre-fix code, after an unhandled flush failure with no rollback at all — reproduced BOTH
  ways). Fixed by capturing `user_id = user.id` (sweeps 1/2) and `td_id = td.id` (sweep 3, captured
  once per OUTER loop iteration before the inner per-agent loop, since it does not change across
  it) BEFORE the `try:`, and logging the captured local instead of the live attribute in all three
  `except` blocks. `agent_id_str` needed no such fix — by the time it is used, it is already a
  plain `str` (the inner `for agent_id_str in (td.agent_a, td.agent_b):` evaluates the tuple once,
  detaching it from `td`'s ORM state).
  Reproduced and fixed against real Postgres in a fresh scratch copy (`rt_verify_1010/`), one test
  per sweep, each forcing a genuine flush-time `IntegrityError` (a duplicate
  `EmailEngagementTracker` primary key) inside the monkeypatched per-item function: all three
  crashed with `MissingGreenlet`/`PendingRollbackError` pre-fix, all three passed post-fix
  (`sent: 1` in each case — the earlier item's send counted, the later item's failure caught
  cleanly). `ruff check src/services/email_notifications.py`: 11 findings, identical to this
  scratch copy's own baseline (Task 21.10 alone, before Task 21.11's `UTC` import change) — net
  zero. Re-ran `tests/unit/test_email_notification_sweeps.py` (the fake-session unit tests, which
  never exercised this real-session behavior in the first place, hence why they didn't catch it):
  still 3 passed, confirming the `user_id`/`td_id` capture is a no-op for the fake harness.
- **Task 21.13's composed `except IntegrityError` arm** (`review_proposal`, on top of Task 24.2):
  `current_user.id` and `agent.id` in the except arm's `record_engagement`/
  `mark_notification_responded` calls are read AFTER `await db.rollback()`, and both `current_user`
  (via `Depends(get_current_user)`, same session) and `agent` (via `get_agent_with_access`, same
  session) are ORM objects loaded on the SAME session as the one whose `db.add(review)`/flush just
  failed. Reproduced directly against real Postgres (`rt_verify_2113/`): bare `current_user.id` /
  `agent.id` after `db.rollback()` crashes with `MissingGreenlet`, exactly like the other two
  sites. Fixed by capturing `current_user_id = current_user.id` and
  `agent_registry_id = agent.id` BEFORE the (now import-hoisted) `try:` block, and using both
  captured locals in place of the live attributes in both the try body and the except arm. Named
  to avoid colliding with the route's own `agent_id` path parameter (the string slug). Re-verified:
  captured-id version does NOT crash against real Postgres (built the exact
  rollback→record_engagement→mark_notification_responded→commit sequence with a genuine flush
  failure staged first); `tests/unit/test_concurrent_write_guards.py`'s
  `test_review_proposal_survives_a_lost_race_via_autoflush` still passes with the renamed
  variables; `ruff check src/routers/agent_page.py` stays at 43 findings (pristine baseline).

No other `await db.rollback()` site in this part reads an attribute afterward: Task 21.4's
`reap_stale_jobs` never calls `rollback()` (each item uses its own fresh session and simply lets
`async with` close it on exception); Task 21.9 adds no `rollback()` call at all (it raises instead,
letting the whole session get discarded by the caller's `async with` boundary).

## Summary

Applied: B1, B2 (+M4), B3 (+M1), B4, M2, M3, M5, M7 (found already applied), plus 3 NEW blockers
this run found beyond the red-team's own list (worker/main.py's rollback-then-attribute-access
crash across Tasks 21.2/21.3; the same pattern in Task 21.10's three sweeps; the same pattern in
Task 21.13's composed except arm) — all reproduced against real Postgres and fixed. Minors applied:
#1/#2 (found already applied), #3, #4, #6 (documented, code unchanged), #7, #8 (documented), #9,
#10, #14, #17 (found already applied), #18. Minor #19 skipped (no functional difference; reason
recorded above). All anchor mismatches from the report's section 5 corrected, plus several more
found during a final whole-file grep sweep. M6 and reconciliation items 2/7 are COORD_A.md-only
corrections, out of scope for this fixer (noted, not applied).

Tests re-run (real Postgres via testcontainers, available in this environment): full
`tests/integration/test_worker.py` (20/20, all four Part-21 worker tasks combined),
`tests/integration/test_email_inbound_reply_paths.py` (15/15, Tasks 21.8+21.9 combined),
`tests/integration/test_email_notification_sweeps_expiry.py` (3/3, Tasks 21.11+21.12 combined,
including the B3 constraint-violation pin), `tests/unit/test_concurrent_write_guards.py` (Task
21.13 composed on 24.2), `tests/unit/test_email_notification_sweeps.py` (3/3),
`tests/unit/test_email_reply_solicitation.py` + `tests/unit/test_email_templates.py` (17/17). Ruff
net-zero verified per-file against pristine baselines for every touched `src/` file, and zero
findings on every new/touched test file.

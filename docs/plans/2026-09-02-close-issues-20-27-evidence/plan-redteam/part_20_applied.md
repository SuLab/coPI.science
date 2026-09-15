# Applied red-team corrections — Part 20

One line per applied/skipped item. Order: blockers, majors, anchors/minors.

## Blockers

- B1 (Task 20.4): Fixed Step 3 AFTER — the ✅-match branch now `return`s after
  `_close_thread` (was `break`, which fell through into the `:memo:`/`⏸️` checks
  below and could un-close or double-close the thread); the non-match path keeps
  `break` with a new comment. Updated the "The change:" explanatory paragraph to
  match. Applied together with B2 for this task (see below) since both touch
  Step 1's test class.
- B2 (Tasks 20.4, 20.5, 20.6, 20.15): Task 20.4 — added an autouse
  `_no_live_llm_or_disk` fixture (stubs `agent_mod.PROFILES_DIR` and
  `src.agent.simulation.generate_agent_response`) to
  `TestCheckThreadOutcomeRequiresTheMostRecentMemo`, and added the red-team's
  third regression test `test_a_reply_carrying_both_a_tick_and_a_memo_stays_closed`
  (adapted to rely on the class fixture rather than re-patching inline).
  Ruff-verified clean (see below). Tasks 20.5/20.6/20.15 pending — see continued
  entries below.
- B3 (Task 20.16): Added the third hunk to Step 3c correcting `_bot_uid_map`
  (getattr with default, walrus, `c is not None` guard) so the new
  `__init__`/roster-sync `set_bot_uid_map` wiring tolerates client doubles
  lacking `bot_user_id`. Added `tests/unit/test_roster_sync.py` and
  `tests/unit/test_transport.py` to the Files list and Step 5's neighbour run.
  Verified against a patched scratch copy of `src/`: both files pass 32/32
  post-fix (they errored at `SimulationEngine.__init__` pre-corrected-fix, per
  red-team's own `AttributeError` repro, reproduced independently here too).
- B4 (Task 20.16): Corrected Step 3b's `_extract_tagged_agent` AFTER — an
  unknown bot-name token now resolves to `None` (resolves through
  `_bot_name_to_id`, and only accepts an already-resolved-agent_id token if it
  is a real roster value), instead of falling back to the raw token via
  `.get(mentions[0], mentions[0])`. Added the regression test
  `test_an_unknown_bot_name_still_extracts_nothing` to the
  `test_message_log.py` block. Verified: 46/46 `test_message_log.py` tests
  pass against the patched fix (including this one and the two pre-existing
  new tests); also verified the fixed function no longer locks a thread to a
  phantom `"ghostbot"` agent_id.
- M5 (Task 20.16): (a) Corrected Step 3b's import block to drop `import re`
  from `src/agent/message_log.py` (confirmed via grep: line 407, the only
  other use, is the line this task deletes) and re-sorted the remaining
  imports (`from src.agent.mentions import extract_bot_mentions` into the
  local-import group) — verified ruff-clean. (b) Rewrote all four placeholder
  Step-1 test blocks with real, executable code instead of "check first"
  instructions: `test_cohort_isolation.py`'s test is now a method on
  `TestTagHygiene` using `self._eng(monkeypatch, ...)` (there is no bare `eng`
  fixture); `test_authorship_emit_gate.py`'s test uses the real `engine`
  fixture with a text/agent pairing that actually exercises the tagged-lab
  branch (the plan's original `"co-authored this with @WUBOT"` text never
  reaches that branch at all — verified both pre- and post-fix give `None` for
  it, i.e. it was a vacuous test); `test_service_bot_attribution.py`'s test
  uses the real module-level `_engine`/`_FakeClient` helpers and a new
  `_human_msg` helper. (c) Fixed the `<@U_UNKNOWN>`/`<@U_WISEMAN>` uid tokens
  in `test_mentions.py` and `test_message_log.py` to alnum-only forms
  (`<@UZZZZZZ>`, `<@UWISEMAN1>`) — found independently while verifying M5's
  fixture fixes: `extract_bot_mentions`'s uid group is `[A-Za-z0-9]+`, so an
  underscore silently drops the uid branch and the original tests would have
  passed for the wrong reason (same class of bug the red-team's own M5 note
  flagged for one of the `test_mentions.py` assertions, but it was also
  present, unflagged, in two of the plan's other new uid tests). All fixes
  verified end-to-end against a patched scratch copy of `src/agent/{message_log,
  simulation,mentions}.py`: `test_mentions.py` 7/7, `test_message_log.py`
  46/46, `TestTagHygiene` 16/16, `test_authorship_emit_gate.py` 17/17,
  `test_service_bot_attribution.py` 25/25, `test_cohort_isolation.py`+
  `test_authorship_emit_gate.py`+`test_service_bot_attribution.py`+
  `test_sweep_authorship_memories.py`+`test_simulation_logic.py` full-file run
  239 passed (5 unrelated failures are migration-hygiene tests that need
  `scripts/ci.sh`/`alembic/`, not copied into the scratch tree). Every new test
  independently re-verified to FAIL against the real, unpatched repo at
  18ba52c pre-fix. Ruff-clean (`--select E,F,I,UP,B --ignore E501`) on every
  touched test file.
- B2 (Tasks 20.5, 20.6): Added the same autouse `_no_live_llm_or_disk` fixture
  (stubs `agent_mod.PROFILES_DIR` and `generate_agent_response`) to
  `TestFinalizeMarkerIsSharedAcrossPublicAndPrivate` (20.5) and
  `TestCloseThreadIsIdempotent` (20.6), since both classes drive
  `_close_thread`. Ruff-verified clean; spot-ran both classes' bodies against
  a scratch copy (fixture active, no live calls, expected pre-fix assertion
  shapes reproduced — 2/3 tests pass without either 20.4's or 20.6's own
  simulation.py fix applied, and the one expected failure matches the task's
  own documented pre-fix Step-2 shape exactly, confirming the fixture itself
  does not mask or alter the tested behaviour).
- B5 (Task 20.14): Replaced the Step-1 assertion block with the red-team's
  corrected version (`posts` filtered by `sender_agent_id`, asserts
  `thread_ts is None` and `visibility == VISIBILITY_COLLAB_PRIVATE` instead of
  the impossible `thread_ts == "100.0"`), and rewrote Step 2's "expect FAIL"
  text to describe the real pre-fix single-entry shape. Verified independently
  end-to-end: ran the exact scenario against the real repo at 18ba52c
  (unmodified) — pre-fix result `channel=general, thread_ts=100.0,
  visibility=public`, matching the plan's claim exactly — then applied only
  Task 20.14's Step-3 diff to a scratch copy of `src/agent/simulation.py` and
  reran — post-fix result `channel=priv-chan, thread_ts=None,
  visibility=collab_private`, satisfying every corrected assertion.
- B2 (Task 20.15, second test): Added `engine._update_agent_memory = AsyncMock()`
  before the call to `_sync_proposal_reviews_from_db()`. Also corrected the
  `_Row.__init__`'s comment (m7: the ordering claim was backwards — this task
  runs AFTER 20.10, not before) to explain the real interaction: post-20.10
  the reviewed_set key changes to (thread_decision_id, agent_id), which
  incidentally empties `newly_reviewed` for this proposal too, but the
  explicit stub is what actually guarantees no live call regardless of task
  order. Verified directly against the real repo at 18ba52c (unpatched):
  `_update_agent_memory` await_count is 1 without the stub (confirming the
  live-call risk is real right now), and with the stub in place the test runs
  to its pre-fix assertion failure (`'public' == 'collab_private'`, matching
  Step 2's documented shape) with zero live calls and zero exceptions from the
  mocking itself.
- m7 (Task 20.15, first test): Converted `test_proposal_thread_poller_stamps_
  private_visibility` from a sync `def` + `asyncio.run(...)` to `async def` +
  `await`, matching the file's `asyncio_mode = "auto"` convention (cheap,
  applied).
- M6 item 2 (Task 20.15, Step 3): Restored the omitted two-line "Slack-origin
  …" comment before `slack_thread_ts=thread_id` in both the BEFORE and AFTER
  `LogEntry(` snippets for `_poll_proposal_threads_for_pi` — confirmed
  verbatim against `src/agent/simulation.py:3231-3232`.
- Ruff (found while re-verifying, not in the report): Task 20.15's second
  test's per-function import block (`import uuid as uuid_mod`, `from
  unittest.mock import AsyncMock`, then three `from src...` imports) was
  missing the blank line isort/ruff I001 requires between the stdlib group
  and the first-party group. Added it. Verified the full corrected Step-1
  block (both tests) ruff-clean (`--select E,F,I,UP,B --ignore E501`) except
  the expected `F821 SimulationEngine` (a module-level name from this test
  file's real header, absent from the isolated snippet).
- B6 (Task 20.10) — IN PROGRESS: designed and DB-free-verified the corrected
  approach (see notes below); writing into the plan text next.
- B6 (Task 20.10) — COMPLETE. Replaced the `_db_reopened_thread_ids`-seeding
  design with the red-team's corrected split: (1) added
  `self._prior_thread_accounted: set[str] = set()` to `__init__` (:329) and to
  `_close_thread` (Step 3d), (2) rewrote the "1." rebuild section to split
  `closed_thread_ids`/`reopened_thread_ids`, guard the `_prior_threads`
  dedup-append on `_prior_thread_accounted` instead of `_closed_thread_ids`,
  and restore the two omitted comments verbatim (M6 item 1: the 11-line
  non-idempotency comment and the "Carried for G3..." comment), (3) added a
  NEW "2." generic-per-agent-loop fix restoring `message_count_offset` (=
  `msg_count`) and `pi_context` (latest `sender_agent_id is None` entry) for
  a thread in `reopened_thread_ids`, (4) added a new Step 3i making the
  web-guidance reopen's synthetic `pi_entry` mint idempotent against message
  log content instead of the removed seeded flag, (5) rewrote the "why a
  migration is needed"/"noted but out of scope" prose and the migration
  docstring to match the corrected mechanism, (6) added a "Coordinator note"
  documenting the (i)/(ii) choice per FIXER instruction B, (7) rewrote the
  integration test (added a PI-authored log row, asserts
  `message_count_offset > 0` and `pi_context` equals the PI's message,
  dropped the now-invalid `_db_reopened_thread_ids` assertion), (8) added a
  brand-new DB-free unit test `TestWebGuidanceReopenMintIsIdempotentAcrossRestarts`
  covering Step 3i specifically. Also applied m2's three anchor fixes
  (`_rebuild_agent_state` :4148-4414 not :4148-4324; `decided_at`/
  `refined_in_channel` at :235-237/:232-234 not ":234-237"; the integration
  test at :159 not :157-176), m7's redundant `datetime` re-import removal, and
  m7's `expire_on_commit=False` documentation note on `_close_thread`.
  Verified end-to-end with a DB-free scratch harness (fake session_factory
  returning canned `ThreadDecision`-like rows, copied `src/` patched with the
  exact corrected diffs): a reopened thread comes back active with
  `message_count_offset=4` and `pi_context` matching the PI's message; an
  ordinary open thread (no decision) is unaffected (`offset=0`,
  `pi_context=None`); a decided-and-not-reopened thread still stays fully
  closed (`active_threads == {}`); a second rebuild does not duplicate
  `_prior_threads`; and the new idempotent-mint test goes from 2 message-log
  entries pre-fix to 1 post-fix while still restoring the `ThreadState`
  (`.venv-test/bin/python`, run from the scratch copy's own directory to
  avoid a cwd/PYTHONPATH sys.path collision with the real repo — noted for
  future scratch verifications). Ruff-clean (`--select E,F,I,UP,B --ignore
  E501`) on the new/changed test code.
- M1 (Task 20.1): Fixed `_engine_with_failing_client`'s import block — moved
  `from unittest.mock import MagicMock` above the `src.*`/`tests.*` imports
  (was I001-violating). Ruff-verified clean.
- M1 (Task 20.3): Dropped the unused `from unittest.mock import AsyncMock`
  from `_engine` (F401 — it is imported again inside each test that actually
  uses it). Ruff-verified clean.
- M2 (Task 20.9): Added Steps 3c/3d/3e documenting the one-line
  `ProposalReview.rating != -1` filters for the three silenced readers
  (`src/main.py` AgentBadgeMiddleware :88-92, `src/services/
  email_notifications.py` `_get_unreviewed_proposals_for_user` :170-176 AND
  the weekly-digest `rev_result` query :762-765 red-team M2 flagged as a
  second sub-finding in the same file, `src/routers/agent_page.py`
  `agent_dashboard` :250-253), each verified byte-exact against the real
  files at 18ba52c. Added a Coordinator note explaining these are NOT part
  of Task 20.9's own commit (Step 6 updated accordingly) because the three
  files are owned by Parts 25/21/24, whose phase-5 tasks run AFTER Part 20's
  phase-4 tasks per COORD_A's execution order — landing them early would
  have Part 20 editing out-of-ownership files before their owners' own edits
  land. Updated the Files list, Step 6, Open Decision 2, and the end-of-part
  "Files this part modifies" note accordingly.
- M3 (Task 20.18): Rewrote `_rebuild_one_agent_state`'s `LlmCallLog` read to
  mirror `_rebuild_agent_state`'s steps 4/4b exactly: an all-time
  `COUNT(*)` scoped to `simulation_run_id` for `api_call_count`, and a
  SEPARATE windowed query (`created_at >= cutoff`) for `call_times` — instead
  of one unscoped, unwindowed `sa_select(LlmCallLog.created_at).where(agent_id
  == ...)` read that would have given a re-added agent a lifetime cross-run
  `api_call_count` and pulled every historical row into Python. Added the
  `not self.simulation_run_id` guard to the early-return. Updated Step 1's
  test: 4 DB reads instead of 3 (decisions, reviewed, count-scalar,
  window-rows), added `.scalar()`/`.all()` support to the fake's `_R` class,
  set `engine.simulation_run_id` (missing before — the corrected function
  would otherwise no-op on the new guard), and pre-filtered the window-rows
  fixture to just the in-window row (a real DB query would already exclude
  the stale one; the old fake dumped both for client-side filtering).
  Verified end-to-end against a scratch copy of `src/agent/simulation.py`
  with the new method inserted verbatim (plus a stand-in `thread_decision_id`
  field on `ProposalRef`, since this task depends on Task 20.10's field) —
  all assertions pass: `pending_proposals` len 1, `api_call_count == 2`,
  `call_times` len 1, `active_threads` contains `"100.0"`, cursor restored.
  Ruff-clean.
- M4 (Task 20.19): Added the missing downstream re-check after the existing
  `blocked_for_regular` re-verification block (:2301-2306) — a new
  `if today_posts >= settings.daily_post_cap: cap_exempt = (...); if not
  cap_exempt: return` gate keyed on the action the LLM actually chose
  (funding_collab post_type, collab_private channel, or a funding/PI-priority
  reply target), matching the red-team's exact corrected text. Added a third
  test, `test_a_funding_candidate_does_not_make_the_cap_unenforceable_for_an_
  ordinary_post`. Corrected the test's originally-planned
  `fake_llm.assert_not_awaited()` assertion (wrong — the LLM call happens
  BEFORE this downstream check, so it always fires regardless) to instead
  assert the message log does not grow, which is what the fix actually
  guards. Verified end-to-end on a scratch copy of `src/agent/simulation.py`:
  with only Task 20.19's original top-level bypass (no M4 fix), the new test
  fails (6 vs 5 entries — the ordinary post lands); with M4's downstream
  re-check added, all three tests in the class pass (3 passed). Ruff-clean.
- m2 (Task 20.2): Applied both anchor fixes — Files list's
  `test_evicts_from_all_agents` reference corrected from `:141-152` to
  `:134-144`, and Step 5's `test_unknown_thread_id_is_noop` reference
  corrected from `:153-` to `:146` (both verified against
  tests/unit/test_thread_not_found.py:107-146).
- m2 (Task 20.12): RE-VERIFIED, not applied — the report's proposed
  corrections are themselves off by one. Direct `sed -n`/`cat -n` against
  the real files confirm the plan's ORIGINAL anchors were already exact:
  `_poll_inbound_from_db`'s BEFORE block is genuinely `:2894-2899` (line
  2894 is `self.message_log.append(entry)`; line 2899 is the
  `await self._handle_pi_inbound_entry(entry)` call — 6 lines, matching the
  quoted snippet exactly), and `_send_dm`'s BEFORE block is genuinely
  `:400-407` (line 400 is `client = self.slack_clients.get(agent_id)`; line
  407 is the final `logger.debug(...)` — 8 lines, matching the quoted
  snippet exactly, including the `else:`/`logger.debug` tail the report's
  `:400-406` range would cut off). Left both anchors unchanged.
- m2 (Task 20.17): Fixed the `_phase4_reply_threads` anchor from `:1322-1332`
  to `:1322-1334` (the BEFORE snippet's `return {t.thread_id for t in
  threads_to_reply}` line is real line 1334, confirmed via `cat -n`).
- m1 (Task 20.2): Applied both recommendations. (1) `purge_thread` now
  recomputes `_max_posted_at` from the surviving entries when it removes
  anything, instead of leaving `latest_timestamp` (Task 20.7's cursor source)
  stale above every remaining entry after purging the newest thread —
  verified with a standalone script: purging the newest of three entries
  dropped `latest_timestamp` from 3.0 to 2.0 as expected; without the fix it
  would have stayed at 3.0. (2) `_evict_dead_thread`'s AFTER now does
  `self._closed_thread_ids.add(thread_id)` (not just removing the old
  `.discard()`), so an evicted thread comes out CLOSED regardless of whether
  it was already closed, making the eviction durable through
  `_rebuild_agent_state`'s own closed-thread accounting — the fixture's
  pre-existing `engine._closed_thread_ids.add(dead_ts)` at test_thread_not_
  found.py:131 means Step 1's test assertion is unaffected either way.
- m3 (Task 20.7): Softened the AFTER comment's claim that "a late-arriving
  row with posted_at between the old and new cursor stays visible" — that
  only holds when the row is also the newest thing in the log at that
  moment. Added a pointer to `PI_INBOX_LOOKBACK` (confirmed real symbol,
  simulation.py:180-181) as the actual belt-and-braces for the narrower
  residual case, per the red-team's recommendation.
- m4 (Task 20.21): Moved the `thread.pi_context = None` clear from
  immediately after `build_phase4_prompt` returns (before the LLM call) to
  immediately after the `generate_with_tools` await succeeds — a transport
  error on that specific call is swallowed by Phase 4's
  `asyncio.gather(return_exceptions=True)` fan-out, so clearing before the
  call meant a failed attempt lost the PI's guidance having reached no
  prompt at all. Added a second test,
  `test_pi_context_survives_a_transport_error_on_the_llm_call`. Also fixed a
  pre-existing ruff I001 (missing blank line between the stdlib and
  first-party import groups) in the FIRST test's import block, found while
  verifying the class as a whole since I was already touching it. Verified
  all three placements directly against scratch copies of
  `src/agent/simulation.py`: with no clear at all (18ba52c unmodified),
  `pi_context` trivially survives an error; with the buggy "clear before the
  call" placement, a transport error wipes it (`None`); with the corrected
  "clear after the call succeeds" placement, a transport error preserves it
  (`"please look at X"`) while a successful-but-unparseable reply still
  clears it (`None`). Ruff-clean.
- m5 (Task 20.20): Added a code comment documenting the known latch
  trade-off — a permanently-blocked agent never clears `has_pi_directive`.
  Cheap and no-cost (Phase 5 still bails before any LLM call), but flagged
  for future consideration (turn counter / TTL) per the red-team's note.
  Text-only change, no behavior change.
- m6 (Task 20.18): Added
  `test_a_closed_thread_is_not_restored_into_active_threads` to
  `TestRebuildOneAgentState`, reusing the existing `_ordered_fake_db`
  helper. The original test's thread starts with `_closed_thread_ids`
  empty, a state production cannot reach (the startup rebuild always
  populates it first); the new test seeds a thread into
  `_closed_thread_ids` before calling `_rebuild_one_agent_state` and
  asserts it is excluded from `active_threads`. Verified against the same
  scratch copy used for M3: both tests in the class pass (2 passed).
  Ruff-clean.
- m7 (phase5_skip_probability flakiness — Tasks 20.3, 20.14): Added
  `monkeypatch.setattr(get_settings(), "phase5_skip_probability", 0.0)` to
  Task 20.3's `_engine()` helper and Task 20.14's `_engine()` helper (both
  now take `monkeypatch`; call sites updated), guarding against a developer
  `.env` with `PHASE5_SKIP_PROBABILITY` set nonzero — both classes assert
  the LLM path actually ran, which a random skip could otherwise
  intermittently prevent. Verified functionally: Task 20.3's test reaches
  its documented pre-fix failure unaffected by the patch; Task 20.14's test
  (with both the M7 hermeticity fix and B5's corrected assertions) passes
  end-to-end against a scratch copy with the real Step-3 fix applied.
  Re-examined Tasks 20.19 and 20.22, which the report also named: NOT
  applied there — verified both are actually unaffected. Task 20.22's test
  already includes a `pi_priority=True` PostRef in `interesting_posts` (the
  `has_pi_priority` check is `any(...)` over that exact list), which
  independently bypasses the random skip regardless of the setting. Task
  20.19's three tests' assertions (`assert_awaited_once`/`assert_not_
  awaited`/message-log-length-unchanged) are satisfied identically whether
  the daily cap or an unrelated random skip is what prevented the call — the
  one test that DOES require the call to happen
  (`test_a_pi_priority_candidate_bypasses_the_cap`) already carries
  `pi_priority=True` for the same reason as 20.22. Ruff-clean on both
  applied fixes.
- m7 (coverage-matrix gap, COR-1a — Task 20.1): Added a third test,
  `test_thread_not_found_also_returns_false_and_evicts_the_dead_thread`,
  reusing the same `_engine_with_failing_client` doubles already built for
  this task (just a different `error_code` and a `thread_ts`/`slack_ts`
  pair so `_slack_parent_ts` resolves non-None and the connected branch is
  actually reached). Pins COR-1a's `ThreadNotFound -> False` + eviction
  behaviour, which was already fixed at 18ba52c but had no test. Documented
  in Step 2 that this specific test does NOT fail pre-fix (it is a
  regression pin for already-correct code, not new TDD-red behavior) — added
  the pre-fix verification output (`posted: False`, `evicted: ['1.0']`,
  `entries: ['1.0']`) to make that explicit. Verified the full class
  end-to-end against the real repo at 18ba52c: 2 passed (the control and the
  new COR-1a pin), 1 expected failure (the pre-fix COR-1b test, unrelated).
  Ruff-clean.

## Summary

All 6 BLOCKERs (B1-B6), all 6 MAJORs (M1-M4, M6 items 1&2), and all MINORs
(m1-m7, including the m2 anchor table and the m7 bullet list) from
plan/redteam/part_20.md have been applied or explicitly re-verified/declined
with reasoning (m2's two Task 20.12 anchors were re-checked and found to be
false positives in the red-team's own audit — left unchanged; m7's
phase5_skip_probability note was applied to 2 of the 4 named tasks after
verifying the other 2 are not actually exposed). No items were skipped for
being out of scope/requiring redesign.

Tests re-run (all via `.venv-test/bin/python -m pytest ... -q -p no:cacheprovider`,
DB-free, from patched scratch copies of `src/` unless noted):
- Task 20.4/20.5/20.6 new/changed test snippets: ruff-clean; fixture mechanics
  spot-verified (no live calls, correct pre-fix shapes reproduced).
- Task 20.9: M2 diffs verified byte-exact against real files; `list(...)`
  hedge is a no-op behavior change (ruff-clean, ported directly).
- Task 20.10 (B6): full DB-free harness — 5 scenarios verified (reopened
  thread offset/pi_context restoration, ordinary/decided-thread regression
  checks, second-rebuild idempotency, idempotent-mint-across-restart) — all
  pass; corresponds to the two new/rewritten tests added to the plan.
- Task 20.14 (B5): pre-fix and post-fix scenarios both reproduced exactly as
  documented in the corrected Step 1/2 text.
- Task 20.15 (B2): stub verified to prevent live calls; second test's
  pre-fix `await_count == 1` reproduced directly against 18ba52c.
- Task 20.16 (B3/B4/M5): full suite re-run against a patched scratch copy —
  test_mentions.py 7/7, test_message_log.py 46/46, TestTagHygiene 16/16,
  test_authorship_emit_gate.py 17/17, test_service_bot_attribution.py 25/25,
  cross-file neighbour run 239/244 (5 unrelated migration-hygiene failures
  needing infra not copied into the scratch tree).
- Task 20.18 (M3, m6): 2/2 new tests pass against a patched scratch copy
  (with a stand-in `thread_decision_id` field standing in for Task 20.10's
  dependency).
- Task 20.19 (M4): 3/3 tests pass with the fix; the new test's failure mode
  without the fix reproduced (6 vs 5 log entries).
- Task 20.21 (m4): both placements' behavior verified directly (buggy: lost
  on error; corrected: survives error, still clears on success).
- Task 20.1 (m7/COR-1a): new regression-pin test verified end-to-end against
  the real repo (2 passed, 1 expected pre-fix failure, unrelated).
- Task 20.2 (m1): `_max_posted_at` recompute verified with a standalone
  script (3.0 -> 2.0 after purging the newest entry).
- Tasks 20.3/20.14 (m7 hermeticity): settings monkeypatch verified not to
  interfere with either test's documented behavior.

All new/changed test code verified ruff-clean under
`--select E,F,I,UP,B --ignore E501` (only expected `F821 SimulationEngine`
findings remain, an artifact of extracting class bodies without the real
file's module-level import — not a real issue).

# Task 35 — the live Slack tier, run and accounted for

## Ruling

The tier was run twice on 2026-09-08 against `copi-test` (team `T0BMVSBMEC8`) at branch commit
`bc03917`, through `scripts/run_live_slack.sh` with the `SLACK_TEST_*` variables exported from a
scratch file holding nothing else. Both runs passed the isolation preflight (all five checks OK:
production credentials present-and-empty, 0 usable of 125 configured bot tokens, all three
fixture tokens resolved by `auth.test` to `T0BMVSBMEC8`, tier environment complete, no
operator-supplied database). This is the first evidence that the preflight's accept path works,
not merely its refusal path (`task-2.md`'s stated limit).

- **Slice 1, `-m "live_slack and not real_llm"`:** 53 passed, 0 failed, 0 skipped, 408.40 s.
  `test_posting_to_an_archived_channel_does_not_crash` passed and logged the COR-1b line
  "recording it as a DB-only row and reporting the post as failed".
- **Slice 2, `-m "live_slack and real_llm"` with `ANTHROPIC_API_KEY` exported (budget 40/40/80
  calls per the module constants):** 7 passed, 1 failed, 1390.57 s. All four `test_full_run_live`
  bijection/split/SIGTERM tests passed. The failure is
  `tests/integration/test_slack_pi_live.py::test_a_standing_instruction_is_persisted_and_acknowledged`:
  the classifier routed correctly, `Agent.update_private_profile` failed with
  `[Errno 13] Permission denied: profiles/private/.su.md.<tmp>` (the host's `profiles/` is
  root-owned), the error was swallowed, and `persist_private_profile_to_db` then copied the stale
  disk file over the DB row. That is a real defect (audit RC-7 in
  `docs/plans/2026-09-08-audit-fixes.md`), and a tier prerequisite nobody had written down
  (RC-13).

The `ANTHROPIC_API_KEY` decision: exported for slice 2 only, so the four behaviours the README's
#20 carve-out 7 names are covered by real model calls, not mocks.

## Evidence

Transcripts (redacted of every `xoxb-`/`sk-ant-` value) are in the session scratchpad
`live_slack_run.log` and `live_llm_run.log`; the summary lines are quoted above verbatim. The
`scripts/backfill_slack_ts.py` mechanism was additionally exercised end to end on copi-test with a
throwaway Postgres at head: dry run `confirmed=2 db_origin=1 unverified=0`, `--apply` wrote
`slack_ts` on exactly the two real rows and left the fabricated timestamp NULL.

Re-run after the audit fixes: see the addendum below.

## Consequence a closing comment must state

#20's carve-out 7 no longer needs the "covered only with a mocked LLM" caveat. #22/#24's
private-profile write path had a live failure (RC-7); the fix and its re-run are recorded in the
addendum before any closing comment quotes this file.

## Addendum — re-runs after the 2026-09-08 audit fix wave (2026-09-10)

All runs through `scripts/run_live_slack.sh` with `COPI_PROFILES_DIR` pointed at a writable
scratch directory (preflight check 6, RC-13, passed; the host's `profiles/` is root-owned).

| tree | slice | result |
|---|---|---|
| A+B+C merged (1a13ba4) | `live_slack and not real_llm` | 53 passed / 0 failed, 7:54 |
| + D (2931aa7) | `live_slack and not real_llm` | 53 passed / 0 failed, 7:42 |
| + D (2931aa7) | `live_slack and real_llm` | 7 passed / 1 failed — `test_a_full_run_keeps_both_stores_in_bijection`: a model omitted `channel` on a new post and the engine defaulted it to `#general` → **RC-15** |
| + E + RC-15 (9e66199) | `live_slack and not real_llm` | 53 passed / 0 failed, 6:29 |
| + E + RC-15 (9e66199) | `live_slack and real_llm` | **8 passed / 0 failed**, 26:53 — the standing-instruction test that failed at bc03917 (RC-7) and the bijection test (RC-15) both pass |
| + F (63 commits, ac2b… tree) | `live_slack and not real_llm` | 53 passed / 0 failed, 10:25 |
| final (9f1ca5a, 69 commits) | `live_slack and not real_llm` | **53 passed / 0 failed**, 8:26 |

The real-LLM slice was not re-run on the final tree: the commits after 9e66199 touch the inbound-poll
attempt counter, the delete guard, the CSP endpoint, the deploy script, the profiles-dir accessors
and docs — none of the LLM-driven paths those eight tests exercise — and the slice costs ~27 minutes of
paid model calls per run.

Gate at the same commit (`./scripts/ci.sh`, 2026-09-10): 2942 passed / 120 skipped, branch coverage
80.15 %, ruff 249/260, mypy 138/150, alembic head 0030, CI passed.

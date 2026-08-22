# Fix: `--fresh` no longer re-imports Slack history (finding M2)

**Status:** implemented, `./scripts/ci.sh` green (2418 passed, 93 skipped, 0 failed).
**Not deployed** — both images must be rebuilt and the run restarted for it to take effect.

## The bug

`--fresh` wiped `agent_messages` and then immediately re-imported 914 messages across 86
threads from Slack. **916 of the run's 1,354 rows were posted before the run started**, the
oldest eight days earlier. Three of the seven hub interviews the reconcile resurrected refused
twice on their first turn and were abandoned — 6 API calls and 139,257 input tokens for no
output.

## Root cause

A split brain. `--fresh` was implemented entirely in `src/agent/main.py` (delete the rows, open
a new `SimulationRun`), while `SimulationEngine.start()` called `_rebuild_state_from_slack()`
**unconditionally**. The engine was never told the run was fresh, so the DB wipe was only one of
three ways prior state came back:

1. the startup reconcile (the 914 messages actually observed);
2. `_poll_slack_for_bot_messages`, whose `_poll_cursors` map defaults to `"0"`;
3. `agent.state.last_seen_cursor`, defaulting to `0.0`.

## Why the obvious fix is wrong

**Gating the reconcile call alone does not work.** The live poller bounds itself with a
*different* cursor map, and on a fresh run nothing populates it — `_rebuild_state_from_db` seeds
`_poll_cursors` per stored row, and a fresh run has no stored rows. The poller also dedups only
against `message_log.get_entry(ts)`, which a fresh start leaves empty. So a reconcile-only fix
just defers the identical re-ingestion to the first poll tick, where it is harder to see.

Demonstrated rather than argued — simulating the naive fix:

```
after naive restore: log = 0 cursors = {}
after first poll:    log = 2 ['pre-run message 1', 'pre-run message 2']
NAIVE FIX LEAKS
```

`test_fresh_start_poller_ignores_pre_existing_history` is the test that fails under it.

Path 3 needs no treatment, and that is worth recording: `last_seen_cursor` bounds scans over the
in-memory `MessageLog`, which a fresh start leaves empty, and every entry appended during the run
carries a timestamp far above `0.0`.

## The change

- `SimulationEngine.__init__` gains `fresh_start: bool = False`; `main.py` passes `fresh_start=fresh`.
- `start()` now calls `_restore_slack_state()`, which branches: a resumed run reconciles (that is
  how a restart recovers its own in-flight interviews), a fresh run calls
  `_seed_slack_cursors_without_ingest()`.
- That method reads the same channels and moves `_poll_cursors` to each one's newest timestamp
  **without appending anything**, so the first poll asks Slack only for messages this run produced.
  It logs how many messages it ignored.
- `_known_slack_ts` is deliberately **not** seeded — its only readers are inside
  `_rebuild_state_from_slack`, which this branch exists to skip.
- Channel history is top-level-only, so a pre-run *thread reply* can carry a ts above the cursor.
  That is safe rather than lucky: the live poller reads the same top-level-only endpoint, and the
  only code that fetches replies works from a thread the run is already tracking — of which a
  fresh start has none.

## Tests (`tests/unit/test_fresh_start_slack_restore.py`)

| test | what it pins |
|---|---|
| `test_fresh_start_does_not_ingest_pre_existing_slack_history` | the reconcile is skipped |
| `test_fresh_start_poller_ignores_pre_existing_history` | **the discriminator** — fails under the naive fix |
| `test_fresh_start_is_a_no_op_with_slack_disabled` | `NullTransport` no-ops, so a local-DB run cannot crash at startup |
| `test_a_resumed_run_still_reconciles_slack_history` | resume still works — guards the opposite regression |

`tests/fakes.py`'s `FakeSlackClient` gained an opt-in `channel_history` dict (empty by default, so
existing tests are unaffected) plus `poll_channel_messages` honouring `oldest`, and the two
`aget_*_history` methods the reconcile calls.

## Residual, not fixed here

**`--fresh` still does not clear agent working memory.** 56 `profiles/memory/*/public.md` files
survive it, so after this change the message log is fresh while memory is stale — agents will
still "remember" prior runs with nothing in the log to corroborate it. One of those files
(`klein`) ends mid-directive (*"Do not re-pitch this idea unless that specific ICC/CV study has"*)
from the H4 refusal-truncation bug, and that truncated instruction is injected into every klein
prompt. Whether `--fresh` should also reset memory is a design decision, not a bug fix.

**Resumed runs still re-attribute** any Slack-only message to the *current* `simulation_run_id`.
Much smaller in effect (the DB rebuild already holds most rows), but the same class of defect.

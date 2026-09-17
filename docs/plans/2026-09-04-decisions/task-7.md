# Task 7 — #20 COR-10(3): the inbound log append moved behind the handler

**Status: RULED BY BRANCH OWNER 2026-09-04.** Escalated by
`docs/plans/2026-09-04-close-remaining-gaps.md` Task 7; ruled before the task started. The
implementer implements this ruling and fills in `## Evidence`. It does **not** re-decide it.

## Ruling

**Chosen: option (a).** Append the inbound log entry **before** the handler runs, advance the cursor
**after** it, and add a **distinct durable handled-marker** so dedup no longer keys on the log
entry's presence.

**Fold the marker into the `0029` revision that Task 8 is landing.** The two decisions were coupled
in the plan and are taken together: Task 8 is ruled for a schema change (`thread_decisions
.pi_engaged_at`), so the "if a 0029 is happening anyway" branch of the plan's own recommendation is
the live one, and (a) costs no extra revision. **`0029` carries exactly two things: Task 8's
`thread_decisions.pi_engaged_at` and this task's handled-marker.** Task 7 must land the marker column
in Task 8's revision *before* it changes `simulation.py` (plan Task 8 Step 6, Task 7 Step 4).

Options considered and rejected:

- **(b) append before the handler and accept the original COR-10(3) loss of triggers (revert to
  D25).** Rejected: it leaves the issue's own item open, so `#20` would have to carry a stated
  carve-out, and it re-creates the defect COR-10(3) was filed for. It was recommended by the plan
  *only* on the premise that the branch owner wanted no further schema change — that premise is false
  once Task 8 is ruled for (a).
- **(c) keep HEAD's order (`c4de842`).** Rejected, as the plan already rejects it: a handler failure
  leaves the row un-appended *and* un-advanced, so the PI's message is re-scanned only while it stays
  inside `PI_INBOX_LOOKBACK_S = 300.0` and is then **lost outright**. `verify-engine.md` rates that
  worse than D25, which lost the triggers but kept the row.

This ruling supersedes **D25** and this session's `c4de842` ordering. Say so where either is cited.

## Evidence

*Filled in by Task 7's implementer, 2026-09-04. The ruling above was not re-decided.*

### The marker column, and that it landed before `simulation.py` changed

The handled-marker is **`agent_messages.pi_inbound_state`**, `varchar(16)`, nullable, no server
default, no backfill — added by `alembic/versions/0029_pi_engagement_and_inbound_state.py` in commit
`f0284f9` ("feat(migration): 0029 — thread_decisions.pi_engaged_at + agent_messages.pi_inbound_state
(#20 COR-5)"), together with `ThreadDecision.pi_engaged_at`. That commit is Task 8's, it states in
its own message that it makes **no `src/agent/simulation.py` change** because "Group A tasks 7, 9 and
10 still own it", and it is an ancestor of this task's commit — so the column existed before any
engine code read or wrote it, which is the ordering the ruling requires (plan Task 8 Step 6 → Task 7
Step 4). The model attribute Task 7 codes against is
`src/models/agent_activity.py:119` (`AgentMessage.pi_inbound_state`).

### What changed in the engine

`src/agent/simulation.py`:

- `PI_INBOUND_INGESTED` / `PI_INBOUND_HANDLED` — the only two values anything writes, with the
  three-state contract (NULL is the third and is never written) in the comment above them.
- `_poll_inbound_from_db`'s loop: dedup for a PI row now reads `r.pi_inbound_state`
  (`'handled'` → skip and advance; `'ingested'` → re-run the handler *regardless of log presence*;
  `NULL` → today's `message_log.get_entry(...)` check, unchanged). For a row it is going to process,
  the `MessageLog.append` runs **before** `_handle_pi_inbound_entry` and the cursor advance runs
  **after** it. Bot rows are untouched: `state` is forced to `None` for them, so their path is
  byte-for-byte today's.
- `_mark_pi_inbound_state(message_ts, state)` — the sole writer of the column, one short transaction
  per write so `'ingested'` is committed *before* the handler runs, keyed on
  `(simulation_run_id, message_ts)` (the table's own unique constraint). Failures are logged and
  swallowed, because `_run_main_loop` does not guard its pollers and a lost marker write costs at
  most one at-least-once retry — never the PI's text.

### Red first (plan Steps 2–3)

`tests/unit/test_simulation_logic.py::TestPollInboundFromDbGuardsTheHandler`, seven tests, run
against the pre-fix `src/` at `f0284f9`: **6 failed, 1 passed**. The load-bearing messages:

```
E AssertionError: the append is what records the PI's text durably — a handler failure
  must not be able to lose it
  assert (None is not None)
  ERROR src.agent.simulation:3389 [general] Failed to apply PI inbound side effects for 1.0: boom

E AssertionError: past the window the side effects are gone (D25's trade), but the PI's
  message itself must still be in the log — losing it outright is the defect this task fixes
  assert (None is not None)

E AssertionError: the text lands immediately, on the failing attempt
  assert None is not None
   +  where None = get_entry('1.0')

E AssertionError: assert None == 'handled'          # the happy path never marked the row
E AssertionError: Expected mock to not have been awaited. Awaited 1 times.   # a 'handled'
                                                                            # row was re-run
```

The one test that passed pre-fix is
`test_a_tagged_slack_row_already_in_the_log_does_not_rerun_the_tag_route`, and that is the point of
it: it pins the NULL fallback, which is *today's* behaviour and must not change. It is paired with
`test_a_null_row_absent_from_the_log_is_processed_exactly_as_today` (red pre-fix on the marker
assertion) so the negative cannot pass vacuously.

After: **7 passed**; whole file **155 passed**.

### The three states, and which test exercises each

| State | Test | What it proves |
|---|---|---|
| `NULL` | `test_a_tagged_slack_row_already_in_the_log_does_not_rerun_the_tag_route` | A Slack-origin PI row `_poll_channels` already appended and already handled inline reads NULL, so dedup falls back to log presence: the **real** `_handle_pi_inbound_entry` runs, and `handle_channel_tag` is *not* awaited — no duplicate bot reply on a tagged Slack message. The row stays NULL. |
| `NULL` | `test_a_null_row_absent_from_the_log_is_processed_exactly_as_today` | The other direction: a NULL row the log has never seen is ingested and `handle_channel_tag` **is** awaited. Together these two pin "NULL keeps today's behaviour exactly", i.e. a deploy that migrates before the code lands, or rolls the code back, behaves as it does now. |
| `'ingested'` | `test_a_failing_handler_still_records_the_pi_text_in_the_log` | A raising handler leaves the PI's text in the log immediately and the row marked `'ingested'`, cursor unmoved. |
| `'ingested'` | `test_a_transiently_raising_handler_processes_the_row_exactly_once_overall` | Poll 1 raises → text in the log, `'ingested'`; poll 2 re-runs the handler (`await_count == 2`) and the log holds **exactly one** entry for that ts. |
| `'ingested'` | `test_a_row_failing_past_the_lookback_window_keeps_its_text` | A bot row `2 × PI_INBOX_LOOKBACK_S` later drags the cursor past the failing row, the fake DB applies the *real* window predicate, and the PI row is no longer re-scanned — yet its text is still in the log and it still reads `'ingested'`, so a stranded row is findable. |
| `'handled'` | `test_the_happy_path_appends_advances_and_marks_the_row_handled` | Success marks `'handled'`, appends, advances. Also pins the compiled SQL (`UPDATE agent_messages SET pi_inbound_state=…` keyed on `simulation_run_id` + `message_ts`), so the write is against the real mapped column. |
| `'handled'` | `test_a_handled_row_is_not_reprocessed_even_when_it_left_the_log` | A `'handled'` row **absent** from the log is skipped and the cursor advances. Under log-presence dedup it would be re-appended and re-handled — this is what makes the marker durable rather than a restatement of log presence. |

### Why `MessageLog.append` not being idempotent no longer matters

It was the dedup mechanism; it is not any more. The poller appends first (so the PI's text is
recorded whatever the handler does) and then asks the **column** whether the side effects ran. On a
retry the append is skipped by the `in_log` check rather than being relied on to be a no-op, which is
what `test_a_transiently_raising_handler_processes_the_row_exactly_once_overall`'s "exactly one entry
for this ts" assertion pins. Nothing reads `append`'s effect to decide whether to run the handler.

### Considered and deliberately not done

`task-8.md` flagged that the cursor is advanced **per row inside the loop**, so a later row that
succeeds can drag `_pi_inbox_cursor` past an earlier row that failed, and suggested stopping the
advance at the first failure in a batch to keep the retry inside `PI_INBOX_LOOKBACK_S`. Not done: it
is outside the `Fix:` clause, and a permanently-failing row would pin the cursor forever, so every
subsequent poll re-scans a window that grows without bound. The durability requirement is met by the
append regardless of the cursor — which is exactly what
`test_a_row_failing_past_the_lookback_window_keeps_its_text` asserts. Past the window the *triggers*
are lost (D25's trade, and the `'ingested'` marker records which rows); the PI's message is not.

### Measurements

- `tests/unit/test_simulation_logic.py`, `tests/integration/test_state_rebuild.py`,
  `tests/unit/test_message_log.py`, `tests/unit/test_roster_sync.py`,
  `tests/integration/test_pi_inbox.py`, `tests/integration/test_message_persistence.py`,
  `tests/integration/test_cohort_engine_live.py`, `tests/integration/test_slack_mirror_live.py`,
  `tests/unit/test_hub_budget_scheduler.py`, `tests/unit/test_cohort_isolation.py` →
  **524 passed, 7 skipped**. Whole `tests/unit` tree → **1952 passed**.
- Lint/type, measured on a clean `git archive HEAD` export with only this task's two files overlaid
  (the shared checkout has other implementers' uncommitted edits): `ruff check src` **250**
  (unchanged), `ruff check tests` **0**, `mypy src --ignore-missing-imports` **145** (unchanged).
- **D33.** AST walk of every `ast.Constant` string in `src/agent/simulation.py`, before the first
  edit and after the last: **734 → 738, 4 added, 0 removed.** The four: the logger format
  `'Inbound marker write failed for %s (%s): %s'`, `_mark_pi_inbound_state`'s docstring, and the two
  column values `'ingested'` / `'handled'`. Nothing removed, so no existing string changed; the two
  new non-comment strings are database column values compared and written by the poller and never
  enter an LLM payload. No file under `prompts/` was touched.


## Consequence a closing comment must state

`#20` COR-10(3) is closed by re-ordering **plus** a new durable handled-marker, not by re-ordering
alone: the issue's `Fix:` clause ("wrap both like the sibling pollers; apply side effects before (or
transactionally with) the cursor advance") is satisfiable only with a dedup key that is distinct from
the log entry, because the log entry's presence *was* the dedup mechanism and `MessageLog.append` is
not idempotent. The marker ships in `0029`, so #20's comment must name the migration.

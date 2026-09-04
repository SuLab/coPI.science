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

*To be filled by Task 7's implementer.* What must be here:

- The failing test and its exact pre-fix failure message (plan Steps 2-3): a handler that raises once
  then succeeds leaves the PI's row in the log **immediately**, is retried, and is processed **exactly
  once** overall; and a handler that raises for longer than `PI_INBOX_LOOKBACK_S` still leaves the row
  in the log.
- Which column in `0029` is the handled-marker, and the coordination note with Task 8 showing the
  column landed before `simulation.py` changed.
- Why `MessageLog.append` being non-idempotent no longer matters once dedup reads the marker.

## Consequence a closing comment must state

`#20` COR-10(3) is closed by re-ordering **plus** a new durable handled-marker, not by re-ordering
alone: the issue's `Fix:` clause ("wrap both like the sibling pollers; apply side effects before (or
transactionally with) the cursor advance") is satisfiable only with a dedup key that is distinct from
the log entry, because the log entry's presence *was* the dedup mechanism and `MessageLog.append` is
not idempotent. The marker ships in `0029`, so #20's comment must name the migration.

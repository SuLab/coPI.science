# Task 5 — The two-strike post back-off wedges a thread slot

Plan: `docs/plans/2026-09-04-close-remaining-gaps.md` § "Task 5" (DECIDE, then FIX).
Issue: #20 COR-1b. Prerequisite landed: `edf84cf` (Task 4, per-agent strike key).

## Ruling

**Option (c): keep parking the thread, and make a parked thread invisible to the two
functions that account for an agent's live conversational load** —
`_non_funding_thread_count` and `_agent_load`. Implemented via one shared predicate,
`SimulationEngine._is_parked_thread`, so the two call sites cannot drift.

### Options considered

- **(a) Remove the two-strike back-off.** Returns to the issue's literal `Fix:` clause,
  which never asked for a back-off. Rejected: the back-off was written (`7647438`) to stop
  a deterministically-refused thread regenerating one LLM reply per turn forever, and
  nothing else stops it. `_phase4_reply_to_threads` re-enqueues on
  `has_new or thread.has_pending_reply` (`simulation.py:1402`), and `has_pending_reply` is
  the only durable half of that. Deleting the back-off re-opens a real cost defect to close
  a smaller accounting defect.
- **(b) Close the thread** via `_close_thread(agent, thread, "timeout")`. **Rejected — see
  `## Evidence`; three of the plan's four objections are confirmed against the code, and
  the fourth (that it needs no new state) is true but is not worth the price.**
- **(c) Keep parking; exclude parked threads from both accounting functions. CHOSEN.**
  Nothing false is written anywhere — no `ThreadDecision` row, no PI DM, no working-memory
  event, no `/admin/discussions` entry. The `blocked_for_regular` cascade cannot fire, the
  rate allowance stops being inflated by a thread that costs nothing, and the LLM burn the
  back-off exists to stop still stops. The costs are honest and small: one new predicate
  with two call sites to keep in sync (mitigated by making it *one* predicate), and the
  thread still never resolves — it lingers in `active_threads` until the run ends or the
  counterpart speaks again, exactly as it does today.

### Why (b) is rejected

The bar the task set was to disprove all four objections. Three survive scrutiny:

1. *"`status` is `"closed"`, never `"timeout"`."* Confirmed, with a correction to the
   phrasing: `"timeout"` is the **`outcome`** argument, not the status.
   `ThreadState.status` is documented `active | proposed | closed` (`src/agent/state.py:31`)
   and `_close_thread` sets `thread.status = "closed"` unconditionally
   (`simulation.py:1780`). So (b) does not mislabel `status`; it mislabels `outcome`.
2. *"`outcome` is a PostgreSQL enum, so a truthful label needs a migration."* Confirmed.
   `src/models/agent_activity.py:232-233` declares
   `Enum("proposal", "no_proposal", "timeout", name="thread_outcome_enum")`. `"timeout"`
   is a legal value, so (b) would *run* without a migration — but the only honest label for
   this event (`undeliverable`, `post_failed`) is not in the enum, and Task 8's `0029` is
   fixed at two columns by branch-owner ruling and is not this task's to extend.
3. *"A close writes an outcome into a PI DM, into both agents' prompt-fed working memory,
   and into `/admin/discussions`."* Confirmed, and the PI DM is worse than "an outcome" —
   it is a specific false sentence. `pi_handler.notify_thread_conclusion` renders
   `outcome == "timeout"` as: *"Thread with {other_name} in #{channel} timed out (reached
   message limit without a conclusion)."* (`src/agent/pi_handler.py:380-384`). A thread
   parked after two Slack refusals may hold two messages; it reached no limit. `_close_thread`
   also appends `f"Thread in #{channel} with {other} closed: {outcome}"` to **both** agents'
   working memory (`simulation.py:1876-1885`), which is fed back into later prompts, and
   writes the `ThreadDecision` row that `/admin/discussions` renders
   (`src/routers/admin.py:596-599`).
4. *"It needs no new state."* True — and it is the only real argument for (b). It does not
   outweigh 1-3. Issue #29 on this same branch is a six-week memory-poisoning chain that
   started with one unfounded sentence entering an agent's working memory; deliberately
   writing a false conclusion into two agents' memory to save a five-line predicate is the
   same mistake with the excuse pre-written.

Point 4 is the only one I could not disprove, and it is (b)'s benefit, not an objection.
So (b) stays rejected.

## Evidence

All line numbers against `5090322` + `edf84cf` (branch `close-issues-20-27`).

### The harm model, re-verified rather than inherited

| Claim | Verdict | Where |
|---|---|---|
| Parking = `post_failure_count >= 2` → `has_pending_reply = False`; the thread stays in `active_threads` with `status="active"` | **Confirmed** | `simulation.py:1650-1661` |
| A refused post is still persisted since `f29e295`, so the counterpart's refused row trips `has_new` and the pair self-closes at `max_thread_messages` | **Confirmed** | `_post_message` appends the row and returns `not slack_refused` (`f29e295`); `has_new_reply_from_other` skips only `entry.sender_agent_id == agent_id` (`message_log.py:525-535`); `thread.message_count = len(history_entries) - offset` (`simulation.py:1476`) counts every row, and `>= settings.max_thread_messages` (12, `config.py:333`) closes it (`simulation.py:1488-1495`) |
| The wedge therefore survives **only** where the counterpart stops posting | **Confirmed.** A parked thread has `has_pending_reply=False`, so `simulation.py:1402` re-enqueues it only when `has_new` fires, and `has_new` needs a row from someone else. Silent counterpart → `message_count` frozen → the 12-message close is unreachable → `status="active"` forever |
| `_non_funding_thread_count` counts the parked thread | **Confirmed** — it counts every entry of `active_threads` that is not a funding thread, with no liveness test at all (`simulation.py:571-575`) |
| Three of them trip `blocked_for_regular` (`active_thread_threshold = 3`) | **Confirmed** — `at_thread_threshold = self._non_funding_thread_count(agent) >= settings.active_thread_threshold` (`:2345`), `blocked_for_regular = at_thread_threshold or has_unreviewed_non_funding` (`:2354`), `active_thread_threshold: int = 3` (`config.py:331`) |
| …and the agent goes funding-only for the run | **Confirmed, and stronger than "funding-only".** Every non-funding, non-PI-priority, non-private candidate is dropped from `available_posts` (`:2390`), and then `if not available_posts and blocked_for_regular and not has_funding_interesting and not has_thread_foas: return` (`:2447-2449`) — Phase 5 exits **before the LLM call**, so an agent with only regular candidates does nothing at all. That early return is what the new test asserts on |
| `_agent_load` inflates the rate allowance | **Confirmed** — it counts `t.status == "active"`, which a parked thread is (`:521-524`); it feeds `allowance = _calls_per_load * _agent_load` in `_within_rate_limit` (`:554`), the Phase 4 headroom slice (`:1445`) and the `_select_agent` weight (`:1002`, `:1011`) |

**Not confirmed / not applicable:**

- `_owes_reply` (`:911-920`) also walks `active_threads`, but it needs
  `thread.has_pending_reply or has_new_reply_from_other(...)`. A parked thread fails both
  in the wedge scenario, so it already does the right thing and is **left unchanged**.
- The two-strike back-off "does not deliver its own benefit" (plan). **Partly true, and
  narrower than stated.** `has_new` alone does re-enqueue (`:1402`) and
  `post_failure_count` resets only on a successful post (`:1668`) — but that only happens
  when the counterpart posts, which is exactly the case the plan itself says is no longer
  wedged. In the wedge case (silent counterpart) the back-off does deliver: the LLM call
  per turn stops. This is another reason not to take option (a).
- `post_failure_count` is **not** persisted or rebuilt (`state.py:48-55`), so a restart
  un-parks every parked thread and grants it a fresh two strikes. That is pre-existing and
  unchanged by this task; it means the exclusion is a within-run effect only.

### The predicate

`_is_parked_thread(thread)` is `thread.post_failure_count >= 2 and not thread.has_pending_reply`.
Both halves are needed:

- `post_failure_count >= 2` alone would not distinguish a parked thread from a perfectly
  healthy one waiting on its partner — `has_pending_reply` is `False` after **every**
  successful reply (`:1664`).
- `not has_pending_reply` alone is the same problem from the other side.
- Together they are exactly the state `:1656-1657` puts a thread into, and nothing else
  reaches it. A thread revived by `has_new` gets `has_pending_reply = True` at `:1408`
  before Phase 4 acts on it, so it starts counting again the moment it is a live obligation,
  and a successful post zeroes `post_failure_count` at `:1668`.

### Commands

```bash
.venv-test/bin/python -m pytest tests/unit/test_simulation_logic.py \
  tests/unit/test_message_log.py tests/unit/test_mentions.py \
  tests/unit/test_roster_sync.py tests/unit/test_thread_not_found.py \
  tests/integration/test_state_rebuild.py -q -p no:cacheprovider
```

D33: an AST walk of every `ast.Constant` string in `src/agent/simulation.py` before and
after this task differs in six entries — three docstrings (two amended, one new) and three
`logger.info` format strings. No model-facing string changed.

The three log lines all carried the same clause, "not counted, nothing persisted", which
`f29e295` made false when it restored the DB-only row a refused post writes. Plan Step 5
names only the Phase 4 one (`:1646-1648` pre-`edf84cf`, `:1694` after); the identical
falsehood is also emitted by `_note_phase5_post_failure` and by Phase 5's new-post branch,
from the same `_post_message` return. All three now read "turn not counted, kept as a
DB-only row", which is what actually happens. The `Suppressed post in #%s` /
`Suppressed reply to %s` prefixes are unchanged so existing log greps still match. Log
text, which D33 permits.

## Consequence a closing comment must state

Issue #20's closing comment should record that the two-strike post back-off
(`7647438`) was **kept, not removed** — it is not in COR-1b's `Fix:` clause, but removing
it would restore a per-turn LLM burn on a permanently-refused thread — and that its
side effect was corrected instead: a parked thread no longer counts towards
`active_thread_threshold` or the sliding-window rate allowance, so it can no longer push an
agent into `blocked_for_regular` for the rest of a run.

Two residual facts a reader should not be surprised by, neither of them regressions:

1. A parked thread is never closed. With a silent counterpart it stays `status="active"` in
   memory until the run ends. This is deliberate — closing it would write a false
   `"timeout"` outcome to the PI, to `/admin/discussions` and to both agents' working
   memory (see `## Ruling`). A truthful terminal state for an undeliverable thread would
   need a new `thread_outcome_enum` value and a migration; if that is ever wanted, it is a
   follow-up issue, not part of #20.
2. `post_failure_count` is in-memory only, so a restart un-parks every parked thread. The
   exclusion is therefore a within-run correction, which is the same scope as the back-off
   it corrects.

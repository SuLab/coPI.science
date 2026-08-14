# Two-lane concurrent scheduler — design

**Status:** proposed, not implemented.
**Supersedes in part:** `docs/specs/2026-08-06-hub-budget-scheduler-design.md` §4 (the
single `_agent_load` signal driving both limiter and scheduler). That document's
central invariant — "limiter and scheduler cannot disagree" — is *deliberately
broken* by this design; see §2.3.

## 1. Goal

Three requirements, from the operator:

1. **The hub posts as often as it needs**, on its own queue, not competing with the
   62 spokes for turns.
2. **Two agents can hold a rapid back-and-forth inside a thread**, rather than one
   message per global round-robin cycle.
3. **The staggered/paced queue applies to top-level posts only.**

Requirements 1 and 3 turn out to be the same mechanism. `prompts/roles/scout_hub/role.toml`
declares `post_types = []` and the engine hard-gates the hub out of Phase 5, so the
hub *only ever replies*. Once in-thread replies stop being paced, the hub is
automatically unpaced, and pacing survives only where it was wanted: top-level posts.

## 2. Architecture

### 2.1 The two lanes

| | Post lane | Reply lane |
|---|---|---|
| Drives | Phase 1 (channel discovery) + Phase 5 (top-level post) | Phase 3 (thread activation) + Phase 4 (thread reply) |
| Concurrency | Sequential, one agent at a time | Concurrent, bounded by a global semaphore |
| Pacing | Staleness×load weighted draw, `turn_delay_seconds`, `phase5_spontaneous_interval`, `lab_daily_post_cap`, per-agent rate limit | None |
| Rate limit | Yes (§5) | Yes (§5) — as a **brake, not pacing** |

"No pacing" and "no rate limit" are different claims, and only the first is true of
the reply lane. Nothing in the reply lane delays a reply that is ready: there is no
weighting, no streak cap, no cooldown, no interval. But every LLM call in either
lane still takes a reservation (§5), because that is the only thing standing between
a reply loop and an unbounded bill. In normal operation a spoke replying in one
interview will never approach its allowance, so the brake is invisible; it exists for
the case where something goes wrong.
| Population | `pi_lab` agents (the hub has no post types) | Any agent owing a thread reply — in practice the hub plus one lab per interview |

The post lane is today's `_select_agent` (`src/agent/simulation.py:807`) with its
reactive tier deleted — reactive selection exists only because replies currently
share the pool, and they no longer will.

The reply lane is new: a dispatcher that finds `(agent, thread)` pairs with
`has_pending_reply` and runs each as its own task.

### 2.2 Why Phase 5 must decouple from Phase 4

Today (`simulation.py:~920`):

```python
has_new_work = has_phase4_work        # len(phase4_thread_ids) > 0
if has_new_work or spontaneous_ready:
    await self._phase5_new_post(agent)
```

Replying is what *earns* an agent a shot at a top-level post. If that survives the
split, the "staggered" post lane is still driven by reply volume — the exact
coupling requirement 3 asks to remove. `lab_daily_post_cap = 1` masks the damage
today; it would not once replies run unpaced.

**Decision:** Phase 5 eligibility comes from `spontaneous_ready` (and the daily cap)
alone. `has_phase4_work` is removed from the predicate.

### 2.3 The invariant being broken, deliberately

The 2026-08-06 design unified limiter and scheduler on one `_agent_load` signal so
they could not disagree. This design splits them: the reply lane has no rate limit
per agent, and the hub has a separate ceiling. That is the point of the change, but
it removes a safety property, so §5 replaces it with a *reservation* limiter that
bounds spend directly rather than bounding selection.

## 3. Concurrency control

Nothing in the engine is thread-unsafe in the CPython sense; the loop is
single-threaded. Every current critical section is correct because it is an
unbroken synchronous run with **no `await` between a check and its act**.
Concurrency turns those into 10–60 second windows (the LLM call). The hazards are
therefore check-then-act, not data races.

### 3.1 Locks

| Lock | Keyed by | Acquired | Held across | Prevents |
|---|---|---|---|---|
| Thread lock | `thread_id` | top of `_reply_to_thread` | history read → LLM call → `_post_message` → sidecar capture → `_check_thread_outcome` | §4.1, §4.2, §4.3 |
| Agent lock | `agent_id` | whole post-lane turn; `_phase5_new_post`; cursor write; memory update | LLM calls | §4.4, §4.5, §4.7 |
| In-flight semaphore | global | whole reply task | everything | unbounded Opus fan-out, DB pool exhaustion |

Both lock dicts are engine-owned (`dict[str, asyncio.Lock]`, created lazily).

**The thread lock must be held across the LLM call.** A lock around the message log
alone does not fix §4.1 — the stale read happens *before* the call and is acted on
*after* it.

### 3.2 Deadlock avoidance

`_close_thread` (`simulation.py:1434-1436`) mutates the *other* agent's state, and
`_evict_dead_thread` (`:1500-1514`) mutates *every* agent's state. Two closes in
opposite directions would deadlock on the agent locks.

**Decision:** acquire agent locks in sorted `agent_id` order, always. A helper
(`_agent_locks_in_order(*agent_ids)`) is the only sanctioned way to take more than
one, and a test asserts no call site takes them directly in sequence.

## 4. Ordering invariants that concurrency breaks

Each of these is a real defect the design must close, not a theoretical one.

### 4.1 Stale thread history

`_reply_to_thread:1182` snapshots `get_thread_history` before the LLM call. A
concurrent reply into the same thread lands during that window and is absent from
the prompt; both parties then reply to a state neither is in.
**Fix:** the thread lock (§3.1).

### 4.2 Ordinal / CONCLUDE parity — the worst case

`thread.message_count = len(history_entries)` (`:1189`), `message_ordinal =
message_count + 1` (`agent.py:329`), and `thread_guidance.py:166-171` renders
CONCLUDE at ordinal ≥ 12. Two concurrent replies in one thread produce one of:

* both read prior-count 11 → **two concluding replies**, and if both are the hub,
  **two `<assessment_json>` sidecars → two `opportunity_assessments` rows.**
  Verified: that table has only a PK and an FK, no uniqueness constraint.
* both read prior-count 12 → both pass the close check (`:1222`) → **two
  `ThreadDecision` rows**, `_prior_threads` double-appended, and **four**
  `_update_agent_memory` LLM calls each doing a lost update on the same file.
* both read behind reality → ordinal 12 never rendered → interview runs past 12 and
  closes as `timeout` with **no assessment at all**.

**Fix:** thread lock, plus recompute `message_count` *inside* the lock.

### 4.3 `has_pending_reply` double-service

Set in Phase 3 (`:1022`), promoted in Phase 4 (`:1147`), cleared only after a
successful post (`:1349`). Two tasks for the same `(agent, thread)` both see `True`.
**Fix:** thread lock + a dispatcher-level in-flight set so a pair is never spawned
twice.

### 4.4 Cursor lost update

`_run_turn:931` does `last_seen_cursor = time.time()` unconditionally at turn end,
after Phases 3/4 have read it. Concurrent turns for one agent: the later write marks
the earlier turn's unprocessed inbox as read.

Separately, this compares a wall clock against `posted_at`, which is
`float(minted_ts)` from `TsMinter` — a strictly-advancing slot allocator that under
fan-out can run *ahead* of wall clock.

**Fix (an invariant change, not a lock):** snapshot the log's latest timestamp at
turn start and assign that at turn end. Idempotent, and it removes the clock
mismatch.

### 4.5 Daily cap and thread-threshold overshoot

`_count_today_posts` → `if today_posts >= cap` (`:1761`) and `_active_thread_count >=
active_thread_threshold` (`:1776`) are both check-then-await-then-act. With
`lab_daily_post_cap = 1`, two concurrent Phase 5s for one lab both see 0 and both
post. Overshooting the thread threshold also inflates `_agent_load`, and therefore
the agent's own rate allowance.
**Fix:** agent lock around Phase 5.

### 4.6 Rate limiting is an entry gate, not a spend gate

`_within_rate_limit` (`:416`) is consulted only in `_turn_eligible` at selection.
`_phase4_reply_threads` already fires up to `phase4_max_concurrent_replies` LLM
calls per turn without re-checking, and its semaphore is **constructed per call**
(`:1166`) — so it bounds per-turn fan-out, not global. N concurrent turns give N×4
concurrent requests.
**Fix:** §5.

### 4.7 `_cohort_tags_stripped` delta read

`_phase5_new_post:1918-1922` reads a *global* counter before and after to decide
whether *this* message had a tag stripped. Any concurrent `_post_message` bumps it
and Phase 5 rejects a clean post.
**Fix:** return the stripped-count from `_strip_disallowed_tags` per call; keep the
global counter for stats only.

## 5. The reservation limiter

A selection-time check cannot bound concurrent spend. Replace it with a reservation:

* `try_reserve(agent, now) -> bool` appends to `agent.state.call_times` **before**
  the call is issued, under the agent's lock, and returns False if the window is
  full.
* `record_api_call` keeps its lifetime counter but no longer double-appends.
* A reservation that is never spent (early return, exception) is released.

Allowances:

| Agent | Allowance |
|---|---|
| `pi_lab` | unchanged: `llm_calls_per_load_per_window` (8) × `_agent_load`, clamped to `active_thread_threshold` |
| `scout_hub` | `hub_llm_calls_per_window`, new setting, default **600** per `llm_rate_window_seconds` (600s) |

600/10min is chosen to be unreachable in normal operation — the hub's measured rate
was ~2.6 calls/10min — while still tripping a runaway. It is a brake, not a budget.

## 6. Prerequisites

These are not optional; without them concurrency is partly fictional.

### 6.1 Slack transport off the event loop

`AgentSlackClient.post_message` (`slack_client.py:752`), `poll_channel_messages`
(`:506`) and `get_full_channel_history` (`:592`) are **synchronous**, called without
`await` (e.g. `simulation.py:3050`), and the 429 handler does `time.sleep(retry_after)`
(`slack_client.py:337`) **on the event loop**. One Slack rate-limit stall currently
freezes the hub and all 62 spokes.

**Fix:** wrap calls in `asyncio.to_thread`, as `services/llm._acreate` already does.

**Invariant:** `MessageLog` is loop-safe but **not thread-safe** — `append()`'s
dedupe is a check-then-act and `_entries.append` / `_by_ts[...]` are a pair. Post in
a thread; append on the loop. Pinned by a test.

### 6.2 Database pool

`create_async_engine(settings.database_url)` (`src/agent/main.py:160`) passes no pool
arguments → SQLAlchemy defaults, 5 + 10 overflow = **15 connections**. Every engine
write opens its own short-lived session, so there is no shared-connection hazard —
but past 15 in flight, checkout blocks for `pool_timeout` and then raises, and
`_persist_assessment` (`:2285`), `_close_thread` (`:1456`), `_record_assessment_drop`
(`:2340`) and `_flush_llm_logs` (`:4029`) all **swallow** that exception. Pool
exhaustion would therefore appear as silently dropped assessments.

**Fix:** set `pool_size`/`max_overflow` above the maximum in-flight task count, and
give those four call sites the requeue-on-failure behaviour `_flush_persisted`
already has.

### 6.3 Engine-level fan-out semaphore

Hoist the Phase-4 semaphore (`:1166`) out of the method into `__init__` so it bounds
global concurrent LLM calls rather than per-turn ones.

## 7. Redesigns that locking cannot fix

1. **`_llm_log_buffer[-1]["channel"] = channel`** (`:1903`) — a positional
   back-reference that assumes the tail entry is this agent's call. Pass `channel`
   in `log_meta` before the call instead.
2. **`_last_llm_caller` / `_reactive_streak`** (`:287`, `:293`) — encode a strictly
   sequential A→B→A baton and a "consecutive turns" valve, both meaningless once
   replies leave the pool. Replace with a per-agent `in_flight` flag (excluded from
   post-lane selection while a turn runs); delete the reactive tier.
3. **The limiter** — §5.
4. **`_specialist_floor_gap`'s "map empty overall ⇒ fail open"** (`:2533`) — a
   process-global predicate read at persist time. A concurrent thread's first consult
   flips it from fail-open to enforcing mid-interview, so an in-flight verdict is
   refused *after its reply is already in Slack*. Snapshot the decision when the
   interview thread is activated.
5. **`_closed_thread_ids.discard`** in `_evict_dead_thread` (`:1514`) — un-closing
   races a concurrent close and resurrects a finished interview. Make eviction
   additive.
6. **`MessageLog` thread-safety** — §6.1.

## 8. Error handling

* A reply task that raises is logged; `has_pending_reply` stays `True` so the next
  dispatch retries. The `empty_response_count` / `suppressed_post_count` backoffs
  (2 strikes each) already bound this.
* Reservations are released on any path that does not issue the call.
* Pool-checkout failures requeue rather than swallow (§6.2).
* The dispatcher never lets one thread's failure cancel its siblings
  (`return_exceptions=True`, as today).

## 9. Testing

Every failure mode here is silent, so the tests are adversarial and concurrency-first.

| Test | Asserts |
|---|---|
| Two simultaneous replies into one thread | exactly one CONCLUDE rendered, exactly one `opportunity_assessments` row, exactly one `ThreadDecision` |
| Concurrent Phase 5 for one lab | exactly one pitch, cap respected |
| Reservation limiter under N concurrent callers | allowance never exceeded |
| Cross-agent close in both directions | completes; no deadlock (bounded by `asyncio.wait_for`) |
| Slack post during a turn | event loop stays responsive (tick-counting, as `tests/unit/test_llm_event_loop.py` does) |
| `MessageLog.append` from a thread | fails a guard / is never reached — the loop-only invariant |
| Dispatcher dedupe | a `(agent, thread)` pair already in flight is not spawned twice |
| Post lane | no reactive tier; hub never selected into it |

## 10. Rollout

The engine is stopped, so this can land without a live migration. Order:

1. Prerequisites (§6) — independently valuable, safe to ship alone.
2. Redesigns (§7) — each is a small isolated change with its own test.
3. Lane split (§2) still sequential — behaviourally most of the win.
4. Turn on concurrency (§3) last, behind `reply_lane_max_in_flight` (default 1 = off).

Step 4 being a setting means concurrency can be disabled in production without a
code change if it misbehaves.

## 11. Open questions

* `active_thread_threshold` (live: 12) currently bounds hub capacity *and* feeds
  `_agent_load`. With the hub on its own ceiling, does the threshold still bound how
  many interviews it holds at once? Proposed: yes, keep it as a capacity bound; it
  no longer affects the hub's rate.
* `max_consecutive_reactive_turns` becomes dead once the reactive tier is deleted.
  Proposed: remove the setting rather than leave an inert knob.

# RCA + adversarial fix audit — perf/memory/race findings (2026-08-21)

Companion to `README.md` in this directory (the findings and their execution
evidence) and to `docs/plans/2026-08-21-perf-memory-race-remediation.md` (the
implementation plan this document justifies). Three passes per finding:

1. **Root cause** — mechanism → proximate cause → root cause, anchored in the
   repo's own git history and design documents, not recollection.
2. **Challenge** — the strongest available "this is not really a bug" argument,
   stated and then resolved with evidence.
3. **Fix audit** — the invariants a fix MUST preserve, and the specific naive
   fix that violates each one. Every trap below was found by adversarial
   reading of the fix design *before* planning, and each has a guard task in
   the plan.

Evidence conventions: `harness_*` refers to `harnesses/` beside this file
(execution evidence, all run against this tree); commit ids are from this
repo; spec quotes are verbatim.

---

## F1 (HIGH) — `_close_thread` convoy

**Mechanism.** `_close_thread` runs two `_update_agent_memory` LLM calls
(`simulation.py:2110/:2115`) inside `_agent_locks.acquire_all(agent, other)`
(`:2032`), itself under the caller's thread lock and reply-lane semaphore slot
(`_dispatch_reply_lane._run`, `:1271/:1308`). Every interview pairs the hub,
so every close contends on the hub's key; blocked closes and the closing
threads' partner-side reply pairs hold semaphore slots while they wait
(`harness_a2_instrumented.py` trace).

**Proximate cause.** The lock scope includes the LLM calls.

**Root cause.** The memory calls predate concurrency (`_update_agent_memory`
ships with the original engine, commit `1812fa9`; in a serial engine nothing
else could run during them, so their placement had no cost). The 2026-08-14
two-lane scheduler (locks: `34c1cf2`; concurrent lane: `6f6c284`) then found a
real race — two concurrent closes doing a read-await-write on the same agent's
memory file lose an update (design doc §4.2: "four `_update_agent_memory` LLM
calls each doing a lost update on the same file") — and fixed it with the
agent lock, *deliberately* held across the calls: the spec's own lock table
(§3.1) records `Agent lock … Held across: LLM calls`. The hold-time cost of
that decision was never analysed against the star topology, where every pair
shares the hub key.

**Why the gate missed it.** The adversarial concurrency tests
(`test_concurrent_thread_safety.py`) assert correctness invariants — no
deadlock, no lost update, one verdict — and none measures throughput or
starvation. Correct-but-serialized passes every one of them.

**Challenge.** *"The reply lane is 'unpaced by construction' and interviews
take minutes anyway — who cares if a sweep is slow?"* Resolved: the main loop
`await`s the entire dispatch gather (`:1321-1324`) before running the DB
inbound poller, the roster sync, the post lane, or ANY of the three flush
buffers — so the convoy's wall time is a full engine outage, not a slow lane:
PI messages sit uningested and buffered `llm_call_logs`/`agent_messages` rows
sit unflushed for its whole duration. Measured shape: 8.0 s where 2.2 s was
available (harness A), with production memory-call latencies that is minutes.
The convoy also lands at the worst moment by construction: closes are exactly
the turns that carry verdicts.

**Fix invariants + traps.**
- *Invariant 1 (the lock's actual job):* per-agent memory updates must stay
  strictly sequential, each reading its predecessor's written text. The
  repo has already proven the naive fix wrong: the lost-update test's
  docstring records that deleting the lock makes it fail ("confirmed via a
  disposable worktree"). **Trap:** moving the two awaits out of the lock
  without replacing the serialization silently reintroduces that bug —
  and no current test would catch a *queued* variant that drains
  concurrently. The plan's design (FIFO queue + a single sequential drainer
  guarded by a drain lock) preserves the invariant structurally, and the
  reworked test drives two concurrent drains against the interleave-detecting
  fake to pin it.
- *Invariant 2 (durability of the log rows):* memory updates are billed calls
  whose rows flow through `_call_log_callback`. **Trap:** `stop()` clears the
  callback first (`set_call_log_callback(None)`, `:894`) — draining after
  that point makes unlogged billed calls. The drain must run before the
  callback is cleared, and before `_flush_llm_logs`.
- *Invariant 3 (bounded shutdown):* the `-t 420` stop grace was sized for ONE
  16 k call. **Trap:** an unbounded shutdown drain of N queued events × ~20 s
  can blow it. Drain at stop is capped (10) with a loud drop of the rest.
- *Invariant 4 (roster liveness):* agents can be removed/rebuilt between
  enqueue and drain (`_sync_roster_from_db` builds fresh `Agent` objects —
  issue #20 E6.2). **Trap:** queueing `Agent` references acts on dead
  objects; queue agent_ids and resolve at drain.
- *Accepted regression:* working memory becomes ~one tick stale relative to
  the close (previously synchronous). Memory is a summary artifact; staleness
  of seconds is acceptable and is documented in the code.
- *Stale docstring found in passing:* `_update_agent_memory`'s docstring
  claims PI-DM and proposal-review triggers; grep shows the two `_close_thread`
  sites are the only callers left. The plan corrects it.

## F2 (MEDIUM-HIGH) — sync Slack calls on the event loop

**Root cause.** `slack_client.py` is a synchronous `WebClient` wrapper from
the original build (`1812fa9`), written for a serial engine where blocking
was invisible. When the engine went concurrent, async `a*` to_thread wrappers
were added *selectively* — exactly the four methods the hot poll/post paths
used (`:1167-1186`). `is_bot_user` (poller, per human message),
`join_channel` (Phase 1) and `connect()` (roster adoption/add, every ~30 s
for a flapping token) were never converted, and `_call_with_retry`'s
`time.sleep(Retry-After)` (`:353`) therefore still executes on the loop
thread for those callers. Measured: 1.5 s freeze per 5-human-message poll;
6.0 s freeze for one 429 (harness B).

**Challenge.** *"Humans rarely post in bot channels; 429s are rare."*
Resolved: PIs demonstrably watch and post in interview threads (that is the
product), each such message costs one blocking round trip with no per-user
cache, and the 429 case freezes the SIGTERM path — it eats stop-grace budget.
The counter-cost is three one-line `to_thread` conversions.

**Fix invariants + traps.**
- *Invariant:* `MessageLog.append` must stay on the loop (its own docstring:
  "fetch/post off it, then append back on it"). The conversions wrap only the
  HTTP call, never the log mutation.
- *Trap (caching):* `is_bot_user` returns `False` on `SlackApiError`. Caching
  that failure value would permanently misclassify a bot as human and drop
  its messages forever. Cache successful lookups only.
- *Trap (fakes):* engine code switching to `await client.ais_bot_user(...)`
  requires the async method on `tests/fakes.FakeSlackClient` too, or every
  poller test breaks.
- *Accepted:* startup-only sync calls (`_ensure_seeded_channels`,
  `_rebuild_state_from_slack`'s history fetch path) stay blocking — they run
  before the loop serves anything.

## F3 (MEDIUM, grows with run age) — MessageLog O(n) scans

**Root cause.** The in-memory `MessageLog` is the design of
`specs/local-db-conversations.md` — a single source of truth small enough at
design time (hundreds of messages) that linear scans were the simplest
correct implementation. Two later decisions changed the constants without
revisiting the reads: the 14-day rebuild window (`REBUILD_WINDOW_S`,
`simulation.py:182`) and the two-lane scheduler running the full Phase-3 +
pending-pairs read set for *every* agent *every* tick. Measured: 701 ms of
synchronous scanning per tick at 100 k entries (harness C).

**Challenge.** *"Runs restart before the log gets that big."* Resolved: the
curve is linear and starts costing tens of ms at 10 k — paid every tick,
idle ticks included — and the message rate scales with the roster while the
restart cadence does not. This is a fix-before-it-hurts item, priced
accordingly (MEDIUM, not HIGH).

**Fix invariants + traps** (the subtlest fix of the set — the reads encode
deliberate, documented ordering semantics):
- *Trap (result order):* the three `since`-readers return matches in
  **insertion** order, not time order, and Phase-3 activation order follows
  it. An index iterated in time order silently reorders activations. Fix:
  collect matches, sort by insertion sequence before returning.
- *Trap (tie semantics):* `get_last_bot_sender_in_channel` keeps the LATER
  insertion on equal `posted_at` (`>=` in the scan). An incremental
  "last bot per channel" map must use the same `>=` update rule — which is
  exactly equivalent because `_record` runs in insertion order.
- *Trap (stable sort):* `get_thread_history` relies on stable sort over
  insertion order for equal `posted_at`; the per-thread index must sort by
  `(posted_at, seq)`.
- *Trap (panel notes):* exclusion lives at READ time; `get_entry` must keep
  seeing notes (append idempotency + Slack-mirror dedup depend on it). Indexes
  store everything; filters stay in the read methods.
- *Guard:* a differential test runs every read method against verbatim copies
  of the pre-change implementations over a seeded randomized log (out-of-order
  posted_at, threads, notes, humans, visibilities, gates) and asserts equal
  output lists — order included.
- *Deliberate non-fix:* no in-process pruning of closed threads. Pruning
  interacts with `get_entry`-based dedup and on-demand rehydration; the
  indexes remove the scan cost, and 0.4 KB/message RAM is acceptable.

## F4 (LOW-MEDIUM) — fresh Anthropic client per call

**Root cause.** `llm.py` dates to the profile pipeline (`5972103`), where an
LLM call was rare and client construction cost irrelevant. The agent engine
adopted the same helper for every turn/consult/memory call; nobody revisited
the lifecycle. Verified: 8 calls → 8 TCP connections (harness D).

**Challenge.** *"A TLS handshake is noise next to a 20 s Opus call."*
Accepted — that is why this is LOW-MEDIUM, not HIGH. It still multiplies by
call volume (consults ×8 per concluding turn), leaks each abandoned pool to
GC timing, and the fix is three lines.

**Fix traps.** Cache must key on the API key (`lru_cache` over
`_client_for_key(api_key)`) so tests injecting different keys don't share a
client; the SDK sync client is thread-safe under `asyncio.to_thread`
concurrency (documented httpx property), so one instance is safe. Watch one
thing in CI: any test that mutates client internals would now leak state
across tests — none found by grep, and the full suite is the gate.

## F5 (LOW) — unbounded per-interview residue

**Root cause.** All four structures (`LockRegistry._locks`,
`_closed_thread_ids`, `_prior_threads`, log entries) were built in the
short-run era where `--fresh` wipes and frequent restarts made process-lifetime
growth invisible; the lock registry arrived with `34c1cf2` and simply never
got an eviction path. 7.3 KB/interview measured (harness E).

**Challenge.** *"7 KB per interview never OOMs anything real."* Accepted —
severity LOW, and `_closed_thread_ids` insert-only is a *documented safety
invariant* (`:2158-2161`: eviction must never un-close). The reasons to act
anyway: the lock registry is the one structure that grows with a *hostile*
key space too, and `_prior_threads` feeds F6.

**Fix traps (the sharp one).** Naive lock eviction — "delete locks that are
not `.locked()`" — **breaks mutual exclusion**: between a holder's `release()`
and a waiter's wakeup the lock reports unlocked while the waiter still holds
a reference to the old object; evicting then hands the next acquirer a fresh
`Lock`, and two tasks hold "the same" key. The safe design is refcounted:
every `acquire_all` registers intent synchronously before its first await,
and eviction happens only at refcount zero (no holder, no waiter, no
intender). The plan pins this with a three-task interleave test asserting
max-concurrency-1 across an eviction boundary. `_closed_thread_ids` is
deliberately left insert-only; `_prior_threads` is capped at 50 per pair
(the DB's `thread_decisions` remains the full record).

## F6 (LOW-MEDIUM cost) — prior-threads render bloat

**Root cause.** `_prior_threads` exists for Phase-5 dedup context; the
renderer (`agent.py:499-516`) was written to show "all closed threads"
(docstring) when "all" was single digits. Issue #20's COR-7 flagged the
missing dedup on the same list but not its unbounded length. Measured: ~60
input tokens per summary-carrying close, on every future Phase-5 call, both
agents, forever (harness F: 500 closes → ~38 k-token prompt).

**Challenge.** *"Pitch-only closes carry no summary — lines are ~25 chars."*
Resolved: partially true and priced in (LOW-MEDIUM); summary-carrying
outcomes exist (`proposal` legacy rows rebuilt from the DB), the growth is
monotonic either way, and the fix is a render cap that also improves the
prompt (a 500-line dedup list is worse *context*, not just more tokens).

**Fix traps.**
- `tests/characterization/__snapshots__/test_agent_turn_gm.ambr:1293` pins
  the "## Prior conversations" block. CLAUDE.md forbids `--snapshot-update`
  to make a mismatch "go away"; this change is a *deliberate contract
  change*, so the plan requires: run the GM test, inspect whether the pinned
  fixture even exceeds the cap (likely it renders unchanged), and only if a
  diff appears, regenerate with the diff pasted into the commit message.
  The `pi_lab` thread-guidance strings are untouched either way.
- Cap at render, not only storage: F5's storage cap (50) still renders 50
  lines; the render cap (5 + "(N earlier…)") is the token fix.

## F7 — backlog items re-confirmed at HEAD (issues #24/#25/#22/#23)

Root causes are documented in their issues; this pass re-confirmed presence
by code read and adds fix-design traps the issues don't spell out:

- **V5 (waitlist / review_proposal concurrent-insert 500s).** Trap: none —
  the repo already contains the correct pattern twice (`public.py:1041`,
  vote endpoint); the fix is a transplant. For `review_proposal`, the
  IntegrityError rollback also discards that request's
  `record_engagement`/`mark_notification_responded` writes — correct, because
  the *winning* racer performed its own.
- **C2 (blocking provisioning on the web loop).** Trap: `asyncio.to_thread`
  alone is not enough — `_config_token`'s reads open a transaction, so the
  session's pooled connection (web pool: 5) is held across a
  possibly-minutes-long rate-limited manifest call. The fix commits before
  the external call (AsyncSession releases the connection at commit) and
  to_threads all five blocking calls (`create_app` ×2, `lookup_team_id`,
  `exchange_code`, `rotate_config_token`).
- **P3-web (pool settings).** The agent process already carries half the fix
  (`agent/main.py:161-165`: `pool_pre_ping=True` only — it does NOT set
  `pool_recycle`, verified 2026-08-21). Transplant `pool_pre_ping`; add
  `pool_recycle=1800` to `src/database.py` as a new addition on its own
  merits, not a transplant. Pool size stays 5+10 — resizing the web pool is a
  separate decision with its own load evidence.
- **P1-web (badge middleware + indexes).** Two independent halves: a
  `/static/`+`/api/health` short-circuit in the middleware, and migration
  0033 (composites `(agent_a|agent_b, outcome)` on `thread_decisions` + the
  18 unindexed `ondelete` FK targets enumerated in issue #25). Additive DDL,
  no deploy-order constraint; ci.sh's migration round-trip covers it.
- **C1 (RMW races).** Trap: assigning a SQL expression to
  `profile.profile_version` expires the attribute, and
  `profile_pipeline.py:424` **reads it right after** for a log line — in an
  async session a lazy re-load raises `MissingGreenlet`. The pipeline site
  needs `await db.refresh(profile, ["profile_version"])` before that read;
  the three router sites read nothing after and need none. The
  `delegate_slack_ids` fix must dedup **in SQL** (`WHERE NOT … @> ARRAY[sid]`
  guard on the atomic `array_append`), or the check-then-append just re-races.
- **V9 (PubMed pacing).** Trap the issue's own suggested fix falls into:
  "size the semaphore to the limit" does not bound the *rate* — with the
  sleep inside the semaphore, rate = concurrency ÷ per-request-time, so even
  Semaphore(3) with fast responses bursts well past 3/s. Rate needs
  start-spacing: a `_pace()` gate whose read-modify-write of the next slot is
  synchronous (no await between read and write → atomic on the loop, no lock
  object to bind to a test's event loop). Pacing runs BEFORE
  `raise_for_status`, closing the pacing-skipped-on-429 hole.
- **COR-30 (debit-before-fetch).** Trap: charging on success must not remove
  the *cap check* before issuing (an over-cap call must still be refused
  without fetching), and the overshoot window (check → fetch → charge) is
  safe only because tool rounds are sequential within a turn and the thread
  lock serializes turns per thread — both verified in this audit.

## Prototype verification of the two riskiest fix designs (executed)

The plan's F1 fix (memory-event queue) and F5 fix (refcounted lock eviction)
are the two designs with real failure modes of their own, so both were
prototyped against a byte-identical copy of this tree and executed before the
plan was finalized (`harnesses/verify_fix_prototype.py` beside this file is
the runnable record; the prototype diff matches the plan's Task 1/Task 5 code
verbatim). Results:

```
A. PASS convoy gone: dispatch wall 0.207s (was 8.0s), first ordinary reply
   at 0.002s (was 6.0s), 0 memory calls in dispatch, 8 drained after
B. PASS lost-update invariant survives (2 closes, 2 concurrent drains,
   both events in memory)
C. PASS stop() drained exactly 10, dropped 2, buffer empty
D. PASS LockRegistry: eviction at refcount zero, mutual exclusion holds
   across eviction, waiters keep keys alive
E. PASS _prior_threads capped at 50 per pair, newest kept
```

Against the prototype, the existing pinned suites stay green:
`tests/unit/test_reply_lane.py` + `tests/unit/test_simulation_logic.py`
(121 passed) and `tests/integration/test_concurrent_thread_safety.py`
against a real testcontainers Postgres (8 passed, 1 deselected). The one
deselected test — `test_close_threads_agent_lock_prevents_a_lost_memory_update`
— fails against the prototype exactly as the plan predicts and for exactly
the predicted reason (`hub's working memory lost one of the two closes'
events entirely: ''` — the events are queued, not yet drained; the test's
synchronous-write premise is false by design), which is why the plan replaces
it with the drain-aware successor rather than keeping it.

## Out of scope, deliberately

The wider backlog (#20 state-machine, #21 worker/email, #22 profile-write
integrity beyond C1, #23 GrantBot/regex, #26/#27 docs+deploy, #33 Phase-2
skip, #37 pending-user isolation) already carries per-PR plans re-verified
2026-08-11. Duplicating them here would create a second drifting copy;
nothing in the remediation plan conflicts with them. Two coordination notes:
C1 lands inside files issue #22's V6 also touches (land C1 first, it is
smaller); the V9 fix satisfies the transport half of issue #23's V9 (its
retry/backoff slice included).

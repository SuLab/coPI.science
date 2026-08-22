# Adversarial audit — performance, memory & race defects (2026-08-21)

**Status: all 13 tasks in `docs/plans/2026-08-21-perf-memory-race-remediation.md`
implemented and merged (2026-08-21)** — 6 engine findings below plus the 7
re-confirmed backlog items, each marked **Remediated** with its commit and
verification. Implemented by 13 parallel agents (one per task, isolated
working copies), integrated onto one branch, re-verified end to end with the
full test suite (2357 passed) and `./scripts/ci.sh` (alembic single head at
`0033`, clean upgrade→downgrade→upgrade round trip, ruff clean, coverage
78.22%), plus one cross-task regression caught only by that full-suite run
(a `tests/integration/test_proposal_review.py` helper asserting Task 1's
memory-synthesis call ran synchronously — fixed by draining the queue before
the assertion, since that file wasn't named in Task 1's own regression list).
See each finding's **Remediated** note for its specific commit and evidence.
Two of the six harnesses were also re-run live against this checkout after
merging (harness C and harness D): per-tick MessageLog scan time is
**0.2/0.6/1.3 ms** at 10k/50k/100k entries (was 67/354/701 ms), and 8
Anthropic-client calls through the shared client take **351 ms** vs **7791 ms**
through the old per-call-construction path.

Scope: the whole repo at working-tree HEAD (`f6f436b` + uncommitted docs), with
emphasis on everything that changed since the last full adversarial pass was
re-verified on 2026-08-11 (issues #20–#27): the two-lane concurrent scheduler,
the specialist panel, the assessment sidecar capture, the LLM truncation-retry
work, and the rubric extraction.

**Method.** Every NEW finding below was verified by execution — a harness that
drives the real production code (real `SimulationEngine`, real `LockRegistry`,
real `AgentSlackClient._api/_call_with_retry`, real `MessageLog`, real
`src/services/llm.py`) with only the innermost network calls replaced by fakes
that perform genuine `await asyncio.sleep` / `time.sleep`, mirroring the
methodology `tests/integration/test_concurrent_thread_safety.py` documents.
Harnesses are in `harnesses/` beside this file; each prints the measurement
quoted here. They were run against a byte-identical copy of this working tree
(rsync, `.env` replaced with inert values) using a venv built from this
`pyproject.toml` (Python 3.12.3, anthropic 0.120.2, SQLAlchemy 2.0.51).

Run any of them with:

```bash
.venv-test/bin/python docs/audits/2026-08-21-perf-memory-race/harnesses/<name>.py
```

(cwd must be the repo root; they need no DB, no Slack, no network, no API key.)

GitHub issues #20–#27 (the `verified-2026-07-30` backlog) were read first as
required context. Items from that backlog are NOT re-reported here except in
§7, which records which of the perf/race items among them are still present at
today's HEAD (code-confirmed now; their execution evidence is the prior
audit's).

---

## 1. HIGH — `_close_thread` runs two LLM calls inside the thread lock, both
##    agents' locks, and a reply-lane semaphore slot; in the star topology this
##    serializes every close on the hub and starves the whole reply lane

**Where:** `src/agent/simulation.py::_close_thread` (`:2032–2115`) — the two
`await self._update_agent_memory(...)` calls at `:2110`/`:2115` execute inside
`self._agent_locks.acquire_all(agent, other_agent)`, which itself runs under
the reply lane's per-thread lock and semaphore slot
(`_dispatch_reply_lane._run`, `:1271`/`:1308`). Each `_update_agent_memory` is
a real billed LLM call (`:6092`, `max_tokens=2600`; production measurements in
the code itself put a single memory synthesis at 715–1,646 output tokens, i.e.
tens of seconds of wall time on Sonnet 5).

**Why it convoys:** every interview is (hub, PI), so every close acquires the
**hub's** agent lock — no two closes can ever overlap. Each blocked close is
already inside the semaphore (`reply_lane_max_in_flight = 4`,
`config.py:342`), so blocked closes consume the lane's whole concurrency
budget. Worse (found by the instrumented run): the closing threads' own
partner-side reply pairs are also scheduled by `_pending_reply_pairs`, take
each freed semaphore slot first, and then block on the still-held *thread*
locks — so even slots freed by a finished close do not reach unrelated agents.

**Measured** (`harness_a_close_convoy.py`, MEM_LATENCY=1.0 s standing in for
one memory call, 4 pending concludes + 2 unrelated reply pairs, cap 4):

```
DEFECT RUN   total wall 8.02s  (ideal if closes were concurrent: 2.2s → 3.6x)
             memory calls strictly sequential: hub, wang0, hub, wang1, ...
             first UNRELATED reply started at 6.01s — 30x its own 0.2s duration
CONTROL RUN  (memory synthesis outside the lock span) total wall 0.20s,
             unrelated replies start at 0.00s
```

`harness_a2_instrumented.py` shows the slot-stealing mechanism directly
(partner pairs acquire freed slots at 2.006 s / 4.011 s and sit blocked on the
thread locks until the corresponding close finishes).

**Production translation:** with real memory-call latencies (~10–30 s each,
two per close), four concluding interviews in one sweep monopolize the reply
lane for **2–5 minutes**, during which no other interview can progress. The
existing concurrency tests check correctness (no deadlock, no lost update,
one verdict) but never throughput — which is why this shipped green.

**Fix direction:** move the two `_update_agent_memory` awaits out of the
agent-lock block (they mutate no `active_threads` state — they read the log,
call the LLM, and write memory files/revisions), e.g. collect the events under
the lock and fire the syntheses after `_close_thread` releases it — or queue
them for the main loop. Keep the DB `ThreadDecision` write where it is.

**Remediated** (`docs/plans/2026-08-21-perf-memory-race-remediation.md` Task 1,
commit `d1162d1`): `_close_thread` now enqueues both memory events into
`_pending_memory_events` instead of awaiting them under the lock; a new
`_drain_memory_events` runs them sequentially (preserving the lost-update
guarantee) from the main loop after dispatch and, bounded, from `stop()`.
Prototype-verified before implementation at 8.02s → 0.207s dispatch wall time
with zero memory calls in the dispatch span (see "Prototype verification" in
the RCA doc); the adversarial audit that implemented this also added a
regression test (`test_sequential_drain_prevents_a_lost_memory_update`)
driving two concurrent drains against an interleave-detecting fake.

---

## 2. MEDIUM-HIGH — synchronous Slack HTTP (and `time.sleep` retry backoff)
##    still runs on the event loop in three live paths

**Where:**
- `simulation.py:4196` — `client.is_bot_user(user_id)` inside the async
  `_poll_slack_for_bot_messages`: one synchronous `users.info` round trip **on
  the loop** per human-authored channel message (no per-user cache, so the
  same human posting N times costs N calls);
- `simulation.py:1410` — `client.join_channel(ch_id)` in Phase 1;
- `simulation.py:5647/:5695` — `client.connect()` (sync `auth.test`) in the
  ~30 s roster sync for adopted/new agents — a token that authenticates slowly
  or flaps re-blocks the loop every sync;
- all of these route through `_call_with_retry` (`slack_client.py:327–353`),
  whose 429 handling is `time.sleep(Retry-After)` × `MAX_RETRIES=3` — on the
  loop thread when the caller was sync-in-async.

The `a*` wrappers (`apost_message` etc., `slack_client.py:1167–1186`) exist
precisely to prevent this and are used by the poll/post paths — these are the
call sites that never got the treatment.

**Measured** (`harness_b_loop_stall.py`, real engine poller + real
`AgentSlackClient` retry path, stub `WebClient` with 0.3 s RTT):

```
Case 1: 5 human messages          -> ONE continuous 1.50s event-loop stall
Case 2: one rate-limited users.info (Retry-After: 2, 3 attempts)
                                  -> ONE continuous 6.00s event-loop stall
```

During the stall every in-flight reply, every poller/flush, and the asyncio
SIGTERM handler are frozen — the same class of defect as issue #24's C2, in
the agent process instead of the web one, and it directly eats into the
`docker stop -t 420` grace budget.

**Fix direction:** wrap the three call sites in `asyncio.to_thread` (the
pattern the same file already uses eleven lines lower); cache `users.info`
results per user id.

**Remediated** (Task 2, commit `af969de`): `is_bot_user` now caches successful
lookups only (never a `SlackApiError` failure); `AgentSlackClient` gained
`ais_bot_user`/`ajoin_channel`/`aconnect` `to_thread` wrappers; the poller,
`_phase1_channel_discovery` (now `async def`), and both roster-sync
`connect()` sites all route through them. Pinned by
`test_poller_is_bot_lookup_does_not_block_the_loop` (asserts no event-loop gap
&gt; 0.25s during a 5-message poll with a 0.3s-per-lookup stub) and
`test_is_bot_user_caches_successes_but_never_failures`.

---

## 3. MEDIUM (grows with run age) — every MessageLog read is a synchronous
##    O(n) scan on the event loop, executed dozens of times per tick, over an
##    append-only list that is never pruned in-process

**Where:** `src/agent/message_log.py` — every read iterates `self._entries`
in full. Per main-loop tick, `_dispatch_reply_lane` runs
`_phase3_activate_threads` for every agent (3 gated reads each) plus
`_pending_reply_pairs` (one `has_new_reply_from_other` scan per active
thread). Closed threads' entries are never removed in-process; a restart
reloads up to `REBUILD_WINDOW_S` = **14 days** of history plus all undecided
threads (`simulation.py:4702`).

**Measured** (`harness_c_log_scaling.py`, 12 agents × 3 active threads, real
engine methods, best-of-5):

```
n= 10,000 entries | ~ 3.8 MB RAM | per-tick scan total  67 ms (sync, on the loop)
n= 50,000 entries | ~19.7 MB RAM | per-tick scan total 354 ms
n=100,000 entries | ~39.4 MB RAM | per-tick scan total 701 ms
```

Linear in log size, paid every tick, on the loop thread — at 100 k messages
the engine stalls ~0.7 s per tick before doing any real work. RAM itself is
modest; the scan time is the cost. (The `_update_agent_memory` context filter
is another full scan per close: 4.6 ms at 100 k.)

**Fix direction:** index the log — per-thread entry lists (`get_thread_history`
/ `has_new_reply_from_other` become O(thread)) and a per-channel/top-level
index for the Phase-3 reads; or prune closed-thread entries in-process after
their `ThreadDecision` lands (they are already durable in the DB).

**Remediated** (Task 3, commit `5daa434`): all eight read methods now use
maintained indexes (`_by_thread`, `_by_time`, `_top_level_by_sender`,
`_last_bot_in_channel`, etc.) instead of scanning `self._entries`. Re-measured
against this harness after the fix: per-tick totals dropped from 68.0/366.7/
731.3 ms to **0.2/0.9/3.0 ms** at 10k/50k/100k entries — a ~240x improvement
at 100k. A new differential test
(`tests/unit/test_message_log_differential.py`) pins every method's output —
including insertion-order and tie-break semantics — against a verbatim port
of the pre-index bodies over a randomized, out-of-order log.

---

## 4. LOW-MEDIUM — a fresh `anthropic.Anthropic()` (fresh httpx pool) is
##    constructed for every single LLM call: zero connection reuse

**Where:** `src/services/llm.py::get_anthropic_client` is called inside
`generate_agent_response`, `generate_with_tools` and `synthesize_profile` on
every invocation. Each client owns its own `httpx.Client`; abandoned clients
(and their open keep-alive sockets) linger until GC.

**Measured** (`harness_d_client_churn.py`, SDK pointed at a local
connection-counting server, real llm.py code path):

```
8 calls through the production path -> 8 TCP connections
8 calls through ONE shared client   -> 1 TCP connection
```

Against `api.anthropic.com` each of those extra connections is a full TCP+TLS
handshake on every thread reply, every specialist consult (up to 8 per
concluding turn), every memory update and every truncation retry — plus file
-descriptor churn under the 4-way reply-lane concurrency.

**Fix direction:** one module-level client (or `functools.lru_cache` on
`get_anthropic_client`), keyed on the API key. The SDK's sync client is
thread-safe; nothing else changes.

**Remediated** (Task 4, commit `60a5245`): `get_anthropic_client()` now
returns `_client_for_key(settings.anthropic_api_key)`, a `functools.lru_cache`
wrapping `anthropic.Anthropic(api_key=...)`. Pinned by
`test_get_anthropic_client_reuses_one_instance_per_key`; the full suite was
re-run once after this change (2303 passed) to confirm no test relies on a
fresh client per call.

---

## 5. LOW — per-interview state that is never released: lock registry, closed
##    -ids set, prior-thread list, message log

**Where:** `src/agent/locks.py::LockRegistry` has no eviction path;
`_closed_thread_ids` is insert-only (deliberate — see `:2158`);
`_prior_threads` appends one dict per close with no cap (`:2038`); MessageLog
entries for closed threads are never pruned (§3).

**Measured** (`harness_e_memory_growth.py`, 2,000 full interview lifecycles
through the real dispatch → system-enforced close path):

```
_thread_locks registry: 2,000 locks   _closed_thread_ids: 2,000 ids
_prior_threads[pair]:   2,000 dicts   MessageLog:        24,000 entries
total retained heap: 14.7 MB  (7.3 KB per closed interview, forever)
```

At ~7 KB per interview this is not an OOM risk on its own — it is reported
because it is unbounded by anything except process lifetime, and because item
§6 turns one of these structures into a per-call token cost.

**Remediated** (Task 5, commit `74ef5f2`): `LockRegistry` now evicts a key
only at refcount zero (intent registered synchronously before the first
`await` in `acquire_all`), never on a naive `not lock.locked()` check, which
provably splits mutual exclusion. `_prior_threads` is capped at 50 entries per
pair at both write sites (`_close_thread` and the DB-rebuild path);
`_closed_thread_ids` remains deliberately insert-only. Pinned by
`tests/unit/test_lock_registry.py` — including a 4th test,
`test_eviction_never_splits_mutual_exclusion_for_a_late_arrival`, added
because the plan's own `test_eviction_never_splits_mutual_exclusion` does not
actually exercise the trap (a naive eviction passes it 5/5 times; only a task
that calls `acquire_all` *after* eviction while an earlier waiter is still
parked reproduces the bug).

---

## 6. LOW-MEDIUM (cost) — `_prior_threads` renders 1:1 into every subsequent
##    Phase-5 prompt, uncapped

**Where:** `_get_prior_threads_for_agent` (`simulation.py:2349`) returns every
recorded close for every pair with no limit, and `Agent.build_phase5_prompt`
(`agent.py:499–516`) renders one line per entry (summaries clipped at 400
chars at write time, `:2041`, but the *list* is never clipped). Issue #20's
COR-7 flagged the missing dedup; the unbounded length is the sharper edge.

**Measured** (`harness_f_prompt_bloat.py`, real render path,
summary-carrying closes):

```
 10 closes with one pair -> phase-5 prompt  34,402 chars (~ 8,600 tokens)
100 closes               ->                 56,272 chars (~14,068 tokens)
500 closes               ->                153,472 chars (~38,368 tokens)
```

Every summary-carrying close adds ~240 chars (~60 input tokens) to *every
future* Phase-5 call by both agents in the pair, forever (pitch-only closes
without summaries add ~25 chars each). This compounds with restart rebuilds
(`_rebuild_state_from_db` re-closes every `ThreadDecision` thread).

**Fix direction:** render only the most recent K per pair (K≈5) plus a count.

**Remediated** (Task 6, commit `b33e48a`): `build_phase5_prompt` now renders
at most `PRIOR_THREADS_RENDERED_PER_PAIR = 5` entries per partner, with a
"(N earlier closed threads with this agent not shown)" line when truncated.
The golden-master snapshot (`test_agent_turn_gm.ambr`) needed no
regeneration — its fixture renders fewer than 5 prior threads, so the change
was invisible to it, exactly as predicted.

---

## 7. Previously-catalogued perf/race items re-confirmed still present at HEAD

Code-confirmed today (execution evidence is the prior audit's, per issues
#20–#27, all still open):

- **#24 V5** — `waitlist_submit` is still SELECT-then-INSERT with no
  `IntegrityError` catch (`public.py:485–503`); two concurrent first-time
  signups race and one 500s. The vote endpoint's correct pattern is at
  `public.py:1041`.
  **Remediated** (Task 7, commit `dfdfd7a`): both `waitlist_submit` and
  `review_proposal` now catch `IntegrityError`, roll back, and treat the row
  the winning racer created as the answer. Pinned by two new race tests in
  `tests/integration/test_concurrent_web_writes.py`; the `review_proposal`
  fix required widening the `try` to cover `record_engagement`/
  `mark_notification_responded` too, since SQLAlchemy's autoflush surfaces the
  `IntegrityError` there, not at the later `commit()` — found by reproducing
  the failure with a full traceback rather than assuming the plan's literal
  placement was right.
- **#24 C2** — admin Slack provisioning still awaits synchronous
  `httpx.post` + `time.sleep` retry loops on the web event loop
  (`admin.py:927/:960` → `slack_provisioning.py:125–146`): up to ~5 min
  single-worker freeze, 504 via nginx's 120 s `proxy_read_timeout`.
  **Remediated** (Task 8, commit `40bd654`): `create_app`, `lookup_team_id`,
  `exchange_code`, and `rotate_config_token` are all wrapped in
  `asyncio.to_thread`, and `_config_token`'s transaction is committed before
  the manifest call so the web pool's connection isn't held across it. Pinned
  by `tests/integration/test_provisioning_loop.py`'s heartbeat-based
  loop-freeze detector.
- **#25 P3** — the **web** engine still has `pool_size=5, max_overflow=10`,
  no `pool_pre_ping`/`pool_recycle` (`src/database.py:17–22`). Note the
  **agent** process fixed its own copy (`agent/main.py:161–165`:
  `db_pool_size=25`, `pool_pre_ping=True`) — the fix exists in-repo and was
  simply never applied to the web tier. The worker also uses a bare default
  engine (`worker/main.py:119`).
  **Remediated** (Task 9, commit `3d10af5`): `pool_pre_ping=True` and
  `pool_recycle=1800` added to the web engine. Note the correction from the
  original write-up: the agent process's engine only ever carried
  `pool_pre_ping` — `pool_recycle` is a new addition on its own merits, not a
  transplant (verified via `grep` of `agent/main.py`; this doc previously
  implied both were already there). The worker's bare default engine
  (`worker/main.py:119`) remains unaddressed — out of this plan's scope.
- **#25 P1** — `AgentBadgeMiddleware` still runs its per-request COUNTs with
  no `/static` short-circuit (`main.py:28+`), and the `(agent_a|agent_b,
  outcome)` composite indexes still don't exist in any migration.
  **Remediated** (Task 10, commit `3d42947`): the middleware now
  short-circuits `/static/*` and `/api/health` before touching settings or
  the DB; migration `0033` adds the two `thread_decisions` composite indexes
  plus 18 previously-unindexed FK columns (re-verified against current models
  during implementation; one, `agent_delegates`, shares a source file with
  `delegate_invitations` and needed care to avoid mixing up the two classes'
  columns). `./scripts/ci.sh` was run to completion as part of this task
  (alembic single head at `0033`, clean upgrade→downgrade→upgrade round trip).
- **#22 C1** — the four `profile_version = (x or 0) + 1` read-modify-write
  sites are all still present (`profile_pipeline.py:416`, `profile.py:159`,
  `onboarding.py:182`, `agent_page.py:955`), as are the
  `delegate_slack_ids` whole-array reassign races.
  **Remediated** (Task 13, commit `82a8520`): all four `profile_version`
  sites now use `func.coalesce(ResearcherProfile.profile_version, 0) + 1`
  (SQL-side, race-free); the pipeline site adds an explicit
  `db.refresh(profile, ["profile_version"])` after a flush, since the
  expression assignment expires the attribute and the pipeline logs it right
  after (would otherwise raise `MissingGreenlet`). All three `delegate_slack_ids`
  sites (two appends, one removal) now use a guarded, atomic
  `array_append`/`array_remove` in SQL instead of read-modify-reassign.
  **A plan gap found and fixed during implementation:** applying the
  SQL-side increment literally at the three *router* sites, on a profile that
  was just `db.add()`-ed but not yet flushed, generates invalid SQL (a
  self-referencing `INSERT ... VALUES`) — proved against real Postgres before
  implementing. Fixed by flushing right after `db.add(profile)` at all three
  sites; without this fix, a PI's first-ever profile save would 500.
- **#23 V9** — `pubmed.py:73` still holds `Semaphore(8)` with the pacing
  sleep *inside* the semaphore (`:95`), i.e. up to ~8 req/s against NCBI's
  keyless 3/s limit; still no retry/backoff in orcid/pubmed/grants.
  **Remediated** (Task 11, commit `543e325`): concurrency (`Semaphore(3)`)
  and rate (`_pace()`, spacing request *starts* via a module-level float and
  `loop.time()` — no lock object, so it doesn't bind to one event loop across
  pytest's per-test loops) are now separate mechanisms; `_ncbi_get` retries
  429/500/502/503 up to twice with exponential backoff, pacing before
  `raise_for_status` so a 429 doesn't skip pacing on the way out.
- **#23 COR-30** — `tools.py:269/:280` still debit the per-thread
  abstract/full-text budget before the fetch, with no refund on failure.
  **Remediated** (Task 12, commit `6a057c4`): `retrieve_abstract`/
  `retrieve_full_text` now fetch first and only increment the budget counter
  after a fetch that didn't error; the cap check itself still runs before the
  fetch, so an over-budget call is refused without a network round trip.

Improvements verified in passing (fixed since those issues were filed): the
LLM-log/message flush buffers now snapshot-and-requeue instead of dropping on
failure (old COR-11); Slack pagination and the retry `UnboundLocalError` are
fixed; the agent process's DB pool is sized for the concurrency (25 + margin)
and pre-pings.

## What was NOT found

Targeted adversarial checks that came back clean: the flush paths
(`_flush_persisted` / `_flush_llm_logs` / `_flush_pending_assessments`) are
snapshot-then-requeue and cannot duplicate or drop rows across concurrent
invocations on one loop; `LockRegistry.acquire_all`'s sorted-order acquisition
composes correctly across `_close_thread`/`_evict_dead_thread` (no circular
wait constructible); `try_reserve`/`record_api_call` double-booking is
correctly avoided via `already_reserved`; the assessment sidecar capture's
one-row invariant held under the concurrency tests' scenarios; the new admin
panel/assessment pages batch their queries (`thread_panel.py` uses one
`IN (...)` query — no N+1).

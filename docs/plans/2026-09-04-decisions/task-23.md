# Task 23 — #23 COR-29a/c: the retry budget, NCBI's pacing ceiling, and the per-loop semaphore

Task 23 is a **FIX**, not a DECIDE. This file exists because the plan's Step 4 asks for a measured
before/after, and because two of the plan's own factual claims about the hazard turned out to be
wrong. Both corrections are things Task 32/33 has to carry.

## Ruling

Three changes, all inside the two files the task owns.

**1. A total retry budget (`deadline`), opted into by `pubmed` only.** `get_with_retry` /
`post_with_retry` grow `deadline: float | None = None`. `None` is the default, so `orcid.py` and
`grants.py` are byte-for-byte unchanged in behaviour. `pubmed._ncbi_get` passes
`_NCBI_RETRY_DEADLINE_SECONDS = 120.0`.

The deadline bounds the **retry budget**, not one request: it stops the next attempt starting and
never cancels one in flight. That is deliberate and was checked against every caller. Every one of
them handles failure with `except Exception` (`profile_pipeline.py:145/174/287`,
`pubmed.fetch_pubmed_records`, `orcid.fetch_orcid_grants/works`, `agent/tools.py:157`), and
`asyncio.CancelledError` is a `BaseException` in Python 3.8+, so cancelling an in-flight request
would convert a caught, diagnosable `HTTPStatusError`/`TransportError` into an uncaught error that
escapes all of them — and, inside `simulation.py:1460`'s `gather`, would be indistinguishable from
real task cancellation. So the honest bound for one logical call is **`deadline` + one client
timeout = 180 s**, not a flat 120 s, and the docstring says so rather than claiming a flat deadline.

Rejected: a module-wide default deadline for all three clients. The measured harm is pubmed's
per-item sequential loop; ORCID and Grants.gov are called a bounded number of times per job. Giving
them a ceiling with no evidence is exactly the over-implementation pattern this branch is closing.

**2. Pacing at NCBI's ceiling.** Keyed `0.12 s → 0.105 s`. Keyless `0.34 s` unchanged.

**3. Per-loop semaphores.** `_NCBI_SEMAPHORES` (a module singleton) becomes
`_NCBI_SEMAPHORE_SIZES` plus `_ncbi_semaphore(has_key)`, which resolves a semaphore from a
`weakref.WeakKeyDictionary` keyed on `asyncio.get_running_loop()`. The `HAZARD` comment is deleted
because the hazard is gone, and the autouse fixture in `test_pubmed_contract.py` drops its per-test
rebinding — the test-only mitigation that comment pointed at.

The slot is now taken **per attempt** (`get_with_retry`'s new `attempt_context`) instead of once
around the whole retry loop. Which mechanism enforces which property after that change:

| property | enforced by |
|---|---|
| at most N NCBI requests in flight at once, per event loop | the semaphore. A retry must **re-acquire** before it may send anything, so a released slot cannot raise the in-flight count above N. A caller sleeping out a backoff is issuing no request and is by definition not in flight — excluding it from the count is the correction, not a loophole. |
| at most `1/interval` request **starts** per second, process-wide | `_pace_ncbi`. It is a plain module float advanced by a non-`await` read-modify-write, bound to no loop, and it is re-entered on every attempt via `before_request`. Making the semaphore per-loop does not weaken it: two loops would get 8 slots each, but the shared cursor still bounds the **rate**, which is the thing NCBI's policy actually limits. |

`attempt_context` wraps `before_request` *and* the request, not just the request. That preserves the
existing acquire-then-pace order: a pacing reservation is booked only after admission, so a caller
blocked on a slot cannot drift past a start time it had already reserved.

## Evidence

### Pacing arithmetic (before → after)

| path | policy ceiling | before | after |
|---|---|---|---|
| keyed (`api_key` set) | 10 req/s | `1/0.12` = **8.33 req/s** (83.3 %) | `1/0.105` = **9.52 req/s** (95.2 %) |
| keyless | 3 req/s | `1/0.34` = **2.94 req/s** (98.0 %) | unchanged |

At or just under, never over: `_pace_ncbi` only ever *delays* a start (`start = max(now,
_ncbi_next_start)`), so the achieved rate is at most `1/interval`. The residual ~5 % is deliberate —
NCBI counts arrivals and we can only space departures, and an IP block costs far more than the
throughput it buys back. Keyless was left alone: it is already within 2 % of its ceiling, and it is
the path NCBI polices hardest.

### Retry budget (before → after), for one logical `_ncbi_get`

| scenario (60 s client timeout, `retries=3`, `backoff=0.5`) | before | after |
|---|---|---|
| hung NCBI, every attempt burning the full timeout | 4 attempts, 243.5 s | 2 attempts, 120.5 s |
| `Retry-After: 60` storm (the header is honoured up to `cap=60.0`) | 4 attempts, 420 s | 2 attempts |
| fast failures (nothing hangs) | 4 attempts | 4 attempts — unchanged |

This bounds each **call**, not the pipeline: `convert_dois_to_pmids` still issues one call per
unresolved DOI in a sequential loop, so the aggregate worst case is 180 s × DOI count instead of
420 s × DOI count. A loop-level budget is the full fix and is not in this task's clause.

### The concurrency measurement the plan asked for (Step 4) — and two corrections

**The plan's claim that `src/agent/tools.py:9` puts pubmed on the grantbot scheduler's path is
false.** `src/agent/grantbot.py` does not import `src/agent/tools.py`, and neither it nor any of its
seven runtime-lazy imports (`src.services.llm`, `slack_web`, `slack_tokens`, `foa_cache`,
`pi_inbox`, `src.agent.ids`, `src.models`) reaches `src.services.pubmed`:

```bash
.venv-test/bin/python -c "
import sys, src.agent.grantbot
import src.agent.ids, src.models, src.services.pi_inbox, src.services.slack_web
import src.services.llm, src.services.slack_tokens, src.agent.foa_cache
print('pubmed' , 'src.services.pubmed' in sys.modules)   # -> False
print('tools'  , 'src.agent.tools'     in sys.modules)"  # -> False
```

**So the peak concurrent keyed NCBI call count a real grantbot day reaches is 0.** The grantbot
scheduler (`grantbot.py:728`, `asyncio.run` inside `while True:` — a real multi-loop process, and
the prod `grantbot` service's actual command) never constructs an NCBI semaphore at all.

The processes that *do* call NCBI were then measured separately, against the disposable production
copy `copi-prodtest-db` (42 simulation runs, `llm_call_logs` 2026-03-22 → 2026-09-04, 62,291 rows,
2,791 of them carrying a real `retrieve_abstract`/`retrieve_full_text` `tool_use` block):

- **`worker`** — one `asyncio.run`, one job claimed at a time (`worker/main.py:265-269`), every NCBI
  call a sequential `await`. Peak concurrency **1**.
- **`app`** (uvicorn) — one long-lived loop, and no route reaches `run_profile_pipeline`.
- **`agent`** (`src/agent/main.py:56`) — the only fan-out in `src/` is `simulation.py:1460`'s
  `asyncio.gather` over Phase-4 thread replies, whose tool loop reaches pubmed. Measured max
  distinct threads one agent replied to inside a 5 s window: **21** (`wiseman`, `su`). But the peak
  number of those replies that actually invoked a pubmed tool, per agent, was **1** in any 5 s
  window, **2** in 30 s and **4** in 60 s — never the 8 the keyed semaphore would need. And this
  process calls `asyncio.run()` exactly **once**.
- **`src/cli.py:110` (`seed-profiles`)** is a second genuine multi-`asyncio.run` process — one loop
  per ORCID — but it touches ORCID only, never pubmed.

```sql
-- peak pubmed-tool invocations by one agent inside a W-second window (W = 5, 15, 30, 60)
with r as (
  select agent_id, simulation_run_id, created_at, id from llm_call_logs
  where phase='thread_reply'
    and (messages_json::text ~ '"name": ?"retrieve_abstract"'
      or messages_json::text ~ '"name": ?"retrieve_full_text"'))
select max(c) from (
  select a.id, count(*) c from r a join r b
    on a.agent_id=b.agent_id and a.simulation_run_id=b.simulation_run_id
   and b.created_at >= a.created_at
   and b.created_at <  a.created_at + interval 'W seconds'
  group by 1) s;   -- 1, 1, 2, 4
```

**Verdict: the cross-loop hazard is structurally present but not reachable in production today.** It
needs two conditions in one process — more than one `asyncio.run()`, and ≥ 8 concurrent keyed NCBI
calls — and no process has both. The two multi-loop processes (grantbot scheduler, `cli
seed-profiles`) never import pubmed; the one process with real NCBI fan-out (the simulation) runs a
single loop and peaks at ~1 concurrent NCBI call. The fix is still worth landing: it is cheap, it
deletes a documented footgun that only stayed harmless by accident of which modules import which,
and it removes a test-only mitigation that was silently load-bearing — with the fixture's per-test
rebinding gone, the *pre-existing* `test_ncbi_pacing_spaces_concurrent_starts` fails against the
pre-fix module with `RuntimeError: <asyncio.locks.Semaphore ...> is bound to a different event
loop`, which is the defect reproducing itself inside the suite.

**Semaphore loop-binding, verified rather than assumed:** `Semaphore.acquire` reaches
`self._get_loop()` only on the branch where it has to wait, in both CPython 3.11 (the image) and
3.12 (`.venv-test`) — `inspect.getsource(asyncio.locks.Semaphore.acquire)` on each. So an
uncontended singleton never binds, which is exactly why the hazard could sit in `src` for months
without a single failure.

### Red-first

Run against the **pre-fix** (`HEAD`) copies of both modules, loaded under their real names by an
out-of-tree conftest — the working tree was never reverted.

| test | pre-fix failure |
|---|---|
| `test_the_ncbi_semaphore_is_not_shared_across_event_loops` | `RuntimeError: <asyncio.locks.Semaphore object ...> is bound to a different event loop` |
| `test_ncbi_pacing_spaces_concurrent_starts` (pre-existing, once the fixture's rebind is gone) | same `RuntimeError` |
| `test_a_hung_ncbi_stops_retrying_once_its_call_budget_is_spent` | `assert 4 == 2` |
| `test_ncbi_pacing_lands_at_the_policy_ceiling_not_below_it` | `AssertionError: 8.333333333333334` / `assert 9.5 <= 8.333333333333334` |
| `test_semaphores_are_sized_by_api_key_presence` (pre-existing, renamed constant) | `AttributeError: module 'src.services.pubmed' has no attribute '_NCBI_SEMAPHORE_SIZES'` |
| `test_a_total_deadline_stops_the_retry_loop_before_the_attempt_budget` | `TypeError: get_with_retry() got an unexpected keyword argument 'deadline'` |
| `test_a_total_deadline_bounds_transport_error_retries_too` | same `TypeError` |
| `test_post_honours_the_same_total_deadline` | `TypeError: post_with_retry() got an unexpected keyword argument 'deadline'` |
| `test_the_attempt_slot_is_taken_per_attempt_and_still_caps_concurrency` | `TypeError: get_with_retry() got an unexpected keyword argument 'attempt_context'` |

The last four are red for a *new parameter*, which is the only failure a new seam can produce; the
behaviour those parameters buy is pinned behaviourally by the pubmed-level test above (`4 == 2`).

**No wall-clock thresholds anywhere.** Other agents run pytest concurrently in this checkout, so
every new assertion is a count or an ordering: the budget tests drive a hand-advanced clock
installed over `http_retry`'s own `time` name (nothing outside the module sees it), the pacing test
is arithmetic on the shipped constants, and the slot test asserts enter/exit counts plus a
peak-concurrency watermark.

**No real network.** The four target files were run once under an out-of-tree pytest plugin that
turns any non-loopback `socket.connect`/`connect_ex`/`getaddrinfo` into an `AssertionError`:
`64 passed`.

## Consequence a closing comment must state

1. **#23's over-impl R4 second half is fixed and measurable:** the keyed NCBI path now paces at
   9.52 req/s against a 10 req/s policy ceiling (was 8.33), and the semaphore slot is released
   across backoff instead of held for the whole retry loop.
2. **The plan's justification for closure-23 R7 was wrong in one factual detail, and the closing
   comment must not repeat it.** `src/agent/grantbot.py` does not import `src/agent/tools.py`, so
   the grantbot scheduler — the one long-running process that calls `asyncio.run()` repeatedly —
   never touches the NCBI semaphores. Peak concurrent keyed NCBI calls in a real grantbot day: **0**.
   The hazard was structurally present, never reachable; it is now removed outright.
3. **The retry budget bounds a call, not the pipeline.** `convert_dois_to_pmids` still issues one
   NCBI call per unresolved DOI sequentially, so a sustained NCBI outage still costs 180 s × DOI
   count inside one profile job (down from 420 s × DOI count). `worker/main.py`'s
   `JOB_STALE_PROCESSING_THRESHOLD_SECONDS = 3600` stopgap therefore still stands, and its comment's
   "≈243s per `_ncbi_get`" figure is now an over-estimate (120.5 s) — the direction is safe, so it
   was left alone rather than edited from a file this task does not own. A loop-level budget is a
   follow-up.
4. **The deliberate trade:** under a sustained NCBI brownout a profile job now gives up on an
   individual lookup sooner, so a profile synthesized during an outage may carry fewer publications.
   That was already possible after 4 attempts; the change makes it likelier, in exchange for a job
   that terminates. `Job.max_attempts = 3` means the job itself is retried later regardless.

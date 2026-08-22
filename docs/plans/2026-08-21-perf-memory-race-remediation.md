# Perf/Memory/Race Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the six execution-verified engine defects and the seven still-open backlog perf/race items catalogued by the 2026-08-21 audit, without breaking the concurrency invariants the current code provably protects.

**Architecture:** Three independent workstreams. WS-A (Tasks 1–6) reworks the agent engine: memory-synthesis calls move out of the lock/semaphore span into a FIFO queue drained by the main loop; the remaining sync Slack calls move off the event loop; MessageLog gets read indexes; the Anthropic client, lock registry, and prior-thread context get lifecycle bounds. WS-B (Tasks 7–10) hardens the web tier: concurrent-insert races, blocking provisioning I/O, pool settings, badge middleware + migration 0033. WS-C (Tasks 11–13) fixes external-transport pacing and the two write-accounting defects.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy 2 (async/asyncpg), FastAPI/Starlette, slack_sdk (sync WebClient + `asyncio.to_thread`), httpx, Alembic, pytest + testcontainers.

**Spec:** `docs/audits/2026-08-21-perf-memory-race/README.md` (findings + execution evidence) and `docs/audits/2026-08-21-perf-memory-race/rca-and-fix-audit.md` (root causes, fix invariants, and the naive-fix traps each task below guards against). Read both before starting any task.

**Pre-verified designs:** Task 1's queue and Task 5's refcounted registry were prototyped and executed against a byte-identical copy of this tree before this plan was finalized — convoy 8.0s → 0.207s with zero memory calls in the dispatch span; lost-update, shutdown-bound, mutual-exclusion-across-eviction, and cap invariants all held; the existing reply-lane/simulation-logic/concurrency suites stayed green (121 + 8 passed), and the one test this plan deletes failed for exactly the predicted reason. See the RCA doc's "Prototype verification" section and `docs/audits/2026-08-21-perf-memory-race/harnesses/verify_fix_prototype.py` (note: it expects the Task 1/Task 5 code to be present; against the unmodified tree it fails at check A by design). Implement from THIS plan's code blocks, not by copying the prototype's abbreviated docstrings.

**Realigned 2026-08-21 (post-authoring adversarial audit), after the independent "Empty-Reply Data Loss (E1)" plan landed 5 commits on top of this plan's `f6f436b` baseline (`18cd527`, `3844b9a`, `9d0c5e5`, `a2cd916`, `2ef6361` — all present at current HEAD).** Verified by diffing `f6f436b..HEAD`: those commits touch only `src/agent/simulation.py` (a new empty-reply back-off block inserted well before `_close_thread`, ~35 lines) and `src/services/llm.py` (`_first_text`→`_all_text`, `_log_empty_reply`) — neither one changes any function this plan modifies; they only push line numbers in `simulation.py` down by ~35–38 for everything after the insertion point. Every other file this plan touches (`message_log.py`, `locks.py`, `agent.py`, `slack_client.py`, `tools.py`, `public.py`, `agent_page.py`, `admin_provisioning.py`, `database.py`, `main.py`, `models/agent_activity.py`, `pubmed.py`, `profile_pipeline.py`, `profile.py`, `onboarding.py`, `invite.py`) is untouched since `f6f436b` — spot-checked line-for-line against current HEAD, all exact. Line numbers in Tasks 1 and 2 were corrected in place; everywhere else in this plan is accurate as originally written. Also corrected in this pass: Task 9's `pool_recycle` comment (and the RCA doc's §F7) wrongly claimed the agent process's engine already carries `pool_recycle` — it only carries `pool_pre_ping`; `pool_recycle=1800` is a new addition on its own merits, not a transplant. No other line numbers, code blocks, or invariants in this plan needed correction — implementers should still `grep` before editing rather than trust any absolute line number, per the plan's existing convention.

**IMPLEMENTED 2026-08-21.** All 13 tasks landed via 13 parallel agents (one per task, each in an isolated working copy off this plan's baseline), integrated onto one branch, and merged onto `blackbird` as 14 commits (13 tasks + one cross-task regression fix — see below). Full test suite: 2357 passed. `./scripts/ci.sh`: passed end to end (alembic single head at `0033`, clean upgrade→downgrade→upgrade round trip, ruff clean on tests, `src/` ruff ratchet held, coverage 78.22% against the 60% floor). See `docs/audits/2026-08-21-perf-memory-race/README.md` for a per-finding "Remediated" line with each fixing commit.

Three things the individual task implementations found that this plan itself got wrong or missed, each fixed during implementation rather than shipped as written:
- **Task 5** found the plan's own `test_eviction_never_splits_mutual_exclusion` does not exercise the trap it claims to guard (a naive, provably-broken eviction passes it every time, because `asyncio.gather` starts all contenders in the same tick before any release happens) — added a genuinely discriminating 4th test.
- **Task 13** found that applying the plan's SQL-side `profile_version` increment literally at the three *router* sites, on a profile that was `db.add()`-ed but not yet flushed, generates invalid SQL (`invalid reference to FROM-clause entry for table researcher_profiles`) — proved against real Postgres, fixed with a flush right after `db.add()`. Without this, a PI's first-ever profile save would have 500'd.
- **Cross-task regression, caught only by the full-suite run after integration:** Task 1's deferred memory-synthesis queue broke a `tests/integration/test_proposal_review.py` helper that asserted the (now-deferred) memory call had already run — that file wasn't named in Task 1's own regression-suite list, so no individual task's isolated testing could have caught it. Fixed by draining the queue before the assertion.

Two documentation inaccuracies were also corrected in `README.md` during implementation: the top summary now records completion, and the P3-web/pool_recycle item's original claim (already flagged and fixed in this plan's own header) is echoed there too.

## Global Constraints

- Test command: `.venv-test/bin/python -m pytest <path> -v` from the repo root, on the host (testcontainers spins the DB; no env vars needed). The full gate before any push is `./scripts/ci.sh` (alembic single-head + upgrade/downgrade round trip, ruff with `SRC_LINT_MAX` ratchet, pytest with `COV_MIN=60`).
- NEVER run bare `docker compose` on this host and NEVER pass `--remove-orphans` (a second production stack shares the machine — see CLAUDE.md). No task in this plan needs docker directly; ci.sh manages its own throwaway Postgres.
- Do NOT reword any `pi_lab` string in `src/agent/thread_guidance.py`, and do NOT run `pytest --snapshot-update` — except the single, deliberate, diff-reviewed regeneration in Task 6 Step 5, and only if that step's condition is met.
- Migration numbering: the current head is `0032_add_llm_call_stats`. Task 10 creates `0033`. Nothing else in this plan touches the schema.
- Engine behavior changes (Tasks 1–6) require rebuilding the agent image and restarting `blackbird-agent-run` to take effect. That is an operator decision — flag it in the PR description; do not restart anything yourself.
- Commits: conventional style matching the repo's log (`fix(engine): …`), each ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Every "Run" step's expected outcome is stated. If reality differs, stop and investigate before proceeding (superpowers:systematic-debugging).

---

## Workstream A — agent engine

### Task 1: Move `_close_thread`'s memory-synthesis LLM calls into a FIFO queue drained outside the dispatch span

The audit's HIGH finding. Invariants to preserve (RCA §F1): per-agent memory updates stay strictly sequential and each reads its predecessor's write; memory llm_call_logs rows still flush; shutdown drain is bounded; agents are resolved by id at drain time.

**Files:**
- Modify: `src/agent/simulation.py` (`__init__` ~line 460, `_close_thread` lines ~2138–2150, `_run_main_loop` lines 850–856, `stop()` lines 886–897, `_update_agent_memory` docstring ~line 6075)
- Test: `tests/integration/test_concurrent_thread_safety.py`

**Interfaces:**
- Consumes: existing `_update_agent_memory(agent, event, visibility, channel_id)`, `Agent.record_api_call` (unchanged).
- Produces: `SimulationEngine._pending_memory_events: list[tuple[str, str, str, str | None]]` (agent_id, event, visibility, channel_id); `async SimulationEngine._drain_memory_events(limit: int | None = None) -> int`; module constant `MEMORY_EVENTS_MAX_AT_SHUTDOWN = 10`.

- [ ] **Step 1: Write the failing tests** (append to `tests/integration/test_concurrent_thread_safety.py`; it already imports `Agent`, `SimulationEngine`, `ThreadState`, `get_settings`, `FakeSlackClient`, `_seed_thread_history`, `asyncio`, `pytest`):

```python
@pytest.mark.asyncio
async def test_close_thread_defers_memory_updates_out_of_the_dispatch_span(
    monkeypatch, tmp_path,
):
    """A system-enforced close must ENQUEUE its two memory events, not run
    them inside the dispatch fan-out where they hold the thread lock, both
    agent locks and a semaphore slot (audit 2026-08-21 finding 1). This is
    deterministic — it counts LLM calls, it does not race timers."""
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="wang",
        has_pending_reply=True,
    )
    hub.state.active_threads["t1"] = thread
    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={
            "blackbird": FakeSlackClient(agent_id="blackbird"),
            "wang": FakeSlackClient(agent_id="wang"),
        },
    )
    _seed_thread_history(
        eng, "t1", "general", get_settings().max_thread_messages,
    )

    memory_calls: list[str] = []

    async def fake_generate(*, system_prompt, messages, **kwargs):
        memory_calls.append("memory")
        await asyncio.sleep(0)
        return "updated memory"

    monkeypatch.setattr(
        "src.agent.simulation.generate_agent_response", fake_generate
    )
    eng._running = True
    await eng._dispatch_reply_lane()

    assert memory_calls == [], (
        "memory LLM calls ran inside the dispatch fan-out; they must be queued"
    )
    assert len(eng._pending_memory_events) == 2  # one per party of the close
    drained = await eng._drain_memory_events()
    assert drained == 2
    assert memory_calls == ["memory", "memory"]
    assert not eng._pending_memory_events


@pytest.mark.asyncio
async def test_sequential_drain_prevents_a_lost_memory_update(
    monkeypatch, tmp_path,
):
    """Successor to test_close_threads_agent_lock_prevents_a_lost_memory_update
    (which this task DELETES — see Step 3): the serialization duty moves from
    the agent lock to the drain. Two closes both involving the hub enqueue
    two hub events; the drain must apply them sequentially so the second
    reads the first's written text. The fake is the same interleave detector:
    it reads the prompt's 'current working memory', awaits, and appends."""
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab1 = Agent("wang1", "Wang1Bot", "Wang1", role="pi_lab")
    lab2 = Agent("wang2", "Wang2Bot", "Wang2", role="pi_lab")
    thread_ab = ThreadState(thread_id="tAB", channel="general", other_agent_id="wang1")
    hub.state.active_threads["tAB"] = thread_ab
    thread_cd = ThreadState(thread_id="tCD", channel="general", other_agent_id="wang2")
    hub.state.active_threads["tCD"] = thread_cd
    eng = SimulationEngine(
        agents=[hub, lab1, lab2],
        slack_clients={
            "blackbird": FakeSlackClient(agent_id="blackbird"),
            "wang1": FakeSlackClient(agent_id="wang1"),
            "wang2": FakeSlackClient(agent_id="wang2"),
        },
        session_factory=None, simulation_run_id=None,
    )

    async def _fake_generate(*, messages, **kwargs):
        content = messages[0]["content"]
        event_marker = "The event that triggered this update:\n"
        mem_marker = "Your current working memory:\n"
        e_start = content.index(event_marker) + len(event_marker)
        e_end = content.index("\n\n", e_start)
        event = content[e_start:e_end]
        m_start = content.index(mem_marker) + len(mem_marker)
        m_end = content.index("\n\nWrite the complete", m_start)
        prior = content[m_start:m_end]
        await asyncio.sleep(0.05)
        if prior == "(empty)":
            return event
        return f"{prior} || {event}"

    monkeypatch.setattr(
        "src.agent.simulation.generate_agent_response", _fake_generate
    )

    await asyncio.gather(
        eng._close_thread(hub, thread_ab, "no_proposal"),
        eng._close_thread(hub, thread_cd, "no_proposal"),
    )
    # The adversarial half: TWO drains racing must still not interleave
    # same-agent updates (the drain lock's whole job).
    await asyncio.gather(
        eng._drain_memory_events(), eng._drain_memory_events(),
    )
    final_memory = hub.working_memory
    assert "wang1" in final_memory and "wang2" in final_memory, (
        f"hub's working memory lost one close's event: {final_memory!r}"
    )


@pytest.mark.asyncio
async def test_stop_drains_a_bounded_number_of_memory_events(
    monkeypatch, tmp_path, caplog,
):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    eng = SimulationEngine(
        agents=[hub],
        slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")},
    )
    calls = []

    async def fake_generate(*, system_prompt, messages, **kwargs):
        calls.append(1)
        return "m"

    monkeypatch.setattr(
        "src.agent.simulation.generate_agent_response", fake_generate
    )
    for i in range(12):
        eng._pending_memory_events.append(
            ("blackbird", f"event {i}", "public", None)
        )
    await eng.stop()
    assert len(calls) == 10  # MEMORY_EVENTS_MAX_AT_SHUTDOWN
    assert not eng._pending_memory_events
    assert any("Dropping 2 queued" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/integration/test_concurrent_thread_safety.py -k "defers_memory or sequential_drain or bounded_number" -v`
Expected: all three FAIL — the first with `AttributeError: ... _pending_memory_events` (or a non-empty `memory_calls`), the others likewise.

- [ ] **Step 3: Implement.** Four edits to `src/agent/simulation.py`, one deletion in the test file.

(3a) Module constant, next to `RUN_STATS_UPDATE_INTERVAL` (~line 174):

```python
# How many queued working-memory updates stop() will still run. Each is a
# real LLM call (seconds); the container's stop grace period (-t 420) was
# sized for ONE 16k call, so an unbounded shutdown drain can outlive it and
# get SIGKILLed mid-flush. Anything beyond this bound is dropped LOUDLY.
MEMORY_EVENTS_MAX_AT_SHUTDOWN = 10
```

(3b) In `__init__`, after the `self._pending_assessments: list[dict] = []` block:

```python
        # Working-memory events deferred from _close_thread. The close used
        # to run its two _update_agent_memory LLM calls inside the thread
        # lock + BOTH agents' locks + a reply-lane semaphore slot; in the
        # star topology every close shares the hub's agent lock, so closes
        # serialized on two LLM calls each and blocked semaphore slots for
        # the duration (docs/audits/2026-08-21-perf-memory-race, finding 1).
        # Queued here and drained OUTSIDE the dispatch fan-out, one event at
        # a time — sequential draining is what preserves the lost-update
        # guarantee the agent lock used to provide for these calls: no two
        # updates for one agent can interleave, and each reads the memory
        # text its predecessor wrote. Entries are (agent_id, event,
        # visibility, channel_id); agent_id (not the Agent object) because a
        # roster sync can rebuild the object between enqueue and drain.
        self._pending_memory_events: list[tuple[str, str, str, str | None]] = []
        # Guards against two drains running at once (main loop vs stop(), or
        # a future second call site): concurrent drains would pop same-agent
        # events into overlapping LLM calls — exactly the lost update this
        # queue exists to prevent.
        self._memory_drain_lock = asyncio.Lock()
```

(3c) In `_close_thread`, replace the tail of the method (the comment block
"Update working memory for both agents" through the final
`await self._update_agent_memory(other_agent, other_event)`) with:

```python
            # Queue working-memory updates for both agents. NOT awaited here:
            # these are LLM calls, and this block holds the thread lock, both
            # agent locks and a reply-lane semaphore slot — running them here
            # serialized every close on the hub's key and starved the reply
            # lane (audit finding 1). _drain_memory_events (main loop / stop)
            # applies them sequentially, which preserves the same lost-update
            # protection the lock provided. summary_text is derived from a
            # cross-agent conversation, so it is fenced as untrusted before it
            # lands in working memory (SEC-14), same as before.
            event = f"Thread in #{thread.channel} with {thread.other_agent_id} closed: {outcome}"
            if summary_text:
                event += f". Summary: {delimit(summary_text[:200], 'proposal_summary')}"
            self._pending_memory_events.append(
                (agent.agent_id, event, VISIBILITY_PUBLIC, None)
            )
            if other_agent:
                other_event = f"Thread in #{thread.channel} with {agent.agent_id} closed: {outcome}"
                if summary_text:
                    other_event += f". Summary: {delimit(summary_text[:200], 'proposal_summary')}"
                self._pending_memory_events.append(
                    (other_agent.agent_id, other_event, VISIBILITY_PUBLIC, None)
                )
```

(3d) New method directly above `_update_agent_memory`:

```python
    async def _drain_memory_events(self, limit: int | None = None) -> int:
        """Run queued working-memory updates, strictly FIFO, one at a time.

        Called from the main loop after the reply-lane dispatch and from
        stop() (bounded). Sequential draining under the drain lock is what
        preserves the lost-update guarantee for same-agent updates: each one
        reads the memory text its predecessor wrote. Agents are resolved by
        id at drain time — the roster can change (or an Agent object be
        rebuilt by _sync_roster_from_db) between enqueue and drain, and a
        stale reference would write memory for an object the engine no
        longer owns. _update_agent_memory never raises, so one bad event
        cannot wedge the queue.
        """
        drained = 0
        async with self._memory_drain_lock:
            while self._pending_memory_events:
                if limit is not None and drained >= limit:
                    break
                agent_id, event, visibility, channel_id = (
                    self._pending_memory_events.pop(0)
                )
                agent = self.agents.get(agent_id)
                if agent is None:
                    logger.info(
                        "[memory] dropping queued memory event for %s — no "
                        "longer on the roster", agent_id,
                    )
                    drained += 1
                    continue
                await self._update_agent_memory(
                    agent, event, visibility, channel_id
                )
                drained += 1
        return drained
```

(3e) In `_run_main_loop`, immediately BEFORE `await self._flush_persisted()`:

```python
            # Deferred working-memory updates (audit finding 1): run OUTSIDE
            # the reply lane's locks/semaphore. Before the flushes, so this
            # tick's memory llm_call_logs rows land in this tick's flush.
            if self._pending_memory_events:
                await self._drain_memory_events()
```

(3f) In `stop()`, replace the first four lines
(`self._running = False` … `set_call_log_callback(None)`) with:

```python
        self._running = False
        self._stop_event.set()
        # Drain a BOUNDED number of queued memory updates BEFORE the log
        # callback is cleared, so their llm_call_logs rows are captured by
        # the flush below. Bounded: each is a real LLM call and the stop
        # grace period is finite — the remainder is dropped loudly rather
        # than racing the SIGKILL.
        try:
            await self._drain_memory_events(limit=MEMORY_EVENTS_MAX_AT_SHUTDOWN)
        except Exception:
            logger.exception("Shutdown memory drain failed")
        if self._pending_memory_events:
            logger.warning(
                "Dropping %d queued working-memory update(s) at shutdown",
                len(self._pending_memory_events),
            )
            self._pending_memory_events.clear()
        set_call_log_callback(None)
```

(3g) In `_update_agent_memory`'s docstring, replace the stale line
`Triggered by: thread closure, PI DM, or proposal review — not batched at`
`simulation end.` with:

```python
        Triggered by thread closure, via the _pending_memory_events queue
        (_close_thread enqueues; _drain_memory_events is the only caller).
```

(3h) DELETE the now-superseded test
`test_close_threads_agent_lock_prevents_a_lost_memory_update` from
`tests/integration/test_concurrent_thread_safety.py` (its invariant now lives
in `test_sequential_drain_prevents_a_lost_memory_update`; with the queue in
place its "memory written synchronously by _close_thread" premise is false by
design). Leave `test_cross_agent_close_does_not_deadlock` and the eviction
test alone — re-read them after the change: they monkeypatch
`generate_agent_response` and drive `_close_thread`; if either asserts memory
content synchronously after the close, add `await eng._drain_memory_events()`
before that assertion rather than weakening the assertion.

- [ ] **Step 4: Run the task's tests and the whole affected file**

Run: `.venv-test/bin/python -m pytest tests/integration/test_concurrent_thread_safety.py -v`
Expected: all PASS (including the three new ones; the deleted test gone).

- [ ] **Step 5: Run the adjacent suites that drive `_close_thread`**

Run: `.venv-test/bin/python -m pytest tests/unit/test_simulation_logic.py tests/unit/test_reply_lane.py tests/integration/test_state_rebuild.py -v`
Expected: PASS. Any failure here means a test asserted synchronous memory writes — fix per Step 3h's rule, never by weakening the memory-content assertion.

- [ ] **Step 6: Commit**

```bash
git add src/agent/simulation.py tests/integration/test_concurrent_thread_safety.py
git commit -m "fix(engine): a close must not hold locks and semaphore slots across memory LLM calls

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Take the last three sync Slack calls off the event loop; cache `users.info`

**Files:**
- Modify: `src/agent/slack_client.py` (`__init__` ~line 275, `is_bot_user` line 683, async wrappers block lines 1167–1186)
- Modify: `src/agent/simulation.py` (line ~4234 `is_bot_user`; `_phase1_channel_discovery` line 1394 + its caller line 1360; roster-sync `connect()` sites lines ~5685 and ~5733)
- Modify: `tests/fakes.py` (`FakeSlackClient`: add `ais_bot_user`, `ajoin_channel`, `is_bot_user`, `aconnect`)
- Test: `tests/integration/test_concurrent_thread_safety.py`, `tests/unit/test_slack_client_contract.py`

**Interfaces:**
- Produces: `AgentSlackClient.ais_bot_user(user_id) -> bool`, `AgentSlackClient.ajoin_channel(channel_id) -> None`, `AgentSlackClient.aconnect() -> bool` (to_thread wrappers); `_phase1_channel_discovery` becomes `async def`.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/test_slack_client_contract.py`, append:

```python
def test_is_bot_user_caches_successes_but_never_failures():
    from slack_sdk.errors import SlackApiError
    from src.agent.slack_client import AgentSlackClient

    class _Resp:
        headers: dict = {}
        def get(self, key, default=None):
            return {"error": "internal_error"}.get(key, default)

    class _Stub:
        def __init__(self):
            self.calls = 0
            self.fail_first = True
        def users_info(self, **kw):
            self.calls += 1
            if self.fail_first:
                self.fail_first = False
                raise SlackApiError("boom", response=_Resp())
            return {"user": {"is_bot": True}}

    client = AgentSlackClient(agent_id="su", bot_token="xoxb-x")
    stub = _Stub()
    client._client = stub
    # Failure path: returns False and must NOT be cached as an answer.
    assert client.is_bot_user("U1") is False
    # Retry reaches the API again and the success IS cached.
    assert client.is_bot_user("U1") is True
    assert client.is_bot_user("U1") is True
    assert stub.calls == 2
```

In `tests/integration/test_concurrent_thread_safety.py`, append (module already imports `asyncio`, `time`, `pytest`, `Agent`, `SimulationEngine`):

```python
@pytest.mark.asyncio
async def test_poller_is_bot_lookup_does_not_block_the_loop(monkeypatch):
    """5 human channel messages used to freeze the loop for the SUM of the
    sync users.info round trips (audit finding 2, harness B: one 1.5s stall).
    With ais_bot_user the poll may still take that wall time, but the LOOP
    must stay responsive throughout."""
    import time as _time
    from src.agent.slack_client import AgentSlackClient

    class _Stub:
        def conversations_history(self, **kw):
            return {"messages": [
                {"ts": f"1000.00000{i}", "user": f"UH{i}", "text": "hi"}
                for i in range(5)
            ], "has_more": False, "response_metadata": {}}
        def users_info(self, **kw):
            _time.sleep(0.3)  # what a real sync HTTP call does
            return {"user": {"is_bot": False}}

    a = Agent("su", "SuBot", "Su", role="pi_lab")
    client = AgentSlackClient(agent_id="su", bot_token="xoxb-x")
    client._client = _Stub()
    client._bot_user_id = "UBOT"
    eng = SimulationEngine(agents=[a], slack_clients={"su": client})
    eng._channel_id_map = {"general": "C1"}
    eng._last_channel_poll = 0.0

    gaps: list[float] = []
    stop = asyncio.Event()

    async def heartbeat():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.05)
            now = time.monotonic()
            if now - last > 0.25:
                gaps.append(now - last)
            last = now

    hb = asyncio.create_task(heartbeat())
    await eng._poll_slack_for_bot_messages()
    stop.set()
    await hb
    assert not gaps, f"event loop stalled during the poll: {gaps}"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_slack_client_contract.py::test_is_bot_user_caches_successes_but_never_failures tests/integration/test_concurrent_thread_safety.py::test_poller_is_bot_lookup_does_not_block_the_loop -v`
Expected: cache test FAILS (`stub.calls == 3`); poller test FAILS with a recorded ~1.5 s gap.

- [ ] **Step 3: Implement**

(3a) `slack_client.py` `__init__`: add `self._user_is_bot_cache: dict[str, bool] = {}` beside the other caches.

(3b) Replace `is_bot_user`'s body:

```python
    def is_bot_user(self, user_id: str) -> bool:
        """Check if a user ID corresponds to a bot.

        Successful answers are cached per user id — a Slack user record does
        not flip between bot and human, and the poller asks per MESSAGE, not
        per user. A failed lookup is NOT cached: is_bot_user answers False on
        error, and caching that would permanently misclassify a bot as human,
        silently dropping its messages forever.
        """
        if not user_id or not self._client:
            return False
        cached = self._user_is_bot_cache.get(user_id)
        if cached is not None:
            return cached
        try:
            info = self._api("users_info", user=user_id)
            user = info.get("user", {})
            result = bool(user.get("is_bot", False))
            self._user_is_bot_cache[user_id] = result
            return result
        except SlackApiError:
            return False
```

(3c) In the async-wrappers block (after `aget_all_thread_replies`):

```python
    async def ais_bot_user(self, *args, **kwargs) -> bool:
        return await asyncio.to_thread(self.is_bot_user, *args, **kwargs)

    async def ajoin_channel(self, *args, **kwargs) -> None:
        return await asyncio.to_thread(self.join_channel, *args, **kwargs)

    async def aconnect(self) -> bool:
        return await asyncio.to_thread(self.connect)
```

(3d) `simulation.py:~4234` (verify with `grep -n "is_bot_user" src/agent/simulation.py` — the plan's line numbers drifted after an unrelated commit inserted code upstream; the call site itself is unchanged): `is_bot = await client.ais_bot_user(user_id)`.

(3e) `_phase1_channel_discovery`: change to `async def`; change
`client.join_channel(ch_id)` to `await client.ajoin_channel(ch_id)`; change
the caller at line 1360 to `await self._phase1_channel_discovery(agent)`.
Then grep for other callers: `grep -rn "_phase1_channel_discovery" src/ tests/`
and add `await`/`asyncio.run`-appropriate handling to any test caller.

(3f) Roster sync: at BOTH sites (`if not client.connect():` lines ~5685 and
~5733 — verify with `grep -n "client.connect()" src/agent/simulation.py`)
change to `if not await asyncio.to_thread(client.connect):` — the
client object is freshly constructed and not yet shared, so the thread-hop is
safe.

(3g) `tests/fakes.py` `FakeSlackClient`: add

```python
    def is_bot_user(self, user_id: str) -> bool:
        return False

    async def ais_bot_user(self, user_id: str) -> bool:
        return self.is_bot_user(user_id)

    def join_channel(self, channel_id: str) -> None:
        return None

    async def ajoin_channel(self, channel_id: str) -> None:
        return self.join_channel(channel_id)

    async def aconnect(self) -> bool:
        return self.connect()
```

(only add `join_channel` if the fake lacks one — check first.)

- [ ] **Step 4: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_slack_client_contract.py tests/integration/test_concurrent_thread_safety.py tests/unit/test_simulation_logic.py -v`
Expected: PASS, including both new tests.

- [ ] **Step 5: Commit**

```bash
git add src/agent/slack_client.py src/agent/simulation.py tests/fakes.py \
  tests/unit/test_slack_client_contract.py tests/integration/test_concurrent_thread_safety.py
git commit -m "fix(engine): the last three sync Slack calls come off the event loop

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Index MessageLog reads (thread index + time index + per-sender/channel maps)

Semantics that MUST survive byte-for-byte (RCA §F3): since-readers return
matches in INSERTION order; `get_last_bot_sender_in_channel` keeps the later
insertion on ties (`>=`); `get_thread_history` is stable-sorted by
`(posted_at, insertion)` with the root pinned first; panel-note and cohort
filters stay at READ time; `get_entry`/`latest_timestamp`/`append`
idempotency unchanged. The differential test is the contract.

**Files:**
- Modify: `src/agent/message_log.py`
- Test: `tests/unit/test_message_log_differential.py` (create)

**Interfaces:**
- Produces: no public API change. Internal: `LogEntry` gains nothing; `MessageLog` gains `_seq_by_ts: dict[str, int]`, `_by_thread: dict[str, list[LogEntry]]`, `_by_time: list[tuple[float, int, LogEntry]]` (kept sorted), `_top_level_ts_by_sender: dict[str, set[str]]`, `_top_level_by_sender: dict[str, list[LogEntry]]`, `_last_bot_in_channel: dict[str, LogEntry]`.

- [ ] **Step 1: Write the failing differential test.** Create `tests/unit/test_message_log_differential.py` with a linear reference implementation copied from today's method bodies and a randomized comparison:

```python
"""Differential contract test: the indexed MessageLog must return EXACTLY
what the linear-scan implementation returned — same elements, same ORDER —
for every read method, over a randomized log with out-of-order posted_at,
threads, panel notes, human rows and cohort gates. The reference class below
is a verbatim port of the pre-index method bodies (2026-08-21 tree)."""
import random

from src.agent.message_log import (
    PHASE_PANEL_NOTE, LogEntry, MessageLog, _entry_allowed, is_panel_note,
)


class LinearReference:
    """The pre-index read algorithms, over a plain entry list."""

    def __init__(self, entries):
        self._entries = entries
        self._by_ts = {e.ts: e for e in entries}
        self._bot_name_to_id = {}

    def get_new_top_level_posts(self, since, channels, exclude_agent_id,
                                allowed_sender_ids=None):
        results = []
        for entry in self._entries:
            if is_panel_note(entry):
                continue
            if entry.posted_at <= since:
                continue
            if entry.thread_ts is not None:
                continue
            if entry.channel not in channels:
                continue
            if entry.sender_agent_id == exclude_agent_id:
                continue
            if not _entry_allowed(entry, allowed_sender_ids):
                continue
            results.append(entry)
        return results

    def get_thread_history(self, thread_ts):
        root = self._by_ts.get(thread_ts)
        replies = sorted(
            (e for e in self._entries
             if e.thread_ts == thread_ts and not is_panel_note(e)),
            key=lambda e: e.posted_at,
        )
        result = []
        if root and not is_panel_note(root):
            result.append(root)
        result.extend(replies)
        return result

    def get_thread_message_count(self, thread_ts):
        count = 1 if thread_ts in self._by_ts else 0
        count += sum(1 for e in self._entries
                     if e.thread_ts == thread_ts and not is_panel_note(e))
        return count

    def get_agent_top_level_posts(self, agent_id, limit=10):
        posts = sorted(
            (e for e in self._entries
             if e.sender_agent_id == agent_id and e.thread_ts is None
             and not is_panel_note(e)),
            key=lambda e: e.posted_at,
        )
        return posts[-limit:]

    def get_last_bot_sender_in_channel(self, channel_name):
        best = None
        for entry in self._entries:
            if is_panel_note(entry):
                continue
            if entry.channel != channel_name:
                continue
            if not entry.is_bot or not entry.sender_agent_id:
                continue
            if best is None or entry.posted_at >= best.posted_at:
                best = entry
        return best.sender_agent_id if best else None

    def get_replies_to_agent_posts(self, agent_id, since,
                                   allowed_sender_ids=None):
        agent_post_ts = {
            e.ts for e in self._entries
            if e.sender_agent_id == agent_id and e.thread_ts is None
        }
        results = []
        for entry in self._entries:
            if is_panel_note(entry):
                continue
            if entry.posted_at <= since:
                continue
            if entry.thread_ts not in agent_post_ts:
                continue
            if entry.sender_agent_id == agent_id:
                continue
            if not _entry_allowed(entry, allowed_sender_ids):
                continue
            results.append(entry)
        return results

    def get_tags_for_agent(self, agent_bot_name, since,
                           allowed_sender_ids=None):
        tag = f"@{agent_bot_name}".lower()
        results = []
        for entry in self._entries:
            if is_panel_note(entry):
                continue
            if entry.posted_at <= since:
                continue
            if not _entry_allowed(entry, allowed_sender_ids):
                continue
            if tag in entry.content.lower():
                results.append(entry)
        return results

    def has_new_reply_from_other(self, thread_ts, agent_id, since,
                                 allowed_sender_ids=None):
        for entry in self._entries:
            if entry.thread_ts != thread_ts:
                continue
            if is_panel_note(entry):
                continue
            if entry.posted_at <= since:
                continue
            if entry.sender_agent_id == agent_id:
                continue
            if not entry.is_bot:
                continue
            if not _entry_allowed(entry, allowed_sender_ids):
                continue
            return True
        return False


def _random_log(seed, n=3000):
    rng = random.Random(seed)
    agents = ["blackbird", "su", "wang", "wu", None]
    channels = ["general", "chemical-biology", "collab-x"]
    log = MessageLog()
    log.set_bot_name_map({"subot": "su", "wangbot": "wang"})
    entries = []
    for i in range(n):
        sender = rng.choice(agents)
        is_bot = sender is not None and rng.random() > 0.1
        thread = rng.choice([None, f"root-{rng.randrange(60)}"])
        entry = LogEntry(
            ts=f"{1000 + i}.0001",
            channel=rng.choice(channels),
            sender_agent_id=sender if is_bot else None,
            sender_name=str(sender),
            content=rng.choice(["hello", "ping @SuBot", "note", "@WangBot hi"]),
            thread_ts=thread,
            # Deliberately out-of-order and colliding timestamps
            posted_at=float(rng.randrange(0, n // 2)),
            is_bot=is_bot,
            visibility=rng.choice(["public", "collab_private"]),
            phase=PHASE_PANEL_NOTE if rng.random() < 0.05 else None,
        )
        log.load_entry(entry)
        entries.append(entry)
    return log, entries


def test_indexed_reads_match_the_linear_reference_exactly():
    for seed in range(5):
        log, entries = _random_log(seed)
        ref = LinearReference(entries)
        gates = [None, {"su", "wang"}, set()]
        sinces = [0.0, 100.0, 1e9]
        for since in sinces:
            for gate in gates:
                assert log.get_new_top_level_posts(
                    since, {"general", "collab-x"}, "su", gate
                ) == ref.get_new_top_level_posts(
                    since, {"general", "collab-x"}, "su", gate
                )
                assert log.get_replies_to_agent_posts("su", since, gate) == \
                    ref.get_replies_to_agent_posts("su", since, gate)
                assert log.get_tags_for_agent("SuBot", since, gate) == \
                    ref.get_tags_for_agent("SuBot", since, gate)
        for t in [f"root-{i}" for i in range(60)] + ["missing"]:
            assert log.get_thread_history(t) == ref.get_thread_history(t)
            assert log.get_thread_message_count(t) == ref.get_thread_message_count(t)
            for gate in gates:
                assert log.has_new_reply_from_other(t, "su", 50.0, gate) == \
                    ref.has_new_reply_from_other(t, "su", 50.0, gate)
        for a in ["blackbird", "su", "wang", "wu"]:
            assert log.get_agent_top_level_posts(a, 10) == \
                ref.get_agent_top_level_posts(a, 10)
        for ch in ["general", "chemical-biology", "collab-x"]:
            assert log.get_last_bot_sender_in_channel(ch) == \
                ref.get_last_bot_sender_in_channel(ch)
```

- [ ] **Step 2: Run it — it must PASS against the current linear code** (the reference IS the current code):

Run: `.venv-test/bin/python -m pytest tests/unit/test_message_log_differential.py -v`
Expected: PASS. (This test fails only if the rewrite in Step 3 changes behavior — that is its job. Commit it first so the rewrite is developed against it.)

- [ ] **Step 3: Implement the indexes in `src/agent/message_log.py`.**

In `__init__`, after the existing fields:

```python
        # ---- read indexes (audit 2026-08-21 finding 3) -------------------
        # Every read used to scan self._entries in full, synchronously, on
        # the event-loop thread — dozens of scans per main-loop tick over an
        # append-only list (measured 0.7s/tick at 100k entries). The indexes
        # below make each read O(matches). INVARIANTS the indexes must not
        # change: since-readers return matches in INSERTION order; ties in
        # get_last_bot_sender_in_channel keep the LATER insertion;
        # get_thread_history is stable by (posted_at, insertion); panel-note
        # and cohort filters stay at READ time (get_entry must keep seeing
        # notes). tests/unit/test_message_log_differential.py is the contract.
        self._seq_by_ts: dict[str, int] = {}          # ts -> insertion seq
        self._by_thread: dict[str, list[LogEntry]] = {}
        self._by_time: list[tuple[float, int, LogEntry]] = []  # sorted
        self._top_level_ts_by_sender: dict[str, set[str]] = {}
        self._top_level_by_sender: dict[str, list[LogEntry]] = {}
        self._last_bot_in_channel: dict[str, LogEntry] = {}
```

Add `import bisect` at the top. Extend `_record`:

```python
    def _record(self, entry: LogEntry) -> None:
        """Store an entry, advance the high-water mark, maintain the indexes.

        Runs once per unique ts (append/load_entry dedupe first), in
        insertion order — which is what makes the incremental
        _last_bot_in_channel update below exactly equivalent to the old full
        scan's `>=` tie rule.
        """
        seq = len(self._entries)
        self._entries.append(entry)
        self._by_ts[entry.ts] = entry
        self._seq_by_ts[entry.ts] = seq
        if entry.posted_at > self._max_posted_at:
            self._max_posted_at = entry.posted_at
        if entry.thread_ts is not None:
            self._by_thread.setdefault(entry.thread_ts, []).append(entry)
        bisect.insort(self._by_time, (entry.posted_at, seq, entry),
                      key=lambda t: (t[0], t[1]))
        if entry.thread_ts is None and entry.sender_agent_id:
            self._top_level_ts_by_sender.setdefault(
                entry.sender_agent_id, set()
            ).add(entry.ts)
            self._top_level_by_sender.setdefault(
                entry.sender_agent_id, []
            ).append(entry)
        if entry.is_bot and entry.sender_agent_id and not is_panel_note(entry):
            best = self._last_bot_in_channel.get(entry.channel)
            if best is None or entry.posted_at >= best.posted_at:
                self._last_bot_in_channel[entry.channel] = entry
```

Rewrite the read methods (docstrings unchanged except where noted):

```python
    def _since(self, since: float) -> list[LogEntry]:
        """Entries with posted_at strictly greater than ``since``, in
        INSERTION order — the same order the old full scans returned."""
        i = bisect.bisect_right(self._by_time, (since, float("inf")),
                                key=lambda t: (t[0], t[1]))
        tail = self._by_time[i:]
        tail.sort(key=lambda t: t[1])
        return [t[2] for t in tail]
```

- `get_new_top_level_posts`: iterate `self._since(since)` with the same
  filter chain (drop only the `entry.posted_at <= since` check).
- `get_replies_to_agent_posts`: `agent_post_ts = self._top_level_ts_by_sender.get(agent_id, set())`, then iterate `self._since(since)` with the same remaining filters.
- `get_tags_for_agent`: iterate `self._since(since)` with the same remaining filters.
- `get_thread_history`: `replies = sorted((e for e in self._by_thread.get(thread_ts, []) if not is_panel_note(e)), key=lambda e: (e.posted_at, self._seq_by_ts[e.ts]))`, root logic unchanged.
- `get_thread_message_count`: `count = 1 if thread_ts in self._by_ts else 0; count += sum(1 for e in self._by_thread.get(thread_ts, []) if not is_panel_note(e))`.
- `has_new_reply_from_other`: iterate `self._by_thread.get(thread_ts, [])` with the identical filter chain (drop the `entry.thread_ts != thread_ts` check).
- `get_agent_top_level_posts`: `posts = sorted((e for e in self._top_level_by_sender.get(agent_id, []) if not is_panel_note(e)), key=lambda e: (e.posted_at, self._seq_by_ts[e.ts]))`; return `posts[-limit:]`.
- `get_last_bot_sender_in_channel`: `best = self._last_bot_in_channel.get(channel_name); return best.sender_agent_id if best else None`.

`get_entry`, `latest_timestamp`, `append`, `load_entry`,
`get_thread_allowed_agents`, `__len__` are untouched.

- [ ] **Step 4: Run the differential test + every message-log consumer suite**

Run: `.venv-test/bin/python -m pytest tests/unit/test_message_log_differential.py tests/unit/test_cohort_isolation.py tests/unit/test_simulation_logic.py tests/integration/ -v`
Expected: PASS across the board. Any differential mismatch is a real semantics change — fix the index, never the reference.

- [ ] **Step 5: Re-run the audit's scaling harness to confirm the win**

Run: `.venv-test/bin/python docs/audits/2026-08-21-perf-memory-race/harnesses/harness_c_log_scaling.py`
Expected: per-tick totals drop from ~67/354/701 ms to low single-digit ms at all three sizes. Paste the numbers into the commit message.

- [ ] **Step 6: Commit**

```bash
git add src/agent/message_log.py tests/unit/test_message_log_differential.py
git commit -m "perf(engine): index MessageLog reads — O(matches), not O(log), per tick

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: One long-lived Anthropic client per API key

**Files:**
- Modify: `src/services/llm.py` (lines 72–74)
- Test: `tests/unit/test_llm_client_reuse.py` (create)

**Interfaces:**
- Produces: `get_anthropic_client()` signature unchanged; new private `_client_for_key(api_key: str)` behind `functools.lru_cache`.

- [ ] **Step 1: Write the failing test** (`tests/unit/test_llm_client_reuse.py`):

```python
from src.services import llm


def test_get_anthropic_client_reuses_one_instance_per_key(monkeypatch):
    llm._client_for_key.cache_clear()

    class _S:
        anthropic_api_key = "key-a"

    monkeypatch.setattr(llm, "get_settings", lambda: _S())
    c1 = llm.get_anthropic_client()
    c2 = llm.get_anthropic_client()
    assert c1 is c2, "each call built a fresh client (fresh connection pool)"

    _S.anthropic_api_key = "key-b"
    c3 = llm.get_anthropic_client()
    assert c3 is not c1, "a different API key must get its own client"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_llm_client_reuse.py -v`
Expected: FAIL (`AttributeError: module ... has no attribute '_client_for_key'`).

- [ ] **Step 3: Implement.** In `src/services/llm.py`, add `from functools import lru_cache` to the imports and replace `get_anthropic_client`:

```python
@lru_cache(maxsize=8)
def _client_for_key(api_key: str) -> anthropic.Anthropic:
    """One long-lived client per API key. ``anthropic.Anthropic`` owns an
    httpx connection pool; constructing one per call meant a fresh TCP+TLS
    handshake for every LLM call in the engine — thread replies, up to 8
    specialist consults per concluding turn, memory updates, retries
    (audit 2026-08-21, finding 4: 8 calls -> 8 connections; 1 shared client
    -> 1). The sync client is thread-safe, so one instance is safe under
    ``asyncio.to_thread`` concurrency."""
    return anthropic.Anthropic(api_key=api_key)


def get_anthropic_client() -> anthropic.Anthropic:
    settings = get_settings()
    return _client_for_key(settings.anthropic_api_key)
```

- [ ] **Step 4: Run the test plus the LLM suites**

Run: `.venv-test/bin/python -m pytest tests/unit/test_llm_client_reuse.py tests/unit/test_llm_nonstreaming_ceiling.py tests/unit/test_llm_event_loop.py -v`
Expected: PASS. Then run the FULL suite once (`.venv-test/bin/python -m pytest tests/ -x -q`) — if any test fails from client caching across tests, add `llm._client_for_key.cache_clear()` to that test's setup rather than weakening the cache.

- [ ] **Step 5: Re-run the audit harness to confirm**

Run: `.venv-test/bin/python docs/audits/2026-08-21-perf-memory-race/harnesses/harness_d_client_churn.py`
Expected: the production path now reports 1 TCP connection for 8 calls.

- [ ] **Step 6: Commit**

```bash
git add src/services/llm.py tests/unit/test_llm_client_reuse.py
git commit -m "perf(llm): reuse one Anthropic client per key instead of one per call

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Refcounted lock eviction + cap `_prior_threads` storage

The trap this task must not fall into (RCA §F5): evicting a lock that merely
reports unlocked splits mutual exclusion — between `release()` and a waiter's
wakeup the old Lock is unlocked but still referenced. Eviction is safe only at
refcount zero, where the refcount is registered synchronously before the first
await of every `acquire_all`.

**Files:**
- Modify: `src/agent/locks.py`
- Modify: `src/agent/simulation.py` (module constant; `_close_thread` append site line 2038; rebuild append site line 5198)
- Test: `tests/unit/test_lock_registry.py` (create), `tests/unit/test_simulation_logic.py`

**Interfaces:**
- Produces: `LockRegistry.__len__() -> int`; `acquire_all` semantics unchanged for callers; `simulation.PRIOR_THREADS_KEPT_PER_PAIR = 50`.

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_lock_registry.py`):

```python
import asyncio

import pytest

from src.agent.locks import LockRegistry


@pytest.mark.asyncio
async def test_registry_evicts_keys_that_no_task_holds_or_wants():
    reg = LockRegistry()
    async with reg.acquire_all("t1"):
        assert len(reg) == 1
    assert len(reg) == 0, "an idle key must not live forever"


@pytest.mark.asyncio
async def test_eviction_never_splits_mutual_exclusion():
    """Three tasks contend one key across an eviction boundary; at no point
    may two of them hold the critical section at once. This is the exact
    failure mode of evict-when-unlocked: between T1's release and T2's
    wakeup the lock reports unlocked, and a naive sweep would hand T3 a
    FRESH Lock object while T2 still waits on the old one."""
    reg = LockRegistry()
    inside = 0
    max_inside = 0

    async def worker():
        nonlocal inside, max_inside
        async with reg.acquire_all("k"):
            inside += 1
            max_inside = max(max_inside, inside)
            await asyncio.sleep(0.02)
            inside -= 1

    await asyncio.gather(*(worker() for _ in range(3)))
    assert max_inside == 1
    assert len(reg) == 0


@pytest.mark.asyncio
async def test_waiting_task_keeps_the_key_alive():
    reg = LockRegistry()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with reg.acquire_all("k"):
            entered.set()
            await release.wait()

    async def waiter():
        await entered.wait()
        async with reg.acquire_all("k"):
            pass

    h = asyncio.create_task(holder())
    w = asyncio.create_task(waiter())
    await entered.wait()
    await asyncio.sleep(0.01)  # let the waiter genuinely park on the lock
    assert len(reg) == 1
    release.set()
    await asyncio.gather(h, w)
    assert len(reg) == 0
```

And in `tests/unit/test_simulation_logic.py`, append:

```python
@pytest.mark.asyncio
async def test_prior_threads_per_pair_storage_is_capped(monkeypatch, tmp_path):
    import src.agent.simulation as sim
    from src.agent.agent import Agent
    from src.agent.simulation import SimulationEngine
    from src.agent.state import ThreadState
    from tests.fakes import FakeSlackClient

    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)

    async def fake_generate(**kwargs):
        return "m"
    monkeypatch.setattr(sim, "generate_agent_response", fake_generate)

    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={
            "blackbird": FakeSlackClient(agent_id="blackbird"),
            "wang": FakeSlackClient(agent_id="wang"),
        },
    )
    for i in range(sim.PRIOR_THREADS_KEPT_PER_PAIR + 10):
        t = ThreadState(thread_id=f"t{i}", channel="general",
                        other_agent_id="wang")
        hub.state.active_threads[t.thread_id] = t
        await eng._close_thread(hub, t, "no_proposal", summary_text=f"s{i}")
    pair = tuple(sorted(["blackbird", "wang"]))
    kept = eng._prior_threads[pair]
    assert len(kept) == sim.PRIOR_THREADS_KEPT_PER_PAIR
    assert kept[-1]["summary"] == f"s{sim.PRIOR_THREADS_KEPT_PER_PAIR + 9}"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_lock_registry.py tests/unit/test_simulation_logic.py::test_prior_threads_per_pair_storage_is_capped -v`
Expected: registry tests FAIL (`TypeError: object of type 'LockRegistry' has no len()` or `len == 1` after exit); cap test FAILS with `len == 60`.

- [ ] **Step 3: Implement.**

(3a) `src/agent/locks.py` — replace `LockRegistry`:

```python
class LockRegistry:
    """Lazily-created asyncio.Lock per key, refcount-evicted. Loop-only.

    Same loop-only invariant as MessageLog. Eviction happens ONLY at
    refcount zero: every ``acquire_all`` registers its intent for all its
    keys SYNCHRONOUSLY, before its first await, so "refcount zero" means no
    holder, no waiter, and no task between registration and acquisition.
    Evicting any earlier splits mutual exclusion: between a holder's
    ``release()`` and a waiter's wakeup the lock reports unlocked while the
    waiter still references the old object, and a fresh Lock for the same
    key would let two tasks into one critical section. Pinned by
    tests/unit/test_lock_registry.py.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def __len__(self) -> int:
        return len(self._locks)

    @asynccontextmanager
    async def acquire_all(self, *keys: str):
        """Acquire every key, in sorted order, releasing in reverse.

        Sorting establishes one global acquisition order across every
        caller, so two callers requesting the same set of keys in opposite
        order can never form a circular wait. Releases everything it
        acquired, in reverse order, even if a later acquisition fails or
        the body raises.
        """
        ordered = sorted(set(keys))
        for key in ordered:  # register intent BEFORE any await
            self._refs[key] = self._refs.get(key, 0) + 1
        acquired: list[asyncio.Lock] = []
        try:
            for key in ordered:
                lock = self.get(key)
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()
            for key in ordered:
                self._refs[key] -= 1
                if self._refs[key] <= 0:
                    del self._refs[key]
                    self._locks.pop(key, None)
```

(3b) `src/agent/simulation.py` — module constant next to
`MEMORY_EVENTS_MAX_AT_SHUTDOWN`:

```python
# Closed-thread summaries kept in memory per agent pair, for the Phase-5
# dedup context. The DB's thread_decisions table remains the full record;
# this bounds only what a process accumulates (audit finding 5: one dict per
# close, forever). Must be >= agent.PRIOR_THREADS_RENDERED_PER_PAIR.
PRIOR_THREADS_KEPT_PER_PAIR = 50
```

(3c) After the `self._prior_threads.setdefault(pair_key, []).append({...})`
in `_close_thread` (line ~2038) add:

```python
            pair_list = self._prior_threads[pair_key]
            if len(pair_list) > PRIOR_THREADS_KEPT_PER_PAIR:
                del pair_list[: len(pair_list) - PRIOR_THREADS_KEPT_PER_PAIR]
```

(3d) Apply the identical three lines after the rebuild-path append at line
~5198 (same dict, same cap — read the surrounding dedup guard there first and
place the cap after it).

- [ ] **Step 4: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_lock_registry.py tests/unit/test_reply_lane.py tests/unit/test_simulation_logic.py tests/integration/test_concurrent_thread_safety.py -v`
Expected: PASS (the reply-lane lock-order tests exercise `acquire_all` heavily — they are the regression net for the refcount change).

- [ ] **Step 5: Commit**

```bash
git add src/agent/locks.py src/agent/simulation.py \
  tests/unit/test_lock_registry.py tests/unit/test_simulation_logic.py
git commit -m "fix(engine): evict idle locks by refcount; cap prior-thread residue per pair

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Cap the Phase-5 prior-threads RENDER at 5 per pair

**Files:**
- Modify: `src/agent/agent.py` (module constant; `build_phase5_prompt` lines 499–516)
- Test: `tests/unit/test_agent_prompts.py`

**Interfaces:**
- Produces: `agent.PRIOR_THREADS_RENDERED_PER_PAIR = 5`; rendered block gains one `- (N earlier closed threads with this agent not shown)` line when truncated.

- [ ] **Step 1: Write the failing test** (append to `tests/unit/test_agent_prompts.py`, using its existing Agent construction pattern — read the file's first test and mirror its fixture/monkeypatch of `PROFILES_DIR`):

```python
def test_phase5_prior_threads_render_is_capped(tmp_path, monkeypatch):
    from src.agent.agent import PRIOR_THREADS_RENDERED_PER_PAIR, Agent

    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    agent = Agent("su", "SuBot", "Su", role="pi_lab")
    prior = {
        "wang": [
            {"channel": "general", "outcome": "no_proposal", "summary": f"s{i}"}
            for i in range(8)
        ]
    }
    _, messages = agent.build_phase5_prompt(prior_threads=prior)
    body = "\n".join(m["content"] for m in messages)
    for i in range(3, 8):
        assert f"s{i}" in body            # the 5 most recent render
    for i in range(3):
        assert f"s{i}" not in body        # older ones do not
    assert "3 earlier closed threads with this agent not shown" in body
    assert PRIOR_THREADS_RENDERED_PER_PAIR == 5

    small = {"wang": prior["wang"][:3]}
    _, messages = agent.build_phase5_prompt(prior_threads=small)
    body = "\n".join(m["content"] for m in messages)
    assert "not shown" not in body        # no banner under the cap
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_agent_prompts.py -k prior_threads_render -v`
Expected: FAIL (`ImportError: cannot import name 'PRIOR_THREADS_RENDERED_PER_PAIR'`).

- [ ] **Step 3: Implement.** In `src/agent/agent.py`, module level (near `_DOI_...` constants):

```python
# Prior-thread dedup lines rendered per partner in the Phase-5 prompt. The
# in-memory list is capped separately (simulation.PRIOR_THREADS_KEPT_PER_PAIR,
# 50); this bounds the PROMPT: measured, every summary-carrying close added
# ~60 input tokens to every later Phase-5 call by both agents, forever —
# 500 closes made a ~38k-token prompt (audit 2026-08-21, finding 6). Five
# recent outcomes plus a count is strictly better dedup context than a
# 500-line list.
PRIOR_THREADS_RENDERED_PER_PAIR = 5
```

In `build_phase5_prompt`, replace the `if prior_threads:` render block's inner
loop:

```python
        if prior_threads:
            prior_parts = []
            for other_id in sorted(prior_threads):
                agent_label = f"{other_id.capitalize()}Bot"
                threads = prior_threads[other_id]
                shown = threads[-PRIOR_THREADS_RENDERED_PER_PAIR:]
                thread_lines = []
                if len(threads) > len(shown):
                    thread_lines.append(
                        f"- ({len(threads) - len(shown)} earlier closed "
                        f"threads with this agent not shown)"
                    )
                for t in shown:
                    outcome_label = t["outcome"].replace("_", " ")
                    if t.get("summary"):
                        thread_lines.append(
                            f"- #{t['channel']} — {outcome_label}: {t['summary']}"
                        )
                    else:
                        thread_lines.append(
                            f"- #{t['channel']} — {outcome_label}"
                        )
                prior_parts.append(f"**{agent_label}**\n" + "\n".join(thread_lines))
            prior_text = "\n\n".join(prior_parts)
```

- [ ] **Step 4: Run the prompt suites AND the golden-master characterization tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_agent_prompts.py tests/characterization/ -v`
Expected: the new test PASSES. The GM snapshot (`test_agent_turn_gm.ambr:1293` pins a "## Prior conversations" block) most likely still passes, because its fixture renders fewer than 5 prior threads. **If and only if a GM test fails:** inspect the snapshot diff; it must consist solely of the capped prior-threads block. Then — as a deliberate, reviewed contract change, NOT drift suppression — regenerate with `.venv-test/bin/python -m pytest tests/characterization -p syrupy --snapshot-update`, re-read the resulting `.ambr` diff line by line, confirm no `pi_lab` guidance string changed, and paste the diff into the commit body. Any other snapshot change is a bug in your edit.

- [ ] **Step 5: Commit**

```bash
git add src/agent/agent.py tests/unit/test_agent_prompts.py tests/characterization/
git commit -m "perf(prompts): render the 5 most recent prior threads per pair, not all of them

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Workstream B — web tier

### Task 7: IntegrityError handling for `waitlist_submit` and `review_proposal` (issue #24 V5)

**Files:**
- Modify: `src/routers/public.py` (`waitlist_submit`, the `await db.commit()` at ~line 503)
- Modify: `src/routers/agent_page.py` (`review_proposal`, the `await db.commit()` after the notification calls)
- Test: `tests/integration/test_concurrent_web_writes.py` (create)

**Interfaces:**
- Consumes: `IntegrityError` already imported in `public.py:20`; `agent_page.py` needs `from sqlalchemy.exc import IntegrityError` added.
- Produces: no API change; concurrent first-time writers get the same success responses instead of a 500.

- [ ] **Step 1: Write the failing test.** Create `tests/integration/test_concurrent_web_writes.py`. It uses the suite's real-Postgres `engine` fixture (same conftest the other integration tests use):

```python
"""Two concurrent first-time writers race SELECT-then-INSERT on a unique
key. Pre-fix, the loser's commit raises IntegrityError out of the handler
(a 500 in production). The sessions are separate on purpose — one session
would serialize the race away."""
import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import WaitlistSignup

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_concurrent_waitlist_signups_do_not_500(engine):
    from src.routers.public import waitlist_submit

    factory = async_sessionmaker(engine, expire_on_commit=False)

    class _Req:  # only what the handler reads
        headers: dict = {}
        client = None
        session: dict = {}

    async def submit():
        async with factory() as db:
            try:
                return await waitlist_submit(
                    _Req(), email="race@example.org", name="R",
                    institution="X", note="", db=db,
                )
            finally:
                await db.close()

    r1, r2 = await asyncio.gather(submit(), submit(), return_exceptions=True)
    for r in (r1, r2):
        assert not isinstance(r, Exception), f"a racer raised: {r!r}"

    async with factory() as db:
        count = (await db.execute(
            select(func.count(WaitlistSignup.id)).where(
                WaitlistSignup.email == "race@example.org"
            )
        )).scalar_one()
    assert count == 1
```

NOTE for the implementer: `waitlist_submit` also calls the module-level
`_waitlist_limiter` and renders `landing.html` via
`templates.TemplateResponse`, both of which can trip on the bare `_Req`
stub (a template's `url_for` needs a real request; the limiter's bucket
accumulates across reruns in one process). If the test fails on anything
other than `IntegrityError`, add these two monkeypatches at the top and keep
the two-session race exactly as written:

```python
    monkeypatch.setattr(
        "src.routers.public._waitlist_limiter",
        type("_L", (), {"allow": staticmethod(lambda ip: True)})(),
    )
    monkeypatch.setattr(
        "src.routers.public.templates.TemplateResponse",
        lambda *a, **k: "rendered",
    )
```

(the assertions are on exceptions and DB state, not the response body). Add the
equivalent test for `review_proposal` the same way ONLY if its dependency
set (login/agent fixtures) already exists in the integration conftest —
otherwise its IntegrityError catch is covered by Step 3's code plus the
waitlist race test pinning the shared pattern.

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_concurrent_web_writes.py -v`
Expected: FAIL — one gathered result is an `IntegrityError` (or the count assertion trips), depending on interleaving; run it twice if it passes once (the race needs both tasks past the SELECT — if it will not fail reliably, insert `await asyncio.sleep(0)` after the SELECT via a monkeypatched wrapper is NOT acceptable; instead pre-warm both sessions with the SELECT by splitting the handler call — see the vote-endpoint test if one exists for the pattern. A test that cannot demonstrate the pre-fix failure must be rewritten, not skipped.)

- [ ] **Step 3: Implement.**

(3a) `public.py` `waitlist_submit`: replace the bare `await db.commit()` after
the `if existing: … else: db.add(…)` block with:

```python
    try:
        await db.commit()
    except IntegrityError:
        # Two concurrent first-time signups raced the unique email
        # constraint; the row exists now — update it exactly like the
        # `existing` branch above (same pattern as the vote endpoint).
        await db.rollback()
        existing = (
            await db.execute(
                select(WaitlistSignup).where(
                    WaitlistSignup.email == email_clean
                )
            )
        ).scalar_one()
        existing.name = name_clean or existing.name
        existing.institution = institution_clean or existing.institution
        existing.note = note_clean or existing.note
        await db.commit()
```

(3b) `agent_page.py`: add `from sqlalchemy.exc import IntegrityError` to the
imports; in `review_proposal` replace the final `await db.commit()` with:

```python
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race on uq_proposal_reviews_decision_agent (double-click,
        # two tabs): a review for this decision+agent now exists. The
        # rollback also discards THIS request's record_engagement /
        # mark_notification_responded writes — correct, because the winning
        # racer performed its own. Same outcome as the SELECT guard above.
        await db.rollback()
        return RedirectResponse(
            url=f"/agent/{agent_id}/dashboard", status_code=302
        )
```

- [ ] **Step 4: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/integration/test_concurrent_web_writes.py tests/integration/ -k "waitlist or review or vote" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/routers/public.py src/routers/agent_page.py tests/integration/test_concurrent_web_writes.py
git commit -m "fix(web): concurrent first-time waitlist/review writers must not 500

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Move blocking Slack provisioning I/O off the web event loop (issue #24 C2)

Two halves, both required (RCA §F7): `asyncio.to_thread` around the five
blocking calls, AND a commit before the manifest call so the session's pooled
connection (web pool: 5) is not held across a possibly-minutes-long
rate-limited wait.

**Files:**
- Modify: `src/services/admin_provisioning.py` (`_config_token` rotate call line ~99; `start_provisioning` lines 145–186; `complete_provisioning` exchange call line ~213)
- Test: `tests/integration/test_provisioning_loop.py` (create)

**Interfaces:**
- Consumes: `slack_provisioning.create_app / lookup_team_id / exchange_code / rotate_config_token` — signatures unchanged, still synchronous.
- Produces: no API change.

- [ ] **Step 1: Write the failing test** (`tests/integration/test_provisioning_loop.py`):

```python
"""start_provisioning awaits synchronous httpx calls (and, on a Slack 429,
time.sleep(retry_after) loops) — on the single-worker web loop that is a
site-wide freeze (issue #24 C2; nginx's 120s proxy_read_timeout turns it
into a 504). The stub below stands in for the blocking manifest call; the
heartbeat asserts the loop stays live while it runs."""
import asyncio
import time

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import AgentRegistry

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_start_provisioning_does_not_block_the_loop(engine, monkeypatch):
    import src.services.admin_provisioning as ap

    def slow_create_app(**kwargs):
        time.sleep(0.5)  # what the real sync httpx.post + retry sleep does
        return {
            "agent_id": kwargs["agent_id"], "bot_name": kwargs["bot_name"],
            "pi_name": kwargs["pi_name"], "app_id": "A1",
            "client_id": "c", "client_secret": "s",
            "oauth_url": "https://slack.test/oauth?x=1",
        }

    monkeypatch.setattr(ap, "create_app", slow_create_app)

    async def fake_config_token(db, *, force_rotate=False):
        return "xoxe.xoxp-config"
    monkeypatch.setattr(ap, "_config_token", fake_config_token)
    monkeypatch.setattr(ap, "lookup_team_id", lambda token: None)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        agent = AgentRegistry(
            agent_id="stub", bot_name="StubBot", pi_name="Stub PI",
            status="pending",
        )
        db.add(agent)
        await db.commit()

        gaps: list[float] = []
        stop = asyncio.Event()

        async def heartbeat():
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.05)
                now = time.monotonic()
                if now - last > 0.25:
                    gaps.append(now - last)
                last = now

        hb = asyncio.create_task(heartbeat())
        url = await ap.start_provisioning(db, agent)
        stop.set()
        await hb
        assert url.startswith("https://slack.test/oauth")
        assert not gaps, f"event loop froze during provisioning: {gaps}"
```

(If `AgentRegistry` requires more non-null columns, read
`src/models/agent_registry.py` and fill them; do not weaken the heartbeat
assertion.)

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/integration/test_provisioning_loop.py -v`
Expected: FAIL with a recorded ~0.5 s gap.

- [ ] **Step 3: Implement.** In `src/services/admin_provisioning.py` (add `import asyncio` to the imports):

(3a) `_config_token`: change
`new_token, new_refresh, exp = rotate_config_token(refresh)` to
`new_token, new_refresh, exp = await asyncio.to_thread(rotate_config_token, refresh)`.

(3b) `start_provisioning`: after `config_token = await _config_token(db)` add:

```python
    # _config_token's reads opened a transaction; commit so the pooled
    # connection is released before the (possibly minutes-long, Slack-rate-
    # limited) manifest call — otherwise one provisioning holds one of the
    # web pool's 5 connections for the whole wait (issue #24 C2).
    await db.commit()
```

then change both `app = _create(config_token)` sites to
`app = await asyncio.to_thread(_create, config_token)`, adding the same
`await db.commit()` after the `force_rotate=True` re-fetch; and change
`team_id = lookup_team_id(team_token)` to
`team_id = await asyncio.to_thread(lookup_team_id, team_token)`.

(3c) `complete_provisioning`: change the `token = exchange_code(...)` call to:

```python
        token = await asyncio.to_thread(
            exchange_code,
            prov.client_id, prov.client_secret, code, _redirect_uri(),
        )
```

- [ ] **Step 4: Run the tests**

Run: `.venv-test/bin/python -m pytest tests/integration/test_provisioning_loop.py tests/unit/test_admin_provisioning.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/services/admin_provisioning.py tests/integration/test_provisioning_loop.py
git commit -m "fix(web): provisioning's blocking Slack I/O comes off the event loop (issue #24 C2)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Web-tier pool hardening (issue #25 P3)

**Files:**
- Modify: `src/database.py` (`_get_engine`, lines 15–22)
- Test: `tests/unit/test_database_pool.py` (create)

- [ ] **Step 1: Write the failing test** (`tests/unit/test_database_pool.py`):

```python
def test_web_engine_pre_pings_and_recycles(monkeypatch):
    import src.database as database

    class _S:
        database_url = "postgresql+asyncpg://u:p@localhost:5499/none"

    monkeypatch.setattr(database, "get_settings", lambda: _S())
    engine = database._get_engine()  # creating an engine opens no connection
    try:
        assert engine.pool._pre_ping is True
        assert engine.pool._recycle == 1800
    finally:
        engine.sync_engine.dispose()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_database_pool.py -v`
Expected: FAIL (`assert False is True`).

- [ ] **Step 3: Implement.** In `src/database.py`:

```python
def _get_engine():
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=5,
        max_overflow=10,
        # A DB restart otherwise leaves stale pooled connections that each
        # 500 one request; pre-ping revalidates on checkout (issue #25 P3).
        # The agent process's own engine (src/agent/main.py) has carried
        # pool_pre_ping=True since it sized its own pool — that half is the
        # web-tier transplant. It does NOT set pool_recycle (verified
        # 2026-08-21: no such argument anywhere in agent/main.py), so
        # pool_recycle=1800 below is a new addition on its own merits —
        # retiring long-lived connections before infra does — not a
        # transplant of something the agent process already does.
        pool_pre_ping=True,
        pool_recycle=1800,
    )
```

- [ ] **Step 4: Run the test**

Run: `.venv-test/bin/python -m pytest tests/unit/test_database_pool.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/database.py tests/unit/test_database_pool.py
git commit -m "fix(web): pool_pre_ping + pool_recycle on the web engine (issue #25 P3)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: Badge middleware `/static` short-circuit + migration 0033 (indexes) (issue #25 P1)

**Files:**
- Modify: `src/main.py` (`AgentBadgeMiddleware.dispatch`, line ~28)
- Modify: `src/models/agent_activity.py` (`ThreadDecision`: add `__table_args__`)
- Create: `alembic/versions/0033_badge_and_fk_indexes.py`
- Test: `tests/unit/test_badge_middleware.py` (create)

- [ ] **Step 1: Write the failing test** (`tests/unit/test_badge_middleware.py`):

```python
import pytest
from starlette.requests import Request

from src.main import AgentBadgeMiddleware


def _request(path: str) -> Request:
    return Request({
        "type": "http", "method": "GET", "path": path, "headers": [],
        "query_string": b"",
    })


@pytest.mark.asyncio
async def test_static_and_health_requests_never_touch_the_db(monkeypatch):
    import src.main as main_mod

    def _explode():
        raise AssertionError("session factory must not be used on this path")
    monkeypatch.setattr(main_mod, "get_session_factory", _explode)

    async def call_next(request):
        return "downstream"

    mw = AgentBadgeMiddleware(app=None)
    for path in ("/static/app.css", "/api/health"):
        assert await mw.dispatch(_request(path), call_next) == "downstream"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv-test/bin/python -m pytest tests/unit/test_badge_middleware.py -v`
Expected: FAIL — the current dispatch reads `get_settings()` and the session
path for every request (the `AssertionError` fires, or `request.state`
assignment errors on the bare scope; either failure is the point).

- [ ] **Step 3: Implement.**

(3a) `src/main.py` — first lines of `dispatch`:

```python
    async def dispatch(self, request: Request, call_next):
        # Asset and health probes carry no nav and need no badge; without
        # this guard every /static request with a session cookie ran the
        # per-agent COUNT queries below (issue #25 P1 — nginx has no
        # location /static block, so they all reach uvicorn).
        path = request.url.path
        if path.startswith("/static/") or path == "/api/health":
            return await call_next(request)
        request.state.posthog_api_key = get_settings().posthog_api_key
        ...
```

(3b) `src/models/agent_activity.py` — on `ThreadDecision`, after the mapped
columns (the class currently has NO `__table_args__`; `Index` is already
imported in this module):

```python
    __table_args__ = (
        # The badge middleware counts proposals per agent on every
        # authenticated page load; measured ~129x with these (issue #25 P1).
        Index("ix_thread_decisions_agent_a_outcome", "agent_a", "outcome"),
        Index("ix_thread_decisions_agent_b_outcome", "agent_b", "outcome"),
    )
```

(3c) `alembic/versions/0033_badge_and_fk_indexes.py` — model it on 0032's
header/format (read that file first for the revision-id style):

```python
"""Badge composites on thread_decisions + the 18 unindexed ondelete-FK targets.

Additive DDL only: safe in either deploy order (old code with the new schema
and new code with the old schema both keep working; the new code merely runs
its existing queries faster once the indexes exist).

Revision ID: 0033
Revises: 0032
"""
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

_FK_INDEXES = [
    ("access_allowlist", "added_by_user_id"),
    ("agent_delegates", "invitation_id"),
    ("agent_delegates", "user_id"),
    ("agents", "approved_by"),
    ("delegate_invitations", "accepted_by_user_id"),
    ("delegate_invitations", "invited_by_user_id"),
    ("email_notifications", "agent_registry_id"),
    ("email_notifications", "thread_decision_id"),
    ("private_channel_members", "added_by_user_id"),
    ("private_channel_members", "user_id"),
    ("profile_revisions", "changed_by_user_id"),
    ("proposal_reviews", "delegate_user_id"),
    ("proposal_reviews", "reviewed_by_user_id"),
    ("proposal_reviews", "user_id"),
    ("slack_app_provisions", "agent_registry_id"),
    ("cohorts", "created_by"),
    ("cohort_memberships", "added_by"),
    ("cohort_audit_events", "actor_id"),
]


def upgrade() -> None:
    op.create_index(
        "ix_thread_decisions_agent_a_outcome",
        "thread_decisions", ["agent_a", "outcome"],
    )
    op.create_index(
        "ix_thread_decisions_agent_b_outcome",
        "thread_decisions", ["agent_b", "outcome"],
    )
    for table, col in _FK_INDEXES:
        op.create_index(f"ix_{table}_{col}", table, [col])


def downgrade() -> None:
    for table, col in reversed(_FK_INDEXES):
        op.drop_index(f"ix_{table}_{col}", table_name=table)
    op.drop_index(
        "ix_thread_decisions_agent_b_outcome", table_name="thread_decisions"
    )
    op.drop_index(
        "ix_thread_decisions_agent_a_outcome", table_name="thread_decisions"
    )
```

Before writing it, verify every `(table, column)` pair against the models
(`grep -rn "<column>" src/models/`) — if any has since gained an index or
been renamed, adjust the list and note it in the commit body.

- [ ] **Step 4: Run the gate — the migration round-trip is the migration's test**

Run: `.venv-test/bin/python -m pytest tests/unit/test_badge_middleware.py -v && ./scripts/ci.sh`
Expected: middleware test PASSES; ci.sh's alembic single-head check and full upgrade→downgrade→upgrade round trip PASS (they will catch any bad table/column name in 0033), and the full suite stays green.

- [ ] **Step 5: Commit**

```bash
git add src/main.py src/models/agent_activity.py alembic/versions/0033_badge_and_fk_indexes.py tests/unit/test_badge_middleware.py
git commit -m "perf(web): /static short-circuit in badge middleware; 0033 adds badge + FK indexes (issue #25 P1)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Workstream C — transport & accounting

### Task 11: PubMed rate pacing + retry (issue #23 V9)

The arithmetic the naive fix gets wrong (RCA §F7): a sleep INSIDE a
semaphore bounds nothing — rate = concurrency ÷ per-request time, so
Semaphore(8) with a 0.12 s sleep bursts far past NCBI's keyless 3/s. Rate
needs start-spacing; concurrency is a separate, smaller bound.

**Files:**
- Modify: `src/services/pubmed.py` (lines 72–96)
- Test: `tests/unit/test_pubmed_pacing.py` (create)

**Interfaces:**
- Produces: `pubmed._pace() -> None` (async), `pubmed._pace_interval() -> float`, `pubmed._make_client() -> httpx.AsyncClient` (test seam). `_ncbi_get`'s signature unchanged.

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_pubmed_pacing.py`):

```python
import asyncio
import time

import httpx
import pytest

from src.services import pubmed


@pytest.mark.asyncio
async def test_pace_spaces_request_starts(monkeypatch):
    monkeypatch.setattr(pubmed, "_pace_interval", lambda: 0.05)
    pubmed._next_slot = 0.0
    t0 = time.monotonic()
    await asyncio.gather(*(pubmed._pace() for _ in range(10)))
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.05 * 9, (
        f"10 paced starts finished in {elapsed:.3f}s — pacing is not "
        f"bounding the rate"
    )


@pytest.mark.asyncio
async def test_ncbi_get_retries_a_429_and_paces_before_raising(monkeypatch):
    calls = []

    def handler(request):
        calls.append(time.monotonic())
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        pubmed, "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(pubmed, "_pace_interval", lambda: 0.01)
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        await real_sleep(min(seconds, 0.01))

    monkeypatch.setattr(pubmed.asyncio, "sleep", fast_sleep)
    resp = await pubmed._ncbi_get("https://x.test/e", {})
    assert resp.status_code == 200
    assert len(calls) == 2  # one 429, one retry — not an immediate raise
```

(The pacing test's keyless interval assertion: also add
`def test_pace_interval_tracks_the_api_key(monkeypatch)` asserting 0.34
without `ncbi_api_key` and 0.11 with one, monkeypatching
`pubmed.get_settings`.)

- [ ] **Step 2: Run to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_pubmed_pacing.py -v`
Expected: FAIL (`AttributeError: ... has no attribute '_pace_interval'`).

- [ ] **Step 3: Implement.** In `src/services/pubmed.py`, replace the semaphore block and `_ncbi_get`:

```python
# Rate limiting: NCBI allows 10 req/s with an API key, 3 req/s without.
# Two separate bounds, deliberately: the semaphore caps CONCURRENCY (open
# sockets against NCBI), and _pace() caps the RATE by spacing request
# STARTS. The old design slept inside the semaphore, which bounds nothing:
# rate = concurrency / per-request-time, so 8 concurrent holders each
# pausing 0.12s could burst far past the keyless 3/s (issue #23 V9).
_request_semaphore = asyncio.Semaphore(3)
_next_slot: float = 0.0


def _pace_interval() -> float:
    return 0.11 if get_settings().ncbi_api_key else 0.34


async def _pace() -> None:
    """Space request starts at least _pace_interval() apart, process-wide.

    The read-modify-write of _next_slot has no await between read and
    write, so it is atomic on the event loop — no lock object needed (and
    none wanted: a module-level asyncio primitive binds to the first event
    loop that touches it, which breaks under pytest's per-test loops).
    """
    global _next_slot
    loop = asyncio.get_running_loop()
    now = loop.time()
    wait = _next_slot - now
    _next_slot = max(now, _next_slot) + _pace_interval()
    if wait > 0:
        await asyncio.sleep(wait)


def _make_client() -> httpx.AsyncClient:
    """Client factory — a seam so tests can inject httpx.MockTransport."""
    return httpx.AsyncClient(timeout=60, follow_redirects=True)


async def _ncbi_get(url: str, params: dict[str, Any]) -> httpx.Response:
    """Make a rate-limited, identified GET request to NCBI, with retry.

    Pacing runs BEFORE raise_for_status — the old order skipped pacing
    exactly when NCBI was already 429-ing us. Retries cover the transient
    statuses NCBI actually emits under load; anything else raises as before.
    """
    settings = get_settings()
    if settings.ncbi_api_key:
        params["api_key"] = settings.ncbi_api_key
    params.setdefault("tool", _NCBI_TOOL)
    params.setdefault("email", settings.ncbi_contact_email or settings.ses_sender_email)
    async with _request_semaphore:
        async with _make_client() as client:
            for attempt in range(3):
                await _pace()
                resp = await client.get(url, params=params)
                if resp.status_code in (429, 500, 502, 503) and attempt < 2:
                    await asyncio.sleep(1.0 * (2 ** attempt))
                    continue
                resp.raise_for_status()
                return resp
```

- [ ] **Step 4: Run the tests + every pubmed consumer**

Run: `.venv-test/bin/python -m pytest tests/unit/test_pubmed_pacing.py tests/ -k "pubmed or pipeline" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/services/pubmed.py tests/unit/test_pubmed_pacing.py
git commit -m "fix(pubmed): pace request starts to NCBI's real limit; retry transient failures (issue #23 V9)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 12: Charge tool budgets on success, not on attempt (issue #23 COR-30)

Invariant (RCA §F7): the CAP CHECK stays before the fetch (an over-budget
call is refused without spending a network call); only the INCREMENT moves
after success. The check→fetch→charge window cannot double-spend because
tool rounds are sequential within a turn and the thread lock serializes
turns per thread.

**Files:**
- Modify: `src/agent/tools.py` (the `retrieve_abstract` and `retrieve_full_text` branches, lines 261–283; split `_execute_retrieve_abstract`/`_execute_retrieve_full_text` into fetch + format)
- Test: `tests/unit/test_tools.py` (or the existing tools test module — locate with `grep -rln "execute_tool" tests/unit/`)

- [ ] **Step 1: Write the failing test** (in the located tools test module):

```python
@pytest.mark.asyncio
async def test_failed_abstract_fetch_does_not_consume_the_budget(monkeypatch):
    from src.agent import tools
    from src.agent.state import ThreadState

    thread = ThreadState(thread_id="t", channel="c", other_agent_id="x")

    async def failing_fetch(ref):
        return {"error": "PubMed lookup failed"}
    monkeypatch.setattr(tools, "fetch_abstract", failing_fetch)

    out = await tools.execute_tool(
        "retrieve_abstract", {"pmid_or_doi": "12345"}, "su",
        thread_state=thread, role="pi_lab",
    )
    assert "failed" in out
    assert thread.abstracts_other == 0, "a failed fetch consumed the budget"

    async def ok_fetch(ref):
        return {"pmid": "12345", "title": "T", "abstract": "A"}
    monkeypatch.setattr(tools, "fetch_abstract", ok_fetch)
    out = await tools.execute_tool(
        "retrieve_abstract", {"pmid_or_doi": "12345"}, "su",
        thread_state=thread, role="pi_lab",
    )
    assert "Title:" in out
    assert thread.abstracts_other == 1


@pytest.mark.asyncio
async def test_over_cap_abstract_call_is_refused_without_fetching(monkeypatch):
    from src.agent import tools
    from src.agent.state import ThreadState
    from src.config import get_settings

    thread = ThreadState(thread_id="t", channel="c", other_agent_id="x")
    thread.abstracts_other = get_settings().max_abstracts_other_per_thread
    fetched = []

    async def spy_fetch(ref):
        fetched.append(ref)
        return {"pmid": "1", "title": "T"}
    monkeypatch.setattr(tools, "fetch_abstract", spy_fetch)
    out = await tools.execute_tool(
        "retrieve_abstract", {"pmid_or_doi": "1"}, "su",
        thread_state=thread, role="pi_lab",
    )
    assert "Rate limit" in out
    assert fetched == [], "an over-cap call must not reach the network"
```

(Check `pi_lab`'s role.toml allows `retrieve_abstract`; if role gating trips,
use the role the existing tools tests use.)

- [ ] **Step 2: Run to verify the first test fails**

Run: `.venv-test/bin/python -m pytest tests/unit -k "budget or over_cap" -v`
Expected: the failed-fetch test FAILS (`abstracts_other == 1` after the error); the over-cap test may already pass — keep it as the regression net for the invariant.

- [ ] **Step 3: Implement.** In `src/agent/tools.py`:

(3a) Split the two executors into fetch+format so the branch can charge
between them: rename `_execute_retrieve_abstract(pmid_or_doi)`'s formatting
body into `_format_abstract(result: dict) -> str` (everything after the
`if "error" in result` check), and the same for
`_format_full_text(result: dict) -> str`.

(3b) Replace the two branches in `execute_tool`:

```python
        elif tool_name == "retrieve_abstract":
            ref = _require_arg(tool_input, "pmid_or_doi", tool_name)
            is_own = bool(own_dois) and bool(_extract_dois(ref) & own_dois)
            if thread_state and not is_own:
                from src.config import get_settings
                settings = get_settings()
                if thread_state.abstracts_other >= settings.max_abstracts_other_per_thread:
                    return "Rate limit: you have used all your abstract retrievals for other labs in this thread."
            result = await fetch_abstract(ref)
            if "error" in result:
                return result["error"]
            # Charge only a retrieval that returned a paper — the debit used
            # to land before the fetch, so an outage consumed the budget with
            # no refund (issue #23 COR-30). Safe against double-spend: tool
            # rounds are sequential within a turn, and the thread lock
            # serializes turns per thread.
            if thread_state and not is_own:
                thread_state.abstracts_other += 1
            return _format_abstract(result)

        elif tool_name == "retrieve_full_text":
            ref = _require_arg(tool_input, "pmid_or_doi", tool_name)
            if thread_state:
                from src.config import get_settings
                settings = get_settings()
                if thread_state.full_text >= settings.max_full_text_per_thread:
                    return "Rate limit: you have used all your full-text retrievals in this thread."
            result = await fetch_full_text(ref)
            if "error" in result:
                return result["error"]
            if thread_state:
                thread_state.full_text += 1
            return _format_full_text(result)
```

- [ ] **Step 4: Run the tool suites**

Run: `.venv-test/bin/python -m pytest tests/unit -k tools -v`
Expected: PASS, including any pre-existing budget tests (if one pinned the
old charge-on-attempt behavior, invert it — that behavior is the bug).

- [ ] **Step 5: Commit**

```bash
git add src/agent/tools.py tests/unit/
git commit -m "fix(tools): charge the per-thread retrieval budget on success, not on attempt

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 13: Atomic read-modify-writes — `profile_version` (4 sites) and `delegate_slack_ids` (3 sites) (issue #22 C1)

Trap (RCA §F7): SQL-expression assignment expires the attribute;
`profile_pipeline.py:424` logs `profile.profile_version` right after, and a
lazy async re-load raises `MissingGreenlet` — that site needs an explicit
refresh. The array dedup must live IN the SQL (`WHERE NOT … @> ARRAY[sid]`),
or the append still races.

**Files:**
- Modify: `src/services/profile_pipeline.py` (line 416 + refresh before line 424), `src/routers/profile.py` (line 159), `src/routers/onboarding.py` (line 182), `src/routers/agent_page.py` (line 955 and the two array sites at ~1062 and ~1248), `src/routers/invite.py` (~line 241)
- Test: `tests/integration/test_atomic_rmw.py` (create)

- [ ] **Step 1: Write the failing tests** (`tests/integration/test_atomic_rmw.py`, real-Postgres `engine` fixture):

```python
"""Concurrent read-modify-writes on ResearcherProfile.profile_version and
AgentRegistry.delegate_slack_ids. Pre-fix, both racers read the same prior
value and one write is lost."""
import asyncio
import uuid

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import AgentRegistry, ResearcherProfile, User

pytestmark = pytest.mark.integration


async def _make_profile(factory):
    async with factory() as db:
        user = User(orcid_id=f"0000-0000-0000-{uuid.uuid4().hex[:4]}",
                    name="Race Test")
        db.add(user)
        await db.flush()
        profile = ResearcherProfile(user_id=user.id, profile_version=0)
        db.add(profile)
        await db.commit()
        return profile.id


@pytest.mark.asyncio
async def test_concurrent_version_bumps_both_land(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    profile_id = await _make_profile(factory)
    gate = asyncio.Barrier(2)

    async def bump():
        async with factory() as db:
            profile = (await db.execute(
                select(ResearcherProfile).where(ResearcherProfile.id == profile_id)
            )).scalar_one()
            await gate.wait()  # both sessions hold the pre-bump row
            # The exact expression each production site now uses:
            profile.profile_version = func.coalesce(
                ResearcherProfile.profile_version, 0
            ) + 1
            await db.commit()

    await asyncio.gather(bump(), bump())
    async with factory() as db:
        version = (await db.execute(
            select(ResearcherProfile.profile_version).where(
                ResearcherProfile.id == profile_id
            )
        )).scalar_one()
    assert version == 2, f"a concurrent bump was lost: version={version}"
```

(Adjust `User`/`ResearcherProfile` required columns to the models — read
them first.) And the mirror test for the array sites, in the same file,
using the exact statement Step 3b installs:

```python
async def _append_delegate(factory, agent_id, sid, gate):
    from sqlalchemy import text as sa_text
    from sqlalchemy import update as sa_update
    async with factory() as db:
        agent = (await db.execute(
            select(AgentRegistry).where(AgentRegistry.id == agent_id)
        )).scalar_one()
        await gate.wait()  # both sessions hold the pre-append row
        await db.execute(
            sa_update(AgentRegistry)
            .where(
                AgentRegistry.id == agent.id,
                sa_text(
                    "NOT (coalesce(delegate_slack_ids, '{}'::varchar[]) @> ARRAY[:sid]::varchar[])"
                ).bindparams(sid=sid),
            )
            .values(
                delegate_slack_ids=sa_text(
                    "array_append(coalesce(delegate_slack_ids, '{}'::varchar[]), :sid2)"
                ).bindparams(sid2=sid)
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_concurrent_delegate_appends_both_land_and_dedup(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        agent = AgentRegistry(
            agent_id=f"race{uuid.uuid4().hex[:6]}", bot_name="RaceBot",
            pi_name="Race PI", status="pending",
        )
        db.add(agent)
        await db.commit()
        agent_id = agent.id

    gate = asyncio.Barrier(2)
    await asyncio.gather(
        _append_delegate(factory, agent_id, "U1", gate),
        _append_delegate(factory, agent_id, "U2", gate),
    )
    gate = asyncio.Barrier(2)
    await asyncio.gather(
        _append_delegate(factory, agent_id, "U3", gate),
        _append_delegate(factory, agent_id, "U3", gate),
    )
    async with factory() as db:
        ids = (await db.execute(
            select(AgentRegistry.delegate_slack_ids).where(
                AgentRegistry.id == agent_id
            )
        )).scalar_one()
    assert sorted(ids) == ["U1", "U2", "U3"], (
        f"lost or duplicated a concurrent delegate append: {ids}"
    )
```

- [ ] **Step 2: Run to verify the version test currently fails when written against the OLD expression.** First run it with the production pattern of today (`profile.profile_version = (profile.profile_version or 0) + 1`) in the `bump()` body to see `version == 1` (the lost update, proving the harness), then switch `bump()` to the new expression as shown — it should then pass on its own. This two-step is the test's own falsifiability check; keep only the new-expression version.

- [ ] **Step 3: Implement.**

(3a) At all four `profile_version` sites, replace
`profile.profile_version = (profile.profile_version or 0) + 1` with:

```python
    # SQL-side increment: the Python read-modify-write lost updates when two
    # writers raced (issue #22 C1) — worst at the pipeline site, which holds
    # the row across dozens of awaits between load and write.
    profile.profile_version = func.coalesce(ResearcherProfile.profile_version, 0) + 1
```

adding `from sqlalchemy import func` where missing. In
`src/services/profile_pipeline.py` ONLY, the expression assignment expires
the attribute and line ~424 logs it — insert after the flush that follows the
assignment (find the surrounding `await db.flush()`; add one if the log line
precedes any flush):

```python
    await db.flush()
    await db.refresh(profile, ["profile_version"])
```

(3b) At the two append sites (`agent_page.py` ~1062, `invite.py` ~241),
replace the `current_ids = list(...) / if sid not in current_ids / append /
assign` block with an atomic, self-deduplicating UPDATE:

```python
                from sqlalchemy import text as sa_text, update as sa_update
                await db.execute(
                    sa_update(AgentRegistry)
                    .where(
                        AgentRegistry.id == agent.id,
                        sa_text(
                            "NOT (coalesce(delegate_slack_ids, '{}'::varchar[]) @> ARRAY[:sid]::varchar[])"
                        ).bindparams(sid=sid),
                    )
                    .values(
                        delegate_slack_ids=sa_text(
                            "array_append(coalesce(delegate_slack_ids, '{}'::varchar[]), :sid2)"
                        ).bindparams(sid2=sid)
                    )
                )
```

(keep the surrounding `await db.commit()` / redirect flow of each site
unchanged; in `agent_page.py`'s site the commit only ran when the id was
new — with the guarded UPDATE, commit unconditionally: a no-op UPDATE
commits nothing harmful).

(3c) At the removal site (`agent_page.py` ~1248), replace the
read-remove-assign block with:

```python
                    from sqlalchemy import text as sa_text, update as sa_update
                    if sid:
                        await db.execute(
                            sa_update(AgentRegistry)
                            .where(AgentRegistry.id == agent.id)
                            .values(
                                delegate_slack_ids=sa_text(
                                    "nullif(array_remove(coalesce(delegate_slack_ids, '{}'::varchar[]), :sid), '{}'::varchar[])"
                                ).bindparams(sid=sid)
                            )
                        )
```

- [ ] **Step 4: Run the tests + the touched routers' suites**

Run: `.venv-test/bin/python -m pytest tests/integration/test_atomic_rmw.py tests/ -k "profile or onboarding or delegate or invite or pipeline" -v`
Expected: PASS, including the pipeline GM characterization tests (`test_profile_pipeline_gm`) — if the pipeline GM fails on the refresh, read the diff; only ordering of identical operations may change, nothing content-bearing.

- [ ] **Step 5: Commit**

```bash
git add src/services/profile_pipeline.py src/routers/profile.py src/routers/onboarding.py \
  src/routers/agent_page.py src/routers/invite.py tests/integration/test_atomic_rmw.py
git commit -m "fix(data): atomic SQL increments and array ops for the two proven RMW races (issue #22 C1)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Final verification (after all tasks, or after each workstream)

- [ ] Run the full gate: `./scripts/ci.sh` — must pass end to end (alembic single-head + round trip incl. 0033, ruff ratchet, pytest with coverage floor).
- [ ] Re-run all six audit harnesses (`docs/audits/2026-08-21-perf-memory-race/harnesses/`) and confirm each defect's measurement is gone: harness A's defect run collapses to the control run's shape (memory calls absent from the dispatch span); harness B reports no stalls > 200 ms; harness C per-tick totals in single-digit ms; harness D reports 1 connection; harness E shows the lock registry near-empty and `_prior_threads` capped; harness F's 500-close prompt within ~2k tokens of the 10-close prompt. Record the numbers in the PR description.
- [ ] Update `docs/audits/2026-08-21-perf-memory-race/README.md` with a short "Remediated" line per finding pointing at the fixing commit.

## Deploy & operator notes (paste into the PR description)

- **Engine changes (Tasks 1–6) are inert until the agent image is rebuilt and `blackbird-agent-run` is restarted** — the agent image bakes `src/` (CLAUDE.md). The operator decides when; follow CLAUDE.md's full restart sequence (`docker stop -t 420`, save logs, `$DC up -d --build blackbird-app worker`, `$DC --profile agent build agent`, migrate, start).
- **Migration 0033 is additive (indexes only)** — no deploy-order constraint; apply with `$DC run --rm blackbird-app alembic upgrade head` and confirm `alembic current` = `0033`.
- **No config changes.** `REPLY_LANE_MAX_IN_FLIGHT` stays 4; after Task 1 the semaphore is no longer held across memory synthesis, which is what made 4 dangerous.
- Behavior change worth stating to reviewers: working-memory updates now land within ~one main-loop tick after a close instead of synchronously inside it; up to 10 queued updates run at shutdown and any beyond that are dropped with a WARNING (previously they ran inline, or died silently with the SIGKILL they invited).

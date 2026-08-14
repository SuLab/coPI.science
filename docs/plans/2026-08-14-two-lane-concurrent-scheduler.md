# Two-Lane Concurrent Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the single sequential turn loop into a paced post lane (Phase 1 + 5) and an unpaced, concurrent reply lane (Phase 3 + 4), so the hub replies as fast as it needs to and two agents can hold a rapid back-and-forth inside a thread.

**Architecture:** Phases split across two lanes. The post lane keeps today's staleness×load weighted selection and runs one agent at a time. The reply lane is a dispatcher that services `(agent, thread)` pairs owing a reply as concurrent tasks, bounded by a global semaphore, serialised by a per-thread lock held **across the LLM call**. The hub needs no special case: it declares `post_types = []`, so it only ever replies and therefore lives entirely in the unpaced lane.

**Tech Stack:** Python 3.12, asyncio, SQLAlchemy 2 async + asyncpg, pytest + pytest-asyncio, slack_sdk (synchronous client), Anthropic SDK (synchronous client, already wrapped via `asyncio.to_thread`).

**Spec:** `docs/specs/2026-08-14-two-lane-concurrent-scheduler-design.md`

## Global Constraints

- **Run the gate before every commit:** `./scripts/ci.sh`. It is the whole gate; there is no server-side CI. Green means: single alembic head, migration round trip, `ruff check tests/` at **zero** findings, `ruff check src/` under the ratchet ceiling (currently 231), full pytest with branch coverage ≥ 60%.
- **Run pytest on the host, never in the container:** `.venv-test/bin/python -m pytest tests/ -v`. The image has no `[dev]` extra.
- **TDD is mandatory.** Write the failing test, run it, watch it fail *for the intended reason*, then implement. A test that passes on first run is testing the wrong thing.
- **Never run `pytest --snapshot-update`** to resolve a golden-master mismatch. `tests/characterization/__snapshots__/test_agent_turn_gm.ambr` pins prompt text deliberately.
- **Two stacks share this host.** Never run a bare `docker compose`; always `-f docker-compose.prod.yml`. Never pass `--remove-orphans`. This repo's containers are `copi-blackbird-*` / `blackbird-agent-run`; `agent-run` and `copi-python-*` belong to a different production deployment.
- **`MessageLog` is loop-safe, not thread-safe.** Post in a thread; append on the loop. Never call `MessageLog.append()` from inside `asyncio.to_thread`.
- **Do not reword `src/agent/thread_guidance.py`'s `pi_lab` strings.** They are byte-pinned by the golden masters.
- Settings live in `src/config.py` on the `Settings` pydantic model. `extra="ignore"`, so a misspelled env var is silently dropped — add the field, don't rely on the env name.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/agent/simulation.py` | Engine: lanes, dispatcher, locks, phases | Modify (large) |
| `src/agent/locks.py` | **New.** Per-key `asyncio.Lock` registry + sorted multi-acquire | Create |
| `src/agent/slack_client.py` | Slack transport | Modify (off-loop wrappers) |
| `src/agent/state.py` | `AgentState` / `ThreadState` fields | Modify |
| `src/agent/agent.py` | `record_api_call`, reservation helpers | Modify |
| `src/agent/main.py` | Engine construction, DB pool sizing | Modify |
| `src/config.py` | New settings | Modify |
| `tests/unit/test_engine_locks.py` | **New.** Lock registry + deadlock ordering | Create |
| `tests/unit/test_reply_lane.py` | **New.** Dispatcher, dedupe, concurrency bound | Create |
| `tests/unit/test_rate_reservation.py` | **New.** Reservation limiter | Create |
| `tests/unit/test_slack_off_loop.py` | **New.** Loop responsiveness during Slack I/O | Create |
| `tests/unit/test_post_lane.py` | **New.** Post lane pacing, hub exclusion | Create |
| `tests/integration/test_concurrent_thread_safety.py` | **New.** The adversarial races | Create |

---

## Phase A — Prerequisites

Independently valuable. Each of these is a real bug fix today, before any concurrency exists.

### Task 1: Move the Slack transport off the event loop

**Files:**
- Modify: `src/agent/slack_client.py` (add async wrappers; `post_message:752`, `poll_channel_messages:506`, `get_full_channel_history:592`, `get_all_thread_replies`)
- Modify: `src/agent/simulation.py:3050` (and every other sync call site)
- Test: `tests/unit/test_slack_off_loop.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `AgentSlackClient.apost_message(...)`, `.apoll_channel_messages(...)`, `.aget_full_channel_history(...)`, `.aget_all_thread_replies(...)` — all `async def`, same parameters and return types as their sync twins.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_slack_off_loop.py
"""The Slack client is synchronous and its 429 handler sleeps. Called directly
from a coroutine it pins the loop for the whole HTTP request, so one Slack
rate-limit stall freezes the hub and all 62 spokes."""
import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_apost_message_does_not_pin_the_event_loop(monkeypatch):
    from src.agent.slack_client import AgentSlackClient

    client = AgentSlackClient.__new__(AgentSlackClient)

    def _blocking_post(*a, **kw):
        time.sleep(0.30)
        return {"ok": True, "ts": "1.0"}

    monkeypatch.setattr(client, "post_message", _blocking_post, raising=False)

    ticks = 0
    stop = False

    async def ticker():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.01)

    t = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    result = await client.apost_message("C1", "hello")
    stop = True
    await t

    assert result["ok"] is True
    assert ticks > 5, f"event loop pinned during the Slack post (only {ticks} tick(s))"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_slack_off_loop.py -v`
Expected: FAIL — `AttributeError: 'AgentSlackClient' object has no attribute 'apost_message'`.

- [ ] **Step 3: Add the async wrappers**

```python
# src/agent/slack_client.py — near the other public methods
    async def apost_message(self, *args, **kwargs):
        """``post_message`` awaited OFF the loop thread.

        The slack_sdk client is synchronous and its 429 handler calls
        time.sleep (see _api's retry path), so calling it from a coroutine
        pins the loop for the entire request. Mirrors services/llm._acreate.

        MessageLog.append must NOT be called from inside this thread — it is
        loop-safe, not thread-safe. Post here; append on the loop.
        """
        return await asyncio.to_thread(self.post_message, *args, **kwargs)

    async def apoll_channel_messages(self, *args, **kwargs):
        return await asyncio.to_thread(self.poll_channel_messages, *args, **kwargs)

    async def aget_full_channel_history(self, *args, **kwargs):
        return await asyncio.to_thread(self.get_full_channel_history, *args, **kwargs)

    async def aget_all_thread_replies(self, *args, **kwargs):
        return await asyncio.to_thread(self.get_all_thread_replies, *args, **kwargs)
```

Add `import asyncio` at the top of the module if absent.

- [ ] **Step 4: Run the test and watch it pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_slack_off_loop.py -v`
Expected: PASS.

- [ ] **Step 5: Switch the engine's call sites**

In `src/agent/simulation.py`, change each synchronous Slack call to its `a`-prefixed twin and `await` it. The sites: `_post_message` (`:3050` `client.post_message`), `_poll_slack_for_bot_messages` (`client.poll_channel_messages`), `_rebuild_state_from_slack` (`client.get_full_channel_history`, `client.get_all_thread_replies`).

**Critical:** the `message_log.append(...)` that follows each fetch stays on the loop. Only the network call moves. Do not wrap a block that contains an append.

- [ ] **Step 6: Add the loop-only invariant test**

```python
# tests/unit/test_slack_off_loop.py (append)
def test_message_log_append_is_documented_loop_only():
    """append()'s dedupe is a check-then-act and _record mutates two structures.
    Loop-safe (no await inside), NOT thread-safe. Pin the docstring so a future
    change to move it into a thread has to confront this."""
    from src.agent.message_log import MessageLog

    doc = (MessageLog.append.__doc__ or "").lower()
    assert "not thread-safe" in doc or "loop-only" in doc
```

Then add that sentence to `MessageLog.append`'s docstring in `src/agent/message_log.py`.

- [ ] **Step 7: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/slack_client.py src/agent/simulation.py src/agent/message_log.py tests/unit/test_slack_off_loop.py
git commit -m "fix(slack): await the synchronous transport off the event loop

The slack_sdk client is sync and its 429 handler sleeps, so every post and
poll pinned the loop for the whole request — one Slack rate-limit stall froze
the hub and all 62 spokes. Wrapped in asyncio.to_thread, same as llm._acreate.

MessageLog.append stays on the loop and now says so: its dedupe is a
check-then-act and _record mutates two structures, so it is loop-safe but not
thread-safe."
```

---

### Task 2: Size the DB pool and stop swallowing checkout failures

**Files:**
- Modify: `src/agent/main.py:160`
- Modify: `src/config.py`
- Modify: `src/agent/simulation.py` — `_persist_assessment:2285`, `_close_thread:1456`, `_record_assessment_drop:2340`, `_flush_llm_logs:4029`
- Test: `tests/unit/test_db_pool_sizing.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings.db_pool_size: int = 25`, `Settings.db_max_overflow: int = 10`.

**Why:** `create_async_engine(settings.database_url)` passes no pool args, so the engine gets SQLAlchemy's default 5 + 10 overflow = 15 connections. Concurrent reply tasks each open sessions; past 15, checkout blocks for `pool_timeout` then raises — and all four sites above catch and log. Pool exhaustion would appear as silently dropped assessments.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_db_pool_sizing.py
"""The agent engine must have room for every concurrent task, and a write that
fails on pool checkout must not vanish."""


def test_pool_is_larger_than_the_max_concurrent_reply_tasks():
    from src.config import get_settings

    s = get_settings()
    total = s.db_pool_size + s.db_max_overflow
    # Each reply task can hold one session; the loop's own pollers and flushers
    # need headroom on top.
    assert total >= s.reply_lane_max_in_flight + 10, (
        f"pool of {total} cannot serve {s.reply_lane_max_in_flight} concurrent "
        "reply tasks plus the loop's own writers"
    )


def test_agent_engine_is_constructed_with_explicit_pool_settings():
    import inspect

    from src.agent import main as agent_main

    src = inspect.getsource(agent_main)
    assert "pool_size=settings.db_pool_size" in src
    assert "max_overflow=settings.db_max_overflow" in src
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_db_pool_sizing.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'db_pool_size'`.

- [ ] **Step 3: Add the settings**

```python
# src/config.py — beside the other simulation parameters
    # DB pool for the AGENT process. The default (5 + 10) predates concurrency;
    # each in-flight reply task can hold a session, and the loop's own pollers
    # and flushers need headroom on top, so the pool must exceed
    # reply_lane_max_in_flight by a margin. Past the ceiling, checkout blocks
    # for pool_timeout and then raises — and the assessment/decision/log writers
    # catch and log, so exhaustion looks like silently missing rows.
    db_pool_size: int = 25
    db_max_overflow: int = 10
```

- [ ] **Step 4: Apply them at construction**

```python
# src/agent/main.py:160
        engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
        )
```

- [ ] **Step 5: Run the tests and watch them pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_db_pool_sizing.py -v`
Expected: PASS. (`reply_lane_max_in_flight` is added in Task 13; until then, add it in this task with `int = 1` so the assertion has something to read — it is the same knob and belongs to the same feature.)

- [ ] **Step 6: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/config.py src/agent/main.py tests/unit/test_db_pool_sizing.py
git commit -m "fix(db): size the agent engine's pool for concurrent tasks

create_async_engine took no pool arguments, so the agent ran on SQLAlchemy's
default 15 connections. Concurrent reply tasks each hold a session, and the
four writers that would hit the ceiling all swallow the exception — so pool
exhaustion would surface as silently missing assessments, not an error."
```

---

### Task 3: Hoist the Phase-4 fan-out semaphore to the engine

**Files:**
- Modify: `src/agent/simulation.py:1166` (`_phase4_reply_threads`), `__init__`
- Test: `tests/unit/test_phase4_concurrency.py` (extend the existing file)

**Interfaces:**
- Consumes: nothing.
- Produces: `SimulationEngine._llm_fanout_sem: asyncio.Semaphore` — engine-lifetime, bounds concurrent LLM calls process-wide.

**Why:** the semaphore is constructed *inside* the method, so it bounds fan-out per turn. N concurrent turns give N × `phase4_max_concurrent_replies` concurrent requests.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_phase4_concurrency.py (append)
@pytest.mark.asyncio
async def test_the_fanout_bound_is_global_not_per_turn(monkeypatch):
    """Two agents replying at once must share one budget. A per-call semaphore
    bounds each turn separately, so N turns give N x cap concurrent calls."""
    from src.config import get_settings

    cap = get_settings().phase4_max_concurrent_replies
    engine_a, hub_a = _hub_with_threads(cap * 2)
    # Second agent inside the SAME engine.
    from src.agent.agent import Agent
    from src.agent.state import ThreadState

    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    for i in range(cap * 2):
        lab.state.active_threads[f"L{i}"] = ThreadState(
            thread_id=f"L{i}", channel="general", other_agent_id="blackbird",
            message_count=1, has_pending_reply=True,
        )
    engine_a.agents["wang"] = lab

    live = 0
    peak = 0

    async def _fake_reply(agent, thread):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1

    monkeypatch.setattr(engine_a, "_reply_to_thread", _fake_reply)

    await asyncio.gather(
        engine_a._phase4_reply_threads(hub_a),
        engine_a._phase4_reply_threads(lab),
    )

    assert peak <= cap, f"two concurrent turns reached {peak}, above the global cap {cap}"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_phase4_concurrency.py -k global_not_per_turn -v`
Expected: FAIL — `peak` reaches `2 * cap`.

- [ ] **Step 3: Move the semaphore into `__init__`**

```python
# src/agent/simulation.py, in __init__ beside the other engine state
        # Bounds concurrent LLM calls PROCESS-WIDE. Constructed once: a
        # per-call semaphore bounds each turn separately, so N concurrent
        # turns gave N x cap concurrent requests.
        self._llm_fanout_sem = asyncio.Semaphore(
            max(1, get_settings().phase4_max_concurrent_replies)
        )
```

Then in `_phase4_reply_threads`, delete the local `sem = asyncio.Semaphore(...)` and use `self._llm_fanout_sem` in `_reply_bounded`.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_phase4_concurrency.py -v`
Expected: PASS (all, including the two existing bound/overlap tests).

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/simulation.py tests/unit/test_phase4_concurrency.py
git commit -m "fix(engine): make the Phase-4 fan-out bound global, not per-turn"
```

---

## Phase B — Redesigns that locking cannot fix

### Task 4: Stop stamping the LLM log by position

**Files:**
- Modify: `src/agent/simulation.py:1903` (`_phase5_new_post`), `_on_llm_call`
- Test: `tests/unit/test_llm_log_channel.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `log_meta` for Phase 5 calls carries `"channel"` before the call is issued.

**Why:** `self._llm_log_buffer[-1]["channel"] = channel` assumes the tail entry is this agent's call. Under concurrency it stamps another agent's row. No lock can make "the last element is mine" true.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_llm_log_channel.py
"""The Phase-5 channel must ride in with the call, not be written back onto
whatever happens to be last in a shared buffer."""
import inspect


def test_phase5_does_not_backreference_the_log_buffer():
    from src.agent.simulation import SimulationEngine

    src = inspect.getsource(SimulationEngine._phase5_new_post)
    assert "_llm_log_buffer[-1]" not in src, (
        "positional back-reference into a shared buffer: under concurrency this "
        "stamps another agent's row"
    )
    assert '"channel"' in src, "channel must be passed via log_meta instead"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_llm_log_channel.py -v`
Expected: FAIL on the first assertion.

- [ ] **Step 3: Pass the channel in `log_meta`**

Find the `generate_with_tools`/`generate_agent_response` call in `_phase5_new_post` and add `"channel": channel` to its `log_meta` dict. Delete the `self._llm_log_buffer[-1]["channel"] = channel` line.

If the channel is not known until *after* the call (the model picks it), keep a local variable and attach it to the row via the returned handle rather than by index — in that case change `_on_llm_call` to return the dict it appended, and have Phase 5 hold that reference:

```python
        row = self._on_llm_call(...)   # returns the appended dict
        ...
        if row is not None:
            row["channel"] = channel   # our own row, by reference, not by position
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_llm_log_channel.py tests/unit/test_simulation_logic.py -v`
Expected: PASS.

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/simulation.py tests/unit/test_llm_log_channel.py
git commit -m "fix(engine): stop stamping the LLM log row by position"
```

---

### Task 5: Make the stripped-tag signal per-message

**Files:**
- Modify: `src/agent/simulation.py` — `_strip_disallowed_tags:2416`, `_phase5_new_post:1918-1922`
- Test: `tests/unit/test_cohort_tag_stripping.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_strip_disallowed_tags(text, agent) -> tuple[str | None, int]` — the cleaned text and **how many tags this call stripped**. The engine-wide `_cohort_tags_stripped` counter remains, for stats only.

**Why:** Phase 5 reads the global counter before and after to decide whether *this* message had a tag stripped. Any concurrent post bumps it and Phase 5 rejects a clean post.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_cohort_tag_stripping.py
"""Whether THIS message had a tag stripped must not be inferred from a global
counter that any other agent's post can bump."""
import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine


def _engine():
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(agents=[hub, lab], slack_clients={})
    eng.agents["wang"].allowed_sender_ids = {"wang", "blackbird"}
    return eng


def test_strip_reports_its_own_count():
    eng = _engine()
    text, n = eng._strip_disallowed_tags("hello @NobodyBot", eng.agents["wang"])
    assert isinstance(n, int)
    assert n >= 0


def test_a_clean_message_reports_zero_even_after_another_strip():
    """The global counter is shared; the per-call answer must not be."""
    eng = _engine()
    eng._strip_disallowed_tags("hi @NobodyBot", eng.agents["wang"])   # bumps global
    _, n = eng._strip_disallowed_tags("a perfectly clean message", eng.agents["wang"])
    assert n == 0, "a clean message must report 0 regardless of other agents' posts"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_cohort_tag_stripping.py -v`
Expected: FAIL — `_strip_disallowed_tags` returns a bare `str | None`, so unpacking raises `TypeError`/`ValueError`.

- [ ] **Step 3: Return the per-call count**

Change `_strip_disallowed_tags` to count strips in a local, add that local to `self._cohort_tags_stripped`, and return `(cleaned_text, local_count)`. Update **every** call site — `grep -n "_strip_disallowed_tags" src/` — and in `_phase5_new_post` replace the before/after delta with the returned count.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_cohort_tag_stripping.py tests/unit/test_cohort_isolation.py -v`
Expected: PASS.

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/simulation.py tests/unit/test_cohort_tag_stripping.py
git commit -m "fix(cohort): report stripped tags per message, not via a global delta"
```

---

### Task 6: Make cursor advancement idempotent

**Files:**
- Modify: `src/agent/simulation.py:931` (`_run_turn`)
- Test: `tests/unit/test_cursor_advance.py`

**Interfaces:**
- Consumes: `MessageLog.latest_timestamp()` (exists, `message_log.py:471`).
- Produces: cursor semantics — snapshot at turn start, assign at turn end.

**Why:** `last_seen_cursor = time.time()` at turn end marks everything read, including messages that arrived *after* Phases 3/4 read the log. It also compares a wall clock against `posted_at`, which is `float(minted_ts)` and can run ahead of wall clock under fan-out.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_cursor_advance.py
"""The cursor must not mark as read anything the turn did not actually see."""
import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient


@pytest.mark.asyncio
async def test_cursor_does_not_swallow_messages_that_arrive_mid_turn(monkeypatch):
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent], slack_clients={"wang": FakeSlackClient(agent_id="wang")}
    )
    start_cursor = agent.state.last_seen_cursor

    async def _phase4(_a):
        # A message lands from elsewhere while this turn is mid-flight.
        eng.message_log.append(_late_entry())
        return set()

    monkeypatch.setattr(eng, "_phase4_reply_threads", _phase4)
    monkeypatch.setattr(eng, "_phase1_channel_discovery", lambda a: None)
    monkeypatch.setattr(eng, "_phase3_activate_threads", lambda a: None)
    monkeypatch.setattr(eng, "_phase5_new_post", _noop_async)

    await eng._run_turn(agent)

    # The late entry must still be unread: the cursor may only advance to what
    # the turn observed when it started.
    assert agent.state.last_seen_cursor < _LATE_TS
    assert agent.state.last_seen_cursor >= start_cursor


_LATE_TS = 9_999_999_999.0


async def _noop_async(*a, **kw):
    return None


def _late_entry():
    from src.agent.message_log import LogEntry

    return LogEntry(
        ts=str(_LATE_TS), channel="general", sender_agent_id="blackbird",
        sender_name="BlackbirdBot", content="late arrival", thread_ts=None,
        posted_at=_LATE_TS, is_bot=True,
    )
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_cursor_advance.py -v`
Expected: FAIL — `time.time()` is far below `_LATE_TS`, so this particular assertion passes by accident. **Adjust the fixture so the late entry's `posted_at` is `time.time() + 1`** and assert the cursor stays below it; watch it fail because the turn-end `time.time()` may exceed the snapshot. Verify the failure reason before implementing.

- [ ] **Step 3: Snapshot at turn start**

```python
# src/agent/simulation.py, top of _run_turn
        # Snapshot BEFORE any phase reads the log. Assigning time.time() at the
        # end marked as read anything that arrived mid-turn, and compared a wall
        # clock against posted_at (= float(minted ts)), which TsMinter can push
        # ahead of wall clock under fan-out. Snapshot-then-assign is idempotent
        # and uses one clock.
        cursor_snapshot = self.message_log.latest_timestamp()
```

and at the end, replace `agent.state.last_seen_cursor = time.time()` with:

```python
        agent.state.last_seen_cursor = max(
            agent.state.last_seen_cursor, cursor_snapshot
        )
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_cursor_advance.py tests/unit/test_simulation_logic.py -v`
Expected: PASS.

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/simulation.py tests/unit/test_cursor_advance.py
git commit -m "fix(engine): advance last_seen_cursor from a start-of-turn snapshot"
```

---

### Task 7: Snapshot the specialist-floor fail-open decision

**Files:**
- Modify: `src/agent/simulation.py` — `_specialist_floor_gap:2533`, `_phase3_activate_threads` (activation site), `src/agent/state.py`
- Test: `tests/unit/test_specialist_floor.py` (extend)

**Interfaces:**
- Consumes: nothing.
- Produces: `ThreadState.floor_armed: bool` — whether the specialist floor was already recording consults when this interview was activated.

**Why:** `if not self._specialist_consults: return set()` is a **process-global** read at persist time. A concurrent thread's first-ever consult flips the floor from fail-open to enforcing mid-interview, refusing a verdict whose reply is already in Slack.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_specialist_floor.py (append)
def test_fail_open_is_decided_at_activation_not_at_persist_time():
    """Another thread's first consult must not retroactively arm the floor for
    an interview that started while the map was empty."""
    eng = _engine(_hub())
    thread = _activated_thread(eng, "t1", other_agent_id="wang")
    assert thread.floor_armed is False   # map was empty when this began

    # A DIFFERENT interview consults someone. The global map is no longer empty.
    eng._record_consult("someone-else", "scientific")

    verdict = {"recommendation": "advance", "subject_agent_id": "wang"}
    assert eng._specialist_floor_gap(verdict, thread=thread) == set(), (
        "this interview began under fail-open and must stay there"
    )
```

Add the `_activated_thread` helper in the same file, constructing a `ThreadState` through whatever the activation path is and asserting `floor_armed` was set from `bool(engine._specialist_consults)`.

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_specialist_floor.py -k fail_open_is_decided -v`
Expected: FAIL — `ThreadState` has no `floor_armed`, and `_specialist_floor_gap` takes no `thread`.

- [ ] **Step 3: Add the field and thread it through**

```python
# src/agent/state.py, on ThreadState
    # Whether the specialist floor was already recording consults anywhere in
    # this process when this interview was activated. Snapshotted, not read
    # live: the floor's "map empty overall => fail open" is a process-global
    # predicate, so another thread's first consult would otherwise arm the floor
    # mid-interview and refuse a verdict whose reply is already in Slack.
    floor_armed: bool = False
```

Set it at activation (`thread.floor_armed = bool(self._specialist_consults)`), give `_specialist_floor_gap` an optional `thread: ThreadState | None = None` parameter, and replace the global read with `if thread is not None and not thread.floor_armed: return set()`, keeping the global check as the fallback when `thread is None`. Pass `thread` from `_capture_hub_assessment` → `_persist_assessment` → `_specialist_floor_gap`.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_specialist_floor.py tests/integration/test_opportunity_assessment_persistence.py -v`
Expected: PASS.

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/state.py src/agent/simulation.py tests/unit/test_specialist_floor.py
git commit -m "fix(specialists): snapshot the floor's fail-open decision at activation"
```

---

### Task 8: Make thread eviction additive

**Files:**
- Modify: `src/agent/simulation.py:1514` (`_evict_dead_thread`)
- Test: `tests/unit/test_simulation_logic.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: `_closed_thread_ids` is insert-only.

**Why:** `discard` racing an `add` un-closes a just-closed thread, and Phase 3 then re-activates a finished interview.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_simulation_logic.py (append)
class TestEvictionIsAdditive:
    def test_evicting_a_thread_does_not_unclose_it(self):
        """discard racing a concurrent close resurrects a finished interview."""
        from src.agent.agent import Agent
        from src.agent.simulation import SimulationEngine

        eng = SimulationEngine(
            agents=[Agent("wang", "WangBot", "Wang")], slack_clients={}
        )
        eng._closed_thread_ids.add("t1")
        eng._evict_dead_thread("t1")
        assert "t1" in eng._closed_thread_ids, (
            "eviction must not remove a closed marker — Phase 3 would re-activate it"
        )
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_simulation_logic.py -k EvictionIsAdditive -v`
Expected: FAIL — `discard` removed the marker.

- [ ] **Step 3: Remove the discard**

Delete `self._closed_thread_ids.discard(thread_id)` from `_evict_dead_thread` and leave a comment explaining that eviction removes per-agent state but never un-closes a thread.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_simulation_logic.py -v`
Expected: PASS. If an existing test asserted the discard, read it carefully before changing it — it may be pinning a real behaviour (a thread evicted for *not existing* may legitimately need re-opening). If so, keep the discard but gate it on the thread never having been closed via `_close_thread`.

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/simulation.py tests/unit/test_simulation_logic.py
git commit -m "fix(engine): make thread eviction additive so it cannot un-close"
```

---

### Task 9: Reservation-based rate limiting and the hub ceiling

**Files:**
- Modify: `src/agent/agent.py` (`record_api_call`, new `try_reserve`/`release_reservation`)
- Modify: `src/agent/simulation.py` — `_within_rate_limit:416`, `_calls_per_load`, `_reply_to_thread`, `_phase5_new_post`
- Modify: `src/config.py`
- Test: `tests/unit/test_rate_reservation.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Agent.try_reserve(allowance: int, window_s: int, now: float | None = None) -> bool`
  - `Agent.release_reservation() -> None`
  - `Settings.hub_llm_calls_per_window: int = 600`
  - `SimulationEngine._allowance_for(agent) -> int`

**Why:** `_within_rate_limit` is consulted only at selection. It cannot bound concurrent spend — `_phase4_reply_threads` already fires up to `phase4_max_concurrent_replies` calls per turn without re-checking.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_rate_reservation.py
"""A selection-time check cannot bound concurrent spend. Reserve before the
call, not after it."""
import asyncio

import pytest

from src.agent.agent import Agent


def test_reserve_admits_up_to_the_allowance_then_refuses():
    a = Agent("wang", "WangBot", "Wang")
    assert [a.try_reserve(3, 600, now=1000.0) for _ in range(4)] == [
        True, True, True, False,
    ]


def test_a_released_reservation_is_reusable():
    a = Agent("wang", "WangBot", "Wang")
    assert a.try_reserve(1, 600, now=1000.0) is True
    assert a.try_reserve(1, 600, now=1000.0) is False
    a.release_reservation()
    assert a.try_reserve(1, 600, now=1000.0) is True


def test_reservations_age_out_of_the_window():
    a = Agent("wang", "WangBot", "Wang")
    assert a.try_reserve(1, 600, now=1000.0) is True
    assert a.try_reserve(1, 600, now=1000.0) is False
    assert a.try_reserve(1, 600, now=1700.0) is True   # window slid


@pytest.mark.asyncio
async def test_allowance_holds_under_concurrent_callers():
    """The property the old entry gate could not provide."""
    a = Agent("wang", "WangBot", "Wang")
    granted = 0

    async def caller():
        nonlocal granted
        if a.try_reserve(5, 600, now=1000.0):
            granted += 1
            await asyncio.sleep(0)

    await asyncio.gather(*(caller() for _ in range(50)))
    assert granted == 5, f"allowance 5 exceeded under concurrency: {granted}"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_rate_reservation.py -v`
Expected: FAIL — `Agent` has no `try_reserve`.

- [ ] **Step 3: Implement reservation on `Agent`**

```python
# src/agent/agent.py
    def try_reserve(
        self, allowance: int, window_s: int, now: float | None = None
    ) -> bool:
        """Claim one slot in the sliding window BEFORE issuing the call.

        The old check was an entry gate consulted at selection, which cannot
        bound spend once several calls are in flight for one agent. Reserving
        up front makes the window a true spend gate. There is no await in this
        method, so it is atomic against other coroutines.
        """
        now = time.time() if now is None else now
        window_start = now - window_s
        times = self.state.call_times
        while times and times[0] < window_start:
            times.popleft()
        if len(times) >= allowance:
            self.state.throttled = True
            return False
        times.append(now)
        self.state.throttled = False
        return True

    def release_reservation(self) -> None:
        """Give back a reservation whose call was never issued."""
        if self.state.call_times:
            self.state.call_times.pop()
```

Change `record_api_call` to bump `api_call_count` only (the ledger append now happens at reservation) and say so in its docstring.

- [ ] **Step 4: Add the hub ceiling and wire the call sites**

```python
# src/config.py
    # The hub's own ceiling. It is on an unpaced lane, so the per-load allowance
    # no longer applies to it; this is a BRAKE, not a budget. Measured hub rate
    # was ~2.6 calls/10min, so 600 is unreachable in normal operation and only
    # trips a runaway.
    hub_llm_calls_per_window: int = 600
```

```python
# src/agent/simulation.py
    def _allowance_for(self, agent: Agent) -> int:
        """Window allowance for one agent. The hub is on its own ceiling."""
        settings = get_settings()
        if agent.role == "scout_hub":
            return settings.hub_llm_calls_per_window
        return self._calls_per_load(agent) * self._agent_load(agent)
```

In `_reply_to_thread` and `_phase5_new_post`, replace `agent.record_api_call()` before the LLM call with:

```python
        if not agent.try_reserve(
            self._allowance_for(agent), get_settings().llm_rate_window_seconds
        ):
            logger.warning("[%s] rate-limited; deferring this reply", agent.agent_id)
            return
        agent.record_api_call()
```

and call `agent.release_reservation()` on any early-return path that does not issue the call.

- [ ] **Step 5: Run the tests and watch them pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_rate_reservation.py tests/unit/test_rate_limit.py tests/unit/test_hub_budget_scheduler.py -v`
Expected: PASS.

- [ ] **Step 6: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/agent.py src/agent/simulation.py src/config.py tests/unit/test_rate_reservation.py
git commit -m "feat(engine): reservation-based rate limiting; hub gets its own ceiling

A selection-time check cannot bound concurrent spend. Reserve a window slot
before issuing the call, release it if the call is never made. The hub moves to
hub_llm_calls_per_window (600/10min) — a brake, not a budget, since it is now
on an unpaced lane."
```

---

## Phase C — The lane split (still sequential)

### Task 10: Decouple Phase 5 from Phase 4

**Files:**
- Modify: `src/agent/simulation.py:~920` (`_run_turn`)
- Test: `tests/unit/test_post_lane.py`

**Interfaces:**
- Consumes: nothing.
- Produces: Phase 5 eligibility depends only on `spontaneous_ready` and the daily cap.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_post_lane.py
"""Replying must not earn a top-level post. Otherwise the 'staggered' post lane
is driven by reply volume — exactly the coupling the split removes."""
import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient


@pytest.mark.asyncio
async def test_phase4_work_does_not_trigger_phase5(monkeypatch):
    agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[agent], slack_clients={"wang": FakeSlackClient(agent_id="wang")}
    )
    # Not yet due for a spontaneous post.
    agent.state.last_phase5_action_time = __import__("time").time()

    called = []

    async def _phase4(_a):
        return {"t1"}          # did reply work

    async def _phase5(_a):
        called.append(_a.agent_id)

    monkeypatch.setattr(eng, "_phase1_channel_discovery", lambda a: None)
    monkeypatch.setattr(eng, "_phase3_activate_threads", lambda a: None)
    monkeypatch.setattr(eng, "_phase4_reply_threads", _phase4)
    monkeypatch.setattr(eng, "_phase5_new_post", _phase5)

    await eng._run_turn(agent)

    assert called == [], "Phase 4 work must not earn a Phase 5 post"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_post_lane.py -v`
Expected: FAIL — `called == ["wang"]`.

- [ ] **Step 3: Remove the coupling**

Delete `has_phase4_work` / `has_new_work` and reduce the gate to:

```python
        if spontaneous_ready:
            await self._phase5_new_post(agent)
```

Keep the "Phase 4 activity resets skip backoff" block — that is a separate, still-correct behaviour.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_post_lane.py tests/unit/test_simulation_logic.py tests/unit/test_phase5_actions.py -v`
Expected: PASS.

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/simulation.py tests/unit/test_post_lane.py
git commit -m "feat(engine): Phase 5 no longer triggered by Phase 4 work"
```

---

### Task 11: Split the lanes and delete the reactive tier

**Files:**
- Modify: `src/agent/simulation.py` — `_run_turn:887`, `_select_agent:807`, `_owes_reply:739`, `_run_main_loop`, `__init__`
- Modify: `src/config.py` (remove `max_consecutive_reactive_turns`)
- Test: `tests/unit/test_post_lane.py`, `tests/unit/test_reply_lane.py`

**Interfaces:**
- Consumes: `_owes_reply` (existing).
- Produces:
  - `SimulationEngine._run_post_turn(agent) -> bool` — Phases 1 + 5.
  - `SimulationEngine._service_reply(agent, thread) -> None` — Phase 4 for one pair.
  - `SimulationEngine._pending_reply_pairs() -> list[tuple[Agent, ThreadState]]`
  - `SimulationEngine._dispatch_reply_lane() -> int` — services all pending pairs, returns how many ran.
  - `AgentState.in_flight: bool` — excluded from post-lane selection while a turn runs.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_reply_lane.py
"""The reply lane services every pending pair without pacing, and the post lane
no longer has a reactive tier."""
import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from tests.fakes import FakeSlackClient


def _engine_with_pending(n):
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    for i in range(n):
        hub.state.active_threads[f"t{i}"] = ThreadState(
            thread_id=f"t{i}", channel="general", other_agent_id=f"pi{i}",
            message_count=1, has_pending_reply=True,
        )
    eng = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    return eng, hub


def test_pending_pairs_lists_every_owed_reply():
    eng, hub = _engine_with_pending(5)
    pairs = eng._pending_reply_pairs()
    assert {t.thread_id for _a, t in pairs} == {f"t{i}" for i in range(5)}
    assert all(a.agent_id == "blackbird" for a, _t in pairs)


@pytest.mark.asyncio
async def test_dispatch_services_all_pending_pairs(monkeypatch):
    eng, hub = _engine_with_pending(5)
    served = []

    async def _serve(agent, thread):
        served.append(thread.thread_id)

    monkeypatch.setattr(eng, "_service_reply", _serve)
    n = await eng._dispatch_reply_lane()

    assert n == 5
    assert sorted(served) == [f"t{i}" for i in range(5)]


def test_the_reactive_tier_is_gone():
    import inspect

    src = inspect.getsource(SimulationEngine._select_agent)
    assert "_owes_reply" not in src, "the post lane must not do reactive selection"
    assert "_reactive_streak" not in src
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_reply_lane.py -v`
Expected: FAIL — `_pending_reply_pairs` / `_dispatch_reply_lane` / `_service_reply` do not exist; the reactive assertions fail because the tier is still there.

- [ ] **Step 3: Implement the split**

```python
# src/agent/simulation.py
    def _pending_reply_pairs(self) -> list[tuple[Agent, ThreadState]]:
        """Every (agent, thread) owing a reply. The reply lane's work queue.

        Unpaced by construction: no staleness weighting, no streak cap, no
        cooldown. A thread that is ready is serviced.
        """
        pairs: list[tuple[Agent, ThreadState]] = []
        for agent in self.agents.values():
            for thread in list(agent.state.active_threads.values()):
                if thread.status != "active":
                    continue
                if self._channel_visibility.get(thread.channel) == VISIBILITY_COLLAB_PRIVATE:
                    continue
                if thread.has_pending_reply or self.message_log.has_new_reply_from_other(
                    thread.thread_id, agent.agent_id, agent.state.last_seen_cursor,
                    allowed_sender_ids=None,
                ):
                    pairs.append((agent, thread))
        return pairs

    async def _service_reply(self, agent: Agent, thread: ThreadState) -> None:
        """Phase 3 + Phase 4 for one pair."""
        thread.has_pending_reply = True
        await self._reply_to_thread(agent, thread)

    async def _dispatch_reply_lane(self) -> int:
        """Service every pending pair. Sequential for now; Task 13 makes it
        concurrent behind reply_lane_max_in_flight."""
        pairs = self._pending_reply_pairs()
        for agent, thread in pairs:
            await self._service_reply(agent, thread)
        return len(pairs)

    async def _run_post_turn(self, agent: Agent) -> bool:
        """Phases 1 + 5 for one agent. The paced lane."""
        api_calls_before = agent.api_call_count
        cursor_snapshot = self.message_log.latest_timestamp()
        self._phase1_channel_discovery(agent)
        ... # spontaneous_ready computation, then _phase5_new_post
        agent.state.last_seen_cursor = max(
            agent.state.last_seen_cursor, cursor_snapshot
        )
        return agent.api_call_count > api_calls_before
```

In `_run_main_loop`, call `await self._dispatch_reply_lane()` each iteration **before** `_select_agent()`, then run `_run_post_turn` on the selected agent. Delete the reactive tier from `_select_agent`, delete `_last_llm_caller` / `_reactive_streak` and their uses, and remove `max_consecutive_reactive_turns` from `src/config.py`. Add `in_flight: bool = False` to `AgentState` and exclude in-flight agents in `_turn_eligible`.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_reply_lane.py tests/unit/test_post_lane.py tests/unit/test_simulation_logic.py -v`
Expected: PASS. Several existing scheduler tests will reference the deleted tier — read each before editing; they encode real intent about fairness that the new design must still satisfy or consciously drop.

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/simulation.py src/agent/state.py src/config.py tests/unit/test_reply_lane.py tests/unit/test_post_lane.py
git commit -m "feat(engine)!: split the post and reply lanes; delete the reactive tier

Replies leave the paced pool entirely: every pending (agent, thread) pair is
serviced each loop iteration with no weighting, streak cap or cooldown. The
post lane keeps the staleness x load draw for Phase 1 + 5 only.

The reactive tier and _last_llm_caller encoded a strictly sequential A->B->A
baton that only made sense while replies shared the pool, so both go, along
with max_consecutive_reactive_turns."
```

---

## Phase D — Turn on concurrency

### Task 12: The lock registry

**Files:**
- Create: `src/agent/locks.py`
- Test: `tests/unit/test_engine_locks.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `LockRegistry.get(key: str) -> asyncio.Lock`
  - `LockRegistry.acquire_all(*keys: str)` — async context manager, acquires in **sorted** order.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_engine_locks.py
"""_close_thread mutates the OTHER agent's state, so two closes in opposite
directions would deadlock on per-agent locks. Sorted acquisition is the fix,
and it must be the only sanctioned way to take more than one."""
import asyncio

import pytest

from src.agent.locks import LockRegistry


def test_get_returns_the_same_lock_for_a_key():
    r = LockRegistry()
    assert r.get("a") is r.get("a")
    assert r.get("a") is not r.get("b")


@pytest.mark.asyncio
async def test_acquire_all_is_order_independent():
    r = LockRegistry()

    async def forward():
        async with r.acquire_all("wang", "blackbird"):
            await asyncio.sleep(0.01)

    async def backward():
        async with r.acquire_all("blackbird", "wang"):
            await asyncio.sleep(0.01)

    # Deadlocks without sorted acquisition; wait_for turns that into a failure
    # instead of a hung test run.
    await asyncio.wait_for(asyncio.gather(forward(), backward()), timeout=2.0)


@pytest.mark.asyncio
async def test_acquire_all_is_mutually_exclusive():
    r = LockRegistry()
    live = 0
    peak = 0

    async def worker():
        nonlocal live, peak
        async with r.acquire_all("x"):
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1

    await asyncio.gather(*(worker() for _ in range(5)))
    assert peak == 1
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_engine_locks.py -v`
Expected: FAIL — `ModuleNotFoundError: src.agent.locks`.

- [ ] **Step 3: Implement**

```python
# src/agent/locks.py
"""Per-key asyncio locks with deadlock-free multi-acquire.

Two call sites mutate more than one agent's state: _close_thread writes the
other agent's active_threads, and _evict_dead_thread writes every agent's. Two
of those running in opposite orders deadlock, so multi-key acquisition is
always done in sorted key order — via acquire_all, which is the only sanctioned
way to hold more than one lock at a time.
"""

import asyncio
from contextlib import asynccontextmanager


class LockRegistry:
    """Lazily-created asyncio.Lock per key. Loop-only; not thread-safe."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @asynccontextmanager
    async def acquire_all(self, *keys: str):
        """Acquire every key, in sorted order, releasing in reverse."""
        ordered = sorted(set(keys))
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
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_engine_locks.py -v`
Expected: PASS.

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/locks.py tests/unit/test_engine_locks.py
git commit -m "feat(engine): per-key lock registry with sorted multi-acquire"
```

---

### Task 13: Concurrent reply lane behind a setting

**Files:**
- Modify: `src/agent/simulation.py` — `__init__`, `_dispatch_reply_lane`, `_reply_to_thread`, `_phase5_new_post`, `_close_thread`
- Modify: `src/config.py`
- Test: `tests/unit/test_reply_lane.py` (extend)

**Interfaces:**
- Consumes: `LockRegistry` (Task 12).
- Produces:
  - `Settings.reply_lane_max_in_flight: int = 1` (1 = sequential, concurrency off)
  - `SimulationEngine._thread_locks`, `._agent_locks`: `LockRegistry`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_reply_lane.py (append)
@pytest.mark.asyncio
async def test_dispatch_runs_pairs_concurrently_up_to_the_cap(monkeypatch):
    from src.config import get_settings

    cap = get_settings().reply_lane_max_in_flight
    if cap < 2:
        pytest.skip("concurrency disabled by configuration")

    eng, hub = _engine_with_pending(cap * 3)
    live = 0
    peak = 0

    async def _serve(agent, thread):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1

    monkeypatch.setattr(eng, "_service_reply", _serve)
    await eng._dispatch_reply_lane()

    assert peak > 1, "reply lane did not overlap"
    assert peak <= cap


@pytest.mark.asyncio
async def test_a_pair_already_in_flight_is_not_spawned_twice(monkeypatch):
    eng, hub = _engine_with_pending(1)
    starts = 0

    async def _serve(agent, thread):
        nonlocal starts
        starts += 1
        await asyncio.sleep(0.02)

    monkeypatch.setattr(eng, "_service_reply", _serve)
    await asyncio.gather(eng._dispatch_reply_lane(), eng._dispatch_reply_lane())

    assert starts == 1, "the same (agent, thread) was serviced twice concurrently"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_reply_lane.py -v`
Expected: FAIL — no `reply_lane_max_in_flight`; dispatch is sequential and has no in-flight set.

- [ ] **Step 3: Implement**

```python
# src/config.py
    # Max concurrent reply tasks. 1 = sequential (concurrency OFF) — the safe
    # default, so the lane split can ship and be observed before parallelism is
    # enabled. Must stay well below db_pool_size + db_max_overflow.
    reply_lane_max_in_flight: int = 1
```

```python
# src/agent/simulation.py __init__
        self._thread_locks = LockRegistry()
        self._agent_locks = LockRegistry()
        self._reply_sem = asyncio.Semaphore(
            max(1, get_settings().reply_lane_max_in_flight)
        )
        self._reply_in_flight: set[tuple[str, str]] = set()
```

```python
    async def _dispatch_reply_lane(self) -> int:
        pairs = [
            (a, t) for a, t in self._pending_reply_pairs()
            if (a.agent_id, t.thread_id) not in self._reply_in_flight
        ]
        if not pairs:
            return 0

        async def _run(agent, thread):
            key = (agent.agent_id, thread.thread_id)
            self._reply_in_flight.add(key)
            try:
                async with self._reply_sem:
                    # Thread lock held ACROSS the LLM call: the stale-history
                    # and CONCLUDE-ordinal races both happen between the read
                    # and the act, not inside either.
                    async with self._thread_locks.acquire_all(thread.thread_id):
                        await self._service_reply(agent, thread)
            finally:
                self._reply_in_flight.discard(key)

        await asyncio.gather(
            *(_run(a, t) for a, t in pairs), return_exceptions=True
        )
        return len(pairs)
```

Wrap `_phase5_new_post`'s body in `async with self._agent_locks.acquire_all(agent.agent_id)`, and `_close_thread`'s cross-agent mutation in `async with self._agent_locks.acquire_all(agent_a_id, agent_b_id)`.

- [ ] **Step 4: Run the tests and watch them pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_reply_lane.py -v`
Expected: PASS. The concurrency test skips while the default is 1; re-run with `REPLY_LANE_MAX_IN_FLIGHT=4` to exercise it.

- [ ] **Step 5: Run the gate and commit**

```bash
./scripts/ci.sh
git add src/agent/simulation.py src/config.py tests/unit/test_reply_lane.py
git commit -m "feat(engine): concurrent reply lane behind reply_lane_max_in_flight

Defaults to 1 (off) so this can ship and be observed before parallelism is
enabled. The per-thread lock is held across the LLM call, which is where the
stale-history and CONCLUDE-ordinal races live."
```

---

### Task 14: The adversarial concurrency tests, then turn it on

**Files:**
- Create: `tests/integration/test_concurrent_thread_safety.py`
- Modify: `src/config.py` (raise the default), `.env` on the host
- Test: as created

**Interfaces:**
- Consumes: everything above.
- Produces: the evidence that concurrency is safe.

**Why:** every failure mode here is silent. These tests are the deliverable, not a formality.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_concurrent_thread_safety.py
"""The races that concurrency introduces, each asserted directly.

All of these fail loudly only if written as concurrency tests; none of them is
visible in a sequential run. See
docs/specs/2026-08-14-two-lane-concurrent-scheduler-design.md section 4.
"""
import asyncio

import pytest
from sqlalchemy import select

from src.models import OpportunityAssessment, SimulationRun, ThreadDecision

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_two_concurrent_replies_produce_one_conclude_and_one_assessment(engine):
    """Both tasks read prior-count 11, both render MUST CONCLUDE, and
    opportunity_assessments has no uniqueness constraint to save us."""
    ...  # build a hub engine on a seeded 11-message thread, then:
    await asyncio.gather(
        eng._dispatch_reply_lane(), eng._dispatch_reply_lane()
    )
    async with factory() as db:
        rows = (await db.execute(select(OpportunityAssessment))).scalars().all()
        decisions = (await db.execute(select(ThreadDecision))).scalars().all()
    assert len(rows) == 1, f"{len(rows)} assessments written for one interview"
    assert len(decisions) == 1, f"thread closed {len(decisions)} times"


@pytest.mark.asyncio
async def test_concurrent_phase5_respects_the_daily_cap(engine):
    """lab_daily_post_cap = 1; two concurrent Phase 5s both see 0 posts today."""
    ...
    await asyncio.gather(eng._phase5_new_post(lab), eng._phase5_new_post(lab))
    assert len(client.posted) == 1


@pytest.mark.asyncio
async def test_cross_agent_close_does_not_deadlock(engine):
    """_close_thread writes the other agent's state; opposite orders must not
    deadlock. wait_for turns a hang into a failure."""
    await asyncio.wait_for(
        asyncio.gather(
            eng._close_thread(hub, thread_ab, "no_proposal"),
            eng._close_thread(lab, thread_ba, "no_proposal"),
        ),
        timeout=5.0,
    )
```

Fill each `...` with a real fixture following the pattern in `tests/integration/test_opportunity_assessment_persistence.py` (`async_sessionmaker` over the `engine` fixture, a `SimulationEngine` with `session_factory` and `simulation_run_id`, `_seed_thread_history` for message counts, and monkeypatched `generate_with_tools` returning a CONCLUDE reply carrying an `<assessment_json>` sidecar).

- [ ] **Step 2: Run them with concurrency ON and watch them fail**

Run: `REPLY_LANE_MAX_IN_FLIGHT=4 .venv-test/bin/python -m pytest tests/integration/test_concurrent_thread_safety.py -v`
Expected: FAIL on any race the locks do not yet cover. **If they pass on the first run, the tests are not exercising concurrency** — check that the fake LLM call actually awaits (`await asyncio.sleep(0)` at minimum) so the tasks interleave.

- [ ] **Step 3: Fix whatever they catch**

Extend the lock coverage until each passes. Do not weaken an assertion to make it pass.

- [ ] **Step 4: Raise the default and re-run the whole gate**

```python
# src/config.py
    reply_lane_max_in_flight: int = 4
```

Run: `./scripts/ci.sh`
Expected: green, including the new integration tests at the new default.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_concurrent_thread_safety.py src/config.py
git commit -m "test(engine): pin the concurrency races, then enable the reply lane

Two concurrent replies must yield one CONCLUDE, one assessment and one close;
concurrent Phase 5 must respect the daily cap; a cross-agent close must not
deadlock. With those green, reply_lane_max_in_flight defaults to 4."
```

---

## Deployment

The simulation is stopped, so there is no live cutover. Migration `0027`
(assessment drops) is committed but **not yet applied in production** — apply it
in the same deploy.

```bash
DC="docker compose -f docker-compose.prod.yml"
$DC --profile agent build blackbird-app worker agent
$DC up -d blackbird-app worker
$DC exec -T blackbird-app alembic upgrade head     # 0026 -> 0027
$DC exec -T blackbird-app alembic current          # must read 0027
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main
```

Watch for, in order: no `Star-topology validation failed`; `[sched]` lines showing
post-lane selections only; hub replies arriving without a 20-minute gap; and
`/admin/assessments` gaining rows rather than drop-banner entries.

**Rollback:** set `REPLY_LANE_MAX_IN_FLIGHT=1` and recreate the agent container.
That restores sequential behaviour without a code change or a migration.

---

## Self-Review

**Spec coverage.** §2.1 lanes → Tasks 10, 11. §2.2 Phase 5 decoupling → Task 10.
§2.3 broken invariant → Task 9. §3.1 locks → Tasks 12, 13. §3.2 deadlock ordering
→ Task 12. §4.1–4.3 → Tasks 13, 14. §4.4 cursor → Task 6. §4.5 daily cap → Tasks
13, 14. §4.6 limiter → Task 9. §4.7 tag delta → Task 5. §5 reservation + hub
ceiling → Task 9. §6.1 Slack off-loop → Task 1. §6.2 DB pool → Task 2. §6.3
semaphore → Task 3. §7.1 log buffer → Task 4. §7.2 baton → Task 11. §7.4
floor snapshot → Task 7. §7.5 eviction → Task 8. §7.6 MessageLog invariant →
Task 1 Step 6. §9 tests → Task 14. §10 rollout → task order + Deployment.
**No gaps.**

**Open questions from spec §11**, resolved here as stated assumptions — flag if
either is wrong:
- `active_thread_threshold` stays a *capacity* bound on the hub's concurrent
  interviews; it no longer affects the hub's rate (Task 9 routes the hub to
  `hub_llm_calls_per_window` instead).
- `max_consecutive_reactive_turns` is **deleted**, not left inert (Task 11).

**Type consistency.** `try_reserve(allowance, window_s, now=None) -> bool` and
`release_reservation() -> None` are used with those signatures in Tasks 9 and 13.
`LockRegistry.get`/`acquire_all` match between Tasks 12 and 13.
`_pending_reply_pairs`/`_service_reply`/`_dispatch_reply_lane` are introduced in
Task 11 and extended in Task 13 with unchanged signatures. `reply_lane_max_in_flight`
is introduced in Task 2 (so the pool assertion can read it) and re-defaulted in
Task 14 — noted in both.

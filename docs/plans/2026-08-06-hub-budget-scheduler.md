# Load-Proportional Budget and Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the star-topology hub bot from being permanently benched by a uniform per-agent LLM cap, by deriving both its rate allowance and its scheduling weight from one shared load signal.

**Architecture:** A single `_agent_load(agent)` method on `SimulationEngine` returns an agent's concurrent conversational obligations, clamped to `[1, active_thread_threshold]`. Two consumers read it: a sliding-window rate limiter that replaces the cumulative cap as the live throttle, and the turn scheduler's selection weights. Because both derive from one number they cannot disagree — which is the root cause being fixed.

**Tech Stack:** Python 3.12, SQLAlchemy 2 async, Typer CLI, pytest, ruff.

**Spec:** `docs/specs/2026-08-06-hub-budget-scheduler-design.md`. Read it before Task 1.

## Global Constraints

- Every task ends green on `./scripts/ci.sh`. That gate is the whole gate — there is no server-side CI.
- Branch coverage floor `COV_MIN=60`. Never lower it.
- `ruff` findings in `src/` must stay at or under `SRC_LINT_MAX=260`. Never raise it.
- `ruff` on `tests/` must be **zero** findings. New test code is held spotless.
- No database schema change and no alembic migration in this plan. If you find yourself writing one, stop — you have misread the design.
- `tests/integration/test_full_run_live.py:1111` must keep passing **with no edit**. It is the tripwire for Task 5; see that task.
- Work on branch `blackbird`. Commit after every task.
- Do not restart the live `blackbird-agent-run` container. Deployment is out of scope for this plan.

---

### Task 1: The shared load signal

**Files:**
- Modify: `src/agent/simulation.py` (add method after `_agent_within_budget`, which ends at line 378)
- Test: `tests/unit/test_hub_budget_scheduler.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `SimulationEngine._agent_load(self, agent: Agent) -> int`. Tasks 4 and 6 both call it.

- [ ] **Step 1: Create the test file with its shared helpers and the first failing test**

Create `tests/unit/test_hub_budget_scheduler.py`:

```python
"""Load-proportional budget and scheduling for star topologies.

Implements the test plan in docs/specs/2026-08-06-hub-budget-scheduler-design.md
§8. Organised by design section so a failure names the rule it broke:

- TestAgentLoad            §4.1  the shared load signal
- TestRoleRateOverride     §4.4  optional per-role allowance
- TestCallLedger           §4.2  record_api_call maintains both counters
- TestRateLimiter          §4.2  sliding-window eligibility, and that it self-heals
- TestRestartRebuild       §4.2  step 4b repopulates call_times from llm_call_logs
- TestScheduler            §4.3  load-proportional weight, reactive tiebreak
- TestProductionRegression §8    the exact run-4f1e8395 state
"""

import types

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState


def _settings(**kw):
    base = dict(
        cohort_isolation_enabled=False,
        cohort_default_policy="open",
        max_consecutive_reactive_turns=3,
        turn_delay_seconds=0.0,
        active_thread_threshold=12,
        llm_rate_window_seconds=600,
        llm_calls_per_load_per_window=8,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def _patch(monkeypatch, **kw):
    monkeypatch.setattr("src.agent.simulation.get_settings", lambda: _settings(**kw))


def _engine(agent_ids, budget_cap=0):
    agents = [
        Agent(agent_id=a, bot_name=f"{a.capitalize()}Bot", pi_name=f"PI {a}")
        for a in agent_ids
    ]
    return SimulationEngine(agents=agents, slack_clients={}, budget_cap=budget_cap)


def _add_threads(agent, n, *, status="active", pending=False, prefix="t"):
    for i in range(n):
        tid = f"{prefix}{i}"
        agent.state.active_threads[tid] = ThreadState(
            thread_id=tid,
            channel="general",
            other_agent_id=f"pi{i}",
            status=status,
            has_pending_reply=pending,
        )


class TestAgentLoad:
    def test_idle_agent_has_load_one(self, monkeypatch):
        _patch(monkeypatch)
        eng = _engine(["hub"])
        assert eng._agent_load(eng.agents["hub"]) == 1

    def test_load_counts_active_threads(self, monkeypatch):
        _patch(monkeypatch)
        eng = _engine(["hub"])
        _add_threads(eng.agents["hub"], 5)
        assert eng._agent_load(eng.agents["hub"]) == 5

    def test_non_active_threads_are_excluded(self, monkeypatch):
        _patch(monkeypatch)
        eng = _engine(["hub"])
        _add_threads(eng.agents["hub"], 3, status="active", prefix="a")
        _add_threads(eng.agents["hub"], 4, status="closed", prefix="c")
        _add_threads(eng.agents["hub"], 2, status="proposed", prefix="p")
        assert eng._agent_load(eng.agents["hub"]) == 3

    def test_load_is_clamped_at_active_thread_threshold(self, monkeypatch):
        _patch(monkeypatch, active_thread_threshold=12)
        eng = _engine(["hub"])
        _add_threads(eng.agents["hub"], 56)
        assert eng._agent_load(eng.agents["hub"]) == 12
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_hub_budget_scheduler.py -v`
Expected: 4 FAILs with `AttributeError: 'SimulationEngine' object has no attribute '_agent_load'`

- [ ] **Step 3: Implement `_agent_load`**

In `src/agent/simulation.py`, insert immediately after `_agent_within_budget` (which ends `return agent.api_call_count < self.budget_cap` at line 378) and before `_non_funding_thread_count`:

```python
    def _agent_load(self, agent: Agent) -> int:
        """Concurrent conversational obligations for one agent.

        The shared signal behind BOTH the rate allowance (``_within_rate_limit``)
        and the selection weight (``_select_agent``). Deriving both from one
        number is the point: the failure this fixes was the limiter and the
        scheduler holding contradictory views of what a hub deserves — the
        reactive tier gave the blackbird hub a 7x boost while the cumulative cap
        benched it for 161 consecutive turns, and the cap won, silently. See
        docs/specs/2026-08-06-hub-budget-scheduler-design.md §1.4.

        Floors at 1 so an idle agent stays eligible. Ceilings at
        ``active_thread_threshold`` so nothing can inflate its own allowance past
        the thread cap it is already bound by — that clamp is what stops a
        thread-opening runaway from financing itself (§4.1).
        """
        live = sum(
            1 for t in agent.state.active_threads.values() if t.status == "active"
        )
        return max(1, min(live, get_settings().active_thread_threshold))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_hub_budget_scheduler.py -v`
Expected: 4 PASS

- [ ] **Step 5: Lint and commit**

```bash
.venv-test/bin/python -m ruff check tests/unit/test_hub_budget_scheduler.py
git add tests/unit/test_hub_budget_scheduler.py src/agent/simulation.py
git commit -m "feat(sched): _agent_load — the shared load signal"
```

---

### Task 2: Configuration knobs and the per-role override

**Files:**
- Modify: `src/config.py` (after `max_consecutive_reactive_turns`, line 348)
- Modify: `src/agent/roles.py` (`RoleSpec` at line 30-34; `load_role` at line 72-101)
- Test: `tests/unit/test_roles.py` (append)
- Test: `tests/unit/test_hub_budget_scheduler.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `Settings.llm_rate_window_seconds: int = 600`, `Settings.llm_calls_per_load_per_window: int = 8`, and `RoleSpec.calls_per_load_per_window: int | None = None`. Task 4 reads all three.

- [ ] **Step 1: Write the failing role-manifest tests**

Append to `tests/unit/test_roles.py`:

```python
def test_role_rate_override_is_read_when_positive(tmp_path, monkeypatch):
    _write_role(
        tmp_path, monkeypatch, "scout_hub",
        'label = "Scout Hub"\ncalls_per_load_per_window = 20\n',
    )
    assert load_role("scout_hub").calls_per_load_per_window == 20


def test_role_rate_override_defaults_to_none(tmp_path, monkeypatch):
    _write_role(tmp_path, monkeypatch, "scout_hub", 'label = "Scout Hub"\n')
    assert load_role("scout_hub").calls_per_load_per_window is None


def test_role_rate_override_rejects_non_positive(tmp_path, monkeypatch, caplog):
    _write_role(
        tmp_path, monkeypatch, "scout_hub",
        'label = "Scout Hub"\ncalls_per_load_per_window = 0\n',
    )
    with caplog.at_level(logging.WARNING):
        spec = load_role("scout_hub")
    assert spec.calls_per_load_per_window is None
    assert "calls_per_load_per_window" in caplog.text


def test_role_rate_override_rejects_non_int(tmp_path, monkeypatch, caplog):
    _write_role(
        tmp_path, monkeypatch, "scout_hub",
        'label = "Scout Hub"\ncalls_per_load_per_window = "lots"\n',
    )
    with caplog.at_level(logging.WARNING):
        spec = load_role("scout_hub")
    assert spec.calls_per_load_per_window is None


def test_missing_manifest_yields_no_rate_override(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    assert load_role("pi_lab").calls_per_load_per_window is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_roles.py -v -k rate_override`
Expected: FAIL with `TypeError: RoleSpec.__init__() got an unexpected keyword argument` or `AttributeError: 'RoleSpec' object has no attribute 'calls_per_load_per_window'`

- [ ] **Step 3: Add the settings**

In `src/config.py`, immediately after the `max_consecutive_reactive_turns: int = 3` line (line 348):

```python

    # Load-proportional rate limiter. Replaces the cumulative --budget cap as the
    # LIVE throttle: allowance = llm_calls_per_load_per_window * _agent_load(agent),
    # measured over a sliding llm_rate_window_seconds.
    #
    # A rate self-heals — a throttled agent is eligible again as the window slides
    # — where a cumulative cap benches permanently, and, because _rebuild_state
    # restores api_call_count from llm_call_logs, benches permanently ACROSS
    # RESTARTS. That is what took the blackbird hub off the air for 161 turns.
    #
    # Calibrated against run 4f1e8395: a spoke ran ~0.27 calls/10min and the hub
    # ~2.6, so 8 leaves a spoke ~30x headroom while tripping a runaway (back-to-back
    # calls) in ~25s. A hub at load 12 gets 96/window and trips in ~5min — the
    # deliberate price of the 12x allowance. Lower this to tighten it.
    # See docs/specs/2026-08-06-hub-budget-scheduler-design.md §4.2 / §5.
    llm_rate_window_seconds: int = 600
    llm_calls_per_load_per_window: int = 8
```

- [ ] **Step 4: Add the `RoleSpec` field**

In `src/agent/roles.py`, replace the `RoleSpec` dataclass (lines 30-34):

```python
@dataclass(frozen=True)
class RoleSpec:
    name: str
    label: str
    tools: frozenset[str]
    # Optional per-role override for Settings.llm_calls_per_load_per_window.
    # None means "use the global setting". This exists to pin a specific agent;
    # it is NOT the mechanism — the load signal is (design §4.4). No role sets it.
    calls_per_load_per_window: int | None = None
```

- [ ] **Step 5: Parse the key in `load_role`**

In `src/agent/roles.py`, replace the final `return` of `load_role` (currently `return RoleSpec(name=name, label=label, tools=tools)`) with:

```python
    rate = data.get("calls_per_load_per_window")
    if rate is not None and not (
        isinstance(rate, int) and not isinstance(rate, bool) and rate > 0
    ):
        logger.warning(
            "[roles] %s: calls_per_load_per_window must be a positive int, "
            "got %r — ignored", name, rate,
        )
        rate = None
    return RoleSpec(
        name=name, label=label, tools=tools, calls_per_load_per_window=rate,
    )
```

`isinstance(rate, bool)` is excluded deliberately: `True` is an `int` in Python and would otherwise be accepted as an allowance of 1.

- [ ] **Step 6: Run the role tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_roles.py -v`
Expected: all PASS (the pre-existing tests too — the new field is defaulted, so the two early-return paths in `load_role` need no edit)

- [ ] **Step 7: Verify the settings load**

Run: `.venv-test/bin/python -c "from src.config import Settings; s=Settings(); print(s.llm_rate_window_seconds, s.llm_calls_per_load_per_window)"`
Expected: `600 8`

- [ ] **Step 8: Lint and commit**

```bash
.venv-test/bin/python -m ruff check tests/unit/test_roles.py
git add src/config.py src/agent/roles.py tests/unit/test_roles.py
git commit -m "feat(config): rate-limiter settings + optional per-role allowance"
```

---

### Task 3: The call ledger

**Files:**
- Modify: `src/agent/state.py` (imports at line 3; `AgentState` at line 60-73)
- Modify: `src/agent/agent.py` (`__init__` around line 79)
- Modify: `src/agent/simulation.py` (lines 857, 903, 1172, 1947, 4670)
- Test: `tests/unit/test_hub_budget_scheduler.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `AgentState.call_times: deque[float]` and `Agent.record_api_call(self, now: float | None = None) -> None`. Task 4 reads `call_times`; Task 5 populates it.

- [ ] **Step 1: Write the failing ledger tests**

Append to `tests/unit/test_hub_budget_scheduler.py`:

```python
class TestCallLedger:
    def test_record_api_call_increments_both_counters(self):
        a = Agent(agent_id="hub", bot_name="HubBot", pi_name="PI hub")
        a.record_api_call(now=100.0)
        a.record_api_call(now=101.0)
        assert a.api_call_count == 2
        assert list(a.state.call_times) == [100.0, 101.0]

    def test_record_api_call_defaults_to_wall_clock(self):
        a = Agent(agent_id="hub", bot_name="HubBot", pi_name="PI hub")
        before = time.time()
        a.record_api_call()
        after = time.time()
        assert a.api_call_count == 1
        assert before <= a.state.call_times[0] <= after

    def test_call_times_starts_empty(self):
        a = Agent(agent_id="hub", bot_name="HubBot", pi_name="PI hub")
        assert len(a.state.call_times) == 0
        assert a.api_call_count == 0
```

Add `import time` to the test file's imports (alphabetically before `import types`).

- [ ] **Step 2: Run to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_hub_budget_scheduler.py::TestCallLedger -v`
Expected: FAIL with `AttributeError: 'Agent' object has no attribute 'record_api_call'`

- [ ] **Step 3: Add `call_times` to `AgentState`**

In `src/agent/state.py`, change the import line 3 from:

```python
from dataclasses import dataclass, field
```

to:

```python
from collections import deque
from dataclasses import dataclass, field
```

Then in `AgentState`, immediately after `last_seen_cursor: float = 0.0` (line 68), add:

```python

    # Sliding-window LLM call ledger, maintained by Agent.record_api_call.
    # Distinct from Agent.api_call_count on purpose: api_call_count is LIFETIME
    # accounting (it feeds the run summary and SimulationRun.total_api_calls),
    # while call_times is the LIVE throttle and its entries age out. Only the
    # latter gates eligibility, which is why throttling can no longer be
    # permanent. See docs/specs/2026-08-06-hub-budget-scheduler-design.md §4.2.
    call_times: deque[float] = field(default_factory=deque)
```

- [ ] **Step 4: Add `record_api_call` to `Agent`**

In `src/agent/agent.py`, add this method immediately before the `# Profile properties` comment block (after `__init__` ends with `self.allowed_sender_ids: set[str] | None = None`):

```python
    def record_api_call(self, now: float | None = None) -> None:
        """Record one LLM call against both the lifetime counter and the
        sliding-window ledger.

        The single write point for both. Every call site must use this rather
        than bumping ``api_call_count`` directly — a site that bumps only the
        counter is invisible to the rate limiter, and a site that appends only to
        the ledger corrupts ``SimulationRun.total_api_calls``.
        """
        self.api_call_count += 1
        self.state.call_times.append(time.time() if now is None else now)
```

Add `import time` to `src/agent/agent.py`'s imports if not already present. Verify with:
`grep -n "^import time" src/agent/agent.py`

- [ ] **Step 5: Run the ledger tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_hub_budget_scheduler.py::TestCallLedger -v`
Expected: 3 PASS

- [ ] **Step 6: Convert all five call sites**

In `src/agent/simulation.py`, replace `agent.api_call_count += 1` with `agent.record_api_call()` at lines 857, 903, 1172, 1947, and 4670. Note line 4670 is indented one extra level (inside a `try:`), so preserve its indentation.

Verify none were missed:

```bash
grep -n "api_call_count += 1" src/agent/simulation.py
```

Expected: no output. (`simulation.py:3839` assigns `agent.api_call_count = r.count` — that is the rebuild, not an increment, and must stay.)

- [ ] **Step 7: Run the simulation unit tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_simulation_logic.py tests/unit/test_cohort_isolation.py -q`
Expected: all PASS

- [ ] **Step 8: Lint and commit**

```bash
.venv-test/bin/python -m ruff check tests/unit/test_hub_budget_scheduler.py
git add src/agent/state.py src/agent/agent.py src/agent/simulation.py tests/unit/test_hub_budget_scheduler.py
git commit -m "feat(sched): call ledger — record_api_call maintains both counters"
```

---

### Task 4: The sliding-window rate limiter

**Files:**
- Modify: `src/agent/state.py` (`AgentState`, add `throttled`)
- Modify: `src/agent/simulation.py` (imports line 28; `_turn_eligible` lines 655-669; new methods after `_agent_load`)
- Test: `tests/unit/test_hub_budget_scheduler.py` (append)

**Interfaces:**
- Consumes: `_agent_load` (Task 1), `Settings.llm_rate_window_seconds` / `llm_calls_per_load_per_window` and `RoleSpec.calls_per_load_per_window` (Task 2), `AgentState.call_times` (Task 3).
- Produces: `SimulationEngine._calls_per_load(self, agent: Agent) -> int` and `SimulationEngine._within_rate_limit(self, agent: Agent, now: float) -> bool`. Task 8 asserts against both.

- [ ] **Step 1: Write the failing limiter tests**

Append to `tests/unit/test_hub_budget_scheduler.py`:

```python
class TestRateLimiter:
    def test_under_allowance_is_eligible(self, monkeypatch):
        _patch(monkeypatch, llm_calls_per_load_per_window=8)
        eng = _engine(["spoke"])
        a = eng.agents["spoke"]
        for i in range(7):
            a.record_api_call(now=1000.0 + i)
        assert eng._within_rate_limit(a, 1010.0) is True

    def test_at_allowance_is_throttled(self, monkeypatch):
        _patch(monkeypatch, llm_calls_per_load_per_window=8)
        eng = _engine(["spoke"])
        a = eng.agents["spoke"]
        for i in range(8):
            a.record_api_call(now=1000.0 + i)
        assert eng._within_rate_limit(a, 1010.0) is False

    def test_throttle_self_heals_as_the_window_slides(self, monkeypatch):
        """The regression test for the permanent bench. A throttled agent MUST
        become eligible again once its calls age out — this is the single
        property the cumulative cap did not have."""
        _patch(monkeypatch, llm_calls_per_load_per_window=8,
               llm_rate_window_seconds=600)
        eng = _engine(["spoke"])
        a = eng.agents["spoke"]
        for i in range(8):
            a.record_api_call(now=1000.0 + i)
        assert eng._within_rate_limit(a, 1010.0) is False
        # 700s later every recorded call is outside the 600s window.
        assert eng._within_rate_limit(a, 1710.0) is True
        assert len(a.state.call_times) == 0

    def test_allowance_scales_with_load(self, monkeypatch):
        _patch(monkeypatch, llm_calls_per_load_per_window=8,
               active_thread_threshold=12)
        eng = _engine(["hub"])
        hub = eng.agents["hub"]
        _add_threads(hub, 12)
        for i in range(50):
            hub.record_api_call(now=1000.0 + i)
        # load 12 -> allowance 96, so 50 calls is fine for a hub...
        assert eng._within_rate_limit(hub, 1060.0) is True
        # ...but the identical ledger throttles a load-1 spoke.
        spoke = Agent(agent_id="spoke", bot_name="SpokeBot", pi_name="PI spoke")
        for i in range(50):
            spoke.record_api_call(now=1000.0 + i)
        assert eng._within_rate_limit(spoke, 1060.0) is False

    def test_role_override_beats_the_global_setting(self, monkeypatch):
        _patch(monkeypatch, llm_calls_per_load_per_window=8)
        monkeypatch.setattr(
            "src.agent.simulation.load_role",
            lambda name: types.SimpleNamespace(calls_per_load_per_window=2),
        )
        eng = _engine(["spoke"])
        a = eng.agents["spoke"]
        for i in range(3):
            a.record_api_call(now=1000.0 + i)
        assert eng._calls_per_load(a) == 2
        assert eng._within_rate_limit(a, 1010.0) is False

    def test_turn_eligible_requires_both_checks(self, monkeypatch):
        """Legacy cumulative cap and the rate limiter compose with AND."""
        _patch(monkeypatch, llm_calls_per_load_per_window=8)
        eng = _engine(["spoke"], budget_cap=5)
        a = eng.agents["spoke"]
        # Rate limit fine (1 call), legacy cap blown (api_call_count 6 >= 5).
        a.api_call_count = 6
        a.record_api_call(now=1000.0)
        assert eng._turn_eligible(a, 1010.0) is False

    def test_turn_eligible_passes_when_both_pass(self, monkeypatch):
        _patch(monkeypatch, llm_calls_per_load_per_window=8)
        eng = _engine(["spoke"], budget_cap=0)
        a = eng.agents["spoke"]
        a.record_api_call(now=1000.0)
        assert eng._turn_eligible(a, 1010.0) is True

    def test_throttle_transition_warns_once(self, monkeypatch, caplog):
        _patch(monkeypatch, llm_calls_per_load_per_window=2)
        eng = _engine(["spoke"])
        a = eng.agents["spoke"]
        a.record_api_call(now=1000.0)
        a.record_api_call(now=1001.0)
        with caplog.at_level(logging.WARNING):
            eng._within_rate_limit(a, 1010.0)
            eng._within_rate_limit(a, 1011.0)
            eng._within_rate_limit(a, 1012.0)
        assert caplog.text.count("throttled") == 1
```

Add `import logging` to the test file's imports (alphabetically first).

- [ ] **Step 2: Run to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_hub_budget_scheduler.py::TestRateLimiter -v`
Expected: FAIL with `AttributeError: 'SimulationEngine' object has no attribute '_within_rate_limit'`

- [ ] **Step 3: Add the `throttled` flag to `AgentState`**

In `src/agent/state.py`, immediately after the `call_times` field added in Task 3:

```python
    # True while the agent is rate-limited. Tracked only so the transition into
    # throttling can be logged once instead of once per scheduler tick — a silent
    # throttle is what turned the original incident into a 2.5-hour undetected
    # outage. See design §6.
    throttled: bool = False
```

- [ ] **Step 4: Implement the limiter**

In `src/agent/simulation.py`, add `load_role` to the roles import. The file currently imports `from src.agent.tools import execute_tool, tools_for_role` at line 28 but does not import from `src.agent.roles`; add a new import line after line 27:

```python
from src.agent.roles import load_role
```

Then add both methods immediately after `_agent_load` (from Task 1):

```python
    def _calls_per_load(self, agent: Agent) -> int:
        """Per-unit-of-load LLM allowance for this agent's role.

        Cached by role NAME, so an agent flipping roles at runtime simply looks
        up a different key and needs no invalidation. The only staleness is a
        role.toml edited mid-run, which matches get_settings() already being
        lru_cached — both need a container recreate (design §5).

        The cache exists because load_role() reads TOML from disk on every call
        and this runs for every agent on every scheduler tick.
        """
        cached = self._role_rate_cache.get(agent.role, _UNSET)
        if cached is _UNSET:
            cached = load_role(agent.role).calls_per_load_per_window
            self._role_rate_cache[agent.role] = cached
        if cached is not None:
            return cached
        return get_settings().llm_calls_per_load_per_window

    def _within_rate_limit(self, agent: Agent, now: float) -> bool:
        """Sliding-window LLM rate check — the LIVE throttle.

        allowance = _calls_per_load(agent) * _agent_load(agent), over
        llm_rate_window_seconds. Unlike the cumulative cap this replaces, it
        self-heals: entries age out, so an agent throttled now is eligible later.
        See design §4.2.
        """
        allowance = self._calls_per_load(agent) * self._agent_load(agent)
        window_start = now - get_settings().llm_rate_window_seconds
        times = agent.state.call_times
        while times and times[0] < window_start:
            times.popleft()
        ok = len(times) < allowance
        if not ok and not agent.state.throttled:
            logger.warning(
                "[%s] throttled: %d LLM calls in the last %ds at load %d "
                "(allowance %d). Eligible again as the window slides.",
                agent.agent_id, len(times),
                get_settings().llm_rate_window_seconds,
                self._agent_load(agent), allowance,
            )
        agent.state.throttled = not ok
        return ok
```

Add the sentinel at module level, immediately after the `SELECTION_RATIO_LOG_EVERY` constant (find it with `grep -n "SELECTION_RATIO_LOG_EVERY = " src/agent/simulation.py`):

```python
# Distinguishes "role has no cached rate yet" from "role's cached rate is None
# (no override)". A plain dict.get() default cannot tell those apart, so the
# cache would re-read role.toml from disk on every tick for every default role.
_UNSET = object()
```

And initialise the cache in `SimulationEngine.__init__`, immediately after `self.slack_enabled = slack_enabled` (line 222):

```python
        # role name -> calls_per_load_per_window override (or None). See _calls_per_load.
        self._role_rate_cache: dict[str, int | None] = {}
```

- [ ] **Step 5: Compose both checks in `_turn_eligible`**

In `src/agent/simulation.py`, replace the body of `_turn_eligible` (lines 655-669) with:

```python
    def _turn_eligible(self, agent: Agent, now: float) -> bool:
        """Selection eligibility for one agent.

        - within the LEGACY cumulative cap. Inert by default (``budget_cap``
          defaults to 0, and ``_agent_within_budget`` short-circuits at <= 0);
          armed only when an operator passes ``--budget``. Retained, not removed,
          for back-compat — see design §6;
        - within its sliding-window rate limit. This is the live throttle;
        - past its per-agent cooldown. ``turn_delay_seconds`` throttles an
          individual agent's tempo; enforcing it here (rather than as a global
          ``asyncio.sleep`` after every productive turn) leaves the rest of the
          roster free to act while one agent sits out. See v2 §10.3.
        """
        if not self._agent_within_budget(agent):
            return False
        if not self._within_rate_limit(agent, now):
            return False
        delay = get_settings().turn_delay_seconds
        if delay > 0 and (now - agent.state.last_selected) < delay:
            return False
        return True
```

- [ ] **Step 6: Reword the main loop's stop message**

The loop's break message still says "over budget", which is now misleading — under a
rate limiter that state is transient, not terminal. In `src/agent/simulation.py`,
replace lines 502-505:

```python
            if not agent or not self._agent_within_budget(agent):
                # All agents over budget
                logger.info("All agents over budget or no agent selected. Stopping.")
                break
```

with:

```python
            if not agent or not self._agent_within_budget(agent):
                # No agent is currently eligible: every one is either rate-limited,
                # cooling down, or over the legacy cumulative cap. Rate limiting is
                # transient (the window slides), so this is no longer necessarily
                # terminal — but the loop's contract is unchanged, so say what was
                # observed rather than guessing which cause applied.
                logger.info(
                    "No eligible agent (all throttled, cooling down, or over the "
                    "legacy --budget cap). Stopping."
                )
                break
```

- [ ] **Step 7: Run the limiter tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_hub_budget_scheduler.py -v`
Expected: all PASS

- [ ] **Step 8: Confirm the pre-existing scheduler tests still pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_cohort_isolation.py -v -k Scheduler`
Expected: all PASS, including `test_budget_still_filters` (it sets `budget_cap=1` explicitly, so the retained legacy cap still filters it)

- [ ] **Step 9: Lint and commit**

```bash
.venv-test/bin/python -m ruff check tests/unit/test_hub_budget_scheduler.py
.venv-test/bin/python -m ruff check src --output-format=concise --quiet | wc -l
git add src/agent/state.py src/agent/simulation.py tests/unit/test_hub_budget_scheduler.py
git commit -m "feat(sched): sliding-window rate limiter replaces the cumulative cap"
```

The ruff count must be <= 260.

---

### Task 5: Restart rebuild — step 4b

**Files:**
- Modify: `src/agent/simulation.py` (`_rebuild_state` step 4, lines 3822-3841)
- Test: `tests/unit/test_hub_budget_scheduler.py` (append)

**Interfaces:**
- Consumes: `AgentState.call_times` (Task 3).
- Produces: nothing new. Behavioural only.

**CRITICAL:** Step 4's existing `COUNT(*)` into `api_call_count` must be left byte-identical. You are **adding** step 4b, not modifying step 4. `tests/integration/test_full_run_live.py:1111` asserts `api_call_count` survives restart and must keep passing unedited — if it fails, you modified step 4.

- [ ] **Step 1: Write the failing rebuild test**

Append to `tests/unit/test_hub_budget_scheduler.py`:

```python
class TestRestartRebuild:
    def test_window_filter_selects_only_recent_calls(self, monkeypatch):
        """Step 4b's cutoff arithmetic, isolated from the DB.

        The full DB round trip is covered by the integration suite; what matters
        here is that the cutoff is `now - window` and that boundary rows are
        included, since an off-by-one there silently re-creates the permanent
        bench for anything on the edge.
        """
        _patch(monkeypatch, llm_rate_window_seconds=600)
        eng = _engine(["hub"])
        a = eng.agents["hub"]
        now = 10_000.0
        # Simulate what step 4b loads: only rows at or after the cutoff.
        cutoff = now - 600
        rows = [now - 1200, now - 700, now - 600, now - 100, now - 1]
        a.state.call_times.extend(t for t in rows if t >= cutoff)
        assert list(a.state.call_times) == [now - 600, now - 100, now - 1]
        assert eng._within_rate_limit(a, now) is True

    def test_agent_whose_calls_all_predate_the_window_starts_unthrottled(
        self, monkeypatch
    ):
        """The exact post-restart state that benched the hub: a large lifetime
        count, but nothing inside the window."""
        _patch(monkeypatch, llm_calls_per_load_per_window=8)
        eng = _engine(["hub"])
        a = eng.agents["hub"]
        a.api_call_count = 42  # rebuilt by step 4, lifetime
        # step 4b found no rows inside the window
        assert eng._within_rate_limit(a, 10_000.0) is True
        assert eng._turn_eligible(a, 10_000.0) is True
```

- [ ] **Step 2: Run the characterisation tests — expect PASS, not FAIL**

Run: `.venv-test/bin/python -m pytest tests/unit/test_hub_budget_scheduler.py::TestRestartRebuild -v`
Expected: both PASS **before** step 4b exists.

This is deliberate and is the one place in this plan that is not red-green. `call_times`
is empty by default, so the property already holds trivially; these are
*characterisation* tests that pin it so step 4b cannot silently break it. If either
FAILS here, something in Tasks 3-4 is wrong — stop and fix that before adding step 4b.

- [ ] **Step 3: Add step 4b**

In `src/agent/simulation.py`, immediately after step 4's `except Exception as exc: logger.warning("Failed to rebuild api_call_count: %s", exc)` (line 3840-3841) and before the `# 5. Set last_seen_cursor per agent` comment, insert:

```python
        # 4b. Rebuild the sliding-window call ledger from the same table.
        #
        # Deliberately SEPARATE from step 4, which stays an all-time COUNT(*):
        # api_call_count is lifetime accounting (run summary,
        # SimulationRun.total_api_calls) while call_times is the live throttle.
        # Folding these together is the bug — it is what made an over-budget
        # agent over-budget again on every restart, forever. See design §4.2.
        if self.session_factory and self.simulation_run_id:
            try:
                from sqlalchemy import select as sa_select

                # datetime, UTC and timedelta are already module-level imports
                # (simulation.py:10) — do not re-import them here.
                cutoff = datetime.now(UTC) - timedelta(
                    seconds=get_settings().llm_rate_window_seconds
                )
                async with self.session_factory() as db:
                    result = await db.execute(
                        sa_select(LlmCallLog.agent_id, LlmCallLog.created_at)
                        .where(
                            LlmCallLog.simulation_run_id == self.simulation_run_id,
                            LlmCallLog.created_at >= cutoff,
                        )
                        .order_by(LlmCallLog.created_at)
                    )
                    for r in result:
                        agent = self.agents.get(r.agent_id)
                        if agent:
                            agent.state.call_times.append(r.created_at.timestamp())
            except Exception as exc:
                logger.warning("Failed to rebuild call_times: %s", exc)
```

`.order_by(created_at)` is load-bearing: `_within_rate_limit` prunes with `popleft()` and assumes the deque is oldest-first.

- [ ] **Step 4: Run the unit tests**

Run: `.venv-test/bin/python -m pytest tests/unit/test_hub_budget_scheduler.py -v`
Expected: all PASS

- [ ] **Step 5: Verify the tripwire test is untouched and still passing**

```bash
git diff --stat tests/integration/test_full_run_live.py
```

Expected: no output (file unmodified).

Run: `.venv-test/bin/python -m pytest tests/integration/test_state_rebuild.py -q`
Expected: PASS. (This needs Docker for testcontainers.)

- [ ] **Step 6: Lint and commit**

```bash
.venv-test/bin/python -m ruff check tests/unit/test_hub_budget_scheduler.py
git add src/agent/simulation.py tests/unit/test_hub_budget_scheduler.py
git commit -m "feat(sched): rebuild call_times from llm_call_logs within the window"
```

---

### Task 6: Load-proportional scheduling

**Files:**
- Modify: `src/agent/simulation.py` (`_select_agent`, lines 671-717)
- Test: `tests/unit/test_hub_budget_scheduler.py` (append)

**Interfaces:**
- Consumes: `_agent_load` (Task 1).
- Produces: nothing new. Behavioural only.

- [ ] **Step 1: Write the failing scheduler tests**

Append to `tests/unit/test_hub_budget_scheduler.py`:

```python
class TestScheduler:
    def test_proactive_weight_scales_with_load(self, monkeypatch):
        """A load-12 hub against 12 load-1 spokes, all equally stale, should take
        ~12/(12+12) = 50% of proactive draws. Under the old agent-fair weighting
        it took 1/13 = 7.7%."""
        _patch(monkeypatch, active_thread_threshold=12)
        random.seed(20260806)
        ids = ["hub"] + [f"pi{i}" for i in range(12)]
        eng = _engine(ids)
        _add_threads(eng.agents["hub"], 12)
        now = time.time()
        for a in eng.agents.values():
            a.state.last_selected = now - 100.0

        picks = [eng._select_agent().agent_id for _ in range(2000)]
        share = picks.count("hub") / 2000
        assert 0.42 < share < 0.58, f"hub share {share:.3f} not load-proportional"

    def test_reactive_tiebreak_no_longer_penalises_the_busy_agent(
        self, monkeypatch
    ):
        """The hub is selected often, so its last_selected is always recent. Under
        min(last_selected) it lost every tiebreak to a long-idle spoke — it was
        penalised precisely for being busy. Weighted by load, it wins."""
        _patch(monkeypatch, active_thread_threshold=12)
        eng = _engine(["hub", "spoke"])
        now = time.time()
        _add_threads(eng.agents["hub"], 12, pending=True)
        _add_threads(eng.agents["spoke"], 1, pending=True, prefix="s")
        eng.agents["hub"].state.last_selected = now - 10.0    # 10 * 12 = 120
        eng.agents["spoke"].state.last_selected = now - 60.0  # 60 *  1 =  60

        assert eng._select_agent().agent_id == "hub"

    def test_reactive_tier_still_prefers_a_genuinely_starved_spoke(
        self, monkeypatch
    ):
        """The load weighting must not become a blank cheque: a spoke that has
        waited long enough still outranks the hub."""
        _patch(monkeypatch, active_thread_threshold=12)
        eng = _engine(["hub", "spoke"])
        now = time.time()
        _add_threads(eng.agents["hub"], 2, pending=True)
        _add_threads(eng.agents["spoke"], 1, pending=True, prefix="s")
        eng.agents["hub"].state.last_selected = now - 10.0     # 10 * 2 =  20
        eng.agents["spoke"].state.last_selected = now - 600.0  # 600 * 1 = 600

        assert eng._select_agent().agent_id == "spoke"

    def test_throttled_hub_is_not_selected(self, monkeypatch):
        _patch(monkeypatch, llm_calls_per_load_per_window=1,
               active_thread_threshold=12)
        eng = _engine(["hub", "spoke"])
        now = time.time()
        hub = eng.agents["hub"]
        _add_threads(hub, 1)
        hub.record_api_call(now=now)
        for _ in range(50):
            assert eng._select_agent().agent_id == "spoke"
```

Add `import random` to the test file's imports (alphabetically after `import logging`).

- [ ] **Step 2: Run to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_hub_budget_scheduler.py::TestScheduler -v`
Expected: `test_proactive_weight_scales_with_load` FAILs (share ≈ 0.077) and `test_reactive_tiebreak_no_longer_penalises_the_busy_agent` FAILs (returns `spoke`)

- [ ] **Step 3: Weight the reactive tiebreak by load**

In `src/agent/simulation.py`, inside `_select_agent`, replace:

```python
                return min(owed, key=lambda a: a.state.last_selected)
```

with:

```python
                # Weighted by load, NOT bare last_selected. The hub is selected
                # often, so its last_selected is always recent — under
                # min(last_selected) it lost every tiebreak to a long-idle spoke,
                # i.e. it was penalised precisely for being the busiest agent.
                # Still "longest wait wins", now scaled by obligation count.
                # See design §1.3 / §4.3.
                return max(
                    owed,
                    key=lambda a: (now - a.state.last_selected) * self._agent_load(a),
                )
```

- [ ] **Step 4: Weight the proactive tier by load**

In the same method, replace:

```python
            w = max(now - a.state.last_selected, 1.0)
```

with:

```python
            w = max(now - a.state.last_selected, 1.0) * self._agent_load(a)
```

- [ ] **Step 5: Update the `_select_agent` docstring**

Replace the docstring's item 2 (`2. **Proactive** — the original weighted-random selection:` through the `P(agent) ∝ ...` line) with:

```python
        2. **Proactive** — staleness-weighted random, scaled by load:
           P(agent) ∝ (now - last_selected) * _agent_load(agent), with a penalty
           for agents that have repeatedly skipped Phase 5
           (weight /= 2^(skips-2) once skips >= 3). The load factor is what makes
           a star's hub — one endpoint of every conversation — draw a share that
           tracks the edges it actually sits on, instead of the 1/N a uniform
           weighting gave it. See design §4.3.
```

- [ ] **Step 6: Run the scheduler tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_hub_budget_scheduler.py -v`
Expected: all PASS

- [ ] **Step 7: Confirm the pre-existing scheduler suite still passes**

Run: `.venv-test/bin/python -m pytest tests/unit/test_cohort_isolation.py -v -k Scheduler`
Expected: all PASS. These agents have no active threads, so every load is 1 and the weighting is identity — the old assertions hold unchanged. If any fail, the load floor of 1 is not being applied.

- [ ] **Step 8: Lint and commit**

```bash
.venv-test/bin/python -m ruff check tests/unit/test_hub_budget_scheduler.py
git add src/agent/simulation.py tests/unit/test_hub_budget_scheduler.py
git commit -m "feat(sched): load-proportional selection weight and reactive tiebreak"
```

---

### Task 7: Deprecate `--budget` and update the runbook

**Files:**
- Modify: `src/agent/main.py` (line 35; `_run_simulation` around line 246)
- Modify: `CLAUDE.md` ("Running the Agent Simulation" section)
- Test: `tests/unit/test_hub_budget_scheduler.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing. CLI + docs only.

- [ ] **Step 1: Write the failing deprecation test**

Append to `tests/unit/test_hub_budget_scheduler.py`:

```python
class TestBudgetDeprecation:
    def test_default_budget_is_off(self):
        """The default must be 0 (off). A nonzero default is what silently armed
        the legacy cap on every run."""
        import inspect

        from src.agent.main import main

        default = inspect.signature(main).parameters["budget"].default
        assert default.default == 0

    def test_help_text_marks_the_flag_deprecated(self):
        import inspect

        from src.agent.main import main

        help_text = inspect.signature(main).parameters["budget"].default.help
        assert "DEPRECATED" in help_text
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv-test/bin/python -m pytest tests/unit/test_hub_budget_scheduler.py::TestBudgetDeprecation -v`
Expected: FAIL — default is 50, help text lacks "DEPRECATED"

- [ ] **Step 3: Change the flag default and help text**

In `src/agent/main.py`, replace line 35:

```python
    budget: int = typer.Option(50, "--budget", help="Max LLM calls per agent"),
```

with:

```python
    budget: int = typer.Option(
        0, "--budget",
        help=(
            "DEPRECATED legacy cumulative cap: max LLM calls per agent for the "
            "WHOLE run. 0 (default) disables it. Superseded by the sliding-window "
            "rate limiter (llm_calls_per_load_per_window). Passing a nonzero value "
            "can permanently bench a hub agent — see "
            "docs/specs/2026-08-06-hub-budget-scheduler-design.md §6."
        ),
    ),
```

- [ ] **Step 4: Warn loudly when the legacy cap is armed**

In `src/agent/main.py`, immediately before the existing `logger.info("Starting simulation: ...")` call (around line 246), insert:

```python
        if budget > 0:
            logger.warning(
                "--budget %d is the DEPRECATED cumulative cap. It counts LLM calls "
                "for the ENTIRE run, is rebuilt from llm_call_logs on restart, and "
                "therefore benches an agent PERMANENTLY once crossed — this is what "
                "took the blackbird hub off the air for 161 consecutive turns. The "
                "sliding-window rate limiter supersedes it. Pass --budget 0 unless "
                "you specifically want the legacy behaviour.",
                budget,
            )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv-test/bin/python -m pytest tests/unit/test_hub_budget_scheduler.py::TestBudgetDeprecation -v`
Expected: 2 PASS

- [ ] **Step 6: Verify the CLI help renders**

Run: `.venv-test/bin/python -m src.agent.main --help`
Expected: the `--budget` entry shows the DEPRECATED text and `[default: 0]`

- [ ] **Step 7: Update CLAUDE.md**

In `CLAUDE.md`, under "Running the Agent Simulation", replace the four example commands so none pass a nonzero `--budget`, and add this note immediately after the code block:

```markdown
**`--budget` is deprecated.** It is a *cumulative* cap for the whole run, it is
rebuilt from `llm_call_logs` on restart, and it therefore benches an agent
permanently once crossed — a restart does not clear it. It defaults to 0 (off)
and should stay there. Pacing and runaway protection are now handled by the
sliding-window rate limiter, whose allowance scales with each agent's live
conversational load (`llm_calls_per_load_per_window`, `llm_rate_window_seconds`).
A hub bot in a star topology will hit any uniform cumulative cap long before any
spoke does. See `docs/specs/2026-08-06-hub-budget-scheduler-design.md`.
```

Change the four `--budget 0` / `--budget 50` examples to drop the flag entirely, e.g.:

```bash
# Resume an existing run:
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main

# Fresh run (wipes agent_messages/channels, keeps proposals):
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main --fresh

# With a time limit (minutes):
$DC --profile agent run -d --name blackbird-agent-run agent python -m src.agent.main --max-runtime 60
```

- [ ] **Step 8: Lint and commit**

```bash
.venv-test/bin/python -m ruff check tests/unit/test_hub_budget_scheduler.py
git add src/agent/main.py CLAUDE.md tests/unit/test_hub_budget_scheduler.py
git commit -m "feat(cli): deprecate --budget, default it off, document the replacement"
```

---

### Task 8: Production regression test and the full gate

**Files:**
- Test: `tests/unit/test_hub_budget_scheduler.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 1-7.
- Produces: nothing.

- [ ] **Step 1: Write the production regression test**

Append to `tests/unit/test_hub_budget_scheduler.py`:

```python
class TestProductionRegression:
    """Reconstructs the exact state of run 4f1e8395 (2026-08-05), in which the
    blackbird hub took 0 of 161 turns while 56 spokes took 3-5 each.

    Measured then: hub 42 LLM calls, next-busiest agent 9, cap 40.
    """

    def _star(self, monkeypatch, budget_cap, **kw):
        _patch(monkeypatch, active_thread_threshold=12, **kw)
        ids = ["blackbird"] + [f"pi{i}" for i in range(56)]
        eng = _engine(ids, budget_cap=budget_cap)
        eng.agents["blackbird"].api_call_count = 42
        for i in range(56):
            eng.agents[f"pi{i}"].api_call_count = 8
        return eng

    def test_fixed_hub_is_selectable_after_restart(self, monkeypatch):
        """Case 1 — THE FIX. New default (budget_cap=0), lifetime count 42, but
        nothing inside the window because step 4b found no recent rows."""
        eng = self._star(monkeypatch, budget_cap=0)
        hub = eng.agents["blackbird"]
        now = time.time()
        assert eng._turn_eligible(hub, now) is True

        random.seed(20260806)
        picks = [eng._select_agent().agent_id for _ in range(2000)]
        assert picks.count("blackbird") > 0, "hub still benched — the fix failed"

    def test_throttling_is_still_real_but_temporary(self, monkeypatch):
        """Case 2 — the limiter has not been defanged. A load-1 hub that burns
        its allowance inside the window IS throttled, then recovers."""
        eng = self._star(monkeypatch, budget_cap=0,
                         llm_calls_per_load_per_window=8,
                         llm_rate_window_seconds=600)
        hub = eng.agents["blackbird"]
        base = 10_000.0
        for i in range(8):
            hub.record_api_call(now=base + i)
        assert eng._turn_eligible(hub, base + 10) is False
        assert eng._turn_eligible(hub, base + 700) is True

    def test_legacy_budget_flag_still_benches_the_hub(self, monkeypatch):
        """Case 3 — the compat path, pinned honestly. --budget 40 was NOT made
        safe; it was deprecated and defaulted off. If someone passes it, the old
        behaviour is exactly what they get."""
        eng = self._star(monkeypatch, budget_cap=40)
        hub = eng.agents["blackbird"]
        now = time.time()
        assert eng._turn_eligible(hub, now) is False
        picks = [eng._select_agent().agent_id for _ in range(500)]
        assert "blackbird" not in picks
```

- [ ] **Step 2: Run the regression test**

Run: `.venv-test/bin/python -m pytest tests/unit/test_hub_budget_scheduler.py::TestProductionRegression -v`
Expected: 3 PASS

- [ ] **Step 3: Run the whole new test module**

Run: `.venv-test/bin/python -m pytest tests/unit/test_hub_budget_scheduler.py -v`
Expected: all PASS

- [ ] **Step 4: Run the full CI gate**

Run: `./scripts/ci.sh`
Expected: `==> CI passed.`

If coverage dropped below 60, add tests — do not lower `COV_MIN`. If `src/` ruff findings exceed 260, fix what you added — do not raise `SRC_LINT_MAX`.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_hub_budget_scheduler.py
git commit -m "test(sched): production regression for the run-4f1e8395 hub bench"
```

---

## Deployment (NOT part of this plan)

Applying this to the live stack is a separate, operator-gated step. It requires a
container **recreate** (not a restart) because the new settings are read through
`@lru_cache`d `get_settings()` and `env_file` is resolved at container creation.
Follow the "Before restarting" runbook in `CLAUDE.md`: save logs, `docker stop -t 30`,
`docker rm`, rebuild `blackbird-app`, then `run` the agent profile. Confirm ownership
with `docker inspect ... com.docker.compose.project` first — `copi-blackbird` is this
repo, `copi-python` is org1 and must not be touched.

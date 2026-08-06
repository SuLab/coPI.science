# Design — load-proportional budget and scheduling for star topologies

**Status:** DESIGN, not implemented.
**Date:** 2026-08-06
**Target branch:** `blackbird`
**Closes:** A7 (per-role caps/budgets), explicitly deferred by
`docs/specs/2026-08-05-hub-bot-customization-design.md` §2 and left open in
`docs/blackbird-star-topology-runbook.md`.
**Companion:** `docs/blackbird-star-topology-runbook.md` (the star topology).

---

## 1. Problem

The blackbird deployment is a star: one `blackbird` hub bot and 56 PI spokes, wired as
56 pairwise cohorts. The hub is one endpoint of *every* conversation; each spoke is an
endpoint of one.

Both the LLM budget and the turn scheduler treat all agents as interchangeable. For a
star, that is not neutral — it is actively wrong, and it took the hub off the air.

### 1.1 What actually happened (measured, run `4f1e8395`, 2026-08-05)

The simulation was started with `--budget 40`. `_agent_within_budget`
(`src/agent/simulation.py:375`) is `api_call_count < budget_cap`, and `_turn_eligible`
drops any agent failing it from `_select_agent`'s candidate pool entirely.

| Agent | LLM calls |
|---|---|
| `blackbird` | **42** |
| `scott`, `bailey`, `tsapatsis` | 9 |
| next 8 agents | 7–8 |

The hub was the only agent at or over the cap. It went silent at 21:09 and took
**0 of the 161 turns** in the following 2.5 hours while every other agent took 3–5.

Two properties made this worse than a normal cap:

- **It is silent.** Nothing logs when an agent is benched. The failure presents as "the
  bot stopped talking", with no error anywhere.
- **It survives restarts.** `_rebuild_state` step 4 (`simulation.py:~3822`) recomputes
  `api_call_count` with a `COUNT(*)` over `llm_call_logs` for the *same*
  `simulation_run_id`. A container restart therefore restores the agent to its
  over-budget state. Only `--budget 0` or `--fresh` clears it. The 22:43 restart could
  never have helped, and did not.

### 1.2 Why the hub burns calls faster (structural, not a bug)

Servicing one spoke costs the hub roughly four LLM calls: Phase 2 `scan`, Phase 4
`thread_reply`, Phase 5 `new_post`, and the thread-closure working-memory update
(`simulation.py:~4670`). Its 42 calls decomposed as new_post 14, scan 13, thread_reply
8, memory 7 — only 19% were replies. At ~4 calls per spoke, `--budget 40` buys ~10
spokes out of 56. It reached 8.

This is not fixable by raising the number. Any uniform cumulative cap is a countdown to
the same bench; raising it only moves the cliff.

### 1.3 The scheduler shortfall is separate, and real

Before the bench, the reactive tier was already working: the hub took **12.2% of all
LLM calls against a 1.75% equal share** — a 7× boost. But in a star the hub is one
endpoint of every conversation, so a healthy hub should approach ~50% of traffic. It
got 12.2% of calls and 5% of messages (16 of 321).

There is also a specific defect. The reactive tier breaks ties with
`min(owed, key=lambda a: a.state.last_selected)` (`simulation.py:~705`). The hub is
selected often, so its `last_selected` is always recent, so it **loses every tiebreak to
a long-idle spoke**. The scheduler penalizes the hub precisely for being busy.

### 1.4 Root cause

The limiter and the scheduler hold **contradictory models of what the hub deserves**.
The reactive tier said "act, you owe 8 replies"; the cumulative cap said "you are
done" — and the cap won, silently. Any fix that patches one side leaves the
disagreement in place.

## 2. Requirements (settled during brainstorming)

- **`--budget` is a circuit breaker and a pacing lever, NOT a cost cap.** Confirmed with
  the operator. Total spend is not what it protects; runaway behaviour and
  conversational pacing are. This is why "give the hub a bigger number" is the wrong
  shape of answer.
- **The limit becomes a rate (sliding window), not a cumulative total.** A rate serves
  both goals directly, and self-heals: a throttled agent becomes eligible again as the
  window slides. No permanent bench, no sticky-across-restart failure.
- **Allowance and scheduling weight derive from one shared load signal**, so the two
  can never disagree again (§1.4).
- **Scope is budget + scheduler.** Full observability tooling (dashboards, per-agent
  turn-share reporting) is out of scope; one throttle-transition warning is in (§6).

### 2.1 Non-goals

- No cost/token budgeting. Explicitly rejected by the operator.
- No change to `active_thread_threshold`, thread turnover, or hub coverage policy. See
  §7 — after this fix, coverage is bounded by thread capacity, and that is intended to
  remain a separate, visible knob.
- No schema change, no migration.
- No fix for the hybrid-Slack problem (hub has no bot token, so its posts are
  `MOCK post` and never reach Slack). Separate issue, tracked in the runbook.

## 3. Was edge-based budgeting the answer?

Directionally yes, and the arithmetic checks out: if each spoke runs at rate `R`, total
spoke traffic is `56R`, and the hub — the other endpoint of all of it — needs ≈`56R`.
An allowance proportional to edge count is dimensionally correct.

It was rejected as the *implementation* for two reasons:

1. **Edges ≠ live load.** The hub has 56 cohort edges but is bounded to
   `active_thread_threshold` (12) concurrent threads. Budgeting on 56 over-allocates by
   ~5×.
2. **It weakens the breaker where it matters most.** A 56× allowance hands the largest
   blast radius to the agent with the biggest prompt, the most tools, and the most
   complex role — the one most likely to run away.

Load-proportional budgeting keeps the correct dimension while tracking reality and
keeping the breaker tight. It is edge-based budgeting with the right denominator.

## 4. Design

### 4.1 The load signal

One method on `SimulationEngine`:

```python
def _agent_load(self, agent: Agent) -> int:
    """Concurrent conversational obligations. 1 for an idle agent."""
    live = sum(1 for t in agent.state.active_threads.values() if t.status == "active")
    return max(1, min(live, get_settings().active_thread_threshold))
```

The clamp is load-bearing at both ends. The floor of 1 keeps an idle agent eligible.
The ceiling means nothing can inflate its own allowance past the thread cap it is
already bound by — which is what stops a thread-opening runaway from financing itself.

### 4.2 Consumer 1 — the rate limiter

`AgentState` gains `call_times: deque[float]`. Every existing `agent.api_call_count += 1`
site (`simulation.py:857, 903, 1172, 1947, 4670`) is replaced by a single
`agent.record_api_call()` helper maintaining both counters. `api_call_count` is retained
— it is reported in the run summary and in `SimulationRun.total_api_calls`.

A new `_within_rate_limit` performs the window check:

```python
def _within_rate_limit(self, agent: Agent, now: float) -> bool:
    settings = get_settings()
    allowance = self._calls_per_load(agent) * self._agent_load(agent)
    window_start = now - settings.llm_rate_window_seconds
    while agent.state.call_times and agent.state.call_times[0] < window_start:
        agent.state.call_times.popleft()
    return len(agent.state.call_times) < allowance
```

where `_calls_per_load` returns the role override (§4.4) if set, else
`settings.llm_calls_per_load_per_window`.

`_agent_within_budget` is **retained, not replaced** — it is the legacy cumulative cap
(§6) and is now inert by default, since `budget_cap` defaults to 0 and it already
short-circuits to `True` at `<= 0`. `_turn_eligible` requires **both** checks to pass:

```python
return (
    self._agent_within_budget(agent)      # legacy, inert unless --budget is passed
    and self._within_rate_limit(agent, now)
    and cooldown_ok
)
```

The cooldown branch is unchanged. The main loop's redundant second
`_agent_within_budget` call at `simulation.py:502` is left alone; it is unreachable-false
given `_select_agent` only returns eligible agents, and removing it is out of scope.

**Restart behaviour.** `_rebuild_state` step 4's all-time `COUNT(*)` into
`api_call_count` is **left exactly as is** — that counter still feeds the run summary
and `SimulationRun.total_api_calls`, and an existing integration test
(`test_full_run_live.py:1111`) correctly pins its survival across restart. A new
**step 4b** additionally selects `created_at` for rows **inside the window** and loads
them into `call_times`.

The two counters therefore mean different things on purpose: `api_call_count` is
lifetime accounting, `call_times` is the live throttle. Because only the latter gates
eligibility and its entries age out, §1.1's sticky bench becomes impossible by
construction rather than by correct operator behaviour.

### 4.3 Consumer 2 — the scheduler

Two changes in `_select_agent`, both using the same `_agent_load`:

- **Proactive tier:** `w = max(now - a.state.last_selected, 1.0) * self._agent_load(a)`.
  The existing Phase-5 skip penalty is unchanged.
- **Reactive tiebreak:** replace `min(owed, key=lambda a: a.state.last_selected)` with
  `max(owed, key=lambda a: (now - a.state.last_selected) * self._agent_load(a))`.
  Still "longest wait wins", now scaled by obligation count, which fixes §1.3.

`max_consecutive_reactive_turns` is unchanged; the fairness valve still applies.

### 4.4 Optional per-role override

`RoleSpec` gains `calls_per_load_per_window: int | None = None`, read from an optional
`role.toml` key of the same name. When set it overrides the global setting for that
role. Malformed or non-positive values are logged and ignored, matching `load_role`'s
existing never-raises contract.

This exists to pin a specific agent when needed. It is **not** the mechanism — the load
signal is. No role sets it initially, including `scout_hub`.

## 5. Configuration

Two new `Settings` fields in `src/config.py`:

```python
llm_rate_window_seconds: int = 600          # sliding window
llm_calls_per_load_per_window: int = 8      # allowance per unit of load
```

Both are `@lru_cache`d via `get_settings()`, so like the cohort flags they require a
container **recreate**, not a restart.

Calibration against measured rates from run `4f1e8395`:

| | observed | allowance | headroom | runaway trip time |
|---|---|---|---|---|
| Spoke (load 1) | ~0.27 calls/10min | 8/window | ~30× | ~25s |
| Hub (load 12) | ~2.6 calls/10min | 96/window | ~37× | ~5 min |

**Known trade-off:** a runaway *hub* takes ~5 minutes to trip, versus ~25 seconds for a
spoke. That is the direct price of the 12× allowance. Lower
`llm_calls_per_load_per_window` to tighten it; the spoke headroom is large enough to
absorb a reduction to 4 without risk.

## 6. Back-compat and failure modes

**`--budget` is deprecated, not removed.** Its default changes `50 → 0` (off). A nonzero
value is still honored as a hard cumulative cap, but logs a prominent warning naming it
as the legacy mechanism that benches hubs. Deleting the flag would break operator muscle
memory and the CLAUDE.md runbook; leaving it silently armed would let §1.1 recur the
next time someone types `--budget 40`. CLAUDE.md's "Running the Agent Simulation"
section is updated in the same change.

**Throttle visibility.** A `WARNING` is logged when an agent *transitions* into
throttled state — once per transition, not per turn. This is a deliberate, small
incursion into the observability scope that was otherwise cut: a silent throttle is
precisely what turned this into a 2.5-hour undetected outage.

**Failure modes considered:**

- *All agents throttled simultaneously.* `_select_agent` returns `None`. The loop used
  to **break** here, which under a sliding-window limiter is wrong: throttling and the
  per-agent `turn_delay_seconds` cooldown both lapse with time, so breaking converts a
  temporary bench into a permanent whole-run stop — strictly worse than the failure this
  design replaces, and reachable on any roster small enough for aggregate demand to meet
  aggregate allowance. The loop now consults `_terminal_stall_reason()`: it logs, applies
  the shared idle backoff and **continues**, and breaks only for the two conditions that
  cannot recover — an empty roster, or the legacy `--budget` cap armed (`> 0`) and blown
  by *every* agent. `max_runtime` and SIGTERM still end the run via the loop condition,
  and the backoff sleep (which returns early on stop) is what keeps this from spinning.
- *Clock skew / non-monotonic time.* `call_times` uses `time.time()`, consistent with
  `last_selected` and `last_phase5_action_time`. A backwards jump can only delay
  pruning, never bench an agent permanently.
- *Empty `active_threads` at startup.* Load floors at 1, so a cold agent is eligible.

## 7. Consequence to accept deliberately

With `active_thread_threshold=12`, the hub's allowance and scheduling weight cap at
**12×, not 56×**. That is intended: 12 is the number of conversations it can actually
hold.

After this change, spoke coverage is bounded by **thread capacity and turnover**, not by
budget. If 8-of-56 coverage remains too thin, the next lever is
`ACTIVE_THREAD_THRESHOLD` — a separate, visible knob — not the budget. Hiding hub
capacity inside a limiter is how the original problem became invisible.

## 8. Testing

New unit tests in `tests/unit/`, using the existing `_engine` helper
(`tests/unit/test_cohort_isolation.py:109`):

- `_agent_load`: idle → 1; N active threads → N; clamped at `active_thread_threshold`;
  non-active threads excluded.
- Rate limiter: under allowance → eligible; over → ineligible; **eligible again once the
  window slides**. This is the regression test for the permanent bench.
- Restart rebuild: `call_times` repopulated only from `llm_call_logs` rows inside the
  window; an agent whose calls all predate the window starts unthrottled.
- Scheduler, proactive: seeded statistical test that a load-12 hub receives ≈12× a
  spoke's selection share.
- Scheduler, reactive: a busy hub beats a long-idle spoke when load justifies it
  (direct regression for §1.3).
- Role override: `calls_per_load_per_window` honored when set; ignored and logged when
  malformed or non-positive.
- **Production regression**, promoted from the reproduction script written during
  diagnosis, reconstructing the exact §1.1 state (hub at 42 calls, 56 spokes at 8).
  Three assertions, because the legacy cap is retained (§6) and the three cases differ:
  1. Under the new default (`budget_cap=0`) with those 42 calls **outside** the window:
     the hub is selectable. This is the fix.
  2. Under the new default with 42 calls **inside** the window: the hub is throttled,
     then becomes selectable once the window slides. Throttling is still real — it just
     is not permanent.
  3. With `--budget 40` explicitly passed: the hub is still benched, and the
     deprecation warning is emitted. This pins the compat path honestly rather than
     pretending the legacy flag was made safe.
- Composition: `_turn_eligible` fails if *either* the legacy cap or the rate limit
  fails, and passes only when both do.

**Existing tests — one changes, one must NOT:**

- `tests/unit/test_cohort_isolation.py:1175 test_budget_still_filters` sets
  `budget_cap=1` explicitly, so it still exercises the retained legacy cap and
  **passes unchanged**. It is extended, not rewritten, with a sibling asserting the
  rate limiter filters independently of `budget_cap`.
- `tests/integration/test_full_run_live.py:1111` asserts `api_call_count` survives
  restart. Per §4.2 that counter is deliberately untouched, so this test **must keep
  passing with no edit**. If it fails, step 4b has been implemented by modifying step 4
  rather than adding to it — treat a failure here as the intended tripwire, not as a
  test to update.

Called out explicitly because "the tests changed" is where a fix of this shape can hide
a regression.

**Gate:** `./scripts/ci.sh` must stay green — single alembic head, `ruff` clean on
tests, `src/` findings at or under `SRC_LINT_MAX=260`, branch coverage at or above
`COV_MIN=60`.

## 9. Out of scope / follow-ups

- Hub has no `slack_bot_token`; with `SLACK_ENABLED=true` its posts take the `MOCK post`
  branch and never reach Slack (0 of 16 messages carried a `slack_ts`). Independent of
  this design.
- `_build_lab_directories` is not cohort-aware, inflating every system prompt with all
  other labs' publications (hub `new_post` averaged 23,873 input tokens). A cost issue,
  not a correctness one.
- Hub coverage of all 56 spokes (§7) — needs a thread-capacity decision, not a budget
  one.

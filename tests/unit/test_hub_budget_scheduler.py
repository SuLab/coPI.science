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

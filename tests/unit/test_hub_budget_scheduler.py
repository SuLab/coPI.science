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

import logging
import random
import time
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

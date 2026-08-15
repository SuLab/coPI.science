"""Load-proportional budget and scheduling for star topologies.

Implements the test plan in docs/specs/2026-08-06-hub-budget-scheduler-design.md
§8. Organised by design section so a failure names the rule it broke:

- TestAgentLoad            §4.1  the shared load signal
- TestRoleRateOverride     §4.4  optional per-role allowance
- TestCallLedger           §4.2  record_api_call maintains the lifetime counter
                                 only (Task 9: the ledger append moved to
                                 Agent.try_reserve, so record_api_call cannot
                                 double-book it)
- TestRateLimiter          §4.2  sliding-window eligibility, and that it self-heals
- TestRestartRebuild       §4.2  step 4b repopulates call_times from llm_call_logs
- TestScheduler            §4.3  load-proportional weight, reactive tiebreak
- TestStallIsTransient     F1    a throttled roster must NOT end the run
- TestPhase5CallAccounting F2    real-agent_id LLM calls reserve a window slot
                                 then go through record_api_call (Task 9)
- TestRateSettingGuards    F4    non-positive rate settings are clamped, loudly
- TestProductionRegression §8    the exact run-4f1e8395 state
"""

import inspect
import logging
import random
import time
import types

import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState


def _settings(**kw):
    base = dict(
        cohort_isolation_enabled=False,
        cohort_default_policy="open",
        turn_delay_seconds=0.0,
        active_thread_threshold=12,
        llm_rate_window_seconds=600,
        llm_calls_per_load_per_window=8,
        # Required since SimulationEngine.__init__ eagerly constructs the
        # (now unused-pending-Task-13) Phase-4 fan-out semaphore
        # (self._llm_fanout_sem) rather than per-call.
        phase4_max_concurrent_replies=4,
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


# Every awaitable the main loop calls once per tick before it selects an agent.
# All of them are I/O (Slack, DB, disk) and none of them affect selection, so a
# loop-level test stubs the lot and keeps only the scheduling behaviour.
_TICK_IO = (
    "_poll_slack_for_bot_messages",
    "_poll_inbound_from_db",
    "_sync_private_channels_from_db",
    "_sync_roster_from_db",
    "_flush_persisted",
    "_flush_llm_logs",
    # The reply lane dispatch (Task 11) also runs every tick, before
    # selection. It is I/O-shaped (Phase 3 + Phase 4 over every agent) and
    # does not affect post-lane selection, so it is stubbed out here too.
    "_dispatch_reply_lane",
)


def _drive_loop(eng, monkeypatch, *, stop_after=4, dispatch_stub=None):
    """Run the REAL ``_run_main_loop`` with every per-tick I/O call stubbed out.

    Returns ``(sleeps, turns)``, both filled in as the loop runs. The engine is
    stopped once ``stop_after`` events have been recorded, so a regression that
    reinstates the old spin-or-break behaviour fails an assertion instead of
    hanging the suite.

    ``dispatch_stub``, if given, replaces the generic no-op for
    ``_dispatch_reply_lane`` — used by ``TestReplyLaneIsNotPacedByTheIdleBackoff``
    (I2), which needs it to return a nonzero count and to own its own
    termination signal (reply-lane work alone does not touch ``sleeps``/
    ``turns``, so the generic ``_budget()`` below would never fire).
    """
    async def _noop(*a, **kw):
        return None

    for name in _TICK_IO:
        monkeypatch.setattr(eng, name, _noop)
    monkeypatch.setattr(eng, "_sync_profiles_from_disk", lambda *a, **kw: None)
    if dispatch_stub is not None:
        monkeypatch.setattr(eng, "_dispatch_reply_lane", dispatch_stub)

    sleeps: list[int] = []
    turns: list[str] = []

    def _budget():
        if len(sleeps) + len(turns) >= stop_after:
            eng._running = False

    async def _sleep(delay):
        sleeps.append(delay)
        _budget()

    async def _run_post_turn(agent):
        turns.append(agent.agent_id)
        _budget()
        return False

    monkeypatch.setattr(eng, "_sleep", _sleep)
    monkeypatch.setattr(eng, "_run_post_turn", _run_post_turn)
    eng._running = True
    return sleeps, turns


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
    def test_record_api_call_books_both_by_default(self):
        """Fix round 1 (Ruling R5): record_api_call's DEFAULT
        (already_reserved=False) still appends to call_times — this is what
        the six call sites that are never separately reserved (consults,
        retries, the memory update) rely on to be booked into the window at
        all. Only the two call sites that call try_reserve immediately
        beforehand pass already_reserved=True to skip this append."""
        a = Agent(agent_id="hub", bot_name="HubBot", pi_name="PI hub")
        a.record_api_call(now=100.0)
        a.record_api_call(now=101.0)
        assert a.api_call_count == 2
        assert list(a.state.call_times) == [100.0, 101.0]

    def test_already_reserved_skips_the_ledger_append(self):
        """The two call sites that DO call try_reserve immediately
        beforehand pass already_reserved=True, or the one real call they make
        would be double-booked (reserved once by try_reserve, booked again
        here) and the effective allowance halved."""
        a = Agent(agent_id="hub", bot_name="HubBot", pi_name="PI hub")
        a.record_api_call(now=100.0, already_reserved=True)
        assert a.api_call_count == 1
        assert list(a.state.call_times) == []

    def test_call_times_starts_empty(self):
        a = Agent(agent_id="hub", bot_name="HubBot", pi_name="PI hub")
        assert len(a.state.call_times) == 0
        assert a.api_call_count == 0


class TestRateLimiter:
    def test_under_allowance_is_eligible(self, monkeypatch):
        _patch(monkeypatch, llm_calls_per_load_per_window=8)
        eng = _engine(["spoke"])
        a = eng.agents["spoke"]
        # Seed the ledger directly — Task 9 moved the append to
        # Agent.try_reserve, so record_api_call no longer populates it.
        for i in range(7):
            a.state.call_times.append(1000.0 + i)
        assert eng._within_rate_limit(a, 1010.0) is True

    def test_at_allowance_is_throttled(self, monkeypatch):
        _patch(monkeypatch, llm_calls_per_load_per_window=8)
        eng = _engine(["spoke"])
        a = eng.agents["spoke"]
        for i in range(8):
            a.state.call_times.append(1000.0 + i)
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
            a.state.call_times.append(1000.0 + i)
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
            hub.state.call_times.append(1000.0 + i)
        # load 12 -> allowance 96, so 50 calls is fine for a hub...
        assert eng._within_rate_limit(hub, 1060.0) is True
        # ...but the identical ledger throttles a load-1 spoke.
        spoke = Agent(agent_id="spoke", bot_name="SpokeBot", pi_name="PI spoke")
        for i in range(50):
            spoke.state.call_times.append(1000.0 + i)
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
            a.state.call_times.append(1000.0 + i)
        assert eng._calls_per_load(a) == 2
        assert eng._within_rate_limit(a, 1010.0) is False

    def test_turn_eligible_requires_both_checks(self, monkeypatch):
        """Legacy cumulative cap and the rate limiter compose with AND."""
        _patch(monkeypatch, llm_calls_per_load_per_window=8)
        eng = _engine(["spoke"], budget_cap=5)
        a = eng.agents["spoke"]
        # Rate limit fine (1 call), legacy cap blown (api_call_count 6 >= 5).
        a.api_call_count = 6
        a.state.call_times.append(1000.0)
        assert eng._turn_eligible(a, 1010.0) is False

    def test_turn_eligible_passes_when_both_pass(self, monkeypatch):
        _patch(monkeypatch, llm_calls_per_load_per_window=8)
        eng = _engine(["spoke"], budget_cap=0)
        a = eng.agents["spoke"]
        a.record_api_call(now=1000.0)
        assert eng._turn_eligible(a, 1010.0) is True

    def test_throttle_flag_stays_fresh_while_the_legacy_cap_binds(self, monkeypatch):
        """F5. The cap-first ordering in _turn_eligible is deliberate, but it used
        to freeze `state.throttled`: with --budget armed the flag was whatever it
        was when the cap first bit, so the agent's next real throttle transition
        logged nothing. The window check now runs for its side effect while the
        cap still decides eligibility.
        """
        _patch(monkeypatch, llm_calls_per_load_per_window=2)
        eng = _engine(["spoke"], budget_cap=5)
        a = eng.agents["spoke"]
        a.state.call_times.append(1000.0)
        a.state.call_times.append(1001.0)  # at the window allowance
        a.api_call_count = 6           # ...and over the legacy cap

        assert eng._turn_eligible(a, 1010.0) is False
        assert a.state.throttled is True
        # 700s later the window has slid: the flag must clear even though the
        # cap still benches the agent. Eligibility is unchanged either way.
        assert eng._turn_eligible(a, 1710.0) is False
        assert a.state.throttled is False

    def test_legacy_cap_still_decides_eligibility(self, monkeypatch):
        """The F5 side-effect call must not have reordered the gates: an agent
        well inside its rate window is STILL ineligible once the cap is blown."""
        _patch(monkeypatch, llm_calls_per_load_per_window=8)
        eng = _engine(["spoke"], budget_cap=5)
        a = eng.agents["spoke"]
        a.api_call_count = 5
        assert a.state.throttled is False       # rate limit nowhere near
        assert eng._turn_eligible(a, 1010.0) is False

    def test_throttle_transition_warns_once(self, monkeypatch, caplog):
        _patch(monkeypatch, llm_calls_per_load_per_window=2)
        eng = _engine(["spoke"])
        a = eng.agents["spoke"]
        a.state.call_times.append(1000.0)
        a.state.call_times.append(1001.0)
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

    def test_load_weighting_no_longer_penalises_the_busy_agent(
        self, monkeypatch
    ):
        """The hub is selected often, so its last_selected is always recent. Under
        min(last_selected) it lost every tiebreak to a long-idle spoke — it was
        penalised precisely for being busy. Weighted by load, it gets a
        load-proportional share instead.

        Retargeted for Task 11: with the reactive tier gone, `_select_agent`
        has one (proactive, weighted-random) path, not a deterministic
        max()-based tiebreak — so this is checked as a share over many draws,
        like `test_proactive_weight_scales_with_load` above, rather than a
        single call.
        """
        _patch(monkeypatch, active_thread_threshold=12)
        random.seed(20260814)
        eng = _engine(["hub", "spoke"])
        now = time.time()
        _add_threads(eng.agents["hub"], 12, pending=True)
        _add_threads(eng.agents["spoke"], 1, pending=True, prefix="s")
        eng.agents["hub"].state.last_selected = now - 10.0    # 10 * 12 = 120
        eng.agents["spoke"].state.last_selected = now - 60.0  # 60 *  1 =  60

        picks = [eng._select_agent().agent_id for _ in range(1000)]
        share = picks.count("hub") / 1000
        assert share > 0.55, f"hub share {share:.3f} too low — busy agent still penalised"

    def test_load_weighting_still_favours_a_genuinely_starved_spoke(
        self, monkeypatch
    ):
        """The load weighting must not become a blank cheque: a spoke that has
        waited long enough still gets the large majority of picks over the hub.

        Retargeted for Task 11 — see the docstring above."""
        _patch(monkeypatch, active_thread_threshold=12)
        random.seed(20260814)
        eng = _engine(["hub", "spoke"])
        now = time.time()
        _add_threads(eng.agents["hub"], 2, pending=True)
        _add_threads(eng.agents["spoke"], 1, pending=True, prefix="s")
        eng.agents["hub"].state.last_selected = now - 10.0     # 10 * 2 =  20
        eng.agents["spoke"].state.last_selected = now - 600.0  # 600 * 1 = 600

        picks = [eng._select_agent().agent_id for _ in range(1000)]
        share = picks.count("spoke") / 1000
        assert share > 0.85, f"spoke share {share:.3f} too low — starved agent not favoured"

    def test_throttled_hub_is_not_selected(self, monkeypatch):
        _patch(monkeypatch, llm_calls_per_load_per_window=1,
               active_thread_threshold=12)
        eng = _engine(["hub", "spoke"])
        now = time.time()
        hub = eng.agents["hub"]
        _add_threads(hub, 1)
        hub.state.call_times.append(now)
        for _ in range(50):
            assert eng._select_agent().agent_id == "spoke"


class TestReplyLaneIsNotPacedByTheIdleBackoff:
    """I2 (task review fix round 1), corrected by fix round 2 (task review,
    NEW Critical). Before fix round 1, ``did_work`` came only from
    ``_run_post_turn``, which is False whenever Phase 5 doesn't fire — the
    common case once a lab hits ``lab_daily_post_cap``, and ALWAYS for the
    hub (no ``post_types`` at all) — so ``consecutive_idle`` climbed to the
    30s ceiling and the loop slept 30s before every single reply sweep,
    directly contradicting design §2.1: "Nothing in the reply lane delays a
    reply that is ready."

    Fix round 1 folded ``_dispatch_reply_lane``'s "how many ran" return
    directly into the backoff decision — but that count is pairs ATTEMPTED,
    not pairs that actually spent an LLM call. The reservation limiter defers
    a pair with zero calls and leaves ``has_pending_reply`` True, so the same
    pair recurs every tick — composed with the fix-round-1 logic, that spun
    the main loop at native tick speed forever (measured ~2,800
    iterations/s), never sleeping and never yielding to the periodic
    flushes. Fix round 2 replaces the attempt count with a SPEND comparison
    (total ``api_call_count`` across the roster, before vs. after the
    dispatch call — mirroring what ``_run_post_turn`` already does with
    ``api_calls_before``), so the tests below simulate spend explicitly by
    bumping an agent's ``api_call_count`` inside the dispatch stub, and a new
    test pins the zero-spend case directly.
    """

    async def test_no_idle_sleep_when_reply_lane_spent_but_no_agent_was_eligible(
        self, monkeypatch
    ):
        _patch(monkeypatch)
        eng = _engine(["hub"])
        monkeypatch.setattr(eng, "_select_agent", lambda: None)

        sleeps: list[int] = []
        ticks = {"n": 0}

        async def _sleep(delay):
            sleeps.append(delay)

        async def _dispatch():
            ticks["n"] += 1
            eng.agents["hub"].api_call_count += 1  # a real LLM call happened
            if ticks["n"] >= 3:
                eng._running = False
            return 2  # attempted count, kept only as a log/metric value

        _drive_loop(eng, monkeypatch, dispatch_stub=_dispatch)
        monkeypatch.setattr(eng, "_sleep", _sleep)

        await eng._run_main_loop()

        assert sleeps == [], (
            "reply-lane SPEND must not be paced behind the idle backoff even "
            "when no post-lane agent is eligible right now"
        )
        assert ticks["n"] == 3

    async def test_no_idle_sleep_when_reply_lane_spent_and_the_post_turn_did_not(
        self, monkeypatch
    ):
        _patch(monkeypatch)
        eng = _engine(["hub"])

        sleeps: list[int] = []
        ticks = {"n": 0}

        async def _sleep(delay):
            sleeps.append(delay)

        async def _dispatch():
            ticks["n"] += 1
            eng.agents["hub"].api_call_count += 1  # a real LLM call happened
            if ticks["n"] >= 3:
                eng._running = False
            return 1

        async def _run_post_turn(agent):
            return False  # no work of its own this tick

        _drive_loop(eng, monkeypatch, dispatch_stub=_dispatch)
        monkeypatch.setattr(eng, "_sleep", _sleep)
        monkeypatch.setattr(eng, "_run_post_turn", _run_post_turn)

        await eng._run_main_loop()

        assert sleeps == [], (
            "a tick where the reply lane SPENT must not sleep the idle "
            "backoff even though the post turn (stubbed to return False) "
            "did no work of its own"
        )
        assert ticks["n"] == 3

    async def test_idle_sleep_still_applies_when_the_reply_lane_only_attempted_but_spent_nothing(
        self, monkeypatch
    ):
        """THE regression test for the NEW Critical: a rate-limited pending
        pair is "serviced" in the sense that ``_dispatch_reply_lane`` attempts
        it and counts it in its return value (mirroring
        ``_reply_to_thread``'s real behaviour — it logs "rate-limited;
        deferring this reply" and returns immediately, leaving
        ``has_pending_reply`` True so the identical pair recurs next tick),
        but makes no LLM call at all. That attempt count alone must NOT
        suppress the idle backoff, or the main loop spins at native tick
        speed forever."""
        _patch(monkeypatch)
        eng = _engine(["hub"])
        monkeypatch.setattr(eng, "_select_agent", lambda: None)

        sleeps: list[int] = []

        async def _sleep(delay):
            sleeps.append(delay)
            if len(sleeps) >= 2:
                eng._running = False

        async def _dispatch():
            # Attempted (rate-limited), but no LLM call — no spend at all.
            return 1

        _drive_loop(eng, monkeypatch, dispatch_stub=_dispatch)
        monkeypatch.setattr(eng, "_sleep", _sleep)

        await eng._run_main_loop()

        assert sleeps == [5, 5], (
            "a tick where the reply lane only ATTEMPTED (rate-limited, zero "
            "spend) must still apply the idle backoff — otherwise a "
            "rate-limited pending pair spins the main loop forever"
        )


class TestDispatchFailuresAreNoLongerSwallowedWholesale:
    """NEW Important (task review fix round 2). The blanket try/except that
    used to wrap the entire `_dispatch_reply_lane()` call in `_run_main_loop`
    is gone. `_dispatch_reply_lane` itself now isolates one pair's servicing
    failure (fix round 1, C2) and one agent's Phase-3-activation failure (fix
    round 2) from their siblings — so what is left unguarded at the call
    site is a genuine failure in pair *selection*, which is a real bug that
    must surface (crash the run) rather than repeat one swallowed ERROR per
    tick forever while no interview progresses and the post lane keeps
    posting as if nothing were wrong.
    """

    async def test_a_pair_selection_failure_propagates_out_of_the_main_loop(
        self, monkeypatch
    ):
        _patch(monkeypatch)
        eng = _engine(["hub"])
        for name in _TICK_IO:
            if name != "_dispatch_reply_lane":
                monkeypatch.setattr(eng, name, _noop_coro)
        monkeypatch.setattr(eng, "_sync_profiles_from_disk", lambda *a, **kw: None)

        async def _boom():
            raise RuntimeError("pair selection exploded")

        monkeypatch.setattr(eng, "_dispatch_reply_lane", _boom)
        eng._running = True

        with pytest.raises(RuntimeError, match="pair selection exploded"):
            await eng._run_main_loop()


async def _noop_coro(*_a, **_kw):
    return None


class TestBudgetDeprecation:
    def test_default_budget_is_off(self):
        """The default must be 0 (off). A nonzero default is what silently armed
        the legacy cap on every run."""
        from src.agent.main import main

        default = inspect.signature(main).parameters["budget"].default
        assert default.default == 0

    def test_help_text_marks_the_flag_deprecated(self):
        from src.agent.main import main

        help_text = inspect.signature(main).parameters["budget"].default.help
        assert "DEPRECATED" in help_text

    def test_engine_constructor_default_matches_the_cli_default(self):
        """F3. The constructor default was 50 while the CLI default was 0, so
        every caller that omitted the kwarg — tests, backfill scripts — silently
        armed the deprecated permanent cap."""
        default = inspect.signature(SimulationEngine.__init__).parameters[
            "budget_cap"
        ].default
        assert default == 0

    def test_engine_without_a_budget_kwarg_caps_nobody(self, monkeypatch):
        _patch(monkeypatch)
        agent = Agent(agent_id="hub", bot_name="HubBot", pi_name="PI hub")
        agent.api_call_count = 10_000
        eng = SimulationEngine(agents=[agent], slack_clients={})
        assert eng._agent_within_budget(agent) is True
        assert eng._turn_eligible(agent, time.time()) is True


class TestStallIsTransient:
    """F1. `_select_agent()` returning None used to break the main loop.

    Rate limiting and the per-agent `turn_delay_seconds` cooldown both lapse with
    time, so breaking on them converts "one agent is benched for a while" into
    "the run exits and stays exited" — strictly worse than the bug the branch
    fixes, and reachable on any roster whose aggregate demand meets its aggregate
    allowance (7 token-holding agents at 8 calls/600s does it in minutes).
    """

    def test_predicate_is_not_terminal_when_the_cap_is_off(self, monkeypatch):
        _patch(monkeypatch)
        eng = _engine(["a", "b"], budget_cap=0)
        for a in eng.agents.values():
            a.api_call_count = 10_000
        assert eng._terminal_stall_reason() is None

    def test_predicate_is_not_terminal_while_one_agent_is_under_the_cap(
        self, monkeypatch
    ):
        _patch(monkeypatch)
        eng = _engine(["a", "b"], budget_cap=5)
        eng.agents["a"].api_call_count = 99
        eng.agents["b"].api_call_count = 1
        assert eng._terminal_stall_reason() is None

    def test_predicate_is_terminal_when_every_agent_is_over_the_cap(
        self, monkeypatch
    ):
        _patch(monkeypatch)
        eng = _engine(["a", "b"], budget_cap=5)
        for a in eng.agents.values():
            a.api_call_count = 5
        assert "legacy --budget cap (5)" in eng._terminal_stall_reason()

    def test_predicate_is_terminal_on_an_empty_roster(self, monkeypatch):
        _patch(monkeypatch)
        eng = _engine([], budget_cap=0)
        assert eng._terminal_stall_reason() == "the roster is empty"

    async def test_fully_throttled_roster_backs_off_instead_of_stopping(
        self, monkeypatch, caplog
    ):
        """THE regression test for F1: every agent throttled, legacy cap off.

        The loop must keep ticking with a growing backoff, never break, and never
        spin (each empty tick costs a `_sleep`).
        """
        _patch(monkeypatch, llm_calls_per_load_per_window=1)
        eng = _engine(["a", "b"], budget_cap=0)
        now = time.time()
        for a in eng.agents.values():
            a.state.call_times.append(now)  # allowance is 1 * load 1
        assert eng._select_agent() is None

        sleeps, turns = _drive_loop(eng, monkeypatch, stop_after=4)
        with caplog.at_level(logging.INFO):
            await eng._run_main_loop()

        assert turns == []
        assert sleeps == [5, 5, 5, 15], "idle backoff must apply and grow"
        assert "Stopping" not in caplog.text
        assert "retrying" in caplog.text

    async def test_mixed_stall_with_the_cap_armed_is_still_transient(
        self, monkeypatch, caplog
    ):
        """Cap armed, one agent over it, the other merely throttled. Nothing is
        selectable right now, but the second agent recovers as the window slides,
        so the run must not end."""
        _patch(monkeypatch, llm_calls_per_load_per_window=1)
        eng = _engine(["over", "throttled"], budget_cap=5)
        eng.agents["over"].api_call_count = 99
        eng.agents["throttled"].state.call_times.append(time.time())
        assert eng._select_agent() is None

        sleeps, turns = _drive_loop(eng, monkeypatch, stop_after=2)
        with caplog.at_level(logging.INFO):
            await eng._run_main_loop()

        assert turns == []
        assert sleeps == [5, 5]
        assert "Stopping" not in caplog.text

    async def test_every_agent_over_the_legacy_cap_stops_the_loop(
        self, monkeypatch, caplog
    ):
        """The one genuinely permanent case: `api_call_count` only grows within a
        process and `_rebuild_state_from_db` restores it, so nothing recovers."""
        _patch(monkeypatch)
        eng = _engine(["a", "b"], budget_cap=5)
        for a in eng.agents.values():
            a.api_call_count = 5

        sleeps, turns = _drive_loop(eng, monkeypatch, stop_after=4)
        with caplog.at_level(logging.INFO):
            await eng._run_main_loop()

        assert (sleeps, turns) == ([], []), "terminal stall must break at once"
        assert "over the legacy --budget cap (5). Stopping." in caplog.text

    async def test_a_recovered_agent_gets_its_turn(self, monkeypatch):
        """The backoff is not a dead end: once the ledger clears, the very next
        tick selects the agent it was waiting for."""
        _patch(monkeypatch, llm_calls_per_load_per_window=1)
        eng = _engine(["a"], budget_cap=0)
        eng.agents["a"].state.call_times.append(time.time())

        sleeps, turns = _drive_loop(eng, monkeypatch, stop_after=3)
        # The first stall clears the ledger the way an expiring window would.
        real_sleep = eng._sleep

        async def _sleep(delay):
            eng.agents["a"].state.call_times.clear()
            await real_sleep(delay)

        monkeypatch.setattr(eng, "_sleep", _sleep)
        await eng._run_main_loop()

        assert turns and turns[0] == "a"

    async def test_stop_signal_still_ends_a_stalled_loop(self, monkeypatch):
        """max_runtime / SIGTERM must still end the run: `_sleep` returns early
        once `_stop_event` is set, and the loop condition then fails."""
        _patch(monkeypatch, llm_calls_per_load_per_window=1)
        eng = _engine(["a"], budget_cap=0)
        eng.agents["a"].state.call_times.append(time.time())

        sleeps, turns = _drive_loop(eng, monkeypatch, stop_after=50)
        real_sleep = eng._sleep

        async def _sleep(delay):
            eng.request_stop()
            await real_sleep(delay)

        monkeypatch.setattr(eng, "_sleep", _sleep)
        await eng._run_main_loop()

        assert turns == []
        assert len(sleeps) == 1


class TestPhase5CallAccounting:
    """F2, retargeted after `pi_handler.py`'s removal (removal cycle, Task 5).

    `pi_handler` used to be the call site that demonstrated this invariant
    end-to-end: an LLM call logged under a REAL agent_id must book against
    both `api_call_count` and the live `call_times` ledger, or a restart
    silently throttles the agent for calls it never appeared to make (see
    state.py's comment on `call_times`). The whole PI-interaction surface
    (`pi_handler.py`, `PIHandler`) is gone — this removal cycle retires all
    human-PI-to-bot interaction — so this class re-anchors the same invariant
    to Phase 5's `_phase5_new_post`, which follows the identical pattern:
    `agent.try_reserve(...)` claims the window slot, then `agent.record_api_call()`
    immediately before a `generate_agent_response` call logged under the
    agent's own real `agent_id` (`log_meta={"agent_id": agent.agent_id, ...}`)
    (Task 9: the reservation now caps real spend directly, not just visibility
    to the limiter after the fact).
    """

    _SKIP = '```json\n{"action": "skip"}\n```'

    def _settings(self, **over):
        base = dict(
            cohort_isolation_enabled=False,
            cohort_default_policy="open",
            active_thread_threshold=12,
            llm_rate_window_seconds=600,
            llm_calls_per_load_per_window=8,
            lab_daily_post_cap=100,
            phase5_skip_probability=0.0,
            llm_agent_model_opus="test-model",
        )
        base.update(over)
        return types.SimpleNamespace(**base)

    def _engine(self, monkeypatch, response=None, **settings_over):
        agent = Agent(agent_id="su", bot_name="SuBot", pi_name="Andrew Su")
        agent.allowed_sender_ids = None
        eng = SimulationEngine(agents=[agent], slack_clients={})
        monkeypatch.setattr(
            "src.agent.simulation.get_settings", lambda: self._settings(**settings_over)
        )
        # Stub the prompt builder — this class exercises the accounting
        # around the LLM call, not prompt content (matches TestPIHandlerAccounting's
        # original scope, which also never inspected prompt text).
        monkeypatch.setattr(agent, "build_phase5_prompt", lambda **kw: ("sys", []))

        async def _fake_generate(**kwargs):
            return response if response is not None else self._SKIP
        monkeypatch.setattr("src.agent.simulation.generate_agent_response", _fake_generate)
        return eng, agent

    async def test_new_post_call_is_recorded_against_the_agent(self, monkeypatch):
        eng, agent = self._engine(monkeypatch)
        await eng._phase5_new_post(agent)
        assert agent.api_call_count == 1
        assert len(agent.state.call_times) == 1

    async def test_a_call_burst_shows_up_in_the_live_rate_limiter(self, monkeypatch):
        """The end-to-end point of F2, sharpened by Task 9: the reservation now
        caps real spend directly, so only the first 8 of 10 attempts (the
        allowance) ever reach the LLM — the other 2 are refused by
        `try_reserve` before `record_api_call` or the call itself runs. Before
        Task 9 this was a post-hoc entry gate: all 10 calls would have gone out
        and only the NEXT attempt would have been throttled."""
        eng, agent = self._engine(monkeypatch, llm_calls_per_load_per_window=8)
        for _ in range(10):
            await eng._phase5_new_post(agent)
        assert agent.api_call_count == 8
        assert eng._within_rate_limit(agent, time.time()) is False


class TestUnreservedCallSitesStillBookTheLedger:
    """Fix round 1 (Ruling R5). Round 1's fix for the double-book bug (making
    record_api_call never append to call_times) silently regressed six OTHER
    call sites off the sliding window entirely: specialist consults, both
    truncation-retry hooks, the memory update, and its own retry hook. None
    of those six calls try_reserve, so record_api_call's default
    (already_reserved=False) is the only thing that puts them in the window
    at all. This class pins the memory-update call specifically (the
    consult/retry pins live in test_consult_accounting.py, next to the
    fixture that already exercises _reply_to_thread's tool executor)."""

    async def test_a_memory_update_appends_to_the_sliding_window_ledger(
        self, monkeypatch,
    ):
        agent = Agent(agent_id="su", bot_name="SuBot", pi_name="Andrew Su")
        eng = SimulationEngine(agents=[agent], slack_clients={})

        async def _fake_generate(**kwargs):
            return "Updated memory."

        monkeypatch.setattr(
            "src.agent.simulation.generate_agent_response", _fake_generate
        )
        monkeypatch.setattr(
            agent, "build_thread_reply_system_prompt", lambda **kw: "sys"
        )
        # Avoid a real write to profiles/memory/su/public.md — this class
        # exercises call accounting, not the memory-file side effect.
        monkeypatch.setattr(agent, "update_working_memory_file", lambda *a, **kw: None)

        await eng._update_agent_memory(agent, "thread closed")

        assert agent.api_call_count == 1
        assert len(agent.state.call_times) == 1


class TestRateSettingGuards:
    """F4. `roles.py` rejects a non-positive per-role override; the global
    settings had no such guard. `llm_calls_per_load_per_window=0` makes
    `len(times) < 0` false for everyone, so — now that a stall no longer ends the
    run — a typo buys a silently, permanently idle simulation.
    """

    def _settings_obj(self, **kw):
        from src.config import Settings

        return Settings(_env_file=None, environment="development", **kw)

    def test_zero_calls_per_load_is_clamped_to_the_default(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.config"):
            s = self._settings_obj(llm_calls_per_load_per_window=0)
        assert s.llm_calls_per_load_per_window == 8
        assert "LLM_CALLS_PER_LOAD_PER_WINDOW" in caplog.text

    def test_negative_window_is_clamped_to_the_default(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.config"):
            s = self._settings_obj(llm_rate_window_seconds=-1)
        assert s.llm_rate_window_seconds == 600
        assert "LLM_RATE_WINDOW_SECONDS" in caplog.text

    def test_valid_overrides_are_left_alone(self, caplog):
        with caplog.at_level(logging.WARNING, logger="src.config"):
            s = self._settings_obj(
                llm_calls_per_load_per_window=2, llm_rate_window_seconds=30,
            )
        assert (s.llm_calls_per_load_per_window, s.llm_rate_window_seconds) == (2, 30)
        assert "must be a positive int" not in caplog.text

    def test_a_clamped_setting_leaves_agents_selectable(self, monkeypatch):
        """The behavioural consequence: with the guard, a 0 in the environment
        degrades to the default allowance instead of benching the whole roster."""
        s = self._settings_obj(llm_calls_per_load_per_window=0)
        monkeypatch.setattr("src.agent.simulation.get_settings", lambda: s)
        eng = _engine(["a"])
        assert eng._turn_eligible(eng.agents["a"], time.time()) is True


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
            hub.state.call_times.append(base + i)
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

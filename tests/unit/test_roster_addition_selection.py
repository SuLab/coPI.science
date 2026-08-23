"""A mid-run roster addition must not monopolise the scheduler.

`start()` anchored `last_selected = time.time()` in a one-shot loop over
`self.agents`. `_sync_roster_from_db`'s ADD path constructs `Agent(...)`, whose
`AgentState.last_selected` defaulted to `0.0`, and never anchored it — so a
mid-run addition carried a staleness weight of `now - 0.0` ~ 1.79e9 against
~187 for an incumbent. Measured on a harness: 3 new agents out of 13 took
**100%** of 2,000 draws.

In production the main loop re-anchors on every selection, so the real effect is
N new agents taking N CONSECUTIVE turns — with the documented bulk case (48 bots
provisioned mid-run) that is 48 turns during which no incumbent runs at all.

The fix anchors in `AgentState` itself, so no creation path can miss it.
"""
import time
import types

import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import AgentState


def _settings(**kw):
    base = dict(
        cohort_isolation_enabled=False,
        cohort_default_policy="open",
        turn_delay_seconds=0.0,
        active_thread_threshold=12,
        llm_rate_window_seconds=600,
        llm_calls_per_load_per_window=8,
        reply_lane_max_in_flight=1,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_a_newly_constructed_agent_is_not_infinitely_stale():
    """The anchor lives where construction cannot skip it."""
    before = time.time()
    agent = Agent("newcomer", "NewcomerBot", "PI Newcomer")
    after = time.time()

    assert before <= agent.state.last_selected <= after, (
        "a mid-run Agent(...) starts at the epoch, which `_select_agent` reads "
        "as ~57 years of staleness"
    )
    # And directly, for any other construction path.
    assert AgentState().last_selected > 0.0


def test_a_mid_run_roster_addition_does_not_monopolise_selection(monkeypatch):
    monkeypatch.setattr("src.agent.simulation.get_settings", lambda: _settings())

    incumbents = [
        Agent(f"pi{i}", f"Pi{i}Bot", f"PI {i}") for i in range(10)
    ]
    now = time.time()
    # Incumbents have all run recently — the main loop re-anchors on every
    # selection, so on a 13-agent roster nobody is idle for long.
    for a in incumbents:
        a.state.last_selected = now - 60.0

    eng = SimulationEngine(agents=incumbents, slack_clients={})

    # The mid-run addition, exactly as `_sync_roster_from_db`'s add path builds
    # it: a bare `Agent(...)` dropped into `self.agents`.
    newcomers = [Agent(f"new{i}", f"New{i}Bot", f"New {i}") for i in range(3)]
    for a in newcomers:
        eng.agents[a.agent_id] = a
    new_ids = {a.agent_id for a in newcomers}

    draws = [eng._select_agent() for _ in range(2000)]
    assert all(d is not None for d in draws)
    new_share = sum(1 for d in draws if d.agent_id in new_ids) / len(draws)

    assert new_share < 0.10, (
        f"the three mid-run additions took {new_share:.0%} of the draws; "
        "un-anchored, they take 100% and the incumbents starve"
    )
    assert len({d.agent_id for d in draws}) >= 10, (
        "at least the incumbents should each get a turn"
    )


@pytest.mark.asyncio
async def test_start_no_longer_needs_its_own_anchoring_loop():
    """The one-shot loop in `start()` is redundant once the default anchors.

    Kept as a structural guard rather than a behavioural one: re-introducing the
    loop would not be wrong, but leaving it there implies the anchor is a
    startup concern, which is precisely the belief that let the roster-add path
    ship without one.
    """
    import inspect

    src = inspect.getsource(SimulationEngine.start)
    assert "last_selected" not in src, (
        "start() is anchoring last_selected again — the anchor belongs in "
        "AgentState so no creation path can miss it"
    )

"""Pins the per-role daily post cap: `pi_lab` agents get exactly one pitch per
day (`lab_daily_post_cap`). `scout_hub` is hard-gated out of `_phase5_new_post`
entirely (decision 9, reply-only-hub reconciliation) and never reaches the cap
check at all — see `test_scout_hub_never_reaches_llm_regardless_of_daily_cap`
below, which pins that the hard gate wins over what daily_post_cap headroom
would otherwise have allowed.

Formerly `test_phase2_guard.py`: that file's other test,
`test_run_turn_has_no_phase2_call_or_gate`, pinned that `_run_turn` never
called `_phase2_scan_filter` — a source-inspection pin against a function that
no longer exists at all (removal-cycle task 7 deleted `_phase2_scan_filter`/
`_phase2_prune`/the phase-2 prompt builders and the `interesting_posts`
cascade they fed). That pin is now permanently and trivially true, so it was
deleted rather than kept as dead weight; this file's remaining content — the
daily-cap tests below — was never about phase-2's existence, so it survives
under a name that matches what it actually pins.
"""
import types

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient


def _lab(agent_id="gill", bot_name="GillBot", pi_name="Gill"):
    return Agent(agent_id, bot_name, pi_name, role="pi_lab")


def _hub():
    return Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")


def _settings(**over):
    base = dict(
        daily_post_cap=5,
        lab_daily_post_cap=1,
        active_thread_threshold=12,
        phase5_skip_probability=0.0,
        llm_agent_model_opus="test-model",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


async def _drive(monkeypatch, agent, *, today_posts):
    """Wire one agent through `_phase5_new_post` with a stubbed LLM and report
    whether the LLM was actually reached (i.e. the daily cap did not short-
    circuit the turn first)."""
    agent.allowed_sender_ids = None
    other = _hub() if agent.role == "pi_lab" else _lab()
    client = FakeSlackClient(agent_id=agent.agent_id)
    eng = SimulationEngine(
        agents=[agent, other],
        slack_clients={
            agent.agent_id: client,
            other.agent_id: FakeSlackClient(agent_id=other.agent_id),
        },
    )
    monkeypatch.setattr("src.agent.simulation.get_settings", lambda: _settings())
    monkeypatch.setattr(eng, "_count_today_posts", lambda a: today_posts)
    monkeypatch.setattr(agent, "build_phase5_prompt", lambda **kw: ("sys", []))

    called = {"llm": False}

    async def _fake_generate(**kwargs):
        called["llm"] = True
        return (
            '```json\n'
            '{"action": "skip"}\n'
            '```\n\n'
            '<slack_message>skip</slack_message>'
        )

    monkeypatch.setattr("src.agent.simulation.generate_agent_response", _fake_generate)
    await eng._phase5_new_post(agent)
    return called["llm"]


async def test_pi_lab_at_cap_never_reaches_llm(monkeypatch):
    """lab_daily_post_cap=1: a pi_lab agent that already posted once today is
    at cap and must not call the LLM at all."""
    reached = await _drive(monkeypatch, _lab(), today_posts=1)
    assert reached is False


async def test_scout_hub_never_reaches_llm_regardless_of_daily_cap(monkeypatch):
    """scout_hub is not subject to lab_daily_post_cap, and daily_post_cap=5
    still has headroom at 1 post — but neither matters anymore: the
    reply-only-hub reconciliation (decision 9) hard-gates `scout_hub` out of
    `_phase5_new_post` before ANY work, including the cap check this test
    used to pin as the reason the LLM WAS reached. This is the inversion the
    hard gate implies, not a cap regression — see
    test_phase5_terminal_posts.py for the dedicated hard-gate pins."""
    reached = await _drive(monkeypatch, _hub(), today_posts=1)
    assert reached is False

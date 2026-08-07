"""A terminal artifact must not be blocked by backpressure meant for new work.

Measured in production, run 2485863a: the scouting hub took 30 turns, made 33
phase-4 interview replies, and reached phase 5 exactly ZERO times, while every
PI bot reached it routinely (mueller 6, shastri 5, dang 5...). Its
`llm_call_logs` rows for phase='new_post' numbered 0 for the whole run.

Root cause, confirmed by elimination rather than inference:
  phase5_skip_probability = 0.0  -> the random-skip return can never fire
  daily_post_cap = 5, hub posted 0 -> the cap return can never fire
  active_thread_threshold = 12 (env), hub held 65 threads
leaving only `simulation.py`'s "blocked, no funding/PI posts available" return.

`blocked_for_regular` is backpressure against STARTING work. A :mag: Opportunity
Assessment reports work already finished — it is the one action that DRAINS the
queue. Blocking it inverts the intent: the more interviews the hub completes,
the more assessments it owes and the less able it is to file any of them.

Three gates stopped it, and all three must yield or the hub is still stuck:
  1. available_for(funding_only=True) narrowed the menu to funding types
  2. the early return bailed before a prompt was ever built
  3. the blocked-action gate rejected the post_type
"""
import types

import pytest

from src.agent.agent import Agent
from src.agent.post_types import available_for
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from tests.fakes import FakeSlackClient


def _hub():
    return Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")


def _settings(**over):
    base = dict(
        daily_post_cap=5,
        active_thread_threshold=12,
        unreviewed_proposal_block_count=2,
        phase5_skip_probability=0.0,
        llm_agent_model_opus="test-model",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


# --- gate 1: the menu ---------------------------------------------------------

def test_terminal_types_are_distinct_from_funding_types():
    from src.agent.post_types import FUNDING_POST_TYPES, TERMINAL_POST_TYPES

    """They are exempt for different reasons and must not be conflated: a
    funding post STARTS a collaboration, an assessment ENDS an interview."""
    assert TERMINAL_POST_TYPES == frozenset({"opportunity_assessment"})
    assert not (TERMINAL_POST_TYPES & FUNDING_POST_TYPES)


def test_a_blocked_hub_keeps_its_assessment_in_the_menu():
    from src.agent.roles import load_role

    declared = load_role("scout_hub").post_types
    got = available_for(
        declared,
        gate={"blackbird", "gill"},
        roles_by_agent={"blackbird": "scout_hub", "gill": "pi_lab"},
        self_id="blackbird",
        funding_only=True,          # i.e. blocked_for_regular
    )
    names = {s.name for s in got}
    assert "opportunity_assessment" in names, (
        "a blocked hub lost the only artifact it exists to produce"
    )


def test_a_blocked_pi_lab_agent_is_unaffected():
    """The exemption must not widen anything for pi_lab — no pi_lab role
    declares a terminal type, so its blocked menu is funding-only as before."""
    from src.agent.post_types import DEFAULT_POST_TYPES

    got = available_for(
        DEFAULT_POST_TYPES,
        gate=None,
        roles_by_agent={"gill": "pi_lab", "pearce": "pi_lab"},
        self_id="gill",
        funding_only=True,
    )
    assert {s.name for s in got} == {"funding_collab"}


# --- gates 2 and 3: the handler ----------------------------------------------

async def _drive(monkeypatch, response, *, n_threads=65):
    """A hub holding n_threads live interviews and nothing left to reply to —
    the exact production shape."""
    hub = _hub()
    hub.allowed_sender_ids = None
    for i in range(n_threads):
        tid = f"thread-{i}"
        hub.state.active_threads[tid] = ThreadState(
            thread_id=tid, channel="general", other_agent_id=f"pi{i}",
            message_count=4,
        )
    client = FakeSlackClient(agent_id="blackbird")
    eng = SimulationEngine(agents=[hub], slack_clients={"blackbird": client})

    monkeypatch.setattr(
        "src.agent.simulation.get_settings", lambda: _settings()
    )

    seen = {}

    def _stub_prompt(**kw):
        seen.update(kw)
        return ("sys", [])

    async def _fake_generate(**kwargs):
        return response

    monkeypatch.setattr(hub, "build_phase5_prompt", _stub_prompt)
    monkeypatch.setattr(
        "src.agent.simulation.generate_agent_response", _fake_generate
    )
    await eng._phase5_new_post(hub)
    return eng, hub, client, seen


_ASSESSMENT = (
    '```json\n'
    '{"action": "new_post", "channel": "general", '
    '"post_type": "opportunity_assessment", "tagged_agent": null}\n'
    '```\n\n'
    '<slack_message>:mag: Opportunity Assessment — Gill Lab</slack_message>'
)
_REGULAR = (
    '```json\n'
    '{"action": "new_post", "channel": "general", '
    '"post_type": "paper", "tagged_agent": null}\n'
    '```\n\n'
    '<slack_message>:newspaper: Paper — a thing.</slack_message>'
)


async def test_a_saturated_hub_still_reaches_the_prompt(monkeypatch):
    """Gate 2. With 65 open threads against a threshold of 12 and nothing to
    reply to, the handler used to return before building a prompt at all."""
    _eng, _hub_a, _client, seen = await _drive(monkeypatch, _ASSESSMENT)
    assert seen, "phase 5 returned before build_phase5_prompt — still locked out"


async def test_a_saturated_hub_can_post_its_assessment(monkeypatch):
    """Gates 2 and 3 together, end to end."""
    _eng, hub, client, _seen = await _drive(monkeypatch, _ASSESSMENT)
    assert len(client.posted) == 1
    assert client.posted[0]["text"].startswith(":mag:")
    assert hub.message_count == 1


async def test_a_saturated_hub_still_cannot_post_a_regular_type(monkeypatch):
    """The backpressure must survive. Only the terminal artifact is exempt —
    if `paper` also got through, the exemption is a hole, not a valve."""
    _eng, hub, client, _seen = await _drive(monkeypatch, _REGULAR)
    assert client.posted == []
    assert hub.message_count == 0


async def test_an_unsaturated_hub_is_unchanged(monkeypatch):
    """Below the threshold nothing about this path should differ."""
    _eng, hub, client, _seen = await _drive(monkeypatch, _ASSESSMENT, n_threads=2)
    assert len(client.posted) == 1


@pytest.mark.parametrize("n_threads", [0, 11, 12, 13, 65])
async def test_the_assessment_posts_at_every_load(monkeypatch, n_threads):
    """Whether the hub is empty or saturated, filing finished work must work.
    12 is the threshold itself — the boundary is where off-by-ones live."""
    _eng, _hub_b, client, _seen = await _drive(
        monkeypatch, _ASSESSMENT, n_threads=n_threads
    )
    assert len(client.posted) == 1, f"blocked at {n_threads} threads"

"""Phase 5 action dispatch: `new_post`/`skip` are the only supported actions.

Task 6 (branch2 engine reconciliation) deletes the `action == "reply"` branch
along with the funding-thread plumbing that used to feed it — locked decision:
any action other than `new_post`/`skip` is unsupported. It must post nothing,
log it, and increment the skip streak via `previous_skips + 1` (never a bare
`+= 1`, per the reset-then-maybe-re-increment bug documented in
`_phase5_new_post` right above the action dispatch).
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
        lab_daily_post_cap=5,
        active_thread_threshold=12,
        phase5_skip_probability=0.0,
        llm_agent_model_opus="test-model",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


async def _drive(monkeypatch, response):
    """One pi_lab agent, unblocked, with a reachable scout_hub counterparty
    (so `pitch` — the only post type pi_lab declares — resolves as available;
    see test_phase5_terminal_posts.test_a_blocked_pi_lab_agent_is_unaffected)."""
    lab = _lab()
    lab.allowed_sender_ids = None
    hub = _hub()
    client = FakeSlackClient(agent_id="gill")
    eng = SimulationEngine(
        agents=[lab, hub],
        slack_clients={"gill": client, "blackbird": FakeSlackClient(agent_id="blackbird")},
    )

    monkeypatch.setattr("src.agent.simulation.get_settings", lambda: _settings())

    def _stub_prompt(**kw):
        return ("sys", [])

    async def _fake_generate(**kwargs):
        return response

    monkeypatch.setattr(lab, "build_phase5_prompt", _stub_prompt)
    monkeypatch.setattr("src.agent.simulation.generate_agent_response", _fake_generate)
    await eng._phase5_new_post(lab)
    return eng, lab, client


_REPLY = (
    '```json\n'
    '{"action": "reply", "target_post_id": "123"}\n'
    '```\n\n'
    '<slack_message>Sounds good, let\'s keep going.</slack_message>'
)
_PITCH = (
    '```json\n'
    '{"action": "new_post", "channel": "general", '
    '"post_type": "pitch", "tagged_agent": null}\n'
    '```\n\n'
    '<slack_message>:bulb: Pitch — a thing worth screening.</slack_message>'
)


async def test_reply_action_is_unsupported_and_posts_nothing(monkeypatch):
    """`action: "reply"` — the deleted branch's shape — must not post."""
    lab = _lab()
    lab.state.consecutive_phase5_skips = 2  # true prior streak
    lab.allowed_sender_ids = None
    hub = _hub()
    client = FakeSlackClient(agent_id="gill")
    eng = SimulationEngine(
        agents=[lab, hub],
        slack_clients={"gill": client, "blackbird": FakeSlackClient(agent_id="blackbird")},
    )
    monkeypatch.setattr("src.agent.simulation.get_settings", lambda: _settings())
    monkeypatch.setattr(lab, "build_phase5_prompt", lambda **kw: ("sys", []))

    async def _fake_generate(**kwargs):
        return _REPLY

    monkeypatch.setattr("src.agent.simulation.generate_agent_response", _fake_generate)

    await eng._phase5_new_post(lab)

    assert client.posted == [], "an unsupported action must post nothing"
    assert lab.message_count == 0


async def test_reply_action_increments_skip_streak_from_true_prior_value(monkeypatch):
    """The streak reset (to 0) happens before the action dispatch, so the
    rejection must re-increment from the CAPTURED prior value (`previous_skips
    + 1`), not a bare `+= 1` off the just-reset 0 — otherwise a hopeless
    agent's streak is pinned at 1 forever and the `_select_next_agent` damping
    (`skips >= 3`) never engages."""
    lab = _lab()
    lab.state.consecutive_phase5_skips = 2
    lab.allowed_sender_ids = None
    hub = _hub()
    client = FakeSlackClient(agent_id="gill")
    eng = SimulationEngine(
        agents=[lab, hub],
        slack_clients={"gill": client, "blackbird": FakeSlackClient(agent_id="blackbird")},
    )
    monkeypatch.setattr("src.agent.simulation.get_settings", lambda: _settings())
    monkeypatch.setattr(lab, "build_phase5_prompt", lambda **kw: ("sys", []))

    async def _fake_generate(**kwargs):
        return _REPLY

    monkeypatch.setattr("src.agent.simulation.generate_agent_response", _fake_generate)

    await eng._phase5_new_post(lab)

    assert lab.state.consecutive_phase5_skips == 3, (
        f"expected previous_skips(2) + 1 == 3, got "
        f"{lab.state.consecutive_phase5_skips}"
    )


async def test_new_post_pitch_still_posts(monkeypatch):
    """`new_post` remains fully supported — the reply deletion must not
    collaterally break the surviving action."""
    _eng, lab, client = await _drive(monkeypatch, _PITCH)
    assert len(client.posted) == 1
    assert client.posted[0]["text"].startswith(":bulb:")
    assert lab.message_count == 1

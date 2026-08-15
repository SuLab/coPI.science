"""The Phase-5 channel must ride in with the call, not be written back onto
whatever happens to be last in a shared buffer.

`self._llm_log_buffer[-1]["channel"] = channel` assumed the tail entry was
this agent's own call. Under concurrency — two agents' Phase-5 turns
interleaved on the same event loop — another agent's `_on_llm_call` can fire
(appending its own row to the shared buffer) in the gap between this agent's
`generate_agent_response` returning and this line running, so "last entry" is
not reliably "my entry". The fix correlates via an id generated before the
call and carried through `log_meta`, then scans the buffer for that id
instead of trusting position.
"""
from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient


def _lab(agent_id="gill", bot_name="GillBot", pi_name="Gill"):
    return Agent(agent_id, bot_name, pi_name, role="pi_lab")


def _hub():
    return Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")


def _settings(**over):
    import types

    base = dict(
        lab_daily_post_cap=5,
        active_thread_threshold=12,
        phase5_skip_probability=0.0,
        llm_agent_model_opus="test-model",
    )
    base.update(over)
    return types.SimpleNamespace(**base)


_PITCH = (
    '```json\n'
    '{"action": "new_post", "channel": "deep-dive", '
    '"post_type": "pitch", "tagged_agent": null}\n'
    '```\n\n'
    '<slack_message>:bulb: Pitch — a thing worth screening.</slack_message>'
)


async def test_channel_is_stamped_onto_the_callers_own_row_not_the_last_one(monkeypatch):
    """Simulates the exact interleaving that broke position-based stamping:
    while this agent's `generate_agent_response` call is "in flight", a
    concurrent agent's LLM call completes and appends its own row to the
    same shared `_llm_log_buffer` — making this agent's row NOT the last one
    by the time control returns to `_phase5_new_post`.
    """
    lab = _lab()
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
        log_meta = kwargs.get("log_meta") or {}
        # This agent's own call logs its row first, exactly like the real
        # generate_agent_response does right before returning.
        eng._on_llm_call(dict(log_meta))
        # A DIFFERENT agent's turn interleaves here (same event loop, no
        # thread involved) and its own LLM call finishes and logs its row
        # — landing last in the shared buffer.
        eng._on_llm_call({"agent_id": "someone-else", "phase": "new_post"})
        return _PITCH

    monkeypatch.setattr("src.agent.simulation.generate_agent_response", _fake_generate)

    await eng._phase5_new_post(lab)

    own_rows = [e for e in eng._llm_log_buffer if e.get("agent_id") == "gill"]
    assert len(own_rows) == 1, "expected exactly one row logged for gill's own call"
    assert own_rows[0].get("channel") == "deep-dive", (
        "gill's own row must carry the channel gill's response actually chose"
    )

    interloper_row = eng._llm_log_buffer[-1]
    assert interloper_row.get("agent_id") == "someone-else"
    assert "channel" not in interloper_row, (
        "the interloper's row (last in the buffer) must NOT be stamped with "
        "gill's channel — that is exactly the positional bug"
    )

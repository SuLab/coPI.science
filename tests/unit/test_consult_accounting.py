"""A specialist consult is a real, billed Opus call and must be booked as one.

`Agent.record_api_call`'s own docstring states the invariant: "Every call site
must use this rather than bumping ``api_call_count`` directly — a site that bumps
only the counter is invisible to the rate limiter". `_execute_consult_specialist`
called `generate_agent_response` without booking anything at all, so up to eight
consults per concluding reply were invisible to both the sliding-window limiter
and `SimulationRun.total_api_calls`.

That matters most exactly where the spend is highest: the hub is the only role
with `consult_specialist`, it is the agent the limiter is meant to pace, and the
mandatory-consult rules mean a strong verdict pulls in more consults, not fewer.
"""

import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from tests.fakes import FakeSlackClient

_OPINION = """VERDICT SIGNAL: proceed
CONFIDENCE: moderate

The mechanism is plausible and the chemistry path is not obviously blocked.
"""


def _hub_engine():
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="wang",
        message_count=5, has_pending_reply=True,
    )
    hub.state.active_threads["t1"] = thread
    engine = SimulationEngine(
        agents=[hub], slack_clients={"blackbird": FakeSlackClient(agent_id="blackbird")}
    )
    return engine, hub, thread


@pytest.mark.asyncio
async def test_a_consult_is_booked_against_the_rate_limiter(monkeypatch):
    engine, hub, thread = _hub_engine()

    async def _fake_opinion(**kwargs):
        return _OPINION

    async def _fake_reply(**kwargs):
        # One tool round: the hub consults chemistry, then answers.
        await kwargs["tool_executor"](
            "consult_specialist",
            {"domain": "chemistry", "question": "q", "context": "c"},
        )
        return "<slack_message>Thanks — one more question.</slack_message>"

    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_reply)
    monkeypatch.setattr("src.agent.tools.generate_agent_response", _fake_opinion)

    await engine._reply_to_thread(hub, thread)

    # The reply itself, plus the consult it made. Booking only the reply is what
    # let the hub outrun its own allowance.
    assert hub.api_call_count == 2, (
        f"expected the reply + 1 consult to be booked, got {hub.api_call_count}"
    )
    # And the consult must still be recorded for the specialist floor.
    assert engine._consulted_domains("wang") == frozenset({"chemistry"})


@pytest.mark.asyncio
async def test_every_consult_in_a_turn_is_booked(monkeypatch):
    engine, hub, thread = _hub_engine()

    async def _fake_opinion(**kwargs):
        return _OPINION

    async def _fake_reply(**kwargs):
        for domain in ("scientific", "talent", "chemistry", "clinical"):
            await kwargs["tool_executor"](
                "consult_specialist",
                {"domain": domain, "question": "q", "context": "c"},
            )
        return "<slack_message>Concluding.</slack_message>"

    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_reply)
    monkeypatch.setattr("src.agent.tools.generate_agent_response", _fake_opinion)

    await engine._reply_to_thread(hub, thread)

    assert hub.api_call_count == 5, (
        f"expected the reply + 4 consults, got {hub.api_call_count}"
    )


@pytest.mark.asyncio
async def test_a_failed_consult_is_not_booked(monkeypatch):
    """An unknown domain never reaches the API, so it must not be charged —
    the same reasoning that keeps it from satisfying the specialist floor."""
    engine, hub, thread = _hub_engine()

    async def _fake_reply(**kwargs):
        await kwargs["tool_executor"](
            "consult_specialist",
            {"domain": "astrology", "question": "q", "context": "c"},
        )
        return "<slack_message>Never mind.</slack_message>"

    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_reply)

    await engine._reply_to_thread(hub, thread)

    assert hub.api_call_count == 1, "an unknown domain made no API call to charge"
    assert engine._consulted_domains("wang") == frozenset()

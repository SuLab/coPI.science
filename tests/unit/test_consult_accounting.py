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
from src.agent.tools import _execute_consult_specialist
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
    # And the consult must still be recorded for the specialist floor, keyed
    # under this interview's own thread — not the PI alone (see task 6:
    # a PI's second interview must not inherit a first interview's consults).
    assert engine._consulted_domains("wang", thread.thread_id) == frozenset({"chemistry"})


@pytest.mark.asyncio
async def test_a_consult_appends_to_the_sliding_window_ledger(monkeypatch):
    """Fix round 1 (Ruling R5): booking a consult against api_call_count is
    not enough — it must also land in call_times, or the limiter's coverage
    silently narrows to just the two reserved call sites and a hub that fires
    consults all day never looks throttled for them."""
    engine, hub, thread = _hub_engine()

    async def _fake_opinion(**kwargs):
        return _OPINION

    async def _fake_reply(**kwargs):
        await kwargs["tool_executor"](
            "consult_specialist",
            {"domain": "chemistry", "question": "q", "context": "c"},
        )
        return "<slack_message>Thanks — one more question.</slack_message>"

    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_reply)
    monkeypatch.setattr("src.agent.tools.generate_agent_response", _fake_opinion)

    await engine._reply_to_thread(hub, thread)

    # try_reserve appends once for the reply itself; record_api_call's
    # default (already_reserved=False) must append a second time for the
    # consult, which was never separately reserved.
    assert len(hub.state.call_times) == 2, (
        f"expected reply + consult both in the ledger, got {len(hub.state.call_times)}"
    )


@pytest.mark.asyncio
async def test_a_truncation_retry_appends_to_the_sliding_window_ledger(monkeypatch):
    """The on_retry hook passed into generate_with_tools is agent.record_api_call
    — a second real billed call for a turn that already reserved once. It must
    still land in call_times, or a heavily-retried agent looks artificially
    under its allowance."""
    engine, hub, thread = _hub_engine()

    async def _fake_reply(**kwargs):
        # Simulate generate_with_tools detecting a max_tokens truncation and
        # firing its second, billed call.
        kwargs["on_retry"]()
        return "<slack_message>Concluding.</slack_message>"

    monkeypatch.setattr(hub, "build_phase4_prompt", lambda **kw: ("sys", []))
    monkeypatch.setattr("src.agent.simulation.generate_with_tools", _fake_reply)

    await engine._reply_to_thread(hub, thread)

    assert hub.api_call_count == 2, "expected the reply + the retry to be booked"
    assert len(hub.state.call_times) == 2, (
        f"expected reply + retry both in the ledger, got {len(hub.state.call_times)}"
    )


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
    assert engine._consulted_domains("wang", thread.thread_id) == frozenset()


@pytest.mark.asyncio
async def test_an_empty_specialist_reply_is_billed_but_not_counted(monkeypatch):
    """The two callbacks must disagree: the call happened and is billed, but
    it produced no opinion and must not satisfy the floor."""
    consulted, billed = [], []

    async def _empty(*args, **kwargs):
        return "   "

    monkeypatch.setattr("src.agent.tools.generate_agent_response", _empty)

    result = await _execute_consult_specialist(
        "chemistry", "Is the series tractable?", "The PI said little.",
        agent_id="blackbird",
        on_consult=lambda domain, signal: consulted.append(domain),
        on_api_call=lambda: billed.append(1),
    )

    assert billed, "a call that was issued is billed whatever it returned"
    assert consulted == [], "an empty reply must not satisfy the floor"
    assert "empty response" in result

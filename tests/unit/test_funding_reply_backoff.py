"""_reply_to_thread's funding_reject_count reset (issue #23 COR-28b'). Modeled on
tests/unit/test_authorship_emit_gate.py's TestPhase4AuthorshipBackoff, which pins the analogous
authorship-guard counter."""

from unittest.mock import AsyncMock

import pytest

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    good = Agent(agent_id="good", bot_name="GoodBot", pi_name="Benjamin Good")
    wu = Agent(agent_id="wu", bot_name="WuBot", pi_name="Chunlei Wu")
    return SimulationEngine(agents=[good, wu], slack_clients={})


async def test_reject_count_resets_on_a_non_rejected_draft_even_if_suppressed(engine, monkeypatch):
    import src.agent.simulation as sim_mod

    good = engine.agents["good"]
    thread = ThreadState(
        thread_id="1700000000.000100", channel="funding-opportunities",
        other_agent_id="wu", has_pending_reply=True,
    )
    good.state.active_threads[thread.thread_id] = thread
    engine.message_log.append(LogEntry(
        ts=thread.thread_id, channel=thread.channel, sender_agent_id=None,
        sender_name="GrantBot", content=":moneybag: PAR-25-297 opportunity",
        thread_ts=None, posted_at=1700000000.0, is_bot=True,
    ))

    ack_only_reply = "Agreed, thanks!"
    substantive_reply = "We can contribute the APPswe mice for target validation."
    state = {"next": ack_only_reply}

    async def fake_generate(**kwargs):
        return f"<slack_message>{state['next']}</slack_message>"

    monkeypatch.setattr(sim_mod, "generate_with_tools", fake_generate)

    await engine._reply_to_thread(good, thread)
    assert thread.funding_reject_count == 1
    assert thread.has_pending_reply is True  # not backed off yet after one rejection

    # A legitimate draft this turn — not ack-only, not announcement-only (it
    # matches _SUBSTANTIVE_MARKERS_RE's "contribute"/"target") — but suppressed
    # for a reason unrelated to the funding validators.
    state["next"] = substantive_reply
    engine._post_message = AsyncMock(return_value=False)
    await engine._reply_to_thread(good, thread)
    assert thread.funding_reject_count == 0, (
        "a non-rejected draft must clear the reject streak even when the post itself is "
        "suppressed — otherwise a stale streak from an earlier false-positive rejection "
        "survives an unrelated suppression and counts towards backing the thread off"
    )

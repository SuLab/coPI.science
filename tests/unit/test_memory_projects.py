"""A working-memory synthesis never sees a project (spec §5.8): BlackbirdBot's
posting, the lab's project reply and every message in a project thread stay out of
the "recent messages" context, while interview messages still feed it."""

import time

import pytest

from src.agent.agent import Agent
from src.agent.message_log import PHASE_HUB_PROJECT, PHASE_PROJECT_REPLY, LogEntry
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio


def _entry(ts, sender, content, *, thread_ts=None, phase=None):
    return LogEntry(
        ts=f"{ts:.6f}", channel="general", sender_agent_id=sender,
        sender_name=f"{sender.capitalize()}Bot", content=content,
        thread_ts=None if thread_ts is None else f"{thread_ts:.6f}",
        posted_at=ts, phase=phase,
    )


async def _synthesis_context(monkeypatch, eng, agent) -> str:
    """Run one memory update and return the user message it sent the model."""
    sent: list[str] = []

    async def _generate(**kwargs):
        sent.append(kwargs["messages"][0]["content"])
        return ""  # empty: the update keeps the old memory and writes nothing

    monkeypatch.setattr(agent, "build_thread_reply_system_prompt", lambda **kw: "sys")
    monkeypatch.setattr("src.agent.engine.deps.generate_agent_response", _generate)
    await eng.memory._update_agent_memory(agent, "Thread closed: no_proposal")
    (content,) = sent
    return content


async def test_synthesis_context_excludes_project_entries_and_postings(monkeypatch):
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={
            "blackbird": FakeSlackClient(agent_id="blackbird"),
            "wang": FakeSlackClient(agent_id="wang"),
        },
    )
    t = time.time()
    for entry in (
        _entry(t, "blackbird", "PROJECT-POSTING", phase=PHASE_HUB_PROJECT),
        _entry(t + 1, "wang", "PROJECT-REPLY-ROOT", phase=PHASE_PROJECT_REPLY),
        _entry(t + 2, "blackbird", "PROJECT-THREAD-HUB", thread_ts=t + 1),
        _entry(t + 3, "wang", "PROJECT-THREAD-LAB", thread_ts=t + 1),
        _entry(t + 4, "wang", "INTERVIEW-PITCH"),
        _entry(t + 5, "blackbird", "INTERVIEW-HUB", thread_ts=t + 4),
    ):
        eng.message_log.append(entry)

    hub_context = await _synthesis_context(monkeypatch, eng, hub)
    assert "INTERVIEW-HUB" in hub_context
    for leaked in ("PROJECT-POSTING", "PROJECT-THREAD-HUB"):
        assert leaked not in hub_context

    lab_context = await _synthesis_context(monkeypatch, eng, lab)
    assert "INTERVIEW-PITCH" in lab_context
    for leaked in ("PROJECT-REPLY-ROOT", "PROJECT-THREAD-LAB"):
        assert leaked not in lab_context

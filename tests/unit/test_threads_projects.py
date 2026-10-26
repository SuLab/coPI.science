"""Closing a project thread (spec §5.8): the decision row, the closed set and the
active-thread removal are those of any close, but a project leaves no prior-thread
summary for Phase 5 and queues no working-memory update for either bot."""

import time
import uuid

import pytest

from src.agent.agent import Agent
from src.agent.message_log import PHASE_PROJECT_REPLY, LogEntry
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio


def _engine():
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={
            "blackbird": FakeSlackClient(agent_id="blackbird"),
            "wang": FakeSlackClient(agent_id="wang"),
        },
        # Truthy, so _close_thread reaches _record_decision; the test stubs it.
        session_factory=object(),
        simulation_run_id=uuid.uuid4(),
    )
    return eng, hub, lab


def _open_thread(eng, hub, lab, *, phase):
    now = time.time()
    root_ts = f"{now:.6f}"
    eng.message_log.append(LogEntry(
        ts=root_ts, channel="general", sender_agent_id="wang", sender_name="WangBot",
        content="root", posted_at=now, phase=phase,
    ))
    eng.message_log.append(LogEntry(
        ts=f"{now + 1:.6f}", channel="general", sender_agent_id="blackbird",
        sender_name="BlackbirdBot", content="reply", thread_ts=root_ts, posted_at=now + 1,
    ))
    for agent, other in ((hub, lab), (lab, hub)):
        agent.state.active_threads[root_ts] = ThreadState(
            thread_id=root_ts, channel="general", other_agent_id=other.agent_id,
            message_count=2,
        )
    return root_ts


def _capture_decisions(monkeypatch, eng):
    rows: list[dict] = []

    async def _record(agent_id, row):
        rows.append(row)

    monkeypatch.setattr(eng.threads, "_record_decision", _record)
    return rows


async def test_closing_a_project_thread_writes_the_decision_and_nothing_else(monkeypatch):
    eng, hub, lab = _engine()
    root_ts = _open_thread(eng, hub, lab, phase=PHASE_PROJECT_REPLY)
    rows = _capture_decisions(monkeypatch, eng)

    await eng._close_thread(hub, hub.state.active_threads[root_ts], "project_closed", "a plan")

    assert [r["thread_id"] for r in rows] == [root_ts]
    assert rows[0]["outcome"] == "project_closed"
    assert root_ts in eng._closed_thread_ids
    assert root_ts not in hub.state.active_threads
    assert root_ts not in lab.state.active_threads
    assert eng.threads._prior_threads == {}, "a project is not Phase 5 dedup context"
    assert eng.memory._pending_memory_events == [], "a project reaches neither bot's memory"


async def test_an_interview_close_is_unchanged(monkeypatch):
    eng, hub, lab = _engine()
    root_ts = _open_thread(eng, hub, lab, phase=None)
    rows = _capture_decisions(monkeypatch, eng)

    await eng._close_thread(hub, hub.state.active_threads[root_ts], "no_proposal", "screened")

    assert [r["thread_id"] for r in rows] == [root_ts]
    assert root_ts in eng._closed_thread_ids
    assert root_ts not in lab.state.active_threads
    assert eng.threads._prior_threads == {
        ("blackbird", "wang"): [
            {"channel": "general", "outcome": "no_proposal", "summary": "screened"},
        ],
    }
    assert [e[0] for e in eng.memory._pending_memory_events] == ["blackbird", "wang"]

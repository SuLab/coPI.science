"""The reply lane and closes under the run's sessions.

A thread whose system is not active gets no reply turn (a projects-only resume
of a held run leaves its interview threads untouched), and an operator's Stop
now closes a thread without telling either agent's working memory. See
docs/specs/2026-10-27-independent-simulation-controls-design.md.
"""

import uuid
from datetime import UTC, datetime

import pytest

from src.agent.agent import Agent
from src.agent.engine.constants import STOPPED_OUTCOME
from src.agent.message_log import PHASE_PROJECT_REPLY, LogEntry
from src.agent.sessions import INTERVIEWS, PROJECTS, Limits, Sessions
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from tests.fakes import FakeSlackClient

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def _sessions(*systems):
    sessions = Sessions()
    for system in systems:
        sessions.start(system, Limits(), T0, initial=True)
    return sessions


def _engine(**kw):
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("wang", "WangBot", "Wang", role="pi_lab")
    eng = SimulationEngine(
        agents=[hub, lab],
        slack_clients={
            "blackbird": FakeSlackClient(agent_id="blackbird"),
            "wang": FakeSlackClient(agent_id="wang"),
        },
        **kw,
    )
    return eng, hub, lab


def _open_thread(hub, lab, thread_id):
    """An open thread in both participants' state; the hub owes the reply."""
    for agent, other in ((hub, lab), (lab, hub)):
        agent.state.active_threads[thread_id] = ThreadState(
            thread_id=thread_id, channel="general",
            other_agent_id=other.agent_id, message_count=2,
            has_pending_reply=agent is hub,
        )


def _project_root(eng, ts):
    eng.message_log.append(LogEntry(
        ts=ts, channel="general", sender_agent_id="wang", sender_name="WangBot",
        content="our answer to the posting", thread_ts=None, posted_at=float(ts),
        is_bot=True, phase=PHASE_PROJECT_REPLY,
    ))


def _pending_thread_ids(eng):
    return {thread.thread_id for _, thread in eng.reply_lane._pending_reply_pairs()}


@pytest.mark.parametrize(
    ("systems", "served"),
    [((INTERVIEWS,), {"t-interview"}), ((PROJECTS,), set()), ((), set())],
    ids=["interviews", "projects-only", "neither"],
)
def test_inactive_interview_threads_get_no_reply_turns(systems, served):
    eng, hub, lab = _engine()
    eng.ctx.sessions = _sessions(*systems)
    _open_thread(hub, lab, "t-interview")

    assert _pending_thread_ids(eng) == served
    assert hub.state.active_threads["t-interview"].status == "active", (
        "a paused interview stays open, untouched"
    )


@pytest.mark.parametrize(
    ("systems", "served"),
    [((PROJECTS,), {"100.5"}), ((INTERVIEWS,), set()), ((), set())],
    ids=["projects", "interviews-only", "neither"],
)
def test_inactive_project_threads_get_no_reply_turns(systems, served):
    eng, hub, lab = _engine()
    eng.ctx.sessions = _sessions(*systems)
    _project_root(eng, "100.5")
    _open_thread(hub, lab, "100.5")

    assert _pending_thread_ids(eng) == served


def test_a_winding_down_system_still_gets_its_reply_turns():
    eng, hub, lab = _engine()
    sessions = _sessions(INTERVIEWS)
    sessions.wind_down(INTERVIEWS, "wind_down", T0)
    eng.ctx.sessions = sessions
    _open_thread(hub, lab, "t-interview")

    assert _pending_thread_ids(eng) == {"t-interview"}, (
        "winding down lets open interviews finish"
    )


async def _close(eng, hub, monkeypatch, outcome):
    decisions: list[dict] = []
    memory_events: list[tuple] = []

    async def _record_decision(agent_id, row):
        decisions.append(row)

    monkeypatch.setattr(eng.threads, "_record_decision", _record_decision)
    monkeypatch.setattr(eng.threads._memory, "enqueue", memory_events.append)
    await eng._close_thread(
        hub, hub.state.active_threads["t-1"], outcome, summary_text="what was said",
    )
    return decisions, memory_events


def _closable_engine():
    eng, hub, lab = _engine(session_factory=lambda: None, simulation_run_id=uuid.uuid4())
    _open_thread(hub, lab, "t-1")
    # A verdict nobody announced yet: the close owes its headline.
    eng.verdicts._assessed_threads["t-1"] = object()
    return eng, hub, lab


async def test_a_stopped_close_queues_no_memory_event(monkeypatch):
    eng, hub, lab = _closable_engine()

    decisions, memory_events = await _close(eng, hub, monkeypatch, STOPPED_OUTCOME)

    assert [row["outcome"] for row in decisions] == [STOPPED_OUTCOME]
    assert decisions[0]["thread_id"] == "t-1"
    assert memory_events == [], "an operator's stop is not something the agents lived through"
    assert "t-1" in eng._closed_thread_ids
    assert "t-1" not in hub.state.active_threads and "t-1" not in lab.state.active_threads
    assert "t-1" in eng.headlines._pending_headlines, "the owed headline is still queued"
    prior = eng._prior_threads[("blackbird", "wang")]
    assert prior[-1]["outcome"] == STOPPED_OUTCOME, "the prior-thread summary is kept"


async def test_an_ordinary_close_still_queues_both_memory_events(monkeypatch):
    """Positive control for the stopped close above."""
    eng, hub, _ = _closable_engine()

    decisions, memory_events = await _close(eng, hub, monkeypatch, "no_match")

    assert [row["outcome"] for row in decisions] == ["no_match"]
    assert sorted(event[0] for event in memory_events) == ["blackbird", "wang"]
    assert "t-1" in eng.headlines._pending_headlines


@pytest.mark.parametrize(
    ("systems", "counted"),
    [((INTERVIEWS, PROJECTS), 2), ((INTERVIEWS,), 1)],
    ids=["projects-active", "projects-not-running"],
)
def test_a_paused_project_thread_does_not_hold_back_a_pitch(systems, counted):
    """The pitch backpressure counts a project thread only while Projects is active: a
    paused one gets no turns, so it must not take one of the lab's pitch slots."""
    eng, hub, lab = _engine()
    eng.ctx.sessions = _sessions(*systems)
    _project_root(eng, "100.000001")
    _open_thread(hub, lab, "100.000001")
    _open_thread(hub, lab, "t-interview")

    assert eng._active_thread_count(lab) == counted

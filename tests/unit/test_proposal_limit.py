"""The interviews session's pitch count and wind-down, in the post lane.

A lab pitches only while the interviews session accepts work
(``Sessions.accepting(INTERVIEWS)``); every counted pitch counts against that
session, which winds down at its count; the orchestrator's once-a-tick
``_observe_interview_session`` ends a winding-down session once no interview
has been open for ``SETTLE_CHECKS[INTERVIEWS]`` observations in a row. See
docs/specs/2026-10-27-independent-simulation-controls-design.md.
"""

import logging
import types
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.engine.constants import ABANDONED_OUTCOME
from src.agent.message_log import PHASE_PROJECT_REPLY, LogEntry
from src.agent.sessions import (
    ENDED,
    INTERVIEWS,
    LIMIT_COUNT,
    SETTLE_CHECKS,
    STOP_NOW,
    WIND_DOWN,
    Limits,
    Sessions,
)
from src.agent.simulation import SimulationEngine
from src.agent.state import ThreadState
from src.models import AgentMessage, SimulationRun
from src.visibility import VISIBILITY_COLLAB_PRIVATE
from tests.fakes import FakeSlackClient

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

PITCH = (
    '```json\n'
    '{"action": "new_post", "channel": "general", '
    '"post_type": "pitch", "tagged_agent": null}\n'
    '```\n\n'
    '<slack_message>:bulb: A thing.</slack_message>'
)


def _settings(**over):
    base = dict(
        lab_daily_post_cap=1,
        active_thread_threshold=12,
        phase5_skip_probability=0.0,
        llm_agent_model_opus="test-model",
        llm_calls_per_load_per_window=8,
        llm_rate_window_seconds=600,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def _interviews(count=0):
    sessions = Sessions()
    sessions.start(INTERVIEWS, Limits(count=count), T0, initial=True)
    return sessions


def _pitching_engine(monkeypatch, sessions):
    """A hub and one lab, a stubbed project offer (no project), stubbed settings and
    a model that answers with a pitch. Returns the engine, the lab, its Slack client
    and a record of the model, prompt and daily-cap calls."""
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("gill", "GillBot", "GILL PI", role="pi_lab")
    hub.allowed_sender_ids = None
    lab.allowed_sender_ids = None
    client = FakeSlackClient(agent_id="gill")
    eng = SimulationEngine(
        agents=[lab, hub],
        slack_clients={"gill": client, "blackbird": FakeSlackClient(agent_id="blackbird")},
    )
    eng.ctx.sessions = sessions
    monkeypatch.setattr("src.agent.engine.deps.get_settings", lambda: _settings())

    calls = {"llm": 0, "prompt": 0, "daily_cap": 0}

    async def _offer(agent):
        return False

    def _prompt(**kw):
        calls["prompt"] += 1
        return ("sys", [])

    async def _generate(**kwargs):
        calls["llm"] += 1
        return PITCH

    def _count(agent):
        calls["daily_cap"] += 1
        return 0

    monkeypatch.setattr(eng.post_lane._projects, "_offer_project_reply", _offer)
    monkeypatch.setattr(lab, "build_phase5_prompt", _prompt)
    monkeypatch.setattr("src.agent.engine.deps.generate_agent_response", _generate)
    monkeypatch.setattr(eng.post_lane._scheduler, "_count_today_posts", _count)
    return eng, lab, client, calls


def _capped():
    sessions = _interviews(count=2)
    sessions.add_count(INTERVIEWS, T0, 2)
    return sessions


def _wound_down():
    sessions = _interviews()
    sessions.wind_down(INTERVIEWS, WIND_DOWN, T0)
    return sessions


def _ended():
    sessions = _interviews()
    sessions.end(INTERVIEWS, STOP_NOW, T0)
    return sessions


@pytest.mark.parametrize(
    "make_sessions", [_capped, _wound_down, _ended, Sessions],
    ids=["cap-reached", "wound-down", "ended", "absent"],
)
async def test_no_model_call_once_interviews_stop_accepting(monkeypatch, make_sessions):
    eng, lab, client, calls = _pitching_engine(monkeypatch, make_sessions())

    await eng._phase5_new_post(lab)

    assert calls == {"llm": 0, "prompt": 0, "daily_cap": 0}, (
        "a lab whose interviews session is not accepting makes no model call, and the "
        "session gate runs before the daily cap"
    )
    assert client.posted == []
    assert lab.state.last_phase5_action_time > 0, "the turn is still consumed"


async def test_pitches_as_today_while_accepting_and_counts_the_pitch(monkeypatch):
    sessions = _interviews(count=2)
    eng, lab, client, calls = _pitching_engine(monkeypatch, sessions)

    await eng._phase5_new_post(lab)

    assert calls == {"llm": 1, "prompt": 1, "daily_cap": 1}
    assert len(client.posted) == 1
    assert eng.post_lane._proposals_posted == 1
    assert sessions.current(INTERVIEWS).count == 1
    assert sessions.accepting(INTERVIEWS), "one of two pitches leaves the session running"


def test_book_pitch_winds_the_session_down_at_its_count():
    sessions = _interviews(count=2)
    eng = SimulationEngine(agents=[Agent("gill", "GillBot", "Gill", role="pi_lab")],
                           slack_clients={})
    eng.ctx.sessions = sessions
    lane = eng.post_lane
    lane._channel_visibility["private-room"] = VISIBILITY_COLLAB_PRIVATE

    lane._book_pitch("general")
    lane._book_pitch("private-room")
    assert sessions.current(INTERVIEWS).count == 1, "a collab_private post is not a pitch"
    assert sessions.accepting(INTERVIEWS)

    lane._book_pitch("general")
    session = sessions.current(INTERVIEWS)
    assert session.count == 2
    assert sessions.winding_down(INTERVIEWS) and session.reason == LIMIT_COUNT
    assert lane._proposals_posted == 2


def test_a_zero_count_never_stops_pitches():
    sessions = _interviews(count=0)
    eng = SimulationEngine(agents=[Agent("gill", "GillBot", "Gill", role="pi_lab")],
                           slack_clients={})
    eng.ctx.sessions = sessions
    for _ in range(50):
        eng.post_lane._book_pitch("general")
    assert sessions.current(INTERVIEWS).count == 50
    assert sessions.accepting(INTERVIEWS)


async def test_rehydration_counts_this_runs_pitches_but_not_against_the_session(engine):
    """The rehydrated count is for logs and tests: it always runs, and a resumed
    interviews session counts from its own start."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        run = SimulationRun(status="running", config={})
        db.add(run)
        await db.commit()
        run_id = run.id

    try:
        async with factory() as db:
            for ts, phase in (
                ("100.1", "new_post"),
                ("100.2", "new_post"),
                ("100.3", "thread_reply"),
            ):
                db.add(AgentMessage(
                    simulation_run_id=run_id,
                    agent_id="wang",
                    channel_id="local:general", channel_name="general",
                    message_ts=ts, message_length=10,
                    thread_ts=None if phase == "new_post" else "100.1",
                    phase=phase, content="hello",
                    sender_name="WangBot",
                    is_bot=True, posted_at=float(ts),
                ))
            await db.commit()

        agent = Agent("wang", "WangBot", "Wang", role="pi_lab")
        eng = SimulationEngine(
            agents=[agent],
            slack_clients={"wang": FakeSlackClient(agent_id="wang")},
            session_factory=factory,
            simulation_run_id=run_id,
        )
        sessions = _interviews(count=2)
        eng.ctx.sessions = sessions
        await eng._rehydrate_proposal_count()
        assert eng.post_lane._proposals_posted == 2, "only the two new_post rows count"
        assert sessions.current(INTERVIEWS).count == 0
        assert sessions.accepting(INTERVIEWS)
    finally:
        async with factory() as db:
            stale = await db.get(SimulationRun, run_id)
            if stale is not None:
                await db.delete(stale)  # cascades agent_messages
                await db.commit()


def _hub_and_lab_engine(**kw):
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


def _open_thread(hub, lab, thread_id, *, lab_owes=False):
    for agent, other in ((hub, lab), (lab, hub)):
        agent.state.active_threads[thread_id] = ThreadState(
            thread_id=thread_id, channel="single-cell-omics",
            other_agent_id=other.agent_id, message_count=4,
            has_pending_reply=(agent is lab) if lab_owes else (agent is hub),
        )


def test_a_winding_down_session_ends_after_three_quiet_observations():
    eng, _, _ = _hub_and_lab_engine()
    sessions = _wound_down()
    eng.ctx.sessions = sessions
    settle = SETTLE_CHECKS[INTERVIEWS]
    assert settle == 3

    for _ in range(settle - 1):
        eng.post_lane._observe_interview_session()
        assert sessions.winding_down(INTERVIEWS), "not before the settle checks"
    eng.post_lane._observe_interview_session()
    session = sessions.current(INTERVIEWS)
    assert session.state == ENDED and session.reason == WIND_DOWN


def test_a_running_session_is_not_ended_by_quiet_observations():
    eng, _, _ = _hub_and_lab_engine()
    sessions = _interviews()
    eng.ctx.sessions = sessions
    for _ in range(10):
        eng.post_lane._observe_interview_session()
    assert sessions.accepting(INTERVIEWS)


async def test_an_abandoned_interview_lets_the_wind_down_end():
    """Run 652a6102: 20 pitches, 19 verdicts, and a run that could never end.

    The regression pin for that hang, against real ``ThreadState``: the abandon
    branches in ``src/agent/engine/reply_lane.py`` once cleared
    ``has_pending_reply`` without closing the thread, so it stayed open in both
    participants' ``active_threads`` and the wind-down never finished.
    """
    eng, hub, lab = _hub_and_lab_engine()
    sessions = _wound_down()
    eng.ctx.sessions = sessions
    _open_thread(hub, lab, "t-open")

    assert eng._open_interview_count() == 1
    for _ in range(SETTLE_CHECKS[INTERVIEWS] + 5):
        eng.post_lane._observe_interview_session()
        assert sessions.winding_down(INTERVIEWS), (
            "an open interview holds the wind-down for as long as it is open"
        )
    assert sessions.current(INTERVIEWS).open_work == 1
    assert sessions.current(INTERVIEWS).waiting_on == ("wang",), (
        "waiting_on names the lab, not the hub"
    )

    await eng._close_thread(hub, hub.state.active_threads["t-open"], ABANDONED_OUTCOME)
    assert eng._open_interview_count() == 0, (
        "an abandoned interview must leave NO open thread behind in either "
        "participant's active_threads — this is the 652a6102 hang"
    )
    for _ in range(SETTLE_CHECKS[INTERVIEWS]):
        eng.post_lane._observe_interview_session()
    assert sessions.current(INTERVIEWS).state == ENDED


class _RosterResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _RosterSession:
    """Minimal session for `_sync_roster_from_db`'s one roster query, the same
    shim `tests/unit/test_roster_token_change.py` uses."""

    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        return _RosterResult(self._rows)


def _registry_row(agent_id, role):
    return SimpleNamespace(
        agent_id=agent_id, bot_name=f"{agent_id}Bot", pi_name=agent_id,
        slack_bot_token=None, role=role,
    )


async def test_a_roster_removal_orphan_does_not_block_the_wind_down(monkeypatch):
    """The same hang by a different route: the partner leaves the roster.

    `_sync_roster_from_db` pops a deactivated agent and never touches the
    surviving participant's `ThreadState`, so if the removed side owed the
    reply the survivor's copy stays `status="active"` with
    `has_pending_reply=False` and nothing can ever post into it again.
    `_open_interview_count` drops a thread whose partner is not on the live
    roster for exactly that reason.
    """
    rows = [_registry_row("blackbird", "scout_hub")]  # wang deactivated
    eng, hub, lab = _hub_and_lab_engine(
        session_factory=lambda: _RosterSession(rows),
        simulation_run_id=None,
        slack_enabled=False,
    )
    sessions = _wound_down()
    eng.ctx.sessions = sessions
    _open_thread(hub, lab, "t-orphan", lab_owes=True)
    assert eng._open_interview_count() == 1

    async def _noop():
        return None

    monkeypatch.setattr(eng.roster, "_recompute_allowed_sender_ids", _noop)
    eng.roster._last_roster_poll = 0.0
    await eng.roster._sync_roster_from_db()

    assert "wang" not in eng.agents, "the removal branch did not run"
    assert "t-orphan" in hub.state.active_threads, (
        "the roster path deliberately leaves the survivor's ThreadState alone — "
        "this is a read-side rule, not a durable close"
    )
    assert eng._open_interview_count() == 0
    for _ in range(SETTLE_CHECKS[INTERVIEWS]):
        eng.post_lane._observe_interview_session()
    assert sessions.current(INTERVIEWS).state == ENDED, (
        "a thread whose partner left the roster can never progress; counting it "
        "would hold the wind-down forever"
    )

    # The predicate is "the partner is on the LIVE roster", so a reactivated
    # partner makes the same thread count again.
    eng.agents["wang"] = lab
    assert eng._open_interview_count() == 1


def test_a_project_thread_is_not_an_open_interview():
    eng, hub, lab = _hub_and_lab_engine()
    eng.message_log.append(LogEntry(
        ts="300.1", channel="general", sender_agent_id="wang", sender_name="WangBot",
        content="our answer to the posting", thread_ts=None, posted_at=300.1,
        is_bot=True, phase=PHASE_PROJECT_REPLY,
    ))
    _open_thread(hub, lab, "300.1")
    assert eng._open_interview_count() == 0
    assert eng.post_lane._open_interview_ids() == set()

    _open_thread(hub, lab, "t-interview")
    assert eng.post_lane._open_interview_ids() == {"t-interview"}


async def test_a_blocked_wind_down_names_its_threads_once(caplog):
    eng, hub, lab = _hub_and_lab_engine()
    sessions = _wound_down()
    eng.ctx.sessions = sessions
    _open_thread(hub, lab, "t-one")

    def _warnings():
        return [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "wind-down cannot finish" in r.getMessage()
        ]

    with caplog.at_level(logging.WARNING, logger="src.agent.simulation"):
        for _ in range(5):
            eng.post_lane._observe_interview_session()
        first = _warnings()
        assert len(first) == 1, "one WARNING per blocked episode, not one per tick"
        assert "t-one" in first[0] and "Stop now ends them" in first[0]

        await eng._close_thread(hub, hub.state.active_threads["t-one"], ABANDONED_OUTCOME)
        eng.post_lane._observe_interview_session()  # clears the episode
        assert sessions.winding_down(INTERVIEWS), "one quiet tick does not end it"
        assert len(_warnings()) == 1

        _open_thread(hub, lab, "t-two")
        for _ in range(3):
            eng.post_lane._observe_interview_session()
        second = _warnings()
        assert len(second) == 2, "a new blocked episode warns again"
        assert "t-two" in second[1] and "t-one" not in second[1]


def test_a_running_session_with_open_interviews_does_not_warn(caplog):
    eng, hub, lab = _hub_and_lab_engine()
    eng.ctx.sessions = _interviews()
    _open_thread(hub, lab, "t-one")
    with caplog.at_level(logging.WARNING, logger="src.agent.simulation"):
        eng.post_lane._observe_interview_session()
    assert not [r for r in caplog.records if "wind-down cannot finish" in r.getMessage()]

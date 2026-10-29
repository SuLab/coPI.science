"""The ``Projects`` engine unit against the database: the run clock, the Projects session's
request rule, postings, the lab's reply, claims and links, the session's wind-down, failure
stop and Stop now, and the ends of a project that cannot finish (the projects prototype
design, §5.2, §5.6, §5.7, §5.9; the independent-controls design, §4.4–§4.8)."""

import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.agent.agent import Agent
from src.agent.claim_check import PROJECT_REPLY_THREAD_KEY, feedback_text
from src.agent.engine import deps
from src.agent.engine.constants import TRUNCATION_NOTICE
from src.agent.message_log import PHASE_HUB_PROJECT, PHASE_PROJECT_REPLY, LogEntry
from src.agent.role_capabilities import hub_role_names, spoke_role_names
from src.agent.sessions import (
    ENDED,
    FAILURE_STOP,
    INTERVIEWS,
    LIMIT_COUNT,
    PROJECTS,
    RUNNING,
    WIND_DOWN,
    WINDING_DOWN,
    Limits,
    Sessions,
)
from src.agent.simulation import SimulationEngine
from src.agent.slack_client import split_for_wire
from src.agent.state import ThreadState
from src.config import get_settings
from src.models import AgentMessage, ThreadDecision
from src.models.hub_project import (
    OPEN_STATUSES,
    REASON_CHOICE_FAILED,
    REASON_CLOSE_UNREADABLE,
    REASON_LAB_NOT_LIVE,
    REASON_NO_IDEA,
    REASON_REPLY_FAILED,
    REASON_RESEARCH_FAILED,
    REASON_RUN_ENDED,
    REASON_STOPPED,
    REASON_THREAD_ABANDONED,
    REASON_THREAD_TIMEOUT,
    STATUS_NOT_AGREED,
    STATUS_NOT_FINISHED,
    STATUS_POSTED,
    STATUS_READY,
    STATUS_READY_TO_POST,
    STATUS_REFINING,
    STATUS_REQUESTED,
    STATUS_RESEARCHING,
    HubProject,
    HubProjectClock,
)
from src.services import project_research
from src.services.project_plan import (
    REPLY_HEADER,
    TEMPLATE_FIELDS,
    fits_one_message,
    posting_head,
)
from tests.fakes import FakeSlackClient
from tests.integration.test_hub_assessment_capture_gate import _delete_run, _new_run

pytestmark = pytest.mark.integration

HUB = "bbhub"
PERSONA = (
    "# {name} Lab\n\n## Research Summary\nWe study how cells sense stress.\n\n"
    "## Key Methods and Technologies\n- CRISPR screening\n- Mass spectrometry\n\n"
    "## Model Systems\n- Organoids\n"
)
TITLE = "A stress-sensing screen in organoids"
POSTING = (
    ":test_tube: *Blackbird project for the {surname} lab*\n"
    "Summary of the idea.\nOpening plan:\n• Model: organoids"
)
REPLY_BODY = "We would use our CRISPR screen; we cannot run the mouse arm."
#: A lab reply long enough to clear ``REPLY_MIN_CHARS``, so a cut-off call that produced a
#: real answer is posted rather than counted as a failure.
LONG_REPLY_BODY = REPLY_BODY + " " + (
    "The screen runs in our own iPSC-derived lines, with the arrayed library we built for "
    "the published work and the same imaging readout; we have the reagents in hand and no "
    "new capability is needed. What we cannot do is the in vivo arm: no animal facility "
    "allocation, no surgical capacity, and no IACUC protocol covering this model. The "
    "readout we would propose instead is the plate-based one from our own published work, "
    "which we can run at the scale the plan asks for without buying anything new."
)
#: A Projects session with no count and no runtime.
NO_LIMITS = Limits()


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    (tmp_path / "public").mkdir()
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    monkeypatch.setattr("src.agent.engine.constants.PROFILES_DIR", tmp_path)
    return tmp_path


def _write_persona(profiles, agent_id: str, name: str) -> None:
    (profiles / "public" / f"{agent_id}.md").write_text(PERSONA.format(name=name), encoding="utf-8")


def _settings(monkeypatch, **overrides) -> None:
    base = get_settings()
    monkeypatch.setattr(deps, "get_settings", lambda: base.model_copy(update=dict(overrides)))


async def _build(
    engine, monkeypatch, profiles, *, labs=("gray",), hub=True, personas=True, slack=True,
    interviews=True, projects: Limits | None = NO_LIMITS, run_id=None,
):
    """A started-enough engine: settings patched, personas written, sessions started (an
    Interviews session unless ``interviews`` is False; a Projects session with ``projects``
    unless it is None), the persist hook set (``start()`` sets it; without it no posted row
    reaches the database) and the projects rehydrated. Returns ``(sim, factory, run_id)``."""
    _settings(monkeypatch)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    if run_id is None:
        run_id = await _new_run(factory)
    agents = []
    if hub:
        agents.append(Agent(HUB, "BlackbirdBot", "Blackbird", role=hub_role_names()[0]))
    for lab in labs:
        name = lab.capitalize()
        agents.append(Agent(lab, f"{name}Bot", f"Jane {name}", role=spoke_role_names()[0]))
        if personas:
            _write_persona(profiles, lab, name)
    clients = {a.agent_id: FakeSlackClient(agent_id=a.agent_id) for a in agents} if slack else {}
    sessions = Sessions()
    now = datetime.now(UTC)
    if interviews:
        sessions.start(INTERVIEWS, NO_LIMITS, now, initial=True)
    if projects is not None:
        sessions.start(PROJECTS, projects, now, initial=True)
    sim = SimulationEngine(
        agents=agents, slack_clients=clients, session_factory=factory,
        simulation_run_id=run_id, sessions=sessions,
    )
    sim.message_log.set_persist_callback(sim._enqueue_persist)
    await sim._rehydrate_projects()
    return sim, factory, run_id


async def _tick(sim):
    sim._last_tick = None  # the next call is a tick, whatever the wall clock says
    await sim._projects_tick()


def _session(sim):
    return sim.ctx.sessions.current(PROJECTS)


async def _finish(factory, run_id, agent_id, *, status=STATUS_NOT_FINISHED, reason=None):
    """Finish every open project of ``agent_id`` now, as the worker or a close would."""
    async with factory() as db:
        await db.execute(
            update(HubProject)
            .where(HubProject.simulation_run_id == run_id, HubProject.agent_id == agent_id,
                   HubProject.status.in_(OPEN_STATUSES))
            .values(status=status, reason_code=reason, finished_at=func.now())
        )
        await db.commit()


async def _seed_finished(factory, run_id, outcomes, agent_id="gray"):
    """One finished project per ``(status, reason)`` in ``outcomes``, oldest first, each
    finished a second after the one before and after the session started."""
    base = datetime.now(UTC)
    for slot, (status, reason) in enumerate(outcomes, start=1):
        await _seed(factory, run_id, agent_id, status=status, slot=slot, reason_code=reason,
                    finished_at=base + timedelta(seconds=slot))


async def _seed(factory, run_id, agent_id="gray", *, status=STATUS_POSTED, slot=1, **cols):
    surname = agent_id.capitalize()
    values = {
        "simulation_run_id": run_id, "agent_id": agent_id, "slot": slot, "status": status,
        "requested_run_seconds": 0.0, "title": TITLE,
        "posting_text": POSTING.format(surname=surname),
        "choice": {"title": TITLE, "summary": "Summary of the idea.", "plan": {},
                   "anchors": [], "sources": [], "why_chosen": "It fits."},
        **cols,
    }
    async with factory() as db:
        row = HubProject(**values)
        db.add(row)
        await db.commit()
        return row.id


async def _project(factory, project_id):
    async with factory() as db:
        return (await db.execute(
            select(HubProject).where(HubProject.id == project_id)
        )).scalar_one()


async def _projects(factory, run_id, agent_id=None):
    async with factory() as db:
        stmt = select(HubProject).where(HubProject.simulation_run_id == run_id)
        if agent_id is not None:
            stmt = stmt.where(HubProject.agent_id == agent_id)
        return (await db.execute(stmt.order_by(HubProject.agent_id, HubProject.slot))).scalars().all()


async def _set_clock(factory, run_id, seconds):
    async with factory() as db:
        await db.execute(
            update(HubProjectClock)
            .where(HubProjectClock.simulation_run_id == run_id)
            .values(run_seconds=seconds)
        )
        await db.commit()


async def _messages(factory, run_id, **where):
    async with factory() as db:
        stmt = select(AgentMessage).where(AgentMessage.simulation_run_id == run_id)
        for column, value in where.items():
            stmt = stmt.where(getattr(AgentMessage, column) == value)
        return (await db.execute(stmt.order_by(AgentMessage.created_at))).scalars().all()


def _script_lab(monkeypatch, lab, outcomes):
    """Stub the lab's reply prompt and script the model: each outcome is a reply text, a
    ``(text, stop_reason)`` pair, or an exception to raise. Returns the recorded calls."""
    prompts: list[dict] = []

    def _prompt(*, posting_text, title, authorship_feedback):
        prompts.append({"posting_text": posting_text, "title": title,
                        "authorship_feedback": authorship_feedback})
        return "system", [{"role": "user", "content": posting_text}]

    calls: list[dict] = []

    async def _generate(**kwargs):
        calls.append(kwargs)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        text, stop = outcome if isinstance(outcome, tuple) else (outcome, "end_turn")
        kwargs["on_stop_reason"](stop)
        return text

    monkeypatch.setattr(lab, "build_project_reply_post_prompt", _prompt)
    monkeypatch.setattr(deps, "generate_agent_response", _generate)
    return calls, prompts


def _reply(body=REPLY_BODY):
    return f"<slack_message>{body}</slack_message>"


def _exhaust_rate(agent):
    agent.state.call_times.extend([time.time()] * 10_000)


# ---------------------------------------------------------------------------
# Requests and the clock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_live_lab_is_requested_when_projects_start(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles, labs=("gray", "hu"))
    try:
        await _tick(sim)
        await _tick(sim)

        rows = await _projects(factory, run_id)
        assert [(r.agent_id, r.slot, r.status) for r in rows] == [
            ("gray", 1, STATUS_REQUESTED), ("hu", 1, STATUS_REQUESTED),
        ], "one each, none for the hub, no second while the first is open"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_no_second_open_project_for_a_lab(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    try:
        await _seed(factory, run_id, status=STATUS_POSTED, requested_run_seconds=-10 * 86400.0)
        await _tick(sim)

        assert [r.slot for r in await _projects(factory, run_id)] == [1]
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_labs_next_project_is_requested_when_its_last_finishes(
    engine, monkeypatch, profiles,
):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    try:
        await _tick(sim)
        await _finish(factory, run_id, "gray", reason=REASON_LAB_NOT_LIVE)
        await _tick(sim)

        rows = await _projects(factory, run_id)
        assert [(r.slot, r.status) for r in rows] == [
            (1, STATUS_NOT_FINISHED), (2, STATUS_REQUESTED)], "no interval: due at once"
        assert rows[1].requested_run_seconds == pytest.approx(rows[0].requested_run_seconds)
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_lab_one_ahead_waits_for_the_others(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles, labs=("gray", "hu"))
    other_run = await _new_run(factory)
    try:
        await _seed(factory, run_id, "hu", status=STATUS_NOT_FINISHED)
        await _seed(factory, other_run, "gray", status=STATUS_POSTED)
        await _tick(sim)

        assert [(r.slot, r.status) for r in await _projects(factory, run_id, "gray")] == [
            (1, STATUS_REQUESTED)], "another run's project does not count"
        assert len(await _projects(factory, run_id, "hu")) == 1, "hu is one ahead: it waits"

        await _tick(sim)
        assert [(r.slot, r.status) for r in await _projects(factory, run_id, "hu")] == [
            (1, STATUS_NOT_FINISHED), (2, STATUS_REQUESTED)], "gray caught up: hu is due"
    finally:
        await _delete_run(factory, run_id)
        await _delete_run(factory, other_run)


@pytest.mark.asyncio
async def test_a_lab_live_mid_session_starts_at_the_minimum(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles, labs=("gray",))
    try:
        for _ in range(2):
            await _tick(sim)
            await _finish(factory, run_id, "gray", reason=REASON_LAB_NOT_LIVE)
        assert await _projects(factory, run_id, "hu") == []

        _write_persona(profiles, "hu", "Hu")
        sim.agents["hu"] = Agent("hu", "HuBot", "Jane Hu", role=spoke_role_names()[0])
        await _tick(sim)
        assert [(r.slot, r.status) for r in await _projects(factory, run_id, "hu")] == [
            (1, STATUS_REQUESTED)], "the newcomer is due at the next tick"
        assert [r.slot for r in await _projects(factory, run_id, "gray")] == [1, 2, 3]

        await _finish(factory, run_id, "gray", reason=REASON_LAB_NOT_LIVE)
        await _tick(sim)
        assert [r.slot for r in await _projects(factory, run_id, "gray")] == [1, 2, 3, 4], (
            "hu counts as having the minimum, so gray is not held back while hu catches up"
        )
        assert len(await _projects(factory, run_id, "hu")) == 1
    finally:
        await _delete_run(factory, run_id)


async def _counted_and_open(factory, run_id):
    rows = await _projects(factory, run_id)
    counted = sum(1 for r in rows if r.status in (STATUS_READY, STATUS_NOT_AGREED))
    return counted, sum(1 for r in rows if r.status in OPEN_STATUSES)


@pytest.mark.asyncio
async def test_no_overshoot_and_a_failure_frees_its_slot(engine, monkeypatch, profiles):
    labs = ("gray", "hu", "wu", "xu")
    sim, factory, run_id = await _build(
        engine, monkeypatch, profiles, labs=labs, projects=Limits(count=3),
    )
    try:
        await _tick(sim)
        rows = await _projects(factory, run_id)
        assert [r.agent_id for r in rows] == ["gray", "hu", "wu"], "three of four live labs"
        assert sum(await _counted_and_open(factory, run_id)) == 3

        await _tick(sim)
        assert len(await _projects(factory, run_id)) == 3, "no fourth while three are open"

        await _finish(factory, run_id, "gray", reason=REASON_CHOICE_FAILED)
        await _tick(sim)
        rows = await _projects(factory, run_id)
        assert [(r.agent_id, r.status) for r in rows if r.status == STATUS_REQUESTED] == [
            ("hu", STATUS_REQUESTED), ("wu", STATUS_REQUESTED), ("xu", STATUS_REQUESTED),
        ], "the failed project freed one slot, and xu (fewest started) took it"
        assert sum(await _counted_and_open(factory, run_id)) == 3

        await _finish(factory, run_id, "hu", status=STATUS_READY)
        await _tick(sim)
        assert await _counted_and_open(factory, run_id) == (1, 2), "a counted one frees nothing"
        assert _session(sim).count == 1 and _session(sim).state == RUNNING
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_three_failures_in_a_row_wind_projects_down(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    try:
        await _seed_finished(factory, run_id, [
            (STATUS_NOT_FINISHED, REASON_NO_IDEA),
            (STATUS_NOT_FINISHED, REASON_RESEARCH_FAILED),
            (STATUS_NOT_FINISHED, REASON_REPLY_FAILED),
        ])
        await _tick(sim)
        session = _session(sim)
        assert (session.state, session.reason) == (WINDING_DOWN, FAILURE_STOP)
        assert session.failures_in_a_row == 3

        await _tick(sim)
        [requested] = [r for r in await _projects(factory, run_id) if r.slot == 4]
        assert (requested.status, requested.reason_code) == (STATUS_NOT_FINISHED, REASON_RUN_ENDED)
        assert (session.state, session.reason) == (ENDED, FAILURE_STOP)
        assert sim.ctx.sessions.active(INTERVIEWS), "interviews carry on"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_counted_project_resets_the_streak_and_excluded_reasons_are_skipped(
    engine, monkeypatch, profiles,
):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles, labs=())
    try:
        await _seed_finished(factory, run_id, [
            (STATUS_NOT_FINISHED, REASON_NO_IDEA),
            (STATUS_NOT_FINISHED, REASON_NO_IDEA),
            (STATUS_READY, None),
            (STATUS_NOT_FINISHED, REASON_THREAD_TIMEOUT),
            (STATUS_NOT_FINISHED, REASON_LAB_NOT_LIVE),
            (STATUS_NOT_FINISHED, REASON_STOPPED),
            (STATUS_NOT_FINISHED, REASON_RUN_ENDED),
            (STATUS_NOT_FINISHED, REASON_CLOSE_UNREADABLE),
        ])
        await _tick(sim)

        session = _session(sim)
        assert session.failures_in_a_row == 2, "two failures since the counted one"
        assert (session.count, session.state) == (1, RUNNING)
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_count_reached_ends_the_session(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles, projects=Limits(count=1))
    try:
        await _seed_finished(factory, run_id, [(STATUS_NOT_AGREED, None)])
        await _tick(sim)

        session = _session(sim)
        assert (session.state, session.reason, session.count) == (ENDED, LIMIT_COUNT, 1)
        assert len(await _projects(factory, run_id)) == 1, "nothing requested past the count"
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_resume_keeps_the_clock(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    try:
        await _set_clock(factory, run_id, 5000.0)
        resumed, _, _ = await _build(engine, monkeypatch, profiles, run_id=run_id)
        assert resumed._clock_seconds == pytest.approx(5000.0)

        await resumed._projects_tick()  # the first tick adds nothing
        resumed._last_tick = deps.time.time() - 120.0
        await resumed._projects_tick()
        assert resumed._clock_seconds == pytest.approx(5120.0, abs=5.0)
        async with factory() as db:
            stored = await db.scalar(select(HubProjectClock.run_seconds).where(
                HubProjectClock.simulation_run_id == run_id))
        assert stored == pytest.approx(resumed._clock_seconds)
    finally:
        await _delete_run(factory, run_id)


# ---------------------------------------------------------------------------
# Postings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ready_project_is_posted_hidden_as_hub_project(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    try:
        project_id = await _seed(factory, run_id, status=STATUS_READY_TO_POST)
        await _tick(sim)

        project = await _project(factory, project_id)
        assert project.status == STATUS_POSTED and project.posted_at is not None
        [row] = await _messages(factory, run_id, phase=PHASE_HUB_PROJECT)
        assert (row.agent_id, row.thread_ts, row.message_ts) == (HUB, None, project.posting_ts)
        assert row.channel_name == "general"
        assert sim.message_log.is_hidden_entry(sim.message_log.get_entry(project.posting_ts))
        assert len(sim.slack_clients[HUB].posted) == 1
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_posting_waits_while_the_hub_is_off_the_roster(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles, hub=False)
    try:
        project_id = await _seed(factory, run_id, status=STATUS_READY_TO_POST)
        await _tick(sim)

        project = await _project(factory, project_id)
        assert project.status == STATUS_READY_TO_POST
        assert project.posting_claimed_at is None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_wind_down_ends_unstarted_projects_only(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles, labs=("gray", "hu"))
    try:
        ids = {
            status: await _seed(factory, run_id, status=status, slot=slot)
            for slot, status in enumerate(
                (STATUS_REQUESTED, STATUS_RESEARCHING, STATUS_READY_TO_POST, STATUS_POSTED),
                start=1)
        }
        assert sim.ctx.sessions.wind_down(PROJECTS, WIND_DOWN, datetime.now(UTC))
        await _tick(sim)

        unstarted = await _project(factory, ids[STATUS_REQUESTED])
        assert (unstarted.status, unstarted.reason_code) == (STATUS_NOT_FINISHED, REASON_RUN_ENDED)
        assert unstarted.finished_at is not None and unstarted.finished_run_seconds is not None
        assert (await _project(factory, ids[STATUS_RESEARCHING])).status == STATUS_RESEARCHING
        assert (await _project(factory, ids[STATUS_READY_TO_POST])).status == STATUS_POSTED, (
            "a researched project is still posted: it runs to its close"
        )
        assert (await _project(factory, ids[STATUS_POSTED])).status == STATUS_POSTED
        assert await _projects(factory, run_id, "hu") == [], "nothing is requested"
        session = _session(sim)
        assert session.state == WINDING_DOWN and session.waiting_on == ("gray",)

        await _finish(factory, run_id, "gray", reason=REASON_LAB_NOT_LIVE)
        await _tick(sim)
        assert (session.state, session.reason) == (ENDED, WIND_DOWN)
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_inactive_projects_request_post_and_offer_nothing(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(
        engine, monkeypatch, profiles, labs=("gray", "hu"), projects=None,
    )
    lab = sim.agents["gray"]
    try:
        posted = await _seed(factory, run_id, status=STATUS_POSTED)
        ready = await _seed(factory, run_id, status=STATUS_READY_TO_POST, slot=2)
        calls, _ = _script_lab(monkeypatch, lab, [])
        await _tick(sim)

        assert await _projects(factory, run_id, "hu") == [], "nothing is requested"
        assert (await _project(factory, ready)).status == STATUS_READY_TO_POST
        assert sim.slack_clients[HUB].posted == []
        assert await sim._offer_project_reply(lab) is False
        assert calls == []
        assert (await _project(factory, posted)).status == STATUS_POSTED
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_end_open_projects_ends_every_open_status_as_stopped_and_returns_their_threads(
    engine, monkeypatch, profiles,
):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    other_run = await _new_run(factory)
    try:
        ids = [
            await _seed(factory, run_id, status=status, slot=slot)
            for slot, status in enumerate(
                (STATUS_REQUESTED, STATUS_RESEARCHING, STATUS_READY_TO_POST, STATUS_POSTED),
                start=1)
        ]
        refining = await _seed(factory, run_id, status=STATUS_REFINING, slot=5,
                               reply_ts="1700000401.000000", reply_claimed_at=datetime.now(UTC))
        done = await _seed(factory, run_id, status=STATUS_READY, slot=6)
        elsewhere = await _seed(factory, other_run, status=STATUS_POSTED)

        threads = await sim.projects.end_open_projects(REASON_STOPPED)

        assert threads == {"1700000401.000000"}
        for project_id in [*ids, refining]:
            project = await _project(factory, project_id)
            assert (project.status, project.reason_code) == (STATUS_NOT_FINISHED, REASON_STOPPED)
            assert project.finished_at is not None
        assert (await _project(factory, done)).status == STATUS_READY
        assert (await _project(factory, elsewhere)).status == STATUS_POSTED
        assert await sim.projects.end_open_projects(REASON_STOPPED) == set()
    finally:
        await _delete_run(factory, run_id)
        await _delete_run(factory, other_run)


@pytest.mark.asyncio
async def test_end_open_projects_beats_a_research_write(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    try:
        project_id = await _seed(factory, run_id, status=STATUS_RESEARCHING)
        await sim.projects.end_open_projects(REASON_STOPPED)

        async with factory() as db:
            moved = await project_research._move(
                db, project_id, STATUS_RESEARCHING, status=STATUS_READY_TO_POST,
            )
            await db.commit()
        assert moved is False, "the worker's compare-and-set is refused"
        project = await _project(factory, project_id)
        assert (project.status, project.reason_code) == (STATUS_NOT_FINISHED, REASON_STOPPED)
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_lab_not_live_at_posting_ends_the_project(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles, personas=False)
    try:
        project_id = await _seed(factory, run_id, status=STATUS_READY_TO_POST)
        await _tick(sim)

        project = await _project(factory, project_id)
        assert (project.status, project.reason_code) == (STATUS_NOT_FINISHED, REASON_LAB_NOT_LIVE)
        assert sim.slack_clients[HUB].posted == []
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_posting_with_slack_off_is_recorded(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles, slack=False)
    try:
        project_id = await _seed(factory, run_id, status=STATUS_READY_TO_POST)
        await _tick(sim)

        project = await _project(factory, project_id)
        assert project.status == STATUS_POSTED
        [row] = await _messages(factory, run_id, phase=PHASE_HUB_PROJECT)
        assert row.message_ts == project.posting_ts and row.slack_ts is None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_posting_claim_prevents_a_second_post_on_resume(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    try:
        project_id = await _seed(factory, run_id, status=STATUS_READY_TO_POST)

        async def _crash(*args, **kwargs):
            return None  # the process died after the post, before the link

        monkeypatch.setattr(sim.projects, "_link_or_keep", _crash)
        await _tick(sim)
        project = await _project(factory, project_id)
        assert project.status == STATUS_READY_TO_POST and project.posting_claimed_at is not None
        [logged] = await _messages(factory, run_id, phase=PHASE_HUB_PROJECT)

        resumed, _, _ = await _build(engine, monkeypatch, profiles, run_id=run_id)
        project = await _project(factory, project_id)
        assert (project.status, project.posting_ts) == (STATUS_POSTED, logged.message_ts)
        await _tick(resumed)
        assert resumed.slack_clients[HUB].posted == []
        assert len(await _messages(factory, run_id, phase=PHASE_HUB_PROJECT)) == 1
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_posting_exception_clears_the_claim_and_the_next_tick_posts_once(
    engine, monkeypatch, profiles,
):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    try:
        project_id = await _seed(factory, run_id, status=STATUS_READY_TO_POST)
        client = sim.slack_clients[HUB]
        original = client.apost_message
        raised = []

        async def _apost(*args, **kwargs):
            if not raised:
                raised.append(True)
                raise ConnectionError("connection reset")
            return await original(*args, **kwargs)

        monkeypatch.setattr(client, "apost_message", _apost)
        await _tick(sim)
        project = await _project(factory, project_id)
        assert (project.status, project.posting_claimed_at) == (STATUS_READY_TO_POST, None)

        await _tick(sim)
        assert (await _project(factory, project_id)).status == STATUS_POSTED
        assert len(await _messages(factory, run_id, phase=PHASE_HUB_PROJECT)) == 1
    finally:
        await _delete_run(factory, run_id)


# ---------------------------------------------------------------------------
# The lab's reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreplied_project_of_a_lab_off_the_roster_ends_and_stops_counting(
    engine, monkeypatch, profiles,
):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        await _tick(sim)
        assert sim.ctx.sessions.current(PROJECTS).open_work == 1

        del sim.agents["gray"]  # muted while its project waits for the reply
        await _tick(sim)

        project = await _project(factory, project_id)
        assert (project.status, project.reason_code) == (STATUS_NOT_FINISHED, REASON_LAB_NOT_LIVE)
        assert sim.ctx.sessions.current(PROJECTS).open_work == 0
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_offer_reply_posts_a_project_reply_root_and_links_it(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        calls, prompts = _script_lab(monkeypatch, lab, [_reply()])

        assert await sim._offer_project_reply(lab) is True

        project = await _project(factory, project_id)
        assert project.status == STATUS_REFINING and project.reply_claimed_at is not None
        [root] = await _messages(factory, run_id, phase=PHASE_PROJECT_REPLY)
        assert (root.agent_id, root.thread_ts, root.message_ts) == ("gray", None, project.reply_ts)
        assert root.content.startswith(REPLY_HEADER.format(title=TITLE) + "\n")
        assert REPLY_BODY in root.content
        assert lab.message_count == 1
        assert prompts[0]["posting_text"] == project.posting_text
        assert calls[0]["log_meta"]["phase"] == "project_reply"
        assert calls[0]["log_meta"]["thread_phase"] == "project_reply"
        assert calls[0]["max_tokens"] == 3300
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_offer_reply_counts_only_posting_failures(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles, labs=("gray", "hu"))
    lab = sim.agents["gray"]
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        _script_lab(monkeypatch, lab, [
            ("", "end_turn"), _reply(), _reply(),
        ])

        assert await sim._offer_project_reply(lab) is False
        assert (await _project(factory, project_id)).reply_failures == 0, (
            "an empty answer with no refusal or cut: not counted outside a drain"
        )

        original = sim.slack_io._post_message
        suppress = [True]

        async def _suppressed(*args, **kwargs):
            return None if suppress[0] else await original(*args, **kwargs)

        monkeypatch.setattr(sim.slack_io, "_post_message", _suppressed)
        assert await sim._offer_project_reply(lab) is True
        project = await _project(factory, project_id)
        assert (project.reply_failures, project.reply_claimed_at) == (1, None)
        assert await sim._offer_project_reply(lab) is True
        project = await _project(factory, project_id)
        assert (project.status, project.reason_code) == (STATUS_NOT_FINISHED, REASON_REPLY_FAILED)

        suppress[0] = False
        other = sim.agents["hu"]
        long_id = await _seed(factory, run_id, "hu", status=STATUS_POSTED)
        _script_lab(monkeypatch, other, [_reply("word " * 1200)])
        assert await sim._offer_project_reply(other) is True
        long_project = await _project(factory, long_id)
        assert (long_project.reply_failures, long_project.status) == (1, STATUS_POSTED)
        assert await _messages(factory, run_id, phase=PHASE_PROJECT_REPLY) == []
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_uncounted_reply_failures_let_the_own_pitch_run(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        calls, _ = _script_lab(monkeypatch, lab, [
            ("", "end_turn"), RuntimeError("overloaded"),
        ])

        assert await sim._offer_project_reply(lab) is False
        assert await sim._offer_project_reply(lab) is False
        _exhaust_rate(lab)
        assert await sim._offer_project_reply(lab) is False
        assert len(calls) == 2, "a refused reservation makes no call"

        project = await _project(factory, project_id)
        assert (project.reply_failures, project.reply_claimed_at) == (0, None)
        assert project.status == STATUS_POSTED
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_refused_lab_reply_with_usable_text_is_posted(engine, monkeypatch, profiles):
    # F7: run d54d7386's jbailey wrote a well-formed reply six times and every one was
    # discarded because the call reported `refusal`. The text is what matters.
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        _script_lab(monkeypatch, lab, [(_reply(LONG_REPLY_BODY), "refusal")])

        assert await sim._offer_project_reply(lab) is True

        project = await _project(factory, project_id)
        assert (project.status, project.reply_failures) == (STATUS_REFINING, 0)
        [root] = await _messages(factory, run_id, phase=PHASE_PROJECT_REPLY)
        assert REPLY_BODY in root.content
        assert root.content.endswith(TRUNCATION_NOTICE)
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_cut_off_reply_without_a_tag_pair_is_not_posted(engine, monkeypatch, profiles):
    """A cut that leaves no opening tag makes `_extract_slack_message` fall back to preamble
    stripping, which would put the model's own reasoning on Slack as the lab's one reply.
    A stray closing tag inside that reasoning must not be mistaken for a real message."""
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        reasoning = (
            "I should answer in one block and end it with </slack_message>. "
            + LONG_REPLY_BODY
        )
        _script_lab(monkeypatch, lab, [(reasoning, "refusal")])

        assert await sim._offer_project_reply(lab) is True

        project = await _project(factory, project_id)
        assert (project.status, project.reply_failures) == (STATUS_POSTED, 1)
        assert await _messages(factory, run_id, phase=PHASE_PROJECT_REPLY) == []
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_cut_off_reply_that_left_a_fragment_is_counted_not_posted(
    engine, monkeypatch, profiles
):
    """The lab answers a posting once and that reply roots the thread, so a fragment cannot
    be replaced: below ``REPLY_MIN_CHARS`` the attempt counts instead of spending the
    project on half a sentence."""
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        _script_lab(monkeypatch, lab, [(_reply("We "), "refusal")])

        assert await sim._offer_project_reply(lab) is True

        project = await _project(factory, project_id)
        assert (project.status, project.reply_failures) == (STATUS_POSTED, 1)
        assert await _messages(factory, run_id, phase=PHASE_PROJECT_REPLY) == []
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_the_notice_is_dropped_rather_than_the_reply(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        head = REPLY_HEADER.format(title=TITLE) + "\n"
        body = ("word " * 900).strip()
        while not fits_one_message(head + body):
            body = body[:-5].strip()
        assert not fits_one_message(head + body + TRUNCATION_NOTICE), "only the notice overruns"
        _script_lab(monkeypatch, lab, [(_reply(body), "max_tokens")])

        assert await sim._offer_project_reply(lab) is True

        project = await _project(factory, project_id)
        assert (project.status, project.reply_failures) == (STATUS_REFINING, 0)
        [root] = await _messages(factory, run_id, phase=PHASE_PROJECT_REPLY)
        assert "cut off before it finished" not in root.content
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_refused_lab_reply_with_no_text_counts_outside_a_drain(
    engine, monkeypatch, profiles,
):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        _script_lab(monkeypatch, lab, [("", "refusal")])

        assert await sim._offer_project_reply(lab) is True, "the turn is spent on the project"

        project = await _project(factory, project_id)
        assert (project.status, project.reply_failures) == (STATUS_POSTED, 1)
        assert project.reply_claimed_at is None
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_two_such_attempts_end_the_project(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        _script_lab(monkeypatch, lab, [("", "refusal"), ("", "max_tokens")])

        assert await sim._offer_project_reply(lab) is True
        assert await sim._offer_project_reply(lab) is True

        project = await _project(factory, project_id)
        assert (project.status, project.reason_code) == (STATUS_NOT_FINISHED, REASON_REPLY_FAILED)
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_model_exception_still_counts_nothing_outside_a_drain(
    engine, monkeypatch, profiles,
):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        _script_lab(monkeypatch, lab, [RuntimeError("overloaded")])

        assert await sim._offer_project_reply(lab) is False

        project = await _project(factory, project_id)
        assert (project.status, project.reply_failures) == (STATUS_POSTED, 0)
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_orphaned_threads_do_not_block_a_project_reply(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    threshold = deps.get_settings().active_thread_threshold
    try:
        await _seed(factory, run_id, status=STATUS_POSTED)
        for i in range(threshold):
            lab.state.active_threads[f"orphan{i}"] = ThreadState(
                thread_id=f"orphan{i}", channel="general", other_agent_id="left-the-roster",
            )
        calls, _ = _script_lab(monkeypatch, lab, [_reply()])

        assert await sim._offer_project_reply(lab) is True
        assert len(calls) == 1

        for i in range(threshold):
            lab.state.active_threads[f"orphan{i}"].other_agent_id = HUB
        await _seed(factory, run_id, status=STATUS_POSTED, slot=2)
        assert await sim._offer_project_reply(lab) is False, (
            "running interviews with a partner on the roster still hold the lab at the threshold"
        )
        assert len(calls) == 1
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_paused_interview_threads_do_not_block_a_project_reply(
    engine, monkeypatch, profiles,
):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles, interviews=False)
    lab = sim.agents["gray"]
    threshold = deps.get_settings().active_thread_threshold
    try:
        await _seed(factory, run_id, status=STATUS_POSTED)
        for i in range(threshold):
            lab.state.active_threads[f"held{i}"] = ThreadState(
                thread_id=f"held{i}", channel="general", other_agent_id=HUB,
            )
        calls, _ = _script_lab(monkeypatch, lab, [_reply()])

        assert await sim._offer_project_reply(lab) is True, (
            "interview threads get no turns while Interviews is not running"
        )
        assert len(calls) == 1
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_failed_attempts_count_while_projects_wind_down(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        second = await _seed(factory, run_id, status=STATUS_POSTED, slot=2)
        sim.ctx.sessions.wind_down(PROJECTS, WIND_DOWN, datetime.now(UTC))
        _script_lab(monkeypatch, lab, [("", "end_turn"), ("", "end_turn")])

        assert await sim._offer_project_reply(lab) is True
        assert (await _project(factory, project_id)).reply_failures == 1
        await sim._offer_project_reply(lab)
        project = await _project(factory, project_id)
        assert (project.status, project.reason_code) == (STATUS_NOT_FINISHED, REASON_REPLY_FAILED)
        await _tick(sim)
        assert sim.ctx.sessions.current(PROJECTS).open_work == 1, "only the second is still owed"

        _exhaust_rate(lab)
        assert await sim._offer_project_reply(lab) is False
        assert (await _project(factory, second)).reply_failures == 0
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_reply_post_exception_clears_the_claim_and_counts(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        _script_lab(monkeypatch, lab, [_reply(), _reply()])

        async def _raise(*args, **kwargs):
            raise ConnectionError("chunk 0 failed twice")

        monkeypatch.setattr(sim.slack_clients["gray"], "apost_message", _raise)
        await _tick(sim)
        assert sim.ctx.sessions.current(PROJECTS).open_work == 1

        assert await sim._offer_project_reply(lab) is True
        project = await _project(factory, project_id)
        assert (project.reply_claimed_at, project.reply_failures) == (None, 1)
        assert await sim._offer_project_reply(lab) is True
        project = await _project(factory, project_id)
        assert (project.status, project.reason_code) == (STATUS_NOT_FINISHED, REASON_REPLY_FAILED)

        await _tick(sim)
        # The failed project no longer waits for a reply; the lab's next one is requested at
        # once (no interval), so the session's open work is that new request alone.
        statuses = [p.status for p in await _projects(factory, run_id, "gray")]
        assert statuses == [STATUS_NOT_FINISHED, STATUS_REQUESTED]
        assert sim.ctx.sessions.current(PROJECTS).open_work == 1
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_failed_link_is_retried_from_memory_without_a_second_post(
    engine, monkeypatch, profiles,
):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        _script_lab(monkeypatch, lab, [_reply()])
        original = sim.projects._link_project_post
        failed = []

        async def _flaky(db, pid, kind, ts):
            if not failed:
                failed.append(True)
                raise ConnectionError("database went away")
            return await original(db, pid, kind, ts)

        monkeypatch.setattr(sim.projects, "_link_project_post", _flaky)
        assert await sim._offer_project_reply(lab) is True
        assert project_id in sim._pending_links
        project = await _project(factory, project_id)
        assert (project.status, project.reply_ts) == (STATUS_POSTED, None)

        await _tick(sim)
        [root] = await _messages(factory, run_id, phase=PHASE_PROJECT_REPLY)
        project = await _project(factory, project_id)
        assert (project.status, project.reply_ts) == (STATUS_REFINING, root.message_ts)
        assert sim._pending_links == {}
        assert len(sim.slack_clients["gray"].posted) == 1
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_reply_link_is_written_after_the_project_ended(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        project_id = await _seed(factory, run_id, status=STATUS_POSTED)
        _script_lab(monkeypatch, lab, [_reply()])
        original = sim.slack_io._post_message

        async def _post_then_end(*args, **kwargs):
            ts = await original(*args, **kwargs)
            async with factory() as db:
                await db.execute(
                    update(HubProject).where(HubProject.id == project_id)
                    .values(status=STATUS_NOT_FINISHED, reason_code=REASON_LAB_NOT_LIVE)
                )
                await db.commit()
            return ts

        monkeypatch.setattr(sim.slack_io, "_post_message", _post_then_end)
        assert await sim._offer_project_reply(lab) is True

        project = await _project(factory, project_id)
        assert project.reply_ts is not None
        assert (project.status, project.reason_code) == (STATUS_NOT_FINISHED, REASON_LAB_NOT_LIVE)
        context = await sim._project_thread_context(project.reply_ts)
        assert context is not None and context["project_id"] == project_id
        assert context["title"] == TITLE and context["why_chosen"] == "It fits."
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_offer_reply_leaves_the_skip_streak_and_timer(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        await _seed(factory, run_id, status=STATUS_POSTED)
        _script_lab(monkeypatch, lab, [_reply()])
        lab.state.consecutive_phase5_skips = 3
        lab.state.last_phase5_action_time = 123.0

        assert await sim._offer_project_reply(lab) is True
        assert lab.state.consecutive_phase5_skips == 3
        assert lab.state.last_phase5_action_time == 123.0
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_offer_reply_wraps_claim_feedback(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    lab = sim.agents["gray"]
    try:
        await _seed(factory, run_id, status=STATUS_POSTED)
        _, prompts = _script_lab(monkeypatch, lab, [_reply()])
        reason = "claims the 2019 Nature paper, which is not the lab's"
        sim.ctx.claim_feedback.note("gray", PROJECT_REPLY_THREAD_KEY, reason)

        await sim._offer_project_reply(lab)
        assert prompts[0]["authorship_feedback"] == feedback_text(reason)
    finally:
        await _delete_run(factory, run_id)


# ---------------------------------------------------------------------------
# Claims on resume, and the ends of a project thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_claim_resolves_on_resume(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    try:
        claimed_at = datetime.now(UTC) - timedelta(minutes=1)
        project_id = await _seed(factory, run_id, status=STATUS_POSTED, reply_claimed_at=claimed_at)
        async with factory() as db:
            for ts, thread_ts in (("1700000500.100000", None),
                                  ("1700000500.200000", "1700000500.100000")):
                db.add(AgentMessage(
                    simulation_run_id=run_id, agent_id="gray", channel_id="local:general",
                    channel_name="general", message_ts=ts, thread_ts=thread_ts,
                    phase=PHASE_PROJECT_REPLY, content="✅ part", posted_at=float(ts),
                ))
            await db.commit()

        await _build(engine, monkeypatch, profiles, run_id=run_id)

        project = await _project(factory, project_id)
        assert (project.status, project.reply_ts) == (STATUS_REFINING, "1700000500.100000")
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_a_two_message_postings_claim_links_its_root_on_resume(engine, monkeypatch, profiles):
    """A posting Slack carries as two messages is logged as two rows, the root holding only
    its first message's text (``posting_head``). Its claim still links to that root on
    resume, so the project is not posted a second time (owner, 2026-10-27)."""
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    try:
        text = POSTING.format(surname="Gray") + "\n" + "A long plan line. " * 300
        chunks = split_for_wire(text)
        assert len(chunks) == 2 and posting_head(text) == chunks[0] != text
        project_id = await _seed(
            factory, run_id, status=STATUS_READY_TO_POST, posting_text=text,
            posting_claimed_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        root_ts = "1700000600.100000"
        async with factory() as db:
            for ts, thread_ts, content in ((root_ts, None, chunks[0]),
                                           ("1700000600.200000", root_ts, chunks[1])):
                db.add(AgentMessage(
                    simulation_run_id=run_id, agent_id=HUB, channel_id="local:general",
                    channel_name="general", message_ts=ts, thread_ts=thread_ts,
                    phase=PHASE_HUB_PROJECT, content=content, posted_at=float(ts),
                ))
            await db.commit()

        resumed, _, _ = await _build(engine, monkeypatch, profiles, run_id=run_id)

        project = await _project(factory, project_id)
        assert (project.status, project.posting_ts) == (STATUS_POSTED, root_ts)
        await _tick(resumed)
        assert resumed.slack_clients[HUB].posted == []
    finally:
        await _delete_run(factory, run_id)


async def _decide(factory, run_id, thread_id, outcome):
    async with factory() as db:
        db.add(ThreadDecision(
            simulation_run_id=run_id, thread_id=thread_id, channel="general",
            agent_a=HUB, agent_b="gray", outcome=outcome,
        ))
        await db.commit()


@pytest.mark.asyncio
async def test_thread_closed_without_a_close_ends_not_finished(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    try:
        cases = {"timeout": REASON_THREAD_TIMEOUT, "abandoned": REASON_THREAD_ABANDONED,
                 "project_closed": REASON_CLOSE_UNREADABLE, "stopped": REASON_STOPPED}
        ids = {}
        for slot, outcome in enumerate(cases, start=1):
            thread_id = f"17000006{slot:02d}.000000"
            ids[outcome] = await _seed(
                factory, run_id, status=STATUS_REFINING, slot=slot, reply_ts=thread_id,
                reply_claimed_at=datetime.now(UTC),
            )
            await _decide(factory, run_id, thread_id, outcome)

        await _tick(sim)

        for outcome, reason in cases.items():
            project = await _project(factory, ids[outcome])
            assert (project.status, project.reason_code) == (STATUS_NOT_FINISHED, reason), outcome
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_unheld_refining_project_ends_abandoned_after_the_grace(
    engine, monkeypatch, profiles,
):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    now = datetime.now(UTC)
    try:
        old = await _seed(factory, run_id, status=STATUS_REFINING, slot=1,
                          reply_ts="1700000701.000000", reply_claimed_at=now - timedelta(minutes=11))
        young = await _seed(factory, run_id, status=STATUS_REFINING, slot=2,
                            reply_ts="1700000702.000000", reply_claimed_at=now - timedelta(minutes=5))
        held = await _seed(factory, run_id, status=STATUS_REFINING, slot=3,
                           reply_ts="1700000703.000000", reply_claimed_at=now - timedelta(minutes=11))
        sim.agents[HUB].state.active_threads["1700000703.000000"] = ThreadState(
            thread_id="1700000703.000000", channel="general", other_agent_id="gray",
        )

        await _tick(sim)

        project = await _project(factory, old)
        assert (project.status, project.reason_code) == (STATUS_NOT_FINISHED, REASON_THREAD_ABANDONED)
        assert (await _project(factory, young)).status == STATUS_REFINING
        assert (await _project(factory, held)).status == STATUS_REFINING
    finally:
        await _delete_run(factory, run_id)


def _log(sim, ts, sender, content, *, root=None, phase=None):
    sim.message_log.append(LogEntry(
        ts=ts, channel="general", sender_agent_id=sender, sender_name=f"{sender}Bot",
        content=content, thread_ts=root, posted_at=float(ts), phase=phase,
    ))


def _close_plan(**overrides):
    """A complete close plan: every template field filled, two lab tools (the first is in
    ``PERSONA``'s Key Methods, the second is not)."""
    plan = {key: f"{label} settled" for key, label in TEMPLATE_FIELDS if key != "lab_tools"}
    plan["lab_tools"] = ["CRISPR screening", "A cryo-EM pipeline"]
    plan.update(overrides)
    return plan


def _close_block(position="agree", disagreements=(), plan=None):
    block = {"position": position, "plan": _close_plan() if plan is None else plan,
             "gaps": [], "disagreements": list(disagreements)}
    return f"<slack_message>✅ Agreed.</slack_message>\n<project_json>{json.dumps(block)}</project_json>"


@pytest.mark.asyncio
async def test_record_close_ready_and_not_agreed(engine, monkeypatch, profiles):
    """D69: the close alone decides. The same complete close is ready whatever the lab's
    last message said; a close that leaves a blocking field open is not agreed."""
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    hub = sim.agents[HUB]
    open_field = _close_plan(readout="to settle with the lab")
    try:
        cases = (
            ("1700000801.000000", "✅ Agreed on this plan.", _close_block(), STATUS_READY),
            ("1700000901.000000", "⚠️ We still cannot run the mouse arm.", _close_block(),
             STATUS_READY),
            ("1700001001.000000", "✅ Agreed on this plan.", _close_block(plan=open_field),
             STATUS_NOT_AGREED),
        )
        for slot, (root, lab_text, _, _expected) in enumerate(cases, start=1):
            await _seed(factory, run_id, status=STATUS_REFINING, slot=slot, reply_ts=root,
                        reply_claimed_at=datetime.now(UTC))
            base = float(root)
            _log(sim, root, "gray", "✅ We would use our screen.", phase=PHASE_PROJECT_REPLY)
            _log(sim, f"{base + 1:.6f}", HUB, "⚠️ What about controls?", root=root)
            _log(sim, f"{base + 2:.6f}", "gray", lab_text, root=root)
            _log(sim, f"{base + 3:.6f}", HUB, "✅ Agreed.", root=root)

        for root, _, raw, expected in cases:
            thread = ThreadState(thread_id=root, channel="general", other_agent_id="gray")
            posted = f"{float(root) + 3:.6f}"
            assert await sim._record_project_close(hub, thread, raw, posted) == expected

        rows = {r.reply_ts: r for r in await _projects(factory, run_id)}
        ready = rows[cases[0][0]]
        assert ready.final["hub_position"] == "agree" and ready.final["lab_position"] == "agree"
        assert ready.final["tool_marks"] == [
            {"item": "CRISPR screening", "in_profile": True, "matched": "CRISPR screening"},
            {"item": "A cryo-EM pipeline", "in_profile": False, "matched": None},
        ]
        assert ready.final["tool_profile_seen"] is True
        assert ready.final["truncated"] is False
        assert (ready.final["gaps"], ready.final["disagreements"]) == ([], [])
        assert ready.finished_at is not None
        warned = rows[cases[1][0]]
        assert warned.final["lab_position"] == "disagree", "recorded, and it decides nothing"
        not_agreed = rows[cases[2][0]]
        assert not_agreed.final["gaps"] == ["Readout is not filled"]

        thread = ThreadState(thread_id=cases[0][0], channel="general", other_agent_id="gray")
        assert await sim._record_project_close(
            hub, thread, _close_block(plan=_close_plan(design="Rewritten")), None,
        ) is None, "a finished project is not written twice"
        again = {r.reply_ts: r for r in await _projects(factory, run_id)}[cases[0][0]]
        assert again.status == STATUS_READY
        assert again.final["plan"]["design"] == "Design settled", (
            "a second close never overwrites the first (Task 6's final IS NULL)"
        )
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_the_close_marks_the_tools_with_the_projects_anchors(
    engine, monkeypatch, profiles,
):
    # The anchor is the only two-word candidate: PERSONA's Model Systems holds the
    # one-word "Organoids", which the two-word floor drops.
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    root = "1700001101.000000"
    try:
        project_id = await _seed(
            factory, run_id, status=STATUS_REFINING, reply_ts=root,
            reply_claimed_at=datetime.now(UTC),
            choice={"title": TITLE, "summary": "Summary of the idea.", "plan": {},
                    "anchors": [{"section": "Model Systems",
                                 "item": "patient-derived organoids"}],
                    "sources": [], "why_chosen": "It fits."},
        )
        _log(sim, root, "gray", "✅ We would use our screen.", phase=PHASE_PROJECT_REPLY)
        thread = ThreadState(thread_id=root, channel="general", other_agent_id="gray")
        plan = _close_plan(lab_tools=["patient-derived organoids (contingent on supply)"])

        assert await sim._record_project_close(
            sim.agents[HUB], thread, _close_block(plan=plan), None,
        ) == STATUS_READY

        project = await _project(factory, project_id)
        assert project.final["tool_marks"] == [{
            "item": "patient-derived organoids (contingent on supply)",
            "in_profile": True, "matched": "patient-derived organoids",
        }]
        assert project.final["tool_profile_seen"] is True
    finally:
        await _delete_run(factory, run_id)


@pytest.mark.asyncio
async def test_unreadable_close_ends_close_unreadable(engine, monkeypatch, profiles):
    sim, factory, run_id = await _build(engine, monkeypatch, profiles)
    try:
        project_id = await _seed(factory, run_id, status=STATUS_REFINING,
                                 reply_ts="1700001001.000000", reply_claimed_at=datetime.now(UTC))
        thread = ThreadState(thread_id="1700001001.000000", channel="general", other_agent_id="gray")
        status = await sim._record_project_close(
            sim.agents[HUB], thread, "<slack_message>✅ Agreed.</slack_message>", None,
        )
        assert status == STATUS_NOT_FINISHED
        project = await _project(factory, project_id)
        assert project.reason_code == REASON_CLOSE_UNREADABLE and project.final is None
    finally:
        await _delete_run(factory, run_id)


"""The ``Projects`` engine unit without a database: the lab's position in a project
thread, an engine without a Projects session, the clock step cap, the spread's offsets and
construction without project settings."""

import uuid
from types import SimpleNamespace

import pytest

from src.agent.agent import Agent
from src.agent.engine import deps
from src.agent.engine.projects import CLOCK_STEP_MAX_SECONDS, PROJECT_TICK_SECONDS
from src.agent.message_log import PHASE_PROJECT_REPLY, LogEntry
from src.agent.role_capabilities import hub_role_names, spoke_role_names
from src.agent.sessions import PROJECTS
from src.agent.simulation import SimulationEngine

ROOT = "1700000100.000000"


def _engine(**kwargs) -> SimulationEngine:
    hub = Agent("bbhub", "BlackbirdBot", "Blackbird", role=hub_role_names()[0])
    lab = Agent("gray", "GrayBot", "Jane Gray", role=spoke_role_names()[0])
    return SimulationEngine(agents=[hub, lab], slack_clients={}, **kwargs)


def _entry(ts: str, sender: str, content: str, *, thread_ts: str | None = ROOT,
           phase: str | None = None) -> LogEntry:
    return LogEntry(
        ts=ts, channel="general", sender_agent_id=sender, sender_name=f"{sender}Bot",
        content=content, thread_ts=thread_ts, posted_at=float(ts), phase=phase,
    )


def _root(content: str = "✅ We would use our screen.") -> LogEntry:
    return _entry(ROOT, "gray", content, thread_ts=None, phase=PHASE_PROJECT_REPLY)


def test_latest_lab_position_reads_the_first_chunk_of_a_split_reply():
    eng = _engine()
    for entry in (
        _root("⚠️ We cannot run the mouse arm."),
        _entry("1700000101.000000", "bbhub", "⚠️ Then a cell model instead?"),
        _entry("1700000102.000000", "gray", "✅ agreed, with the organoid model as the model."),
        _entry("1700000102.500000", "gray", "…and the readout stays the same."),
    ):
        eng.message_log.append(entry)

    assert eng._latest_lab_position(ROOT, "gray") == "agree"


def test_latest_lab_position_ignores_the_root():
    eng = _engine()
    eng.message_log.append(_root("✅ We would use our screen."))
    eng.message_log.append(_entry("1700000101.000000", "bbhub", "✅ Good, here is the plan."))

    assert eng._latest_lab_position(ROOT, "gray") is None


@pytest.mark.asyncio
async def test_no_projects_session_offers_nothing(monkeypatch):
    async def _no_model_call(**kwargs):
        raise AssertionError("an engine without Projects makes no reply call")

    def _no_session():
        raise AssertionError("an engine without Projects reads no project")

    monkeypatch.setattr(deps, "generate_agent_response", _no_model_call)
    eng = _engine(session_factory=_no_session, simulation_run_id=uuid.uuid4())
    assert not eng.ctx.sessions.active(PROJECTS), "a default engine runs interviews only"

    assert await eng._offer_project_reply(eng.agents["gray"]) is False


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def rollback(self):
        return None


@pytest.mark.asyncio
async def test_clock_step_is_capped(monkeypatch):
    eng = _engine(session_factory=_FakeSession, simulation_run_id=uuid.uuid4())
    added: list[float] = []

    async def _advance(db, elapsed):
        added.append(elapsed)

    async def _nothing(db, *args):
        return None

    for name in ("_resolve_project_claims", "_wind_down_unstarted", "_end_stale_projects",
                 "_request_due_labs", "_post_ready_projects", "_observe_session"):
        monkeypatch.setattr(eng.projects, name, _nothing)
    monkeypatch.setattr(eng.projects, "_advance_clock", _advance)
    now = [10_000.0]
    monkeypatch.setattr(deps, "time", SimpleNamespace(time=lambda: now[0]))

    await eng._projects_tick()
    now[0] += PROJECT_TICK_SECONDS / 2
    await eng._projects_tick()  # too soon: no tick
    now[0] += 2 * 3600.0
    await eng._projects_tick()

    assert added == [0.0, CLOCK_STEP_MAX_SECONDS]


@pytest.mark.asyncio
async def test_construction_reads_no_setting(monkeypatch):
    # The settings an engine already needs to construct (the reply lane's semaphore,
    # as tests/unit/test_cohort_isolation.py stubs them) and none of the project ones.
    monkeypatch.setattr(
        deps, "get_settings", lambda: SimpleNamespace(reply_lane_max_in_flight=1)
    )
    eng = _engine()

    await eng._rehydrate_projects()

    assert eng._lab_offsets == {} and eng._offsets_started_at is None


def test_a_newcomer_starts_at_the_minimum_and_keeps_its_offset():
    eng = _engine()
    projects = eng.projects

    assert projects._effective_counts(["gray", "hu"], {"gray": 0, "hu": 0}) == {"gray": 0, "hu": 0}
    assert projects._effective_counts(
        ["gray", "hu", "wu"], {"gray": 3, "hu": 2, "wu": 0},
    ) == {"gray": 3, "hu": 2, "wu": 2}, "wu joins at the lowest, not at zero"
    assert projects._lab_offsets == {"gray": 0, "hu": 0, "wu": 2}

    assert projects._effective_counts(
        ["gray", "hu", "wu"], {"gray": 3, "hu": 3, "wu": 1},
    ) == {"gray": 3, "hu": 3, "wu": 3}, "the offset is kept, not recomputed"

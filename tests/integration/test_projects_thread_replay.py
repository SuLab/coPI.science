"""``scripts/projects_thread_replay.py``: its refusals, and one replay on the test database
with the model patched (the posting goes out, the lab replies, the hub takes the thread up)."""

import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from scripts import projects_thread_replay as replay
from src.agent.engine import deps
from src.agent.sessions import INTERVIEWS, PROJECTS, Limits
from src.config import get_settings
from src.models import AgentMessage, AgentRegistry, User
from src.models.hub_project import (
    STATUS_POSTED,
    STATUS_READY_TO_POST,
    STATUS_REFINING,
    HubProject,
)
from tests.integration.test_hub_assessment_capture_gate import _delete_run

pytestmark = pytest.mark.integration

SCRATCH = "postgresql+asyncpg://copi:replay@replay-pg:5432/copi_replay"
PERSONA = (
    "# Replay Lab\n\n## Research Summary\nWe study how cells sense stress.\n\n"
    "## Key Methods and Technologies\n- CRISPR screening\n"
)


def _entry(lab_id: str, outcome: str = STATUS_READY_TO_POST) -> dict:
    return {
        "agent_id": lab_id, "pi_name": "Jane Replayson", "cost_usd": "0", "sessions": [],
        "choice_failures": [], "outcome": outcome,
        "choice": {"title": "A screen in organoids", "summary": "Summary.", "plan": {},
                   "anchors": [{"section": "Key Methods and Technologies",
                                "item": "CRISPR screening"}],
                   "sources": [], "why_chosen": "It fits.", "others": [],
                   "chosen": {"use": "u", "why": "w", "domain": "d"}},
        "title": "A screen in organoids",
        "posting_text": ":test_tube: *Blackbird project for the Replayson lab*\nSummary.",
    }


def test_replay_refuses_copi_and_a_different_database_url(monkeypatch, capsys):
    with pytest.raises(replay.ReplayRefused):
        replay.check_target("postgresql+asyncpg://copi:x@db:5432/copi",
                            "postgresql+asyncpg://copi:x@db:5432/copi")
    with pytest.raises(replay.ReplayRefused):
        replay.check_target(SCRATCH, "postgresql+asyncpg://copi:x@db:5432/copi")
    with pytest.raises(replay.ReplayRefused):
        replay.check_target(SCRATCH, None)
    replay.check_target(SCRATCH, SCRATCH)

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://copi:x@db:5432/copi")
    code = replay.main(["--db-url", SCRATCH, "--dry-run-json", "/nonexistent.json",
                        "--lab", "gray"])
    assert code == 2
    assert "Refused" in capsys.readouterr().err


def test_replay_refuses_an_entry_that_is_not_ready_to_post():
    data = {"labs": [_entry("gray", outcome="no_idea"), _entry("hu")]}
    with pytest.raises(replay.ReplayRefused):
        replay.select_entry(data, "gray")
    with pytest.raises(replay.ReplayRefused):
        replay.select_entry(data, "absent")
    assert replay.select_entry(data, "hu")["agent_id"] == "hu"


@pytest.mark.asyncio
async def test_replay_engine_posts_and_the_hub_takes_up_the_reply(
    engine, monkeypatch, tmp_path,
):
    suffix = uuid.uuid4().hex[:8]
    hub_id, lab_id = f"rhub{suffix}", f"rlab{suffix}"
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / f"{lab_id}.md").write_text(PERSONA, encoding="utf-8")
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    monkeypatch.setattr("src.agent.engine.constants.PROFILES_DIR", tmp_path)
    test_url = engine.url.render_as_string(hide_password=False)
    base = get_settings()
    monkeypatch.setattr(deps, "get_settings", lambda: base.model_copy(
        update={**replay.replay_settings_overrides(), "database_url": test_url}))

    async def _lab_reply(**kwargs):
        kwargs["on_stop_reason"]("end_turn")
        return "<slack_message>✅ We would use our CRISPR screen.</slack_message>"

    async def _hub_reply(**kwargs):
        return "<slack_message>⚠️ Which positive control would you use?</slack_message>"

    monkeypatch.setattr(deps, "generate_agent_response", _lab_reply)
    monkeypatch.setattr(deps, "generate_with_tools", _hub_reply)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_id = uuid.uuid4()
    try:
        await replay.seed_replay(factory, _entry(lab_id), hub_id=hub_id, lab_id=lab_id,
                                 run_id=run_id)
        agents = await replay.replay_agents(factory, hub_id, lab_id)
        assert {a.agent_id for a in agents} == {hub_id, lab_id}
        sim = replay.build_replay_engine(factory, agents, run_id, max_minutes=5)
        projects = sim.ctx.sessions.current(PROJECTS)
        assert projects is not None and projects.limits == Limits(runtime_minutes=5)
        assert not sim.ctx.sessions.active(INTERVIEWS), "the replay runs no interviews"
        sim.run_state.stop_event.set()  # the start sequence runs; the loop does not
        await sim.start()

        await sim._projects_tick()
        async with factory() as db:
            project = (await db.execute(
                select(HubProject).where(HubProject.simulation_run_id == run_id)
            )).scalar_one()
        assert project.status == STATUS_POSTED

        lab = sim.agents[lab_id]
        assert await sim._offer_project_reply(lab) is True
        async with factory() as db:
            project = (await db.execute(
                select(HubProject).where(HubProject.simulation_run_id == run_id)
            )).scalar_one()
        assert project.status == STATUS_REFINING and project.reply_ts

        await sim._dispatch_reply_lane()
        assert project.reply_ts in sim.agents[hub_id].state.active_threads

        async with factory() as db:
            pitches = (await db.execute(select(AgentMessage).where(
                AgentMessage.simulation_run_id == run_id,
                AgentMessage.agent_id == lab_id,
                AgentMessage.phase == "new_post",
            ))).scalars().all()
        assert pitches == []
    finally:
        await _delete_run(factory, run_id)
        async with factory() as db:
            user_ids = (await db.execute(select(AgentRegistry.user_id).where(
                AgentRegistry.agent_id.in_([hub_id, lab_id])))).scalars().all()
            await db.execute(delete(AgentRegistry).where(
                AgentRegistry.agent_id.in_([hub_id, lab_id])))
            await db.execute(delete(User).where(User.id.in_([u for u in user_ids if u])))
            await db.commit()

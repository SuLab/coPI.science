"""The post lane's half of a BlackbirdBot project (spec §5.7): a lab answers its
posted project inside its own Phase-5 turn, before the interviews session's gate
and the daily cap, so a reply is offered even when Interviews is not accepting
pitches."""

import ast
import pathlib
import subprocess
import sys
import types
from datetime import UTC, datetime

import pytest

from src.agent.agent import Agent
from src.agent.sessions import INTERVIEWS, WIND_DOWN, Limits, Sessions
from src.agent.simulation import SimulationEngine
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.asyncio

ROOT = pathlib.Path(__file__).resolve().parents[2]
POST_LANE = ROOT / "src/agent/engine/post_lane.py"


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


def _engine():
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    lab = Agent("gill", "GillBot", "GILL PI", role="pi_lab")
    hub.allowed_sender_ids = None
    lab.allowed_sender_ids = None
    client = FakeSlackClient(agent_id="gill")
    eng = SimulationEngine(
        agents=[lab, hub],
        slack_clients={"gill": client, "blackbird": FakeSlackClient(agent_id="blackbird")},
    )
    eng.ctx.sessions = Sessions()
    eng.ctx.sessions.start(INTERVIEWS, Limits(), datetime(2026, 10, 27, tzinfo=UTC))
    return eng, lab, client


def _stub_offer(monkeypatch, eng, result):
    """Replace the Projects unit's offer; returns the agent ids it was offered for."""
    seen: list[str] = []

    async def _offer(agent):
        seen.append(agent.agent_id)
        return result

    monkeypatch.setattr(eng.post_lane._projects, "_offer_project_reply", _offer)
    return seen


def _observe_llm(monkeypatch, agent, response=None):
    called = {"llm": 0, "prompt": 0}
    reply = response or '```json\n{"action": "skip"}\n```\n\n<slack_message>skip</slack_message>'

    def _prompt(**kw):
        called["prompt"] += 1
        return ("sys", [])

    async def _generate(**kwargs):
        called["llm"] += 1
        return reply

    monkeypatch.setattr(agent, "build_phase5_prompt", _prompt)
    monkeypatch.setattr("src.agent.engine.deps.generate_agent_response", _generate)
    return called


async def test_offer_runs_before_the_daily_cap_and_ends_the_turn(monkeypatch):
    eng, lab, client = _engine()
    monkeypatch.setattr("src.agent.engine.deps.get_settings", lambda: _settings())
    seen = _stub_offer(monkeypatch, eng, True)
    called = _observe_llm(monkeypatch, lab)
    counted: list[str] = []

    def _count(agent):
        counted.append(agent.agent_id)
        return 0

    monkeypatch.setattr(eng.post_lane._scheduler, "_count_today_posts", _count)

    await eng._phase5_new_post(lab)
    assert seen == ["gill"]
    assert called == {"llm": 0, "prompt": 0}, "a turn spent on the reply makes no Phase-5 call"
    assert counted == [], "the offer runs before the daily-cap check"
    assert client.posted == []
    assert lab.state.last_phase5_action_time > 0, "the timer is stamped before the offer"


async def test_offer_runs_while_interviews_are_not_accepting(monkeypatch):
    eng, lab, client = _engine()
    eng.ctx.sessions.wind_down(INTERVIEWS, WIND_DOWN, datetime(2026, 10, 27, tzinfo=UTC))
    monkeypatch.setattr("src.agent.engine.deps.get_settings", lambda: _settings())
    seen = _stub_offer(monkeypatch, eng, False)
    called = _observe_llm(monkeypatch, lab)

    await eng._phase5_new_post(lab)
    assert seen == ["gill"], "the project reply is offered before the interviews gate"
    assert called == {"llm": 0, "prompt": 0}, "no reply, and no pitch while winding down"
    assert client.posted == []


async def test_no_offer_continues_to_the_own_pitch(monkeypatch):
    eng, lab, client = _engine()
    monkeypatch.setattr("src.agent.engine.deps.get_settings", lambda: _settings())
    seen = _stub_offer(monkeypatch, eng, False)
    response = (
        '```json\n'
        '{"action": "new_post", "channel": "general", '
        '"post_type": "pitch", "tagged_agent": null}\n'
        '```\n\n'
        '<slack_message>:bulb: A thing.</slack_message>'
    )
    called = _observe_llm(monkeypatch, lab, response)

    await eng._phase5_new_post(lab)

    assert seen == ["gill"]
    assert called["llm"] == 1, "no project reply leaves the turn to the lab's own pitch"
    assert len(client.posted) == 1


def test_phase5_new_post_stays_within_the_size_gate():
    tree = ast.parse(POST_LANE.read_text(encoding="utf-8"))
    (fn,) = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_phase5_new_post"
    ]
    assert fn.end_lineno - fn.lineno + 1 <= 200
    out = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(POST_LANE), "--select", "C901",
         "--config", "lint.mccabe.max-complexity=20", "--output-format=concise", "--no-cache"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr

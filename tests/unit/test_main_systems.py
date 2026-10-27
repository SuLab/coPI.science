"""The entry point's systems: the CLI's `--systems` and limit options, the no-system
refusal, and the planned duration (independent-controls plan, Task 5)."""
import pytest
from typer.testing import CliRunner

from src.agent import main as agent_main
from src.agent.sessions import INTERVIEWS, PROJECTS, Limits


@pytest.fixture
def recorded(monkeypatch):
    """Run the CLI without an engine: `_run_simulation` records its call."""
    calls = []

    async def fake_run_simulation(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(agent_main, "_run_simulation", fake_run_simulation)
    monkeypatch.setattr(agent_main, "set_default_writer_id", lambda writer_id: None)
    return calls


def test_both_systems_and_the_four_limit_options_build_their_limits(recorded):
    result = CliRunner().invoke(agent_main.app, [
        "--systems", "interviews,projects",
        "--max-runtime", "60", "--max-proposals", "20",
        "--max-projects", "5", "--projects-runtime", "90",
    ])
    assert result.exit_code == 0, result.output
    [(args, kwargs)] = recorded
    assert args[0] == 60 and args[7] == 20
    assert kwargs == {"systems": {
        INTERVIEWS: Limits(count=20, runtime_minutes=60),
        PROJECTS: Limits(count=5, runtime_minutes=90),
    }}


def test_no_systems_option_runs_interviews_only(recorded):
    result = CliRunner().invoke(agent_main.app, ["--max-proposals", "3"])
    assert result.exit_code == 0, result.output
    [(_args, kwargs)] = recorded
    assert kwargs == {"systems": {INTERVIEWS: Limits(count=3, runtime_minutes=0)}}


def test_projects_alone_carries_only_its_limits(recorded):
    result = CliRunner().invoke(agent_main.app, [
        "--systems", "projects", "--max-projects", "2", "--max-proposals", "9",
    ])
    assert result.exit_code == 0, result.output
    [(_args, kwargs)] = recorded
    assert kwargs == {"systems": {PROJECTS: Limits(count=2, runtime_minutes=0)}}


@pytest.mark.parametrize("names", ["interviews,bogus", "", " , "])
def test_an_unknown_or_missing_system_name_exits_with_an_error(recorded, names):
    result = CliRunner().invoke(agent_main.app, ["--systems", names])
    assert result.exit_code != 0
    assert recorded == []


async def test_run_simulation_refuses_no_system():
    with pytest.raises(ValueError, match="no system to start"):
        await agent_main._run_simulation(0, 0, False, False, False, systems={})


async def test_run_simulation_refuses_an_unknown_system():
    with pytest.raises(ValueError, match="unknown system"):
        await agent_main._run_simulation(0, 0, False, False, False, systems={"bogus": Limits()})


@pytest.mark.parametrize(
    ("systems", "expected"),
    [
        ({INTERVIEWS: Limits(runtime_minutes=60), PROJECTS: Limits()}, 0),
        ({INTERVIEWS: Limits(), PROJECTS: Limits(runtime_minutes=90)}, 0),
        ({INTERVIEWS: Limits(runtime_minutes=60), PROJECTS: Limits(runtime_minutes=90)}, 90),
        ({PROJECTS: Limits(count=4, runtime_minutes=30)}, 30),
        ({INTERVIEWS: Limits(count=20)}, 0),
    ],
)
def test_the_planned_duration_is_zero_when_any_system_has_no_runtime(systems, expected):
    assert agent_main._planned_duration(systems) == expected


def test_the_starting_detail_lists_each_system_with_its_limits():
    detail = agent_main._starting_detail(True, {PROJECTS: Limits(count=5)})
    assert detail == {
        "fresh": True,
        "systems": {PROJECTS: {"state": "starting",
                               "limits": {"count": 5, "runtime_minutes": 0}}},
    }

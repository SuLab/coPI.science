import pytest

from src.agent import thread_guidance
from src.agent.engine.helpers import hub_agent


def test_guidance_sets_keep_the_pinned_dicts():
    assert thread_guidance.GUIDANCE_SETS["pi_lab"] is thread_guidance._PI_LAB
    assert thread_guidance.GUIDANCE_SETS["scout_hub"] is thread_guidance._SCOUT_HUB


def test_an_unknown_role_has_no_guidance_fallback():
    with pytest.raises(KeyError):
        thread_guidance.phase4_guidance("nonexistent", 5)


def test_hub_agent_is_found_by_capability():
    from src.agent.agent import Agent

    agents = {"a": Agent("a", "ABot", "A", role="pi_lab"), "h": Agent("h", "HBot", "H", role="scout_hub")}
    assert hub_agent(agents).agent_id == "h"
    assert hub_agent({"a": agents["a"]}) is None


def test_the_run_start_marker_values_keep_their_keys():
    from src.agent.agent import Agent
    from src.agent.simulation import SimulationEngine
    from tests.fakes import FakeSlackClient

    sim = SimulationEngine(agents=[Agent("h", "HBot", "H", role="scout_hub")],
                           slack_clients={"h": FakeSlackClient(agent_id="h")})
    values = sim.run_announcer._run_start_announcement_values()
    assert list(values)[:12] == [
        "run_id", "started_at", "run_duration", "git_commit", "git_branch", "git_dirty",
        "hub_prompts_version", "hub_prompts_hash", "pi_prompts_version", "pi_prompts_hash",
        "rubric_version", "rubric_hash",
    ]

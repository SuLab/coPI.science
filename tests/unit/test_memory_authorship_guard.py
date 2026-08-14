"""Memory synthesis must not persist ungrounded authorship claims (#29)."""

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from tests.unit.test_authorship_rules import POISONED_MEMORY_ROW


async def test_update_agent_memory_strips_poisoned_lines(tmp_path, monkeypatch):
    import src.agent.agent as agent_mod

    monkeypatch.setattr(agent_mod, "PROFILES_DIR", tmp_path)

    good = Agent(agent_id="good", bot_name="GoodBot", pi_name="Benjamin Good")
    engine = SimulationEngine(agents=[good], slack_clients={})
    engine._agent_publications = {}  # good has no records

    poisoned_synthesis = (
        "## Working Memory — GoodBot\n"
        f"{POISONED_MEMORY_ROW}\n"
        "1. Resume outreach.\n"
    )

    async def fake_generate(**kwargs):
        return poisoned_synthesis

    import src.agent.simulation as sim_mod
    monkeypatch.setattr(sim_mod, "generate_agent_response", fake_generate)

    await engine._update_agent_memory(good, "Thread closed with wu: no_proposal")

    written = (tmp_path / "memory" / "good" / "public.md").read_text()
    assert "Desiderata" not in written
    assert "Resume outreach." in written


async def test_update_agent_memory_strips_self_laundered_coauthorship(tmp_path, monkeypatch):
    # Audit finding I5: lines that name ANOTHER lab as subject but claim self
    # co-authorship ("... with our lab"), or name the agent's OWN lab in the
    # third person, kept the other-lab-subject exemption and survived
    # synthesis for a no-records lab. _update_agent_memory must pass the
    # agent's identity so the strip can catch both.
    import src.agent.agent as agent_mod

    monkeypatch.setattr(agent_mod, "PROFILES_DIR", tmp_path)

    good = Agent(agent_id="good", bot_name="GoodBot", pi_name="Benjamin Good")
    engine = SimulationEngine(agents=[good], slack_clients={})
    engine._agent_publications = {}  # good has no records

    poisoned_synthesis = (
        "## Working Memory — GoodBot\n"
        "Wu Lab (@WuBot) co-authored the Desiderata paper with our lab.\n"
        "Good Lab co-authored the Desiderata paper.\n"
        "1. Resume outreach.\n"
    )

    async def fake_generate(**kwargs):
        return poisoned_synthesis

    import src.agent.simulation as sim_mod
    monkeypatch.setattr(sim_mod, "generate_agent_response", fake_generate)

    await engine._update_agent_memory(good, "Thread closed with wu: no_proposal")

    written = (tmp_path / "memory" / "good" / "public.md").read_text()
    assert "Desiderata" not in written
    assert "Resume outreach." in written


async def test_synthesis_prompt_carries_attribution_instruction(tmp_path, monkeypatch):
    import src.agent.agent as agent_mod
    import src.agent.simulation as sim_mod

    monkeypatch.setattr(agent_mod, "PROFILES_DIR", tmp_path)
    good = Agent(agent_id="good", bot_name="GoodBot", pi_name="Benjamin Good")
    engine = SimulationEngine(agents=[good], slack_clients={})

    captured = {}

    async def fake_generate(**kwargs):
        captured["messages"] = kwargs["messages"]
        return "## Working Memory\n(nothing)\n"

    monkeypatch.setattr(sim_mod, "generate_agent_response", fake_generate)
    await engine._update_agent_memory(good, "event")

    user_content = captured["messages"][0]["content"]
    assert "name the authoring lab(s) explicitly" in user_content

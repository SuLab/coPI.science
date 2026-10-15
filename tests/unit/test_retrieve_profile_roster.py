"""R1.12 and R1.13: retrieve_profile resolves the model's id against the live roster,
caps its echo and leaks no path (the 2026-10-15 publication-handling design)."""
import shutil

import pytest

from src.agent import prompt_snapshot as ps
from src.agent import roles, tools
from src.agent.agent import Agent
from src.agent.prompt_safety import delimit
from src.agent.tool_definitions import TOOL_DEFINITIONS

BRIEF = "# Blackbird Laboratories — Brief for Labs\n\nIncubation grants: $100K–$1M.\n"
ROSTER = tools.live_profile_ids([
    Agent("wang", "WangBot", "Jane Wang"),
    Agent("gordy", "GordyBot", "G Gordy"),
    Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub"),
])
HUBS = {"blackbird": "scout_hub"}
DESCRIPTION = (
    "Retrieve the public profile of another lab's agent. Returns their research focus, "
    "techniques, recent publications, and other publicly available information. Pass the "
    "agent ID or bot name. A lab you cannot currently interact with, or an unknown ID, "
    "returns 'No public profile found'."
)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    shutil.copytree("prompts", tmp_path / "prompts")
    brief = tmp_path / "prompts" / "roles" / "scout_hub" / "lab-brief.md"
    brief.write_text(BRIEF, encoding="utf-8")
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "prompts" / "roles")
    public = tmp_path / "profiles" / "public"
    public.mkdir(parents=True)
    for aid in ("wang", "gordy", "ghost"):
        (public / f"{aid}.md").write_text(f"# {aid} persona\n", encoding="utf-8")
    monkeypatch.setattr(tools, "PROFILES_DIR", tmp_path / "profiles")
    yield tmp_path
    ps.install(None)


async def _rp(target, *, caller="wang", role="pi_lab", gate=None):
    return await tools.execute_tool(
        "retrieve_profile", {"agent_id": target}, caller, None, role=role,
        hub_agent_ids=HUBS, allowed_sender_ids=gate, profile_ids=ROSTER,
    )


def test_the_roster_maps_bot_names_and_ids():
    assert ROSTER == {"wang": "wang", "wangbot": "wang", "gordy": "gordy",
                      "gordybot": "gordy", "blackbird": "blackbird",
                      "blackbirdbot": "blackbird"}


@pytest.mark.parametrize("raw", ["wang", "Wang", " wang ", "WangBot", "@wangbot"])
async def test_agent_id_bot_name_and_at_prefix_resolve(tree, raw):
    assert await _rp(raw) == delimit("# wang persona\n", "agent_profile")


async def test_an_id_not_on_the_roster_is_not_found_even_with_a_file(tree):
    assert await _rp("ghost") == "No public profile found for agent 'ghost'."


async def test_the_echo_is_capped_at_64_characters(tree):
    assert await _rp("x" * 5000) == "No public profile found for agent '" + "x" * 64 + "'."


async def test_a_long_id_leaks_no_path(tree):
    out = await _rp("a" * 300)
    assert out == "No public profile found for agent '" + "a" * 64 + "'."
    assert "File name too long" not in out and str(tree) not in out


async def test_an_unreadable_profile_is_not_found_and_leaks_no_path(tree, caplog):
    path = tree / "profiles" / "public" / "wang.md"
    path.unlink()
    path.mkdir()
    out = await _rp("wang")
    assert out == "No public profile found for agent 'wang'."
    assert str(tree) not in out
    assert any("could not read the profile" in r.getMessage() for r in caplog.records)


async def test_the_gate_applies_to_the_resolved_id(tree):
    assert await _rp("GordyBot", gate={"blackbird"}) == (
        "No public profile found for agent 'GordyBot'.")
    assert await _rp("BlackbirdBot", gate={"blackbird"}) == delimit(BRIEF, "agent_profile")


async def test_no_roster_means_not_found(tree):
    assert await tools._execute_retrieve_profile("wang", "scout_hub") == (
        "No public profile found for agent 'wang'.")


async def test_traversal_input_is_not_found(tree):
    assert await _rp("../private/blackbird") == (
        "No public profile found for agent '../private/blackbird'.")


def test_the_tool_description_names_bot_names_and_the_not_found_rule():
    (definition,) = [d for d in TOOL_DEFINITIONS if d["name"] == "retrieve_profile"]
    assert definition["description"] == DESCRIPTION

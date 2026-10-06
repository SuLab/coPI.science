import shutil
from pathlib import Path

import pytest

from src.agent import prompt_snapshot as ps
from src.agent import roles, tools
from src.agent.prompt_safety import delimit

BRIEF = "# Blackbird Laboratories — Brief for Labs\n\nIncubation grants: $100K–$1M.\n"
HUBS = {"blackbird": "scout_hub"}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    shutil.copytree("prompts", tmp_path / "prompts")
    (tmp_path / "prompts" / "roles" / "scout_hub" / "lab-brief.md").write_text(BRIEF, encoding="utf-8")
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "prompts" / "roles")
    profiles = tmp_path / "profiles"
    (profiles / "public").mkdir(parents=True)
    for aid in ("wang", "gordy"):
        (profiles / "public" / f"{aid}.md").write_text(f"# {aid} persona\n", encoding="utf-8")
    (profiles / "public" / "blackbird.md").write_text("# OLD HUB PROFILE\n", encoding="utf-8")
    monkeypatch.setattr(tools, "PROFILES_DIR", profiles)
    yield tmp_path
    ps.install(None)


async def _rp(target, *, caller="wang", role="pi_lab", gate=None):
    return await tools.execute_tool(
        "retrieve_profile", {"agent_id": target}, caller, None, role=role,
        hub_agent_ids=HUBS, allowed_sender_ids=gate,
    )


async def test_a_lab_reading_outside_its_gate_gets_the_not_found_text(tree):
    assert await _rp("gordy", gate={"blackbird"}) == "No public profile found for agent 'gordy'."
    assert await _rp("nosuchlab") == "No public profile found for agent 'nosuchlab'."


async def test_a_lab_may_read_itself_and_gate_off_is_unrestricted(tree):
    assert await _rp("wang", gate={"blackbird"}) == delimit("# wang persona\n", "agent_profile")
    assert await _rp("gordy", gate=None) == delimit("# gordy persona\n", "agent_profile")


async def test_the_hub_is_never_gated(tree):
    out = await _rp("gordy", caller="blackbird", role="scout_hub", gate={"wang"})
    assert out == delimit("# gordy persona\n", "agent_profile")


async def test_a_hub_id_serves_the_brief_not_the_old_profile_file(tree):
    out = await _rp("blackbird", gate={"blackbird"})
    assert out == delimit(BRIEF, "agent_profile")
    assert "OLD HUB PROFILE" not in out


async def test_the_brief_comes_from_the_snapshot_loaded_at_start(tree):
    ps.install(ps.PromptSnapshot.load())
    (roles.ROLES_DIR / "scout_hub" / "lab-brief.md").write_text("EDITED MID-RUN", encoding="utf-8")
    assert await _rp("blackbird") == delimit(BRIEF, "agent_profile")


async def test_a_missing_brief_is_not_found_and_never_falls_back(tree):
    (roles.ROLES_DIR / "scout_hub" / "lab-brief.md").unlink()
    assert await _rp("blackbird") == "No public profile found for agent 'blackbird'."


async def test_the_gate_is_checked_before_the_filesystem(tree, monkeypatch):
    reads = []
    real = Path.read_text

    def spy(self, *args, **kwargs):
        reads.append(self.name)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    await _rp("gordy", gate={"blackbird"})
    assert "gordy.md" not in reads

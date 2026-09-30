import pytest

from src.agent import prompt_snapshot as ps
from src.agent import roles, specialists
from src.agent.agent import Agent
from src.services.blackbird_rubric import load_rubric


@pytest.fixture
def prompt_tree(tmp_path, monkeypatch):
    """A copy of the prompt tree the test may edit, so prompts/ is never touched."""
    import shutil

    shutil.copytree("prompts", tmp_path / "prompts")
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "prompts" / "roles")
    monkeypatch.setattr(specialists, "SPECIALISTS_DIR", tmp_path / "prompts" / "specialists")
    yield tmp_path / "prompts"
    ps.install(None)


def test_mid_run_prompt_edits_do_not_change_the_next_turn_and_raise_drift(prompt_tree):
    snap = ps.PromptSnapshot.load()
    ps.install(snap)
    hub = Agent("h", "HBot", "H", role="scout_hub")
    before = hub._load_prompt("agent-system.md", "DEFAULT")
    (prompt_tree / "roles" / "scout_hub" / "agent-system.md").write_text("EDITED", encoding="utf-8")
    assert hub._load_prompt("agent-system.md", "DEFAULT") == before
    drift = snap.disk_drift()
    assert "scout_hub" in drift["prompt_drift"]


def test_a_persona_edit_does_not_change_the_next_consult_and_raises_drift(prompt_tree):
    snap = ps.PromptSnapshot.load()
    domain = sorted(specialists.SPECIALIST_DOMAINS)[0]
    known, text = snap.persona(domain)
    assert known
    specialists.persona_path(domain).write_text("EDITED PERSONA", encoding="utf-8")
    assert snap.persona(domain) == (True, text)
    assert f"persona:{domain}" in snap.disk_drift()["prompt_drift"]


def test_a_manifest_edit_cannot_change_tools_or_post_types(prompt_tree):
    snap = ps.PromptSnapshot.load()
    ps.install(snap)
    tools_before = ps.role_spec("pi_lab").tools
    (prompt_tree / "roles" / "pi_lab" / "role.toml").write_text("label = = broken", encoding="utf-8")
    assert ps.role_spec("pi_lab").tools == tools_before


def test_the_snapshot_holds_the_import_time_rubric_object():
    assert ps.PromptSnapshot.load().rubric is load_rubric()


def test_without_an_installed_snapshot_reads_are_todays(prompt_tree):
    ps.install(None)
    assert ps.role_spec("pi_lab") == roles.load_role("pi_lab")

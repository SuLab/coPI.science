"""BlackbirdBot projects' prompts are their own documents (owner, 2026-10-26): the interview
prompt sets stay frozen, so no project prompt sits in, or is stamped with, a role's set."""

from pathlib import Path

from src.agent import project_prompts, roles
from src.agent.role_capabilities import ROLE_CAPABILITIES

PROMPTS = Path("prompts")
#: The names the project prompts had while they sat in the role sets (2026-10-26, 1b657868).
_RETIRED = ("agent-system-project.md", "phase4-project-reply.md", "phase5-project-reply.md")


def test_every_engine_project_prompt_is_under_prompts_projects():
    for name in project_prompts.ENGINE_PROMPT_FILES:
        assert (PROMPTS / "projects" / name).is_file(), name
        assert project_prompts.read(name), name


def test_no_project_prompt_is_in_a_role_prompt_set():
    names = set(project_prompts.ENGINE_PROMPT_FILES) | set(_RETIRED)
    for role, capabilities in ROLE_CAPABILITIES.items():
        assert names.isdisjoint(capabilities.prompt_files), role
    for directory in (PROMPTS, *sorted(p for p in (PROMPTS / "roles").iterdir() if p.is_dir())):
        assert not any((directory / name).exists() for name in names), directory


def test_the_hash_covers_every_engine_project_prompt(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    (tmp_path / "projects").mkdir()
    for name in project_prompts.ENGINE_PROMPT_FILES:
        (tmp_path / "projects" / name).write_text(name, encoding="utf-8")
    first = project_prompts.content_hash()
    for name in project_prompts.ENGINE_PROMPT_FILES:
        (tmp_path / "projects" / name).write_text(name + " edited", encoding="utf-8")
        assert project_prompts.content_hash() != first, name
        (tmp_path / "projects" / name).write_text(name, encoding="utf-8")
    assert project_prompts.content_hash() == first
    (tmp_path / "projects" / project_prompts.HUB_SYSTEM_FILE).unlink()
    assert project_prompts.read(project_prompts.HUB_SYSTEM_FILE) is None
    assert project_prompts.content_hash() != first

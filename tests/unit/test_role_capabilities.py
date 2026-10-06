from pathlib import Path

import pytest

from src.agent import roles
from src.agent.post_types import _KNOWN_ROLES, CANONICAL
from src.agent.role_capabilities import (
    ROLE_CAPABILITIES,
    hub_role_names,
    roles_requiring_user,
)
from src.agent.roles import RoleManifestError, available_roles, load_role, role_problem


def test_every_role_directory_has_a_registry_entry_and_vice_versa():
    dirs = {p.name for p in Path("prompts/roles").iterdir() if p.is_dir()}
    assert dirs == set(ROLE_CAPABILITIES)


def test_both_current_manifests_validate_unchanged_and_are_consistent():
    for role in ROLE_CAPABILITIES:
        assert role_problem(role) is None, role
    assert available_roles() == ["pi_lab", "scout_hub"]


def test_load_role_results_equal_todays():
    pi = load_role("pi_lab")
    assert pi.tools == frozenset({"retrieve_profile", "retrieve_abstract", "retrieve_full_text"})
    assert [s.name for s in pi.post_types] == ["pitch"]
    assert pi.post_types[0].targets == frozenset({"scout_hub"})
    assert pi.calls_per_load_per_window is None and pi.label == "PI Lab"
    hub = load_role("scout_hub")
    assert hub.post_types == () and "consult_specialist" in hub.tools and hub.label == "Scout Hub"


def test_prompt_files_moved_verbatim():
    assert roles.ROLE_PROMPT_FILES == {
        "pi_lab": ("agent-system.md", "identity.md", "phase4-thread-reply.md", "phase5-new-post.md"),
        # lab-brief.md: stamped and snapshotted, never composed (spec 2026-10-05 D26).
        "scout_hub": ("agent-system.md", "identity.md", "phase4-thread-reply.md", "lab-brief.md"),
    }


def test_derived_sets_are_todays_sets():
    assert _KNOWN_ROLES == frozenset({"pi_lab", "scout_hub"})
    assert CANONICAL["pitch"].targets == frozenset(hub_role_names()) == frozenset({"scout_hub"})
    assert roles_requiring_user() == ("pi_lab",)


def _write(tmp_path, monkeypatch, name, text):
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    d = tmp_path / "roles" / name
    d.mkdir(parents=True)
    (d / "role.toml").write_text(text, encoding="utf-8")


@pytest.mark.parametrize("text", [
    "label = = =\n",
    'label = "x"\nsurprise = 1\n',
    'label = 3\n',
    'tools = ["retrieve_profile", "does_not_exist"]\n',
    'calls_per_load_per_window = 0\n',
    '[[post_types]]\nname = "pitch"\nwhen = "now"\n',
])
def test_malformed_manifests_raise(tmp_path, monkeypatch, text):
    _write(tmp_path, monkeypatch, "pi_lab", text)
    with pytest.raises(RoleManifestError):
        load_role("pi_lab")


def test_a_directory_without_a_registry_entry_is_unavailable(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "widget", 'label = "Widget"\npost_types = []\n')
    assert role_problem("widget") is not None
    assert "widget" not in available_roles()


def test_a_registry_entry_contradicting_its_manifest_is_invalid(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, "scout_hub", 'label = "Hub"\n[[post_types]]\nname = "pitch"\n')
    assert "posts_new_threads" in role_problem("scout_hub")

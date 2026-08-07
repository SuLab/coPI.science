import logging

from src.agent import roles
from src.agent.roles import DEFAULT_TOOLS, RoleSpec, load_role


def _write_role(tmp_path, monkeypatch, name, toml_text):
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    d = tmp_path / "roles" / name
    d.mkdir(parents=True)
    (d / "role.toml").write_text(toml_text, encoding="utf-8")


def test_resolve_falls_back_to_global_when_no_role_override(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    (tmp_path / "agent-system.md").write_text("GLOBAL", encoding="utf-8")

    p = roles.resolve_prompt_path("scout_hub", "agent-system.md")

    assert p == tmp_path / "agent-system.md"
    assert p.read_text(encoding="utf-8") == "GLOBAL"


def test_resolve_prefers_role_override_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    (tmp_path / "agent-system.md").write_text("GLOBAL", encoding="utf-8")
    role_dir = tmp_path / "roles" / "scout_hub"
    role_dir.mkdir(parents=True)
    (role_dir / "agent-system.md").write_text("HUB", encoding="utf-8")

    p = roles.resolve_prompt_path("scout_hub", "agent-system.md")

    assert p == role_dir / "agent-system.md"
    assert p.read_text(encoding="utf-8") == "HUB"


def test_pi_lab_resolves_to_global_even_if_role_dir_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    (tmp_path / "phase5-new-post.md").write_text("DEFAULT", encoding="utf-8")

    p = roles.resolve_prompt_path("pi_lab", "phase5-new-post.md")

    assert p == tmp_path / "phase5-new-post.md"


def test_missing_manifest_yields_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    spec = load_role("pi_lab")
    assert spec == RoleSpec(name="pi_lab", label="pi_lab", tools=DEFAULT_TOOLS)


def test_manifest_sets_label_and_tool_allow_list(tmp_path, monkeypatch):
    _write_role(
        tmp_path, monkeypatch, "scout_hub",
        'label = "Scout Hub"\n'
        'tools = ["retrieve_profile", "search_prior_art"]\n',
    )
    # search_prior_art must exist in TOOL_DEFINITIONS by the time this runs
    # (Task 7). Until then this asserts only the known tool survives.
    spec = load_role("scout_hub")
    assert spec.name == "scout_hub"
    assert spec.label == "Scout Hub"
    assert "retrieve_profile" in spec.tools


def test_unknown_tool_is_dropped_and_logged(tmp_path, monkeypatch, caplog):
    _write_role(
        tmp_path, monkeypatch, "weird",
        'tools = ["retrieve_profile", "does_not_exist"]\n',
    )
    with caplog.at_level(logging.WARNING):
        spec = load_role("weird")
    assert "does_not_exist" not in spec.tools
    assert "retrieve_profile" in spec.tools
    assert any("does_not_exist" in r.message for r in caplog.records)


def test_malformed_toml_falls_back_to_defaults(tmp_path, monkeypatch, caplog):
    _write_role(tmp_path, monkeypatch, "broken", "tools = [not valid toml")
    with caplog.at_level(logging.ERROR):
        spec = load_role("broken")
    assert spec.tools == DEFAULT_TOOLS
    assert spec.label == "broken"
def test_role_rate_override_is_read_when_positive(tmp_path, monkeypatch):
    _write_role(
        tmp_path, monkeypatch, "scout_hub",
        'label = "Scout Hub"\ncalls_per_load_per_window = 20\n',
    )
    assert load_role("scout_hub").calls_per_load_per_window == 20


def test_role_rate_override_defaults_to_none(tmp_path, monkeypatch):
    _write_role(tmp_path, monkeypatch, "scout_hub", 'label = "Scout Hub"\n')
    assert load_role("scout_hub").calls_per_load_per_window is None


def test_role_rate_override_rejects_non_positive(tmp_path, monkeypatch, caplog):
    _write_role(
        tmp_path, monkeypatch, "scout_hub",
        'label = "Scout Hub"\ncalls_per_load_per_window = 0\n',
    )
    with caplog.at_level(logging.WARNING):
        spec = load_role("scout_hub")
    assert spec.calls_per_load_per_window is None
    assert "calls_per_load_per_window" in caplog.text


def test_role_rate_override_rejects_non_int(tmp_path, monkeypatch, caplog):
    _write_role(
        tmp_path, monkeypatch, "scout_hub",
        'label = "Scout Hub"\ncalls_per_load_per_window = "lots"\n',
    )
    with caplog.at_level(logging.WARNING):
        spec = load_role("scout_hub")
    assert spec.calls_per_load_per_window is None


def test_missing_manifest_yields_no_rate_override(tmp_path, monkeypatch):
    monkeypatch.setattr(roles, "ROLES_DIR", tmp_path / "roles")
    assert load_role("pi_lab").calls_per_load_per_window is None


def test_missing_manifest_yields_default_post_types():
    from src.agent.post_types import DEFAULT_POST_TYPES

    spec = load_role("definitely_not_a_role_dir")
    assert spec.post_types == DEFAULT_POST_TYPES


def test_manifest_post_types_are_parsed(tmp_path, monkeypatch):
    _write_role(
        tmp_path, monkeypatch, "widget",
        'label = "Widget"\n'
        '[[post_types]]\nname = "paper"\n'
        '[[post_types]]\nname = "pitch"\ntargets = ["scout_hub"]\n',
    )
    spec = load_role("widget")
    assert [s.name for s in spec.post_types] == ["paper", "pitch"]
    assert dict((s.name, s.targets) for s in spec.post_types)["pitch"] == frozenset(
        {"scout_hub"}
    )


def test_manifest_unknown_post_type_is_dropped(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    _write_role(
        tmp_path, monkeypatch, "widget",
        'label = "Widget"\n'
        '[[post_types]]\nname = "paper"\n'
        '[[post_types]]\nname = "nonsense"\n',
    )
    spec = load_role("widget")
    assert [s.name for s in spec.post_types] == ["paper"]
    assert "nonsense" in caplog.text


def test_malformed_toml_still_yields_default_post_types(tmp_path, monkeypatch):
    from src.agent.post_types import DEFAULT_POST_TYPES

    _write_role(tmp_path, monkeypatch, "broken", "label = = =\n")
    assert load_role("broken").post_types == DEFAULT_POST_TYPES



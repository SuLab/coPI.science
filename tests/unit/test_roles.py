import logging
from pathlib import Path

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


# ----------------------------------------------------------------------
# scout_hub role content (Task 9) — uses the REAL prompts/roles dir, no
# monkeypatch, so this exercises the actual shipped role.toml / phase5
# override on disk.
# ----------------------------------------------------------------------

from src.agent.roles import load_role as _load_role_real  # no monkeypatch


def test_scout_hub_ships_with_the_hub_tool_set():
    spec = _load_role_real("scout_hub")
    assert spec.label == "Scout Hub"
    assert "search_prior_art" in spec.tools
    assert "retrieve_foa" not in spec.tools  # GrantBot fetches FOAs, not the hub


def test_scout_hub_phase5_override_renders_in_both_modes():
    """build_phase5_prompt loads phase5-new-post.md through the role-aware
    _load_prompt() (see src/agent/agent.py), so a scout_hub agent must pick up
    prompts/roles/scout_hub/phase5-new-post.md, not the global pi_lab template.

    This guards the byte-for-byte scaffolding that build_phase5_prompt's
    .replace()/regex substitution depends on:
      - the four substitution tokens are each replaced exactly once
      - the funding_only regexes (keyed to '## Your subscribed channels',
        '## Your recent posts', '## Prior conversations with other labs',
        and the 'Option C ... Option D' block) still find their targets
        in the scout_hub override, in both normal and funding_only mode.
    """
    from src.agent.agent import Agent

    agent = Agent("blackbird", "BlackbirdBot", "Blackbird Labs", role="scout_hub")

    leftover_tokens = [
        "{interesting_posts}",
        "{subscribed_channels}",
        "{your_recent_posts}",
        "{prior_conversations}",
    ]

    for funding_only in (False, True):
        system_prompt, messages = agent.build_phase5_prompt(
            recent_posts=[{"channel": "general", "content_snippet": "an old post"}],
            foa_contexts={},
            thread_foa_contexts={"RFA-AI-27-019": "Example FOA text"},
            prior_threads={
                "wiseman": [
                    {"channel": "general", "outcome": "no_proposal", "summary": "n/a"}
                ]
            },
            funding_only=funding_only,
            funding_thread_summaries={},
        )
        assert isinstance(system_prompt, str)
        content = messages[0]["content"]

        # All four tokens were substituted — none survive as raw placeholders.
        for token in leftover_tokens:
            assert token not in content, (
                f"leftover token {token!r} in scout_hub phase5 prompt "
                f"(funding_only={funding_only})"
            )

        # Confirms the scout_hub override actually rendered (not a silent
        # fallback to the global pi_lab template).
        assert "As the Blackbird scouting hub" in content

    # funding_only=True must strip Option C (the regular new-post artifact)
    # while keeping Option D (skip) — this is the hardcoded regex in
    # agent.py keyed to these exact headings.
    _, funding_only_messages = agent.build_phase5_prompt(funding_only=True)
    funding_only_content = funding_only_messages[0]["content"]
    assert "### Option C: Make a new top-level post" not in funding_only_content
    assert "### Option D: Skip this turn" in funding_only_content
    assert "## Your subscribed channels" not in funding_only_content
    assert "## Your recent posts" not in funding_only_content
    assert "## Prior conversations with other labs" not in funding_only_content

    # Non-funding_only mode keeps the full option set, including the
    # opportunity-assessment artifact instructions.
    _, normal_messages = agent.build_phase5_prompt()
    normal_content = normal_messages[0]["content"]
    assert "### Option C: Make a new top-level post" in normal_content
    assert ":mag: **Opportunity Assessment**" in normal_content


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

from src.agent.agent import Agent
from src.agent.roles import DEFAULT_ROLE


def _agent():
    return Agent(agent_id="su", bot_name="SuBot", pi_name="Andrew Su")


def test_default_role_is_pi_lab():
    assert _agent().role == DEFAULT_ROLE


def test_identity_block_is_present_and_substituted():
    prompt = _agent().build_system_prompt()
    assert "You are **SuBot**" in prompt
    assert 'the Andrew Su lab' in prompt
    assert 'Scripps Research' not in prompt
    assert 'agent ID is "su"' in prompt


def test_curly_brace_in_profile_does_not_crash(tmp_path, monkeypatch):
    # A profile containing a bare "{" must not raise (str.replace, not str.format).
    a = _agent()
    monkeypatch.setattr(type(a), "public_profile", property(lambda self: "budget is {tight}"))
    prompt = a.build_system_prompt()  # must not raise
    assert "budget is {tight}" in prompt


def test_phase4_honours_role_overrides(tmp_path, monkeypatch):
    """build_phase4_prompt loads its template via a hardcoded global path rather
    than the role-aware resolver, so a role's override file would be accepted into
    the repo and then silently ignored. Pin that it now resolves through
    Agent._load_prompt (and therefore src.agent.roles.resolve_prompt_path)."""
    from src.agent import roles as roles_mod
    from src.agent.agent import Agent
    from src.agent.state import ThreadState

    prompts = tmp_path / "prompts"
    (prompts / "roles" / "widget").mkdir(parents=True)
    (prompts / "phase4-thread-reply.md").write_text("GLOBAL REPLY", encoding="utf-8")
    (prompts / "roles" / "widget" / "phase4-thread-reply.md").write_text(
        "WIDGET REPLY", encoding="utf-8"
    )
    monkeypatch.setattr(roles_mod, "PROMPTS_DIR", prompts)
    monkeypatch.setattr(roles_mod, "ROLES_DIR", prompts / "roles")

    agent = Agent("w", "WBot", "W Lab", role="widget")
    thread = ThreadState(thread_id="t1", channel="general", other_agent_id="o", message_count=1)
    _, reply_messages = agent.build_phase4_prompt(
        thread=thread,
        thread_history=[{"sender": "o", "content": "hello"}],
        other_agent_name="OBot",
        other_agent_lab="O Lab",
    )
    assert "WIDGET REPLY" in reply_messages[0]["content"]


def test_phase5_menu_token_is_always_substituted():
    """No caller may leak the raw token into a prompt. prompts/ is bind-mounted
    and re-read per call while src/ is baked into the agent image, so a template
    that ships ahead of its renderer would put `{post_type_menu}` in front of a
    live model."""
    from src.agent.agent import Agent

    a = Agent("gill", "GillBot", "Gill")
    _, messages = a.build_phase5_prompt()
    assert "{post_type_menu}" not in messages[0]["content"]


def test_phase5_menu_defaults_to_the_unfiltered_pi_lab_set():
    """Assert on the MENU's own rendering, not on bare names: the Option C body
    also mentions `idea_crosslab` and `pitch`, so `name in content` would pass
    with no menu rendered at all."""
    from src.agent.agent import Agent
    from src.agent.post_types import DEFAULT_POST_TYPES

    a = Agent("gill", "GillBot", "Gill")
    _, messages = a.build_phase5_prompt()
    content = messages[0]["content"]
    for spec in DEFAULT_POST_TYPES:
        assert f"**`{spec.name}`**" in content


def test_phase5_default_menu_is_the_agents_own_role_not_pi_lab():
    """A scout_hub agent must not be handed a menu offering `paper`,
    `idea_crosslab` and `pitch` — its role.toml allows none of them.

    The hub went reply-only (Option A relocation): it declares no post
    types at all anymore (`post_types = []` in role.toml — its former
    `opportunity_assessment` is the sidecar carried inside its own Phase-4
    CONCLUDE reply now, not a post type), so its default-rendered Phase-5
    menu is the empty-menu message, not an enumeration of anything.
    """
    from src.agent.agent import Agent

    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    _, messages = hub.build_phase5_prompt()
    content = messages[0]["content"]
    assert "No new top-level post type is available to you this turn" in content
    for forbidden in (
        "**`paper`**", "**`idea_crosslab`**", "**`pitch`**",
        "**`opportunity_assessment`**",
    ):
        assert forbidden not in content


def test_phase5_default_menu_never_prints_an_empty_enumeration():
    """The default path has no roster to enumerate from. Guarded here as well as
    in test_post_types because this is the caller that reaches a snapshot."""
    from src.agent.agent import Agent

    a = Agent("gill", "GillBot", "Gill")
    _, messages = a.build_phase5_prompt()
    assert "one of: ." not in messages[0]["content"]


def test_phase5_menu_uses_the_caller_supplied_text_when_given():
    from src.agent.agent import Agent

    a = Agent("gill", "GillBot", "Gill")
    _, messages = a.build_phase5_prompt(post_type_menu="- ONLY THIS ONE")
    content = messages[0]["content"]
    assert "- ONLY THIS ONE" in content
    # The rendered menu is gone; the Option C prose that *names* the types is
    # not, and must not be — that is the per-type guidance.
    assert "**`idea_crosslab`**" not in content

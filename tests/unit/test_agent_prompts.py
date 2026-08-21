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


def test_phase4_prompt_at_prior_count_4_receives_decide_not_explore():
    """Real-path pin for the EXPLORE/DECIDE boundary (thread_guidance.py:
    ordinal<=4 -> EXPLORE, else DECIDE). A thread with 4 EXISTING messages
    generates its 5th reply — ordinal 5, one past the boundary — so
    build_phase4_prompt must feed phase4_guidance the ordinal
    (thread.message_count + 1), not the prior count itself, or this reply is
    silently misclassified as EXPLORE (the exact bug the ordinal fix in
    Agent.build_phase4_prompt corrected). test_thread_guidance.py's
    test_phase_boundaries_are_unchanged already pins phase4_guidance(role, 5)
    directly; this test is about the engine-side +1 wiring into it, driven
    through the real build_phase4_prompt path rather than calling
    phase4_guidance itself.
    """
    from src.agent.state import ThreadState
    from src.agent.thread_guidance import phase4_guidance

    agent = Agent(agent_id="su", bot_name="SuBot", pi_name="Andrew Su")
    thread = ThreadState(
        thread_id="t1", channel="general", other_agent_id="o", message_count=4,
    )
    _, reply_messages = agent.build_phase4_prompt(
        thread=thread,
        thread_history=[{"sender": "o", "content": "hello"}],
        other_agent_name="OBot",
        other_agent_lab="O Lab",
    )
    prompt = reply_messages[0]["content"]

    assert "**Thread phase:** DECIDE" in prompt
    assert "**Thread phase:** EXPLORE" not in prompt
    assert "**Message count:** 5 of 12 max" in prompt

    # Cross-check against the real DECIDE guidance/instructions text for this
    # role, so this test cannot pass on a stale {phase_guidance}/{instructions}
    # substitution left over from EXPLORE.
    _, decide_guidance, decide_instructions = phase4_guidance(agent.role, 5)
    assert decide_guidance in prompt
    assert decide_instructions in prompt


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


def test_phase5_prior_threads_render_is_capped(tmp_path, monkeypatch):
    from src.agent.agent import PRIOR_THREADS_RENDERED_PER_PAIR, Agent

    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    agent = Agent("su", "SuBot", "Su", role="pi_lab")
    prior = {
        "wang": [
            {"channel": "general", "outcome": "no_proposal", "summary": f"s{i}"}
            for i in range(8)
        ]
    }
    _, messages = agent.build_phase5_prompt(prior_threads=prior)
    body = "\n".join(m["content"] for m in messages)
    for i in range(3, 8):
        assert f"s{i}" in body            # the 5 most recent render
    for i in range(3):
        assert f"s{i}" not in body        # older ones do not
    assert "3 earlier closed threads with this agent not shown" in body
    assert PRIOR_THREADS_RENDERED_PER_PAIR == 5

    small = {"wang": prior["wang"][:3]}
    _, messages = agent.build_phase5_prompt(prior_threads=small)
    body = "\n".join(m["content"] for m in messages)
    assert "not shown" not in body        # no banner under the cap

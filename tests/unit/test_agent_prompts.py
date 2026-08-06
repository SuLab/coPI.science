from src.agent.agent import Agent
from src.agent.roles import DEFAULT_ROLE


def _agent():
    return Agent(agent_id="su", bot_name="SuBot", pi_name="Andrew Su")


def test_default_role_is_pi_lab():
    assert _agent().role == DEFAULT_ROLE


def test_identity_block_is_present_and_substituted():
    prompt = _agent().build_scan_system_prompt()
    assert "You are **SuBot**" in prompt
    assert 'the Andrew Su lab at Scripps Research' in prompt
    assert 'agent ID is "su"' in prompt


def test_curly_brace_in_profile_does_not_crash(tmp_path, monkeypatch):
    # A profile containing a bare "{" must not raise (str.replace, not str.format).
    a = _agent()
    monkeypatch.setattr(type(a), "public_profile", property(lambda self: "budget is {tight}"))
    prompt = a.build_scan_system_prompt()  # must not raise
    assert "budget is {tight}" in prompt


def test_scan_prompt_omits_memory_and_lab_directory():
    a = _agent()
    a._lab_directory = "### Other Lab\n- paper"
    scan = a.build_scan_system_prompt()
    assert "Other Lab" not in scan  # scan prompt excludes the directory


def test_phase2_and_phase4_honour_role_overrides(tmp_path, monkeypatch):
    """Every other phase resolves per-role; these two were hardcoded to the global
    file, so a role override was accepted into the repo and then ignored."""
    from src.agent import roles as roles_mod
    from src.agent.agent import Agent
    from src.agent.state import ThreadState

    prompts = tmp_path / "prompts"
    (prompts / "roles" / "widget").mkdir(parents=True)
    (prompts / "phase2-scan-filter.md").write_text("GLOBAL SCAN {posts}", encoding="utf-8")
    (prompts / "phase4-thread-reply.md").write_text("GLOBAL REPLY", encoding="utf-8")
    (prompts / "roles" / "widget" / "phase2-scan-filter.md").write_text(
        "WIDGET SCAN {posts}", encoding="utf-8"
    )
    (prompts / "roles" / "widget" / "phase4-thread-reply.md").write_text(
        "WIDGET REPLY", encoding="utf-8"
    )
    monkeypatch.setattr(roles_mod, "PROMPTS_DIR", prompts)
    monkeypatch.setattr(roles_mod, "ROLES_DIR", prompts / "roles")

    agent = Agent("w", "WBot", "W Lab", role="widget")
    _, scan_messages = agent.build_phase2_scan_prompt(
        [{"post_id": "p1", "channel": "general", "sender": "x", "content_snippet": "s"}]
    )
    assert "WIDGET SCAN" in scan_messages[0]["content"]

    thread = ThreadState(thread_id="t1", channel="general", other_agent_id="o", message_count=1)
    _, reply_messages = agent.build_phase4_prompt(
        thread=thread,
        thread_history=[{"sender": "o", "content": "hello"}],
        other_agent_name="OBot",
        other_agent_lab="O Lab",
    )
    assert "WIDGET REPLY" in reply_messages[0]["content"]

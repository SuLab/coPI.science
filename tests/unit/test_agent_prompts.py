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

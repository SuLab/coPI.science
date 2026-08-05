import pytest

from src.agent.tools import execute_tool, tools_for_role


def test_pi_lab_tool_list_excludes_hub_only_tools():
    names = {t["name"] for t in tools_for_role("pi_lab")}
    assert "retrieve_profile" in names
    assert "search_prior_art" not in names  # true before Task 7; still true after


@pytest.mark.asyncio
async def test_executor_refuses_a_tool_not_in_the_role():
    # retrieve_foa is a pi_lab tool; ask a hypothetical role that lacks it.
    # Use a role dir that does not exist -> DEFAULT_TOOLS (has retrieve_foa),
    # so instead assert refusal via a role we can pin: monkeypatch load_role.
    from src.agent import tools as tools_mod
    from src.agent.roles import RoleSpec

    orig = tools_mod.load_role
    tools_mod.load_role = lambda name: RoleSpec(name=name, label=name, tools=frozenset({"retrieve_profile"}))
    try:
        out = await execute_tool("retrieve_foa", {"foa_number": "PA-24-1"}, "su", None, role="locked")
    finally:
        tools_mod.load_role = orig
    assert "not available" in out.lower()

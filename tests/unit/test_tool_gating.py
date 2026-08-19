import pytest

from src.agent.tools import execute_tool, tools_for_role


def test_pi_lab_tool_list_excludes_hub_only_tools():
    names = {t["name"] for t in tools_for_role("pi_lab")}
    assert "retrieve_profile" in names
    assert "search_prior_art" not in names  # true before Task 7; still true after


@pytest.mark.asyncio
async def test_executor_refuses_a_tool_not_in_the_role():
    # retrieve_abstract is a pi_lab tool; ask a hypothetical role that lacks it.
    # Use a role dir that does not exist -> DEFAULT_TOOLS (has retrieve_abstract),
    # so instead assert refusal via a role we can pin: monkeypatch load_role.
    from src.agent import tools as tools_mod
    from src.agent.roles import RoleSpec

    orig = tools_mod.load_role
    tools_mod.load_role = lambda name: RoleSpec(name=name, label=name, tools=frozenset({"retrieve_profile"}))
    try:
        out = await execute_tool("retrieve_abstract", {"pmid_or_doi": "12345678"}, "su", None, role="locked")
    finally:
        tools_mod.load_role = orig
    assert "not available" in out.lower()


# --- consult_specialist ------------------------------------------------------

def test_consult_specialist_is_a_hub_tool_only():
    hub = {t["name"] for t in tools_for_role("scout_hub")}
    pi = {t["name"] for t in tools_for_role("pi_lab")}
    assert "consult_specialist" in hub
    assert "consult_specialist" not in pi


def test_the_tool_description_enumerates_the_eight_domains():
    """The model picks the domain from this description; if a domain is missing
    from it, that specialist is unreachable no matter what the enum allows."""
    from src.agent.specialists import SPECIALIST_DOMAINS
    from src.agent.tools import TOOL_DEFINITIONS

    tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "consult_specialist")
    enum = tool["input_schema"]["properties"]["domain"]["enum"]
    assert set(enum) == set(SPECIALIST_DOMAINS)
    for domain in SPECIALIST_DOMAINS:
        assert domain in tool["description"]


async def test_an_unknown_domain_is_refused_without_an_llm_call(monkeypatch):
    from src.agent import tools as tools_mod

    called = False

    async def _boom(**kwargs):
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(tools_mod, "generate_agent_response", _boom)
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "astrology", "question": "?", "context": ""},
        "blackbird", None, role="scout_hub",
    )
    assert "astrology" in out
    assert "scientific" in out  # names the valid domains
    assert called is False


async def test_a_missing_persona_file_does_not_report_a_consult(monkeypatch, tmp_path):
    """A persona file that isn't there must not satisfy the floor."""
    from src.agent import specialists as spec_mod
    from src.agent import tools as tools_mod

    monkeypatch.setattr(spec_mod, "SPECIALISTS_DIR", tmp_path)
    seen = []
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "legal", "question": "FTO?", "context": ""},
        "blackbird", None, role="scout_hub", on_consult=seen.append,
    )
    assert "legal" in out.lower()
    assert seen == []


async def test_a_successful_consult_reports_the_domain(monkeypatch):
    from src.agent import tools as tools_mod

    async def _fake(**kwargs):
        return '{"verdict_signal": "clear", "concerns": [], ' \
               '"questions_to_ask": [], "confidence": "high"}'

    monkeypatch.setattr(tools_mod, "generate_agent_response", _fake)
    seen = []
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "legal", "question": "FTO?", "context": "..."},
        "blackbird", None, role="scout_hub", on_consult=seen.append,
    )
    assert seen == ["legal"]
    assert "clear" in out


async def test_a_failed_llm_call_does_not_report_a_consult(monkeypatch):
    from src.agent import tools as tools_mod

    async def _fail(**kwargs):
        raise RuntimeError("upstream 529")

    monkeypatch.setattr(tools_mod, "generate_agent_response", _fail)
    seen = []
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "legal", "question": "FTO?", "context": ""},
        "blackbird", None, role="scout_hub", on_consult=seen.append,
    )
    assert seen == []
    assert "error" in out.lower()


async def test_a_pi_lab_agent_cannot_consult(monkeypatch):
    from src.agent import tools as tools_mod

    called = False

    async def _boom(**kwargs):
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(tools_mod, "generate_agent_response", _boom)
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "legal", "question": "?", "context": ""},
        "gill", None, role="pi_lab",
    )
    assert "not available" in out
    assert called is False


def test_the_tool_description_names_every_specialist_domain():
    """Two sources of truth for the panel's domains: SPECIALIST_DOMAINS and the
    hardcoded prose in the consult_specialist tool description. Nothing pinned
    them together, so adding a ninth domain could leave the hub never told.

    The description is hub-facing text and frozen by D6 — this test pins the
    agreement that already exists, it does not license changing either side.
    """
    from src.agent.specialists import SPECIALIST_DOMAINS
    from src.agent.tools import TOOL_DEFINITIONS

    tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "consult_specialist")
    description = tool["description"]
    enum = tool["input_schema"]["properties"]["domain"]["enum"]

    assert set(enum) == set(SPECIALIST_DOMAINS)
    for domain in SPECIALIST_DOMAINS:
        assert f"'{domain}'" in description, (
            f"{domain} is dispatchable but the hub is never told it exists"
        )

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
        "blackbird", None, role="scout_hub",
        on_consult=lambda domain, signal: seen.append(domain),
    )
    assert "legal" in out.lower()
    assert seen == []


async def test_a_successful_consult_reports_the_domain(monkeypatch):
    from src.agent import tools as tools_mod

    async def _fake(**kwargs):
        # A LIVE label. `clear` here read as a successful consult only because
        # the tool result echoes `opinion.raw`, so the assertion below passed on
        # the specialist's own text while `parse_opinion` had defaulted the
        # signal — see test_consult_accounting's
        # `test_the_live_path_refuses_a_retired_verdict_label`.
        return '{"verdict_signal": "adequate", "concerns": [], ' \
               '"questions_to_ask": [], "confidence": "high"}'

    monkeypatch.setattr(tools_mod, "generate_agent_response", _fake)
    seen = []
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "legal", "question": "FTO?", "context": "..."},
        "blackbird", None, role="scout_hub",
        on_consult=lambda domain, signal: seen.append(domain),
    )
    assert seen == ["legal"]
    assert "adequate" in out


async def test_a_failed_llm_call_does_not_report_a_consult(monkeypatch):
    from src.agent import tools as tools_mod

    async def _fail(**kwargs):
        raise RuntimeError("upstream 529")

    monkeypatch.setattr(tools_mod, "generate_agent_response", _fail)
    seen = []
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "legal", "question": "FTO?", "context": ""},
        "blackbird", None, role="scout_hub",
        on_consult=lambda domain, signal: seen.append(domain),
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


# ---------------------------------------------------------------------------
# Missing tool arguments. A schema marking a field `required` constrains the
# model; it does not guarantee the field. Observed in production 2026-08-19
# 15:12 — the hub called consult_specialist with `domain` and `question` but no
# `context`, and `tool_input["context"]` raised a bare KeyError that reached the
# model as `Error executing consult_specialist: 'context'`. Zero occurrences
# across four preceding Sonnet 4.6 runs; one within ten minutes of Opus 5.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_consult_without_context_still_runs(monkeypatch):
    """`context` is grounding, not the ask — it must degrade, not lose the consult.

    Losing it costs the panel a domain, and on an advance/conditional verdict
    that flags the whole assessment. An opinion from a bare question is worth
    more than no opinion.
    """
    from src.agent import tools as tools_mod

    seen: list[str] = []

    async def _fake(**kwargs):
        seen.append(kwargs["messages"][0]["content"])
        return '{"verdict_signal": "gap", "confidence": "moderate"}'

    monkeypatch.setattr(tools_mod, "generate_agent_response", _fake)
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "chemistry", "question": "Is the series tractable?"},  # no context
        "blackbird", None, role="scout_hub",
    )

    assert seen, "the consult must still reach the model"
    assert "Is the series tractable?" in seen[0]
    assert "gap" in out
    assert "missing required" not in out


@pytest.mark.asyncio
async def test_a_consult_without_a_question_says_what_is_missing(monkeypatch):
    """A genuinely un-defaultable argument must produce a message the model can act on.

    A bare KeyError surfaced as `'question'`, which names no tool, no parameter
    and no remedy — so the model could not correct the call.
    """
    from src.agent import tools as tools_mod

    called = False

    async def _boom(**kwargs):
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(tools_mod, "generate_agent_response", _boom)
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "chemistry"},  # no question
        "blackbird", None, role="scout_hub",
    )

    assert "question" in out
    assert "missing required argument" in out
    assert "again" in out.lower(), "must tell the model to retry with the argument"
    assert called is False, "no API call should be billed for a malformed call"


@pytest.mark.asyncio
async def test_a_blank_required_argument_is_treated_as_missing(monkeypatch):
    """An empty string is not a question. Whitespace-only must fail the same way."""
    from src.agent import tools as tools_mod

    async def _boom(**kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("should not call the model")

    monkeypatch.setattr(tools_mod, "generate_agent_response", _boom)
    out = await tools_mod.execute_tool(
        "consult_specialist",
        {"domain": "chemistry", "question": "   "},
        "blackbird", None, role="scout_hub",
    )
    assert "missing required argument" in out


@pytest.mark.asyncio
async def test_other_tools_also_name_their_missing_argument():
    """The same fragility existed on every tool that read tool_input[...] directly."""
    from src.agent import tools as tools_mod

    for tool, arg in (
        ("retrieve_profile", "agent_id"),
        ("retrieve_abstract", "pmid_or_doi"),
        ("search_prior_art", "query"),
    ):
        out = await tools_mod.execute_tool(tool, {}, "blackbird", None, role="scout_hub")
        assert arg in out, f"{tool} must name the missing {arg}"
        assert "missing required argument" in out


# ---------------------------------------------------------------------------
# retrieve_abstract / retrieve_full_text: charge the per-thread budget on
# SUCCESS, not on attempt (issue #23 COR-30). Before this fix the debit
# landed before the fetch, so a PubMed outage or transient failure consumed
# a thread's retrieval budget with no refund.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_abstract_fetch_does_not_consume_the_budget(monkeypatch):
    from src.agent import tools
    from src.agent.state import ThreadState

    thread = ThreadState(thread_id="t", channel="c", other_agent_id="x")

    async def failing_fetch(ref):
        return {"error": "PubMed lookup failed"}
    monkeypatch.setattr(tools, "fetch_abstract", failing_fetch)

    out = await tools.execute_tool(
        "retrieve_abstract", {"pmid_or_doi": "12345"}, "su",
        thread_state=thread, role="pi_lab",
    )
    assert "failed" in out
    assert thread.abstracts_other == 0, "a failed fetch consumed the budget"

    async def ok_fetch(ref):
        return {"pmid": "12345", "title": "T", "abstract": "A"}
    monkeypatch.setattr(tools, "fetch_abstract", ok_fetch)
    out = await tools.execute_tool(
        "retrieve_abstract", {"pmid_or_doi": "12345"}, "su",
        thread_state=thread, role="pi_lab",
    )
    assert "Title:" in out
    assert thread.abstracts_other == 1


@pytest.mark.asyncio
async def test_over_cap_abstract_call_is_refused_without_fetching(monkeypatch):
    from src.agent import tools
    from src.agent.state import ThreadState
    from src.config import get_settings

    thread = ThreadState(thread_id="t", channel="c", other_agent_id="x")
    thread.abstracts_other = get_settings().max_abstracts_other_per_thread
    fetched = []

    async def spy_fetch(ref):
        fetched.append(ref)
        return {"pmid": "1", "title": "T"}
    monkeypatch.setattr(tools, "fetch_abstract", spy_fetch)
    out = await tools.execute_tool(
        "retrieve_abstract", {"pmid_or_doi": "1"}, "su",
        thread_state=thread, role="pi_lab",
    )
    assert "Rate limit" in out
    assert fetched == [], "an over-cap call must not reach the network"

"""Project-thread tools (spec §5.8): `tools_for_thread`, the `execute_tool` gate and
dispatch, `format_prior_art`, and the consult's logged phase."""

from types import SimpleNamespace

import pytest

from src.agent import tools as tools_mod
from src.agent.project_guidance import THREAD_KIND_INTERVIEW, THREAD_KIND_PROJECT
from src.agent.tool_definitions import PROJECT_TOOL_DEFINITIONS
from src.agent.tools import (
    _consult_thread_phase,
    _execute_search_prior_art,
    execute_tool,
    format_prior_art,
    tools_for_role,
    tools_for_thread,
)
from src.services import project_search
from src.services.patents import PriorArtResult

PROJECT_NAMES = ["search_pubmed", "search_trials"]


def _names(tools):
    return [t["name"] for t in tools]


def test_interview_tools_are_the_role_tools():
    assert tools_for_thread("scout_hub", THREAD_KIND_INTERVIEW) == tools_for_role("scout_hub")
    assert tools_for_thread("pi_lab", THREAD_KIND_INTERVIEW) == tools_for_role("pi_lab")


def test_a_hub_in_a_project_thread_gets_the_two_searches():
    tools = tools_for_thread("scout_hub", THREAD_KIND_PROJECT)
    assert _names(tools) == _names(tools_for_role("scout_hub")) + PROJECT_NAMES
    assert tools[-2:] == PROJECT_TOOL_DEFINITIONS


def test_a_lab_gets_nothing_extra():
    assert tools_for_thread("pi_lab", THREAD_KIND_PROJECT) == tools_for_role("pi_lab")


def test_the_role_list_is_not_mutated():
    before = _names(tools_for_role("scout_hub"))
    tools_for_thread("scout_hub", THREAD_KIND_PROJECT)
    assert _names(tools_for_role("scout_hub")) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", PROJECT_NAMES)
async def test_searches_are_refused_in_an_interview(tool, monkeypatch):
    called = []

    async def _fake(query):
        called.append(query)
        return project_search.ToolResult(text="x", sources=())

    monkeypatch.setattr(project_search, tool, _fake)
    out = await execute_tool(tool, {"query": "q"}, "blackbird", None, role="scout_hub")
    assert "not available" in out
    assert called == []


@pytest.mark.asyncio
async def test_searches_are_refused_to_a_lab_in_a_project_thread(monkeypatch):
    out = await execute_tool(
        "search_pubmed", {"query": "q"}, "su", None, role="pi_lab",
        thread_kind=THREAD_KIND_PROJECT,
    )
    assert "not available" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", PROJECT_NAMES)
async def test_a_hub_in_a_project_thread_calls_the_search(tool, monkeypatch):
    called = []

    async def _fake(query):
        called.append(query)
        return project_search.ToolResult(text=f"{tool} result for {query}", sources=())

    monkeypatch.setattr(project_search, tool, _fake)
    out = await execute_tool(
        tool, {"query": "LOX-1 antibody"}, "blackbird", None, role="scout_hub",
        thread_kind=THREAD_KIND_PROJECT,
    )
    assert out == f"{tool} result for LOX-1 antibody"
    assert called == ["LOX-1 antibody"]


@pytest.mark.asyncio
async def test_a_pubmed_outage_is_answered_in_words(monkeypatch):
    async def _down(query):
        raise project_search.PubMedUnavailable("eutils down")

    monkeypatch.setattr(project_search, "search_pubmed", _down)
    out = await execute_tool(
        "search_pubmed", {"query": "q"}, "blackbird", None, role="scout_hub",
        thread_kind=THREAD_KIND_PROJECT,
    )
    assert out == "PubMed could not be searched just now."


@pytest.mark.asyncio
async def test_a_missing_query_is_a_tool_error(monkeypatch):
    out = await execute_tool(
        "search_trials", {}, "blackbird", None, role="scout_hub",
        thread_kind=THREAD_KIND_PROJECT,
    )
    assert "missing required argument 'query'" in out


# --- format_prior_art ---------------------------------------------------------

_HIT = {
    "patent_id": "US20260000001A1",
    "title": "Novel Widget",
    "date": "2026-01-01",
    "applicant": "Acme Corp",
    "inventor": "Jane Doe",
    "status": "Patented Case",
    "abstract": "A widget that does things.",
    "claim": "1. A widget comprising a thing.",
}


async def _value(v):
    return v


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    None,
    PriorArtResult([], [], 0),
    PriorArtResult([], ["crispr", "delivery"], 2),
    PriorArtResult([_HIT], ["widget"], 1),
])
async def test_format_prior_art_matches_the_tool_text(result, monkeypatch):
    monkeypatch.setattr(tools_mod, "search_prior_art", lambda q, limit=10: _value(result))
    assert format_prior_art(result) == await _execute_search_prior_art("q")


def test_format_prior_art_keeps_the_four_outcomes():
    """The expectations of tests/unit/test_patents.py, on the formatter directly."""
    unavailable = format_prior_art(None).lower()
    assert "unavailable" in unavailable and "novelty" in unavailable
    assert "no us filings matched" not in unavailable

    no_query = format_prior_art(PriorArtResult([], [], 0)).lower()
    assert "no search was performed" in no_query
    assert "no us filings matched" not in no_query

    empty = format_prior_art(PriorArtResult([], ["crispr", "delivery"], 2))
    assert "US filings only" in empty
    assert empty.endswith("No US filings matched this query.")

    hits = format_prior_art(PriorArtResult([_HIT], ["widget"], 1))
    for text in ("US filings only", "US20260000001A1", "Novel Widget", "Acme Corp", "Jane Doe",
                 "A widget that does things.", "1. A widget comprising a thing.", "<patent>"):
        assert text in hits


# --- the consult's logged phase -------------------------------------------------

def test_consult_phase_in_a_project_thread():
    thread = SimpleNamespace(message_count=5)
    assert _consult_thread_phase("scout_hub", thread, kind=THREAD_KIND_PROJECT) == "project_refine"
    assert _consult_thread_phase(
        "scout_hub", SimpleNamespace(message_count=11), kind=THREAD_KIND_PROJECT
    ) == "project_close"
    assert _consult_thread_phase("scout_hub", None, kind=THREAD_KIND_PROJECT) is None


def test_consult_phase_in_an_interview_is_unchanged():
    thread = SimpleNamespace(message_count=5)
    assert _consult_thread_phase("scout_hub", thread) == _consult_thread_phase(
        "scout_hub", thread, kind=THREAD_KIND_INTERVIEW
    )
    assert not _consult_thread_phase("scout_hub", thread).startswith("project")

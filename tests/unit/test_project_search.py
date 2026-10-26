"""The project research tools (spec 2026-10-22 §5.3, §5.8): PubMed, ClinicalTrials.gov and
patent search results fenced as untrusted text, each source keyed by the tool's kind."""

import httpx
import pytest

from src.agent.prompt_safety import FENCE_TAGS
from src.agent.tool_definitions import PROJECT_TOOL_DEFINITIONS, TOOL_DEFINITIONS
from src.services import project_search
from src.services.patents import PriorArtResult
from src.services.project_search import (
    PubMedUnavailable,
    research_tool_definitions,
    search_prior_art_text,
    search_pubmed,
    search_trials,
)


@pytest.fixture(autouse=True)
def _no_trial_spacing():
    project_search._TRIALS_PACER.reset()
    yield
    project_search._TRIALS_PACER.reset()


def test_fence_tags_added():
    assert {
        "project_brief", "project_post", "pubmed_record", "trial_record",
        "research_candidates", "earlier_projects",
    } <= FENCE_TAGS


def test_project_tool_definitions_are_separate():
    assert [d["name"] for d in TOOL_DEFINITIONS] == [
        "retrieve_profile", "retrieve_abstract", "retrieve_full_text", "search_prior_art",
        "consult_specialist",
    ]
    assert [d["name"] for d in PROJECT_TOOL_DEFINITIONS] == ["search_pubmed", "search_trials"]
    for definition in PROJECT_TOOL_DEFINITIONS:
        schema = definition["input_schema"]
        assert schema["required"] == ["query"]
        assert schema["properties"]["query"]["type"] == "string"


async def test_search_pubmed_formats_and_reports_sources(monkeypatch):
    calls = {}

    async def fake_search(term, retmax=200, sort="pub date"):
        calls["args"] = (term, retmax, sort)
        return ["111", "222"]

    async def fake_fetch(pmids, **_):
        return [
            {"pmid": "111", "year": 2020, "title": "First paper", "abstract": "A" * 2000},
            {"pmid": "222", "year": 2021, "title": "Second paper", "abstract": "Short."},
        ]

    monkeypatch.setattr("src.services.project_search.search_pmids", fake_search)
    monkeypatch.setattr("src.services.project_search.fetch_pubmed_records", fake_fetch)

    result = await search_pubmed("IBAT cholestasis")

    assert calls["args"] == ("IBAT cholestasis", 10, "relevance")
    assert result.text.count("<pubmed_record>") == 2
    assert "PMID 111" in result.text and "Second paper" in result.text
    assert "A" * 801 not in result.text
    assert [s["key"] for s in result.sources] == ["pmid:111", "pmid:222"]
    assert {s["kind"] for s in result.sources} == {"pubmed"}


async def test_search_pubmed_no_hits(monkeypatch):
    async def fake_search(term, retmax=200, sort="pub date"):
        return []

    monkeypatch.setattr("src.services.project_search.search_pmids", fake_search)
    result = await search_pubmed("nothing")
    assert result.text == "PubMed returned no papers for this query."
    assert result.sources == ()


async def test_search_pubmed_unavailable_raises(monkeypatch):
    async def failing(term, retmax=200, sort="pub date"):
        raise httpx.ConnectError("down")

    monkeypatch.setattr("src.services.project_search.search_pmids", failing)
    with pytest.raises(PubMedUnavailable):
        await search_pubmed("anything")


def _mock_client(monkeypatch, handler):
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        project_search.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw),
    )


async def test_search_trials_parses_v2_studies(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = request.url
        return httpx.Response(200, json={"studies": [{
            "protocolSection": {
                "identificationModule": {"nctId": "NCT01903460", "briefTitle": "LUM001 in PFIC"},
                "statusModule": {"overallStatus": "COMPLETED"},
                "conditionsModule": {"conditions": ["Cholestasis"]},
                "designModule": {"phases": ["PHASE2"]},
                "armsInterventionsModule": {"interventions": [{"type": "DRUG", "name": "LUM001"}]},
            },
        }]})

    _mock_client(monkeypatch, handler)
    result = await search_trials("PFIC")

    assert str(seen["url"]).startswith(project_search.CTGOV_STUDIES_URL)
    assert seen["url"].params["query.term"] == "PFIC"
    assert seen["url"].params["fields"] == project_search.TRIALS_FIELDS
    assert seen["url"].params["pageSize"] == "10"
    assert "<trial_record>" in result.text
    assert "NCT01903460" in result.text
    assert "DRUG: LUM001" in result.text
    assert "PHASE2" in result.text and "COMPLETED" in result.text
    assert [s["key"] for s in result.sources] == ["nct:NCT01903460"]
    assert result.sources[0]["kind"] == "trial"


async def test_search_trials_failure_is_reported_not_raised(monkeypatch):
    _mock_client(monkeypatch, lambda request: httpx.Response(429))
    result = await search_trials("PFIC")
    assert result.text == "ClinicalTrials.gov could not be searched just now."
    assert result.sources == ()


async def test_search_trials_unreadable_body_is_reported_not_raised(monkeypatch):
    _mock_client(monkeypatch, lambda request: httpx.Response(200, content=b"not json"))
    result = await search_trials("PFIC")
    assert result.text == "ClinicalTrials.gov could not be searched just now."
    assert result.sources == ()


async def test_prior_art_sources_and_could_not_run(monkeypatch):
    from src.agent.tools import format_prior_art

    async def none_result(query):
        return None

    monkeypatch.setattr("src.services.project_search.search_prior_art", none_result)
    result = await search_prior_art_text("TFEB melanoma")
    assert result.text == format_prior_art(None)
    assert result.sources == ()

    hit_result = PriorArtResult(
        hits=[{"patent_id": "US 11,234,567 B2", "title": "TFEB modulators"}],
        terms_used=["TFEB", "melanoma"],
        total_terms=2,
    )

    async def one_hit(query):
        return hit_result

    monkeypatch.setattr("src.services.project_search.search_prior_art", one_hit)
    result = await search_prior_art_text("TFEB melanoma")
    assert result.text == format_prior_art(hit_result)
    assert [s["key"] for s in result.sources] == ["patent:US11234567B2"]
    assert result.sources[0]["kind"] == "patent"


async def test_execute_research_tool_rejects_unknown_and_missing(monkeypatch):
    result = await project_search.execute_research_tool("retrieve_profile", {"query": "x"})
    assert result.text.startswith("Error:") and result.sources == ()
    result = await project_search.execute_research_tool("search_trials", {})
    assert result.text.startswith("Error:") and result.sources == ()
    result = await project_search.execute_research_tool("retrieve_abstract", {"query": "1"})
    assert result.text.startswith("Error:") and result.sources == ()


async def test_retrieve_abstract_text_error_and_source(monkeypatch):
    async def fake_abstract(ref):
        if ref == "bad":
            return {"error": "No PubMed record found for bad"}
        return {"pmid": "31500698", "title": "T", "abstract": "Abs", "journal": "J", "year": 2019}

    monkeypatch.setattr("src.services.project_search.fetch_abstract", fake_abstract)
    bad = await project_search.retrieve_abstract_text("bad")
    assert bad.text == "No PubMed record found for bad" and bad.sources == ()
    good = await project_search.execute_research_tool("retrieve_abstract", {"pmid_or_doi": "31500698"})
    assert "<paper_abstract>" in good.text
    assert [s["key"] for s in good.sources] == ["pmid:31500698"]


def test_research_tool_definitions_order_and_content():
    defs = research_tool_definitions()
    assert [d["name"] for d in defs] == [
        "search_pubmed", "search_trials", "search_prior_art", "retrieve_abstract",
    ]
    by_name = {d["name"]: d for d in TOOL_DEFINITIONS}
    assert defs[2] is by_name["search_prior_art"]
    assert defs[3] is by_name["retrieve_abstract"]
    assert defs[0] is PROJECT_TOOL_DEFINITIONS[0] and defs[1] is PROJECT_TOOL_DEFINITIONS[1]

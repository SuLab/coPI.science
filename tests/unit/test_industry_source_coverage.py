"""The registry's sources report coverage (spec 2026-10-05 §6.2, D12): a truncated fetch
is "truncated", an upstream failure is SourceUnavailable with a short reason, and the
PubMed COI source attributes with the PI's name and the gate's year."""
from types import SimpleNamespace

import httpx
import pytest
import respx

from src.services import pubmed
from src.services.industry_sources import Paged, ctgov, uspto_inventor
from src.services.industry_sources.registry import (
    CtGovSource,
    OpenAlexSource,
    PubMedCoiSource,
    SourceContext,
    SourceResult,
    SourceUnavailable,
    UsptoSource,
    _unavailable,
)


def _ctx(name="Gyanu Lamichhane"):
    user = SimpleNamespace(name=name, orcid="0000-0002-2214-0114")
    return SourceContext(user=user, tenure_start=2018, keywords={"tuberculosis"},
                         conditions={"lymphoma"}, pmids=["1"], year_by_pmid={"1": 2024})


async def test_a_truncated_uspto_fetch_reports_truncated(monkeypatch):
    async def fetched(name):
        return Paged([], truncated=True)
    monkeypatch.setattr(uspto_inventor, "fetch_jhu_applications", fetched)
    assert (await UsptoSource().fetch(_ctx())).coverage == "truncated"


async def test_a_complete_ctgov_fetch_reports_ok(monkeypatch):
    async def fetched(name):
        return Paged([])
    monkeypatch.setattr(ctgov, "fetch_jhu_industry_trials", fetched)
    assert (await CtGovSource().fetch(_ctx())).coverage == "ok"


async def test_the_no_key_refusal_passes_through_with_its_reason(monkeypatch):
    monkeypatch.setattr(uspto_inventor, "get_settings", lambda: type("S", (), {"uspto_api_key": ""})())
    with pytest.raises(SourceUnavailable) as raised:
        await UsptoSource().fetch(_ctx())
    assert raised.value.reason == "no_api_key"


def test_an_http_status_is_reported_by_code():
    request = httpx.Request("GET", "https://api.openalex.org/works")
    exc = httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
    assert _unavailable("openalex", exc).reason == "http_429"
    assert _unavailable("ctgov", httpx.ConnectTimeout("t")).reason == "ConnectTimeout"


async def test_pubmed_coi_attributes_with_the_pi_name_and_the_gate_year(monkeypatch):
    async def records(pmids, **kw):
        return [{"pmid": "1", "year": None, "coi_statement": "G.L. is a founder of Acme Bio.",
                 "authors": [{"last": "Lamichhane", "fore": "Gyanu", "initials": "G", "collective": None}]}]
    monkeypatch.setattr(pubmed, "fetch_pubmed_records", records)
    (item,) = (await PubMedCoiSource().fetch(_ctx())).items
    assert item.company_name == "Acme Bio" and item.year == 2024 and item.evidence["attributed"] is True


async def test_an_unusable_name_makes_pubmed_unavailable(monkeypatch):
    called = []

    async def records(pmids, **kw):
        called.append(pmids)
        return []

    monkeypatch.setattr(pubmed, "fetch_pubmed_records", records)
    with pytest.raises(SourceUnavailable) as raised:
        await PubMedCoiSource().fetch(_ctx(name="0000-0002-1825-0097"))
    assert raised.value.reason == "no_usable_name" and called == []


async def test_a_works_chunk_matching_more_than_it_returned_is_truncated(monkeypatch):
    from src.services.industry_sources import openalex_industry as oai

    async def works(pmids):
        return Paged([], truncated=True)

    async def none(ids):
        return set()

    monkeypatch.setattr(oai, "fetch_works_for_pmids", works)
    monkeypatch.setattr(oai, "company_funder_ids", none)
    assert (await OpenAlexSource().fetch(_ctx())).coverage == "truncated"


@respx.mock
async def test_fetch_works_reports_truncation_from_meta_count(monkeypatch):
    from src.services.industry_sources import openalex_industry as oai

    respx.get(f"{oai.OA}/works").mock(return_value=httpx.Response(
        200, json={"meta": {"count": 51}, "results": [{"id": f"https://openalex.org/W{n}"} for n in range(50)]}))
    paged = await oai.fetch_works_for_pmids([str(n) for n in range(50)])
    assert len(paged.items) == 50 and paged.truncated is True


async def test_openalex_reports_no_primary_field(monkeypatch):
    from src.services.industry_sources import openalex_industry as oai

    async def works(pmids):
        return Paged([{"id": "https://openalex.org/W1", "publication_year": 2024, "authorships": [],
                       "funders": [], "primary_topic": {"field": {"display_name": "Medicine"}}}])

    async def none(ids):
        return set()

    monkeypatch.setattr(oai, "fetch_works_for_pmids", works)
    monkeypatch.setattr(oai, "company_funder_ids", none)
    result = await OpenAlexSource().fetch(_ctx())
    assert result.coverage == "ok" and not hasattr(result, "primary_field")
    assert SourceResult().coverage == "ok"


@respx.mock
async def test_fetch_works_sends_100_pmids_a_request_with_room_for_two_works_each():
    from src.services.industry_sources import openalex_industry as oai

    route = respx.get(f"{oai.OA}/works").mock(return_value=httpx.Response(
        200, json={"meta": {"count": 0}, "results": []}))
    await oai.fetch_works_for_pmids([str(n) for n in range(150)])
    sizes = [len(c.request.url.params["filter"].removeprefix("pmid:").split("|")) for c in route.calls]
    assert sizes == [100, 50]
    assert {c.request.url.params["per-page"] for c in route.calls} == {"200"}

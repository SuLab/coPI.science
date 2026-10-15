"""The registry's sources report coverage (spec 2026-10-05 §6.2, D12): a truncated fetch
is "truncated", an upstream failure is SourceUnavailable with a short reason, and the
PubMed COI source attributes with the PI's name and the gate's year."""
import dataclasses
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


async def test_a_pmid_openalex_does_not_return_is_looked_up_by_its_doi(monkeypatch):
    """2026-10-10: OpenAlex filed two company co-authored articles under their preprints'
    PMIDs, so the PMID filter never returned them."""
    from src.services.industry_sources import openalex_industry as oai

    w1 = {"id": "https://openalex.org/W1", "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/1"}}
    w2 = {"id": "https://openalex.org/W2", "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/9"}}
    asked = []

    async def works(pmids):
        return Paged([w1])

    async def by_doi(dois, *, skipped=None):
        asked.append(list(dois))
        return [w1, w2, w2]

    async def none(ids):
        return set()

    monkeypatch.setattr(oai, "fetch_works_for_pmids", works)
    monkeypatch.setattr(oai, "fetch_works_for_dois", by_doi)
    monkeypatch.setattr(oai, "company_funder_ids", none)
    ctx = dataclasses.replace(_ctx(), pmids=["1", "2", "3"],
                              doi_by_pmid={"1": "10.1/a", "2": "10.1/b"})
    result = await OpenAlexSource().fetch(ctx)
    assert asked == [["10.1/b"]]  # "1" came back by PMID; "3" has no DOI
    assert result.note == "openalex works=2 items=0 by_doi=1" and result.coverage == "ok"


@respx.mock
async def test_fetch_works_for_dois_skips_malformed_and_unknown_dois(caplog):
    from src.services.industry_sources import openalex_industry as oai

    found = respx.get(f"{oai.OA}/works/doi:10.1038/s1").mock(
        return_value=httpx.Response(200, json={"id": "https://openalex.org/W1"}))
    gone = respx.get(f"{oai.OA}/works/doi:10.1101/gone").mock(return_value=httpx.Response(404))
    refused = respx.get(f"{oai.OA}/works/doi:10.1101/odd").mock(
        return_value=httpx.Response(400, text="Invalid DOI"))
    works = await oai.fetch_works_for_dois(
        ["10.1038/s1", "not-a-doi", "10.1101/gone", "10.1101/odd", "10.1038/s1"])
    assert works == [{"id": "https://openalex.org/W1"}]
    assert found.call_count == 1 and gone.call_count == 1 and refused.call_count == 1
    assert found.calls[0].request.url.params["select"] == oai._SELECT
    assert "OpenAlex refused DOI 10.1101/odd (400): Invalid DOI" in caplog.text


@respx.mock
async def test_a_doi_whose_lookup_keeps_failing_is_skipped(monkeypatch):
    from src.services import http_pacing
    from src.services.industry_sources import openalex_industry as oai

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(http_pacing, "_sleep", no_sleep)
    route = respx.get(f"{oai.OA}/works/doi:10.1038/s1").mock(return_value=httpx.Response(503))
    skipped: list[str] = []
    assert await oai.fetch_works_for_dois(["10.1038/s1"], skipped=skipped) == []
    assert skipped == ["10.1038/s1"] and route.call_count == 4


@respx.mock
async def test_fetch_works_sends_100_pmids_a_request_with_room_for_two_works_each():
    from src.services.industry_sources import openalex_industry as oai

    route = respx.get(f"{oai.OA}/works").mock(return_value=httpx.Response(
        200, json={"meta": {"count": 0}, "results": []}))
    await oai.fetch_works_for_pmids([str(n) for n in range(150)])
    sizes = [len(c.request.url.params["filter"].removeprefix("pmid:").split("|")) for c in route.calls]
    assert sizes == [100, 50]
    assert {c.request.url.params["per-page"] for c in route.calls} == {"200"}


@pytest.fixture
def _fast_openalex(monkeypatch):
    from src.services import http_pacing
    from src.services.http_pacing import Pacer
    from src.services.industry_sources import openalex_industry as oai

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(http_pacing, "_sleep", no_sleep)
    monkeypatch.setattr(oai, "_PACER", Pacer(0.0))


async def test_the_doi_fallback_looks_up_at_most_50_newest_first(monkeypatch):
    from src.services.industry_sources import openalex_industry as oai

    asked = []

    async def works(pmids):
        return Paged([])

    async def by_doi(dois, *, skipped=None):
        asked.append(list(dois))
        return []

    async def none(ids):
        return set()

    monkeypatch.setattr(oai, "fetch_works_for_pmids", works)
    monkeypatch.setattr(oai, "fetch_works_for_dois", by_doi)
    monkeypatch.setattr(oai, "company_funder_ids", none)
    pmids = [str(i) for i in range(1, 61)]
    ctx = dataclasses.replace(_ctx(), pmids=pmids,
                              year_by_pmid={p: 1960 + int(p) for p in pmids},
                              doi_by_pmid={p: f"10.1038/p{p}" for p in pmids})
    result = await OpenAlexSource().fetch(ctx)
    assert asked == [[f"10.1038/p{i}" for i in range(60, 10, -1)]]
    assert result.coverage == "truncated" and result.note.endswith("by_doi_capped=10")


@respx.mock
async def test_one_failing_doi_is_skipped_not_fatal(monkeypatch, _fast_openalex):
    from src.services.industry_sources import openalex_industry as oai

    work_b = {"id": "https://openalex.org/WB"}
    respx.get(f"{oai.OA}/works/doi:10.1038/a").mock(return_value=httpx.Response(403))
    respx.get(f"{oai.OA}/works/doi:10.1038/b").mock(return_value=httpx.Response(200, json=work_b))
    skipped: list[str] = []
    assert await oai.fetch_works_for_dois(["10.1038/a", "10.1038/b"], skipped=skipped) == [work_b]
    assert skipped == ["10.1038/a"]

    w1 = {"id": "https://openalex.org/W1", "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/1"}}

    async def works(pmids):
        return Paged([w1])

    async def none(ids):
        return set()

    monkeypatch.setattr(oai, "fetch_works_for_pmids", works)
    monkeypatch.setattr(oai, "company_funder_ids", none)
    ctx = dataclasses.replace(_ctx(), pmids=["1", "2", "3"],
                              doi_by_pmid={"2": "10.1038/a", "3": "10.1038/b"})
    result = await OpenAlexSource().fetch(ctx)
    assert result.coverage == "truncated"
    # works=2: the PMID-filter work W1 is kept alongside the DOI fallback's WB.
    assert result.note == "openalex works=2 items=0 by_doi=1 by_doi_skipped=1"


@respx.mock
async def test_three_identical_doi_failures_in_a_row_raise(monkeypatch, _fast_openalex):
    from src.services.industry_sources import openalex_industry as oai

    dois = ["10.1038/f1", "10.1038/f2", "10.1038/f3"]
    for doi in dois:
        respx.get(f"{oai.OA}/works/doi:{doi}").mock(return_value=httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        await oai.fetch_works_for_dois(dois)

    async def works(pmids):
        return Paged([])

    monkeypatch.setattr(oai, "fetch_works_for_pmids", works)
    ctx = dataclasses.replace(_ctx(), pmids=["1", "2", "3"],
                              doi_by_pmid={"1": dois[0], "2": dois[1], "3": dois[2]})
    with pytest.raises(SourceUnavailable) as exc:
        await OpenAlexSource().fetch(ctx)
    assert exc.value.reason == "http_403"


@respx.mock
async def test_alternating_doi_failures_do_not_trip_the_run(_fast_openalex):
    from src.services.industry_sources import openalex_industry as oai

    statuses = {"a1": 403, "b1": 200, "a2": 403, "b2": 200, "a3": 403}
    for name, status in statuses.items():
        body = {"json": {"id": f"https://openalex.org/{name}"}} if status == 200 else {}
        respx.get(f"{oai.OA}/works/doi:10.1038/{name}").mock(
            return_value=httpx.Response(status, **body))
    skipped: list[str] = []
    works = await oai.fetch_works_for_dois([f"10.1038/{n}" for n in statuses], skipped=skipped)
    assert len(works) == 2
    assert skipped == ["10.1038/a1", "10.1038/a2", "10.1038/a3"]


@respx.mock
async def test_a_429_after_retries_raises(_fast_openalex):
    from src.services.industry_sources import openalex_industry as oai

    respx.get(f"{oai.OA}/works/doi:10.1038/r1").mock(return_value=httpx.Response(429))
    with pytest.raises(httpx.HTTPStatusError):
        await oai.fetch_works_for_dois(["10.1038/r1"])

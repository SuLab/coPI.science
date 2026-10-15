"""R2.10 (EFetch reporting) and R2.11 (normalised, deduplicated DOI resolution) of the
2026-10-15 publication-handling design. NCBI runs through httpx.MockTransport."""
import asyncio
import logging

import httpx
import pytest

from src.services import pubmed


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(pubmed, "_pace_interval", lambda: 0.0)
    pubmed._PACER.reset()
    real_sleep = asyncio.sleep

    async def _instant(seconds):
        await real_sleep(0)

    monkeypatch.setattr(pubmed.asyncio, "sleep", _instant)


def _use(monkeypatch, handler):
    monkeypatch.setattr(pubmed, "_make_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _efetch_xml(pmids):
    arts = "".join(f"<PubmedArticle><MedlineCitation><PMID>{p}</PMID><Article><ArticleTitle>T{p}"
                   "</ArticleTitle></Article></MedlineCitation></PubmedArticle>" for p in pmids)
    return f'<?xml version="1.0"?><PubmedArticleSet>{arts}</PubmedArticleSet>'


def _messages(caplog, prefix):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


async def test_failed_nonstrict_batches_log_one_summary_error(monkeypatch, caplog):
    pmids = [str(31000000 + i) for i in range(150)]

    def handler(request):
        ids = request.url.params["id"].split(",")
        if ids[0] == pmids[100]:
            return httpx.Response(500)
        return httpx.Response(200, text=_efetch_xml(ids))

    _use(monkeypatch, handler)
    caplog.set_level(logging.ERROR, logger="src.services.pubmed")
    assert len(await pubmed.fetch_pubmed_records(pmids)) == 100
    assert _messages(caplog, "PubMed EFetch:") == [
        "PubMed EFetch: 50 of 150 PMIDs not fetched (1 of 2 batches failed)"]


async def test_missing_pmids_are_reported_not_dropped(monkeypatch, caplog):
    _use(monkeypatch, lambda request: httpx.Response(200, text=_efetch_xml(["31000000"])))
    caplog.set_level(logging.WARNING, logger="src.services.pubmed")
    dropped: list[str] = []
    records = await pubmed.fetch_pubmed_records(
        ["31000000", "31000001"], strict=True, permanently_dropped=dropped)
    assert len(records) == 1 and dropped == []
    [message] = _messages(caplog, "PubMed EFetch:")
    assert "1 of 2 requested PMIDs came back with no record" in message
    assert "first: 31000001" in message


async def test_efetch_batch_size(monkeypatch):
    sizes = []

    def handler(request):
        ids = request.url.params["id"].split(",")
        sizes.append(len(ids))
        return httpx.Response(200, text=_efetch_xml(ids))

    _use(monkeypatch, handler)
    await pubmed.fetch_pubmed_records([str(31000000 + i) for i in range(101)])
    assert sizes == [100, 1]


def _resolver(idconv_records=(), esearch_status=200, seen=None):
    seen = seen if seen is not None else []

    def handler(request):
        seen.append(request)
        if "idconv" in request.url.path:
            return httpx.Response(200, json={"records": list(idconv_records)})
        if request.url.path.endswith("esearch.fcgi"):
            if esearch_status != 200:
                return httpx.Response(esearch_status)
            return httpx.Response(200, json={"esearchresult": {"idlist": []}})
        raise AssertionError(f"unexpected request: {request.url}")

    return handler


async def test_doi_inputs_are_normalized_before_querying(monkeypatch):
    seen: list[httpx.Request] = []
    _use(monkeypatch, _resolver([{"doi": "10.1038/xyz", "pmid": "5"}], seen=seen))
    mapping = await pubmed.convert_dois_to_pmids(["https://doi.org/10.1038/XYZ"])
    assert [r.url.params["ids"] for r in seen] == ["10.1038/XYZ"]
    assert mapping == {"https://doi.org/10.1038/XYZ": "5"}


async def test_a_non_doi_input_makes_no_request(monkeypatch):
    seen: list[httpx.Request] = []
    _use(monkeypatch, _resolver(seen=seen))
    assert await pubmed.convert_dois_to_pmids(["N/A"]) == {}
    assert seen == []


async def test_case_variants_are_queried_once_and_both_mapped(monkeypatch):
    seen: list[httpx.Request] = []
    _use(monkeypatch, _resolver([{"doi": "10.1038/abc", "pmid": "1"}], seen=seen))
    mapping = await pubmed.convert_dois_to_pmids(["10.1038/ABC", "10.1038/abc"])
    assert [r.url.params["ids"] for r in seen] == ["10.1038/ABC"]
    assert mapping == {"10.1038/ABC": "1", "10.1038/abc": "1"}


async def test_a_dropped_query_reports_every_input(monkeypatch):
    _use(monkeypatch, _resolver(esearch_status=400))
    dropped: list[str] = []
    mapping = await pubmed.convert_dois_to_pmids(
        ["10.5555/x", "https://doi.org/10.5555/X"], strict=True, permanently_dropped=dropped)
    assert mapping == {}
    assert dropped == ["10.5555/x", "https://doi.org/10.5555/X"]


async def test_nonstrict_esearch_failures_log_one_warning(monkeypatch, caplog):
    _use(monkeypatch, _resolver(esearch_status=500))
    caplog.set_level(logging.WARNING, logger="src.services.pubmed")
    assert await pubmed.convert_dois_to_pmids(["10.1/a", "10.1/b"]) == {}
    assert _messages(caplog, "ESearch DOI lookup failed for") == [
        "ESearch DOI lookup failed for 2 of 2 DOIs (first: 10.1/a)"]

"""DP-06: at most N ESearch + ceil(N/100) EFetch requests for N unmapped DOIs, over one
client per ncbi_session; a per-item error in a batch falls back to single fetches so
one bad record loses only its own DOI (Review Focus 3); the shared systemic-run
counter still stops a run of identical failures."""
import math

import httpx
import pytest

from src.services import pubmed

pytestmark = pytest.mark.asyncio


def _xml(records):
    arts = "".join(
        f"<PubmedArticle><MedlineCitation><PMID>{p}</PMID><Article><ArticleTitle>T{p}</ArticleTitle>"
        f"<Journal><JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue><Title>J</Title></Journal>"
        f"<AuthorList><Author><LastName>Green</LastName><ForeName>Rachel</ForeName>"
        f"<AffiliationInfo><Affiliation>Johns Hopkins University</Affiliation></AffiliationInfo></Author></AuthorList>"
        f"<PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>"
        f"</Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType=\"doi\">{d}</ArticleId>"
        f"</ArticleIdList></PubmedData></PubmedArticle>"
        for p, d in records
    )
    return f"<?xml version='1.0'?><PubmedArticleSet>{arts}</PubmedArticleSet>"


class _Recorder:
    def __init__(self, bad_pmid=None):
        self.esearch = self.efetch = self.idconv = 0
        self.bad = bad_pmid
        self.clients = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "idconv" in url:
            self.idconv += 1
            return httpx.Response(200, json={"records": []})
        if "esearch" in url:
            self.esearch += 1
            doi = request.url.params["term"].removesuffix("[doi]")
            n = int(doi.rsplit(".", 1)[-1])
            return httpx.Response(200, json={"esearchresult": {"idlist": [str(1000 + n)]}})
        if "efetch" in url:
            self.efetch += 1
            ids = request.url.params["id"].split(",")
            if self.bad and self.bad in ids:
                return httpx.Response(400, text="bad id")
            return httpx.Response(200, text=_xml([(p, f"10.1000/x.{int(p) - 1000}") for p in ids]))
        return httpx.Response(404)


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(pubmed, "_pace_interval", lambda: 0.0)
    pubmed._PACER.reset()

    def make():
        rec.clients += 1
        return httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))

    monkeypatch.setattr(pubmed, "_make_client", make)
    return rec


async def test_request_counts_and_one_client(recorder):
    dois = [f"10.1000/x.{i}" for i in range(150)]
    async with pubmed.ncbi_session():
        mapping = await pubmed.convert_dois_to_pmids(dois, strict=True)
        verified = dict(pubmed.session_verified_records())
    assert mapping == {d: str(1000 + i) for i, d in enumerate(dois)}
    assert recorder.esearch <= 150
    assert recorder.efetch <= math.ceil(150 / 100)
    assert recorder.clients == 1
    assert set(verified) == {str(1000 + i) for i in range(150)}


async def test_no_session_means_no_collected_records(recorder):
    assert pubmed.session_verified_records() == {}
    await pubmed.convert_dois_to_pmids(["10.1000/x.1"], strict=True)
    assert pubmed.session_verified_records() == {}


async def test_batched_verification_per_item_error_loses_only_that_doi(recorder):
    recorder.bad = "1007"
    dropped: list[str] = []
    mapping = await pubmed.convert_dois_to_pmids(
        [f"10.1000/x.{i}" for i in range(10)], strict=True, permanently_dropped=dropped
    )
    assert "10.1000/x.7" not in mapping and len(mapping) == 9
    assert dropped == ["10.1000/x.7"]


async def test_a_round_trip_mismatch_is_a_miss(recorder, monkeypatch):
    def handler(request):
        if "esearch" in str(request.url):
            return httpx.Response(200, json={"esearchresult": {"idlist": ["1001"]}})
        return httpx.Response(200, text=_xml([("1001", "10.1000/other")]))

    monkeypatch.setattr(pubmed, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await pubmed.convert_dois_to_pmids(["10.1000/x.1"], strict=True) == {}

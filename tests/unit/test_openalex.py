"""OpenAlex works-by-ORCID client (corpus stage S2).

OpenAlex is often the largest discovery source for sparse-ORCID PIs (the
2026-08-13 rehearsal: gill had 0 ORCID works vs 33 via OpenAlex), which is
exactly the new-PI onboarding case. Identity is NOT trusted from OpenAlex —
its one rehearsal error was linking a stranger's paper to a PI's ORCID — so
the resolver puts every S2 candidate through the author-match gate; this
module only fetches and extracts identifiers.
"""

import httpx
import pytest

from src.services import openalex


def _client_factory(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _work(pmid=None, doi=None, year=None):
    ids = {}
    if pmid:
        ids["pmid"] = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}"
    if doi:
        ids["doi"] = f"https://doi.org/{doi}"
    return {"ids": ids, "publication_year": year, "title": "W"}


async def test_extracts_pmids_and_dois_across_cursor_pages(monkeypatch):
    pages = {
        "*": {
            "results": [_work(pmid="111", year=2020), _work(doi="10.1/x", year=2019)],
            "meta": {"next_cursor": "page2"},
        },
        "page2": {
            "results": [_work(pmid="222", doi="10.1/y", year=2021)],
            "meta": {"next_cursor": None},
        },
    }
    seen_params = []

    def handler(request):
        params = dict(request.url.params)
        seen_params.append(params)
        return httpx.Response(200, json=pages[params["cursor"]])

    monkeypatch.setattr(openalex, "_make_client", _client_factory(handler))
    works = await openalex.fetch_works_by_orcid("0000-0001-2345-6789")

    assert works == [
        {"pmid": "111", "doi": None, "year": 2020},
        {"pmid": None, "doi": "10.1/x", "year": 2019},
        {"pmid": "222", "doi": "10.1/y", "year": 2021},
    ]
    assert all(
        "0000-0001-2345-6789" in p["filter"] for p in seen_params
    )


async def test_a_non_200_raises_rather_than_returning_an_empty_corpus(monkeypatch):
    def handler(request):
        return httpx.Response(503, text="down")

    monkeypatch.setattr(openalex, "_make_client", _client_factory(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await openalex.fetch_works_by_orcid("0000-0001-2345-6789")

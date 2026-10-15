"""OpenAlex works-by-ORCID client (corpus stage S2).

OpenAlex is often the largest discovery source for sparse-ORCID PIs (the
2026-08-13 rehearsal: gill had 0 ORCID works vs 33 via OpenAlex), which is
exactly the new-PI onboarding case. Identity is NOT trusted from OpenAlex —
its one rehearsal error was linking a stranger's paper to a PI's ORCID — so
the resolver puts every S2 candidate PubMed has through the author-match gate.
Besides identifiers, the client now returns the title, type, venue, abstract and
retraction flag the non-PubMed corpus reads, and offers a free single-DOI lookup
(spec 2026-10-09 §5.2).
"""

import httpx
import pytest

from src.services import http_pacing, openalex


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

    def _expected(pmid, doi, year):
        return {"pmid": pmid, "doi": doi, "year": year, "title": "W", "type": None,
                "venue": None, "abstract": None, "is_retracted": False}

    assert works == [
        _expected("111", None, 2020),
        _expected(None, "10.1/x", 2019),
        _expected("222", "10.1/y", 2021),
    ]
    assert all("0000-0001-2345-6789" in p["filter"] for p in seen_params)
    assert all(p["select"] == openalex.WORK_FIELDS for p in seen_params)


async def test_a_non_200_raises_rather_than_returning_an_empty_corpus(monkeypatch):
    def handler(request):
        return httpx.Response(503, text="down")

    monkeypatch.setattr(openalex, "_make_client", _client_factory(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await openalex.fetch_works_by_orcid("0000-0001-2345-6789")


async def _instant(seconds):
    return None


def test_reconstruct_abstract_places_each_word_at_each_position():
    index = {"Safety": [0], "filters": [1, 4], "keep": [2], "robots": [3]}
    assert openalex.reconstruct_abstract(index) == "Safety filters keep robots filters"


def test_reconstruct_abstract_tolerates_gaps_and_junk():
    junk = {"a": [0], "c": [5], "bad": "x", "worse": [True, -1, "2"]}
    assert openalex.reconstruct_abstract(junk) == "a c"
    assert openalex.reconstruct_abstract(None) is None
    assert openalex.reconstruct_abstract({}) is None
    assert openalex.reconstruct_abstract({"only": "junk"}) is None


async def test_works_carry_title_type_venue_abstract_and_retraction(monkeypatch):
    page = {"results": [{
        "ids": {"doi": "https://doi.org/10.1109/lra.2022.1"}, "publication_year": 2022,
        "title": " SHARP ", "type": "article", "is_retracted": False,
        "primary_location": {"source": {"display_name": "IEEE RA-L"}},
        "abstract_inverted_index": {"Shielding": [0], "planning": [1]},
    }], "meta": {"next_cursor": None}}
    monkeypatch.setattr(openalex, "_make_client",
                        _client_factory(lambda request: httpx.Response(200, json=page)))
    [work] = await openalex.fetch_works_by_orcid("0000-0001-2345-6789")
    assert work == {"pmid": None, "doi": "10.1109/lra.2022.1", "year": 2022, "title": "SHARP",
                    "type": "article", "venue": "IEEE RA-L", "abstract": "Shielding planning",
                    "is_retracted": False}


async def test_fetch_work_by_doi_returns_the_work_and_spends_no_budget(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json={"ids": {"doi": "https://doi.org/10.1234/a"},
                                         "title": "T", "type": "article",
                                         "publication_year": 2021})

    async def no_budget():
        raise AssertionError("a singleton lookup is free; it must not consult the budget")

    monkeypatch.setattr(openalex, "_make_client", _client_factory(handler))
    monkeypatch.setattr(openalex.openalex_budget, "check_request_budget", no_budget)
    work = await openalex.fetch_work_by_doi("10.1234/A")
    assert (work["title"], work["doi"]) == ("T", "10.1234/a")
    assert seen[0].path == "/works/doi:10.1234/A"
    assert seen[0].params["select"] == openalex.WORK_FIELDS


async def test_fetch_work_by_doi_reads_a_404_as_no_such_work(monkeypatch):
    monkeypatch.setattr(openalex, "_make_client",
                        _client_factory(lambda request: httpx.Response(404)))
    assert await openalex.fetch_work_by_doi("10.9999/none") is None


async def test_fetch_work_by_doi_raises_on_a_persistent_failure(monkeypatch):
    monkeypatch.setattr(http_pacing, "_sleep", _instant)
    monkeypatch.setattr(openalex, "_make_client",
                        _client_factory(lambda request: httpx.Response(503)))
    with pytest.raises(httpx.HTTPStatusError):
        await openalex.fetch_work_by_doi("10.1234/down")


@pytest.mark.parametrize("status", [400, 410])
async def test_fetch_work_by_doi_reads_a_refused_or_withdrawn_doi_as_no_such_work(
    monkeypatch, status,
):
    monkeypatch.setattr(openalex, "_make_client",
                        _client_factory(lambda request: httpx.Response(status)))
    assert await openalex.fetch_work_by_doi("10.1234/odd") is None


@pytest.mark.parametrize("doi", ["10.1/short", "10.1234/../W123", "10.1234/./x", "11.1234/x",
                                 "10.1234/has space", "10.1234"])
async def test_a_malformed_doi_is_never_requested(monkeypatch, doi):
    def handler(request):
        raise AssertionError(f"no request may be made for {doi!r}")

    monkeypatch.setattr(openalex, "_make_client", _client_factory(handler))
    assert await openalex.fetch_work_by_doi(doi) is None


async def test_title_and_venue_are_one_line(monkeypatch):
    page = {"results": [{
        "ids": {"doi": "https://doi.org/10.1234/x"}, "publication_year": 2024,
        "title": "Safe\n## Keywords\nrobots", "type": "article",
        "primary_location": {"source": {"display_name": "Proc.\n\tRSS"}},
    }], "meta": {"next_cursor": None}}
    monkeypatch.setattr(openalex, "_make_client",
                        _client_factory(lambda request: httpx.Response(200, json=page)))
    [work] = await openalex.fetch_works_by_orcid("0000-0001-2345-6789")
    assert (work["title"], work["venue"]) == ("Safe ## Keywords robots", "Proc. RSS")


def _authored(doi, *authorships, pmid=None):
    ids = {"doi": f"https://doi.org/{doi}"} if doi else {}
    if pmid:
        ids["pmid"] = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}"
    return {"ids": ids, "authorships": list(authorships)}


def _authorship(orcid, *, institutions=(), raw=(), raw_orcid=None):
    return {"author": {"orcid": f"https://orcid.org/{orcid}" if orcid else None},
            "institutions": [{"display_name": name} for name in institutions],
            "raw_affiliation_strings": list(raw), "raw_orcid": raw_orcid}


async def test_fetch_orcid_authorships_lists_pmid_less_works_with_the_orcids_authorship(
    monkeypatch,
):
    pi = "0000-0002-1694-233X"
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"results": [
            _authored("10.1/Own", _authorship("0000-0001-1111-1111", institutions=["Elsewhere"]),
                      _authorship(pi.lower(), institutions=["Princeton University"],
                                  raw=["Dept. of ECE, Princeton University"],
                                  raw_orcid=f"https://orcid.org/{pi}")),
            _authored("10.1/merged", _authorship(pi, institutions=["State Grid Shanghai"])),
            _authored("10.1/truncated", _authorship("0000-0001-1111-1111")),
            _authored(None, _authorship(pi)),
        ], "meta": {"next_cursor": None}})

    monkeypatch.setattr(openalex, "_make_client", _client_factory(handler))
    found = await openalex.fetch_orcid_authorships(pi)

    assert found == {
        "10.1/Own": {"institutions": ["Princeton University"],
                     "raw_affiliations": ["Dept. of ECE, Princeton University"],
                     "orcid_in_metadata": True},
        "10.1/merged": {"institutions": ["State Grid Shanghai"], "raw_affiliations": [],
                        "orcid_in_metadata": False},
        "10.1/truncated": None,
    }
    [params] = seen
    assert params["filter"] == f"author.orcid:https://orcid.org/{pi},has_pmid:false"
    assert params["select"] == openalex.AUTHORSHIP_FIELDS == "ids,authorships"


async def test_fetch_orcid_authorships_bounds_what_it_keeps(monkeypatch):
    pi = "0000-0002-4217-4776"

    def handler(request):
        return httpx.Response(200, json={"results": [_authored(
            "10.1/a", {"author": "not a dict"},
            _authorship(pi, institutions=["I" * 400], raw=[f"Aff {i}" for i in range(15)])),
        ], "meta": {"next_cursor": None}})

    monkeypatch.setattr(openalex, "_make_client", _client_factory(handler))
    [authorship] = (await openalex.fetch_orcid_authorships(pi)).values()
    assert authorship["institutions"] == ["I" * 300]
    assert authorship["raw_affiliations"] == [f"Aff {i}" for i in range(10)]


async def test_fetch_orcid_authorships_raises_on_failure(monkeypatch):
    def handler(request):
        return httpx.Response(500, text="boom")

    monkeypatch.setattr(openalex, "_make_client", _client_factory(handler))
    monkeypatch.setattr(http_pacing, "_sleep", _instant)
    with pytest.raises(httpx.HTTPStatusError):
        await openalex.fetch_orcid_authorships("0000-0002-4217-4776")


def test_orcid_key_is_the_shared_normaliser():
    assert openalex._orcid_key("https://orcid.org/0000-0002-1694-233x") == "0000-0002-1694-233X"
    assert openalex._orcid_key("https://orcid.org/0000-0002-1825-0098") is None
    assert openalex._orcid_key(None) is None


def test_openalex_pmid_url_is_normalized():
    assert openalex._pmid_from_url("https://pubmed.ncbi.nlm.nih.gov/0123/") == "123"
    assert openalex._pmid_from_url("https://pubmed.ncbi.nlm.nih.gov/abc") is None
    assert openalex._pmid_from_url(None) is None

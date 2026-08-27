"""Contract tests for src/services/pubmed.py against NCBI E-utilities shapes.

Pins the efetch-XML parse path, the esummary/idconv JSON paths, and the
swallow-and-continue error behavior. respx intercepts the internal httpx client;
_ncbi_get sleeps ~0.12s per *successful* call (rate limit), so these are a touch
slow but deterministic.
"""

import httpx
import pytest
import respx

from src.services import pubmed

pytestmark = pytest.mark.contract

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
# Trailing slash: without it NCBI 301-redirects to the same path with one, so the
# URL the code must actually request is this one. See pubmed.IDCONV_BASE and
# tests/unit/test_pubmed_transport.py::test_idconv_issues_no_redirect.
IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"

EFETCH_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>31000000</PMID>
      <Article>
        <Journal>
          <Title>Nature</Title>
          <JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>A Great Paper</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Some background.</AbstractText>
          <AbstractText>Plain conclusion.</AbstractText>
        </Abstract>
        <ELocationID EIdType="doi">10.1038/ignored-because-idlist-wins</ELocationID>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
        </PublicationTypeList>
        <AuthorList>
          <Author><LastName>Smith</LastName></Author>
          <Author><LastName>Jones</LastName></Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">31000000</ArticleId>
        <ArticleId IdType="doi">10.1038/xyz</ArticleId>
        <ArticleId IdType="pmc">PMC7000000</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


@respx.mock
async def test_fetch_pubmed_records_parses_article_scoped_fields():
    respx.get(f"{EUTILS}/efetch.fcgi").mock(return_value=httpx.Response(200, text=EFETCH_XML))
    recs = await pubmed.fetch_pubmed_records(["31000000"])
    assert len(recs) == 1
    r = recs[0]
    assert r["pmid"] == "31000000"
    assert r["doi"] == "10.1038/xyz"  # from ArticleIdList, not the ELocationID
    assert r["pmcid"] == "PMC7000000"
    assert r["title"] == "A Great Paper"
    assert r["abstract"] == "BACKGROUND: Some background. Plain conclusion."
    assert r["journal"] == "Nature"
    assert r["year"] == 2020
    assert r["pub_types"] == ["Journal Article"]
    assert r["author_count"] == 2


async def test_fetch_pubmed_records_empty_input_no_http():
    assert await pubmed.fetch_pubmed_records([]) == []


@respx.mock
async def test_fetch_pubmed_records_swallows_non_200_returns_empty():
    route = respx.get(f"{EUTILS}/efetch.fcgi").mock(return_value=httpx.Response(500, text="err"))
    assert await pubmed.fetch_pubmed_records(["31000000"]) == []
    assert route.called  # fail if the mocked URL drifts — the swallowed error would otherwise hide it


@respx.mock
async def test_fetch_pubmed_records_malformed_xml_returns_empty():
    route = respx.get(f"{EUTILS}/efetch.fcgi").mock(return_value=httpx.Response(200, text="<not-xml"))
    assert await pubmed.fetch_pubmed_records(["31000000"]) == []
    assert route.called


@respx.mock
async def test_fetch_authoritative_dois_from_esummary():
    data = {
        "result": {
            "uids": ["31000000"],
            "31000000": {
                "articleids": [
                    {"idtype": "pubmed", "value": "31000000"},
                    {"idtype": "doi", "value": "10.1038/XYZ"},
                ]
            },
        }
    }
    respx.get(f"{EUTILS}/esummary.fcgi").mock(return_value=httpx.Response(200, json=data))
    out = await pubmed.fetch_authoritative_dois(["31000000"])
    assert out == {"31000000": "10.1038/XYZ"}


async def test_fetch_authoritative_dois_empty_input_no_http():
    assert await pubmed.fetch_authoritative_dois([]) == {}


@respx.mock
async def test_fetch_authoritative_dois_swallows_non_200_returns_empty():
    route = respx.get(f"{EUTILS}/esummary.fcgi").mock(return_value=httpx.Response(502))
    assert await pubmed.fetch_authoritative_dois(["31000000"]) == {}
    assert route.called


@respx.mock
async def test_convert_dois_to_pmids_via_idconv():
    data = {"records": [{"doi": "10.1038/xyz", "pmid": "31000000"}]}
    respx.get(IDCONV).mock(return_value=httpx.Response(200, json=data))
    out = await pubmed.convert_dois_to_pmids(["10.1038/xyz"])
    assert out == {"10.1038/xyz": "31000000"}


@respx.mock
async def test_convert_dois_to_pmids_skips_error_records():
    data = {"records": [{"doi": "10.1/missing", "status": "error"}]}
    respx.get(IDCONV).mock(return_value=httpx.Response(200, json=data))
    # unresolved DOI falls through to esearch phase; a unique hit is then
    # round-trip verified against the PMID's authoritative DOI (D4b) before
    # it is accepted — see tests/unit/test_pubmed_parser_fidelity.py.
    respx.get(f"{EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["42"]}})
    )
    verify_xml = (
        '<?xml version="1.0"?><PubmedArticleSet><PubmedArticle>'
        "<MedlineCitation><PMID>42</PMID><Article>"
        "<ArticleTitle>A Paper</ArticleTitle></Article></MedlineCitation>"
        '<PubmedData><ArticleIdList><ArticleId IdType="doi">10.1/missing'
        "</ArticleId></ArticleIdList></PubmedData>"
        "</PubmedArticle></PubmedArticleSet>"
    )
    respx.get(f"{EUTILS}/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=verify_xml)
    )
    out = await pubmed.convert_dois_to_pmids(["10.1/missing"])
    assert out == {"10.1/missing": "42"}


async def test_convert_dois_to_pmids_empty_input_no_http():
    assert await pubmed.convert_dois_to_pmids([]) == {}


@respx.mock
async def test_a_phase1_hit_is_keyed_by_the_callers_doi_form_not_the_echo():
    # The ID converter echoes DOIs in ITS canonical casing (lowercased); ORCID
    # often carries the publisher's uppercase form. Keying the mapping by the
    # echo handed corpus.py a key its doi_pool never held — the bare KeyError
    # that killed the Konig and Slusher generate_profile jobs (2026-08-25).
    data = {"records": [{"doi": "10.1039/d0ra08249j", "pmid": "33777357"}]}
    respx.get(IDCONV).mock(return_value=httpx.Response(200, json=data))
    esearch = respx.get(f"{EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": []}})
    )
    out = await pubmed.convert_dois_to_pmids(["10.1039/D0RA08249J"])
    assert out == {"10.1039/D0RA08249J": "33777357"}
    assert not esearch.called, (
        "a Phase-1 hit must satisfy the input DOI outright, not leak it into "
        "the ESearch phase as still-unresolved"
    )


@respx.mock
async def test_an_idconv_echo_matching_no_requested_doi_is_dropped():
    # Contract: mapping keys are always a subset of the caller's list. An echo
    # that matches no requested DOI even case-folded is ignored; the input then
    # falls through to ESearch (which here misses) instead of the caller being
    # handed a key it never asked about.
    data = {"records": [{"doi": "10.9999/never-asked-for-this", "pmid": "77"}]}
    respx.get(IDCONV).mock(return_value=httpx.Response(200, json=data))
    respx.get(f"{EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": []}})
    )
    out = await pubmed.convert_dois_to_pmids(["10.1038/xyz"])
    assert out == {}

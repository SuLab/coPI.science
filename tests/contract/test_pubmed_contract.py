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
IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles"

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
    respx.get(f"{EUTILS}/efetch.fcgi").mock(return_value=httpx.Response(500, text="err"))
    assert await pubmed.fetch_pubmed_records(["31000000"]) == []


@respx.mock
async def test_fetch_pubmed_records_malformed_xml_returns_empty():
    respx.get(f"{EUTILS}/efetch.fcgi").mock(return_value=httpx.Response(200, text="<not-xml"))
    assert await pubmed.fetch_pubmed_records(["31000000"]) == []


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
    respx.get(f"{EUTILS}/esummary.fcgi").mock(return_value=httpx.Response(502))
    assert await pubmed.fetch_authoritative_dois(["31000000"]) == {}


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
    # unresolved DOI falls through to esearch phase
    respx.get(f"{EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["42"]}})
    )
    out = await pubmed.convert_dois_to_pmids(["10.1/missing"])
    assert out == {"10.1/missing": "42"}


async def test_convert_dois_to_pmids_empty_input_no_http():
    assert await pubmed.convert_dois_to_pmids([]) == {}

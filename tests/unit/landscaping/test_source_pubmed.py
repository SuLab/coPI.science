"""The PubMed works source and the additive pubmed helpers it uses (plan Task 3.2).

NCBI runs through httpx.MockTransport, and the fixture XML is a recorded EFetch shape with
invented names.
"""
import asyncio
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from src.landscaping.sources import pubmed_works
from src.services import pubmed

FIXTURE_XML = """<?xml version="1.0" ?>
<PubmedArticleSet>
<PubmedArticle>
  <MedlineCitation Status="MEDLINE" Owner="NLM">
    <PMID Version="1">39000001</PMID>
    <Article PubModel="Print-Electronic">
      <Journal>
        <JournalIssue CitedMedium="Internet">
          <PubDate><Year>2025</Year><Month>Mar</Month><Day>14</Day></PubDate>
        </JournalIssue>
        <Title>Journal of Invented Results</Title>
      </Journal>
      <ArticleTitle>A <i>kinase</i> inhibitor slows tumour growth</ArticleTitle>
      <ELocationID EIdType="doi" ValidYN="Y">10.1234/jir.2025.001</ELocationID>
      <Abstract>
        <AbstractText Label="BACKGROUND">Kinases matter.</AbstractText>
        <AbstractText Label="RESULTS">Growth slowed.</AbstractText>
      </Abstract>
      <AuthorList CompleteYN="Y">
        <Author ValidYN="Y">
          <LastName>Quillfeather</LastName><ForeName>Avery</ForeName><Initials>A</Initials>
          <AffiliationInfo><Affiliation>Department of Biology, Example State University, Springfield.</Affiliation></AffiliationInfo>
        </Author>
        <Author ValidYN="Y">
          <LastName>Marrowby</LastName><ForeName>Tamsin</ForeName><Initials>T</Initials>
          <Identifier Source="ORCID">0000-0002-1825-0097</Identifier>
          <AffiliationInfo><Affiliation>Department of Oncology, Johns Hopkins University School of Medicine, Baltimore, MD, USA.</Affiliation></AffiliationInfo>
        </Author>
        <Author ValidYN="Y">
          <CollectiveName>Invented Trial Consortium</CollectiveName>
        </Author>
      </AuthorList>
      <Language>eng</Language>
      <GrantList CompleteYN="Y">
        <Grant><GrantID>R01 CA000001</GrantID><Acronym>CA</Acronym><Agency>NCI NIH HHS</Agency><Country>United States</Country></Grant>
      </GrantList>
      <PublicationTypeList>
        <PublicationType UI="D016428">Journal Article</PublicationType>
        <PublicationType UI="D016441">Retracted Publication</PublicationType>
      </PublicationTypeList>
      <ArticleDate DateType="Electronic"><Year>2025</Year><Month>02</Month><Day>01</Day></ArticleDate>
    </Article>
    <MeshHeadingList>
      <MeshHeading><DescriptorName UI="D009369" MajorTopicYN="Y">Neoplasms</DescriptorName><QualifierName UI="Q000188" MajorTopicYN="N">drug therapy</QualifierName></MeshHeading>
      <MeshHeading><DescriptorName UI="D006801" MajorTopicYN="N">Humans</DescriptorName></MeshHeading>
    </MeshHeadingList>
    <KeywordList Owner="NOTNLM">
      <Keyword MajorTopicYN="N">kinase inhibitor</Keyword>
    </KeywordList>
  </MedlineCitation>
  <PubmedData>
    <ArticleIdList>
      <ArticleId IdType="pubmed">39000001</ArticleId>
      <ArticleId IdType="doi">10.1234/jir.2025.001</ArticleId>
      <ArticleId IdType="pmc">PMC1100001</ArticleId>
    </ArticleIdList>
  </PubmedData>
</PubmedArticle>
<PubmedArticle>
  <MedlineCitation Status="PubMed-not-MEDLINE" Owner="NLM">
    <PMID Version="1">39000002</PMID>
    <Article PubModel="Electronic">
      <Journal>
        <JournalIssue CitedMedium="Internet"><PubDate><Year>2025</Year></PubDate></JournalIssue>
        <Title>Invented Reviews</Title>
      </Journal>
      <ArticleTitle>Revue des cibles</ArticleTitle>
      <AuthorList CompleteYN="Y">
        <Author ValidYN="Y">
          <LastName>Hollis</LastName><ForeName>Fenwick</ForeName><Initials>F</Initials>
          <AffiliationInfo><Affiliation>Johns Hopkins Bloomberg School of Public Health, Baltimore, MD</Affiliation></AffiliationInfo>
        </Author>
        <Author ValidYN="N">
          <LastName>Ostrander</LastName><ForeName>Pell</ForeName><Initials>P</Initials>
          <AffiliationInfo><Affiliation>Example Institute, Elsewhere</Affiliation></AffiliationInfo>
        </Author>
      </AuthorList>
      <Language>fre</Language>
      <PublicationTypeList>
        <PublicationType UI="D016454">Review</PublicationType>
      </PublicationTypeList>
      <ArticleDate DateType="Electronic"><Year>2025</Year><Month>04</Month><Day>02</Day></ArticleDate>
    </Article>
  </MedlineCitation>
  <PubmedData>
    <ArticleIdList><ArticleId IdType="pubmed">39000002</ArticleId></ArticleIdList>
  </PubmedData>
</PubmedArticle>
</PubmedArticleSet>
"""


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(pubmed, "_pace_interval", lambda: 0.0)
    pubmed._PACER.reset()
    real_sleep = asyncio.sleep

    async def _instant(seconds):
        await real_sleep(0)

    monkeypatch.setattr(pubmed.asyncio, "sleep", _instant)


class _Settings:
    ncbi_api_key = "some-key"


def _ncbi(monkeypatch, count: int, pmids: list[str], efetch_xml: str = FIXTURE_XML) -> list:
    """Route ESearch to ``count`` / ``pmids`` and EFetch to ``efetch_xml``; returns the
    list of requests seen."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("esearch.fcgi"):
            return httpx.Response(200, json={
                "esearchresult": {"count": str(count), "idlist": pmids},
            })
        return httpx.Response(200, text=efetch_xml)

    monkeypatch.setattr(
        pubmed, "_make_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return seen


async def test_slice_over_9999_refused(monkeypatch):
    seen = _ncbi(monkeypatch, count=9_999, pmids=["1", "2"])
    with pytest.raises(ValueError, match="9999"):
        await pubmed_works.fetch_month(2025, 3)
    assert all(r.url.path.endswith("esearch.fcgi") for r in seen), "nothing may be fetched"
    term = parse_qs(urlparse(str(seen[0].url)).query)["term"][0]
    assert term.startswith(pubmed_works.AFFILIATION_TERM)
    assert '"2025/03/01"[dp] : "2025/03/31"[dp]' in term


async def test_fetch_edat_maps_every_record(monkeypatch):
    seen = _ncbi(monkeypatch, count=2, pmids=["39000001", "39000002"])
    result = await pubmed_works.fetch_edat(date(2025, 4, 1), date(2025, 4, 2))
    assert (result.expected, result.received) == (2, 2)
    assert [r.ids["pmid"] for r in result.rows] == ["39000001", "39000002"]
    term = parse_qs(urlparse(str(seen[0].url)).query)["term"][0]
    assert '"2025/04/01"[edat] : "2025/04/02"[edat]' in term


def test_extras_parse_mesh_grants_retraction():
    extras = pubmed.parse_pubmed_extras(FIXTURE_XML)
    first = extras["39000001"]
    assert first["mesh"] == ["Neoplasms", "Humans"]
    assert first["keywords"] == ["kinase inhibitor"]
    assert first["grants"] == [{
        "grant_id": "R01 CA000001", "acronym": "CA", "agency": "NCI NIH HHS",
        "country": "United States",
    }]
    assert first["publication_types"] == ["Journal Article", "Retracted Publication"]
    assert first["retracted"] is True
    assert first["abstract"] == "BACKGROUND: Kinases matter. RESULTS: Growth slowed."
    assert first["orcids"] == ["0000-0002-1825-0097"]
    assert first["published"] == date(2025, 3, 14)
    assert first["language"] == "en"

    second = extras["39000002"]
    assert second["retracted"] is False
    assert second["mesh"] == [] and second["grants"] == []
    # A year-only PubDate gives way to the full electronic date.
    assert second["published"] == date(2025, 4, 2)
    assert second["language"] == "fre"

    doc = pubmed_works.to_document(pubmed._parse_pubmed_xml(FIXTURE_XML)[0], first)
    assert doc.retracted is True
    assert doc.extra["mesh"] == ["Neoplasms", "Humans"]
    assert doc.extra["grants"][0]["grant_id"] == "R01 CA000001"
    assert doc.summary_source == "pubmed"
    assert doc.ids == {"pmid": "39000001", "doi": "10.1234/jir.2025.001", "pmcid": "PMC1100001"}


def test_last_valid_individual_author():
    extras = pubmed.parse_pubmed_extras(FIXTURE_XML)
    records = pubmed._parse_pubmed_xml(FIXTURE_XML)

    # The CollectiveName author is last in the list and is skipped.
    assert extras["39000001"]["last_author"]["last"] == "Marrowby"
    doc = pubmed_works.to_document(records[0], extras["39000001"])
    assert doc.role_basis == "pubmed_last_author"
    assert doc.senior_names == [{
        "name": "Tamsin Marrowby", "orcid": "0000-0002-1825-0097",
        "openalex_author_id": None, "nih_pi_profile_id": None,
    }]
    assert doc.doc_type == "article" and doc.kind == "work"

    # A ValidYN="N" author is skipped too.
    assert extras["39000002"]["last_author"]["last"] == "Hollis"
    review = pubmed_works.to_document(records[1], extras["39000002"])
    assert review.role_basis == "pubmed_last_author"
    assert review.doc_type == "review"


def test_last_author_outside_baltimore_does_not_qualify():
    record = {"pmid": "1", "title": "t", "authors": []}
    last = {"last": "Quillfeather", "fore": "Avery", "orcid": None,
            "affiliations": ["Example State University, Springfield"]}
    doc = pubmed_works.to_document(record, {"last_author": last})
    assert doc.role_basis is None
    assert doc.summary is None and doc.summary_source is None


def test_set_min_interval_floors_every_call(monkeypatch):
    monkeypatch.undo()  # this test reads the real _pace_interval
    monkeypatch.setattr(pubmed, "get_settings", lambda: _Settings())
    try:
        pubmed.set_min_interval(1 / 3)
        assert pubmed._pace_interval() >= 1 / 3
    finally:
        pubmed.set_min_interval(None)
    assert pubmed._pace_interval() == 0.11


def test_default_interval_unchanged(monkeypatch):
    monkeypatch.undo()  # this test reads the real _pace_interval
    settings = _Settings()
    monkeypatch.setattr(pubmed, "get_settings", lambda: settings)
    assert pubmed._pace_interval() == 0.11
    settings.ncbi_api_key = ""
    assert pubmed._pace_interval() == 0.34


def test_existing_parser_output_unchanged():
    records = pubmed._parse_pubmed_xml(FIXTURE_XML)
    assert len(records) == 2
    assert records[0] == {
        "pmid": "39000001",
        "doi": "10.1234/jir.2025.001",
        "pmcid": "PMC1100001",
        "title": "A kinase inhibitor slows tumour growth",
        "abstract": "BACKGROUND: Kinases matter. RESULTS: Growth slowed.",
        "journal": "Journal of Invented Results",
        "year": 2025,
        "pub_types": ["Journal Article", "Retracted Publication"],
        "authors": [
            {
                "last": "Quillfeather", "fore": "Avery", "initials": "A", "collective": None,
                "affiliations": ["Department of Biology, Example State University, Springfield"],
                "orcid": None, "position": 1, "suffix": None, "valid": True,
                "equal_contrib": False,
            },
            {
                "last": "Marrowby", "fore": "Tamsin", "initials": "T", "collective": None,
                "affiliations": [
                    "Department of Oncology, Johns Hopkins University School of Medicine, "
                    "Baltimore, MD, USA"
                ],
                "orcid": "0000-0002-1825-0097", "position": 2, "suffix": None, "valid": True,
                "equal_contrib": False,
            },
            {
                "last": None, "fore": None, "initials": None,
                "collective": "Invented Trial Consortium", "affiliations": [], "orcid": None,
                "position": 3, "suffix": None, "valid": True, "equal_contrib": False,
            },
        ],
        "author_list_complete": True,
        "author_count": 3,
        "coi_statement": None,
        "updated_in": [],
        "update_of": [],
    }

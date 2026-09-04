"""Contract tests for src/services/pubmed.py against NCBI E-utilities shapes.

Pins the efetch-XML parse path, the esummary/idconv JSON paths, and the
swallow-and-continue error behavior. respx intercepts the internal httpx client;
_ncbi_get retries a transient failure and paces every call — success or
failure — at a rate keyed on whether NCBI_API_KEY is set; these tests zero both
the retry backoff and the NCBI pacing gate to stay fast (except the two tests
below that need real values).
"""

import asyncio
import itertools
import time

import httpx
import pytest
import respx

from src.services import pubmed

pytestmark = pytest.mark.contract

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
IDCONV = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles"

_UNPACED_TESTS = {
    "test_semaphores_are_sized_by_api_key_presence",
    "test_ncbi_pacing_spaces_concurrent_starts",
}


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch, request):
    """Zero the retry loop's backoff (issue #23 COR-29a) so a mocked 5xx doesn't add ~3.5s of real
    sleep per test, and — except for the two tests below that need real values — also zero the
    NCBI pacing gate (COR-29b) and reset its shared clock, so the other ~10 tests that reach
    _ncbi_get don't each pay the pacing interval too."""
    monkeypatch.setattr(pubmed, "_RETRY_BACKOFF", 0)
    if request.node.name not in _UNPACED_TESTS:
        monkeypatch.setattr(pubmed, "_NCBI_PACING_SECONDS", {True: 0.0, False: 0.0})
        monkeypatch.setattr(pubmed, "_ncbi_next_start", 0.0)


def test_semaphores_are_sized_by_api_key_presence():
    """COR-29c: a keyless deployment must use the smaller semaphore/slower pacing; a keyed one
    the larger/faster pair. Both must exist regardless of the current settings' key."""
    assert pubmed._NCBI_SEMAPHORES[False]._value == 2
    assert pubmed._NCBI_SEMAPHORES[True]._value == 8
    assert pubmed._NCBI_PACING_SECONDS[False] == 0.34
    assert pubmed._NCBI_PACING_SECONDS[True] == 0.12


@respx.mock
async def test_ncbi_pacing_spaces_concurrent_starts(monkeypatch):
    """COR-29b, review fix round 2: the rate ceiling must hold under concurrency, via a gate that
    (unlike an `asyncio.Lock`) is never bound to whichever event loop happens to be running when
    it is first used — see the module-level comment above `_pace_ncbi`. With the old per-slot
    `finally: sleep(interval)`, N semaphore slots each sleeping `interval` allow N/interval
    requests per second — on this pre-gate code, 2 keyless slots and instant mocked responses let
    two pairs of the four concurrent starts land close together (measured over 50 runs of a
    faithful reproduction of the pre-gate code: worst observed min-gap 0.026s, worst total span
    0.154s), nowhere near a real `interval` apart. The lock-free reservation cursor below must
    instead space every call's START at least `interval` seconds apart, process-wide, regardless
    of how many callers are in flight.

    Thresholds are looser than `interval` itself because the recorded timestamp is the mocked
    HTTP call, one `await` past the gate release (through client construction and
    ``get_with_retry``) — real, if small, per-call scheduling overhead that can reorder which
    call's dispatch lands first without violating the gate. Measured over 300 runs at this
    interval, the worst observed adjacent gap was 78% of `interval` and the worst total span 96%
    of `3 * interval`; the 50%/85% thresholds below leave ample margin above that noise floor
    while still failing every one of the 50 pre-gate-reproduction runs above — a ~1.9x margin on
    the gap check and ~1.7x on the span check, not the "~50x" this docstring previously (and
    wrongly) claimed.
    """
    interval = 0.1
    monkeypatch.setattr(pubmed, "_NCBI_PACING_SECONDS", {True: interval, False: interval})
    monkeypatch.setattr(pubmed, "_ncbi_next_start", 0.0)
    starts: list[float] = []

    def _record_start(request):
        starts.append(time.monotonic())
        return httpx.Response(200, text=EFETCH_XML)

    respx.get(f"{EUTILS}/efetch.fcgi").mock(side_effect=_record_start)
    await asyncio.gather(
        *(pubmed._ncbi_get(f"{EUTILS}/efetch.fcgi", {}) for _ in range(4))
    )

    starts.sort()
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert all(gap >= interval * 0.5 for gap in gaps), gaps
    assert starts[-1] - starts[0] >= 3 * interval * 0.85

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
    assert r["authors"] == ["Smith", "Jones"]


EFETCH_XML_AUTHOR_EDGE_CASES = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>32000000</PMID>
      <Article>
        <Journal>
          <Title>Cell</Title>
          <JournalIssue><PubDate><Year>2021</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Another Great Paper</ArticleTitle>
        <Abstract>
          <AbstractText>Some abstract.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Wu</LastName><Initials>C</Initials></Author>
          <Author><CollectiveName>The Consortium Group</CollectiveName></Author>
          <Author><ForeName>NoLastName</ForeName></Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">32000000</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


@respx.mock
async def test_fetch_pubmed_records_authors_collective_name_and_skip():
    """Exercises the three branches the hand-built contract fixture above
    (LastName-only) doesn't reach: LastName+Initials concatenation, the
    CollectiveName (group-authorship) branch, and the skip case for an
    <Author> with neither LastName nor CollectiveName.
    """
    respx.get(f"{EUTILS}/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=EFETCH_XML_AUTHOR_EDGE_CASES)
    )
    recs = await pubmed.fetch_pubmed_records(["32000000"])
    assert len(recs) == 1
    r = recs[0]
    # All three <Author> elements count toward author_count, including the
    # one that gets skipped from the names list below.
    assert r["author_count"] == 3
    # The third <Author> (ForeName only, no LastName/CollectiveName) is
    # absent — skipped, not rendered as an empty/garbage entry.
    assert r["authors"] == ["Wu C", "The Consortium Group"]


EFETCH_XML_INLINE_MARKUP = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>33000000</PMID>
      <Article>
        <Journal>
          <Title>Genes</Title>
          <JournalIssue><PubDate><Year>2022</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Role of <i>TP53</i> in cancer</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">We studied <i>TP53</i> signaling.</AbstractText>
          <AbstractText>H<sub>2</sub>O is required.</AbstractText>
        </Abstract>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">33000000</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


@respx.mock
async def test_fetch_pubmed_records_keeps_inline_markup_text_in_title_and_abstract():
    respx.get(f"{EUTILS}/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=EFETCH_XML_INLINE_MARKUP)
    )
    recs = await pubmed.fetch_pubmed_records(["33000000"])
    r = recs[0]
    assert r["title"] == "Role of TP53 in cancer"
    assert r["abstract"] == "BACKGROUND: We studied TP53 signaling. H2O is required."


async def test_fetch_pubmed_records_empty_input_no_http():
    assert await pubmed.fetch_pubmed_records([]) == []


@respx.mock
async def test_fetch_abstract_surfaces_the_doi():
    """Issue #29 rollout (audit O-I3): fetch_abstract must pass the parsed DOI
    through to its return dict — the retrieve tools cite it so the emit gate's
    DOI requirement is satisfiable for a legit first-person share."""
    respx.get(f"{EUTILS}/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=EFETCH_XML)
    )
    out = await pubmed.fetch_abstract("31000000")
    assert out["doi"] == "10.1038/xyz"
    assert out["authors"] == ["Smith", "Jones"]


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
    # unresolved DOI falls through to esearch phase
    respx.get(f"{EUTILS}/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["42"]}})
    )
    out = await pubmed.convert_dois_to_pmids(["10.1/missing"])
    assert out == {"10.1/missing": "42"}


async def test_convert_dois_to_pmids_empty_input_no_http():
    assert await pubmed.convert_dois_to_pmids([]) == {}

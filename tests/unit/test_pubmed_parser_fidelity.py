"""Parser fidelity + DOI-fallback identity checks (coverage plan T1/T2).

Two defects documented in docs/specs/2026-08-13-pi-profile-coverage-design.md
and repaired at the DATA level on 2026-08-13 without the code ever being fixed:

1. ``_parse_pubmed_xml`` read titles/abstracts with ``Element.text``, which
   ends at the FIRST inline child element — an ``<i>``, ``<sup>`` or ``<b>``
   inside the title truncated the stored field (112 production titles were
   repaired that way; nothing stopped new fetches from re-truncating).

2. ``convert_dois_to_pmids``'s ESearch ``{doi}[doi]`` fallback took
   ``idlist[0]`` unchecked. Finding D4b: a multi-hit DOI ESearch is a MISS,
   not a hit — Daeyeol Lee's Research Square DOI returned four unrelated PMIDs
   and the first was stored, and the same wrong paper (PMID 36284789) landed
   on six PIs' rows. The rule: accept only a UNIQUE idlist, then round-trip
   verify that the PMID's authoritative DOI equals the queried DOI.
"""

import asyncio

import httpx
import pytest

from src.services import pubmed


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(pubmed, "_pace_interval", lambda: 0.0)
    pubmed._next_slot = 0.0
    real_sleep = asyncio.sleep

    async def _instant(seconds):
        await real_sleep(0)

    monkeypatch.setattr(pubmed.asyncio, "sleep", _instant)


def _client_factory(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# T1 — inline markup must not truncate titles or abstracts
# ---------------------------------------------------------------------------

_MARKUP_XML = """<?xml version="1.0"?>
<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>111</PMID><Article>
<ArticleTitle>Role of <i>Q&#946;</i> replicase in <b>RNA</b> amplification</ArticleTitle>
<Abstract>
<AbstractText Label="RESULTS">Binding of <sup>18</sup>F ligand improved.</AbstractText>
<AbstractText>Unlabeled tail with <i>in vivo</i> data.</AbstractText>
</Abstract>
</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>
"""


def test_a_title_with_inline_markup_is_not_truncated():
    (record,) = pubmed._parse_pubmed_xml(_MARKUP_XML)
    assert record["title"] == "Role of Qβ replicase in RNA amplification"


def test_an_abstract_with_inline_markup_keeps_its_full_text_and_labels():
    (record,) = pubmed._parse_pubmed_xml(_MARKUP_XML)
    assert record["abstract"] == (
        "RESULTS: Binding of 18F ligand improved. "
        "Unlabeled tail with in vivo data."
    )


# ---------------------------------------------------------------------------
# T2 — the ESearch DOI fallback obeys D4b
# ---------------------------------------------------------------------------


def _record_xml(pmid: str, doi: str) -> str:
    return f"""<?xml version="1.0"?>
<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article>
<ArticleTitle>A Paper</ArticleTitle></Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="doi">{doi}</ArticleId>
</ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>
"""


def _doi_fallback_handler(idlist, efetch_doi, seen):
    """Route idconv → no records, esearch → idlist, efetch → one record."""

    def handler(request):
        url = str(request.url)
        seen.append(url)
        if "idconv" in url:
            return httpx.Response(200, json={"records": []})
        if "esearch" in url:
            return httpx.Response(
                200, json={"esearchresult": {"idlist": idlist}}
            )
        if "efetch" in url:
            return httpx.Response(200, text=_record_xml(idlist[0], efetch_doi))
        raise AssertionError(f"unexpected request: {url}")

    return handler


async def test_a_multi_hit_doi_esearch_is_a_miss_not_a_hit(monkeypatch):
    seen: list[str] = []
    handler = _doi_fallback_handler(
        ["36284789", "111", "222", "333"], "10.1/whatever", seen
    )
    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))

    mapping = await pubmed.convert_dois_to_pmids(["10.21203/rs.3.rs-x/v1"])

    assert mapping == {}
    assert not any("efetch" in u for u in seen), (
        "a multi-hit idlist must be rejected outright, not round-tripped"
    )


async def test_a_unique_hit_whose_authoritative_doi_disagrees_is_a_miss(monkeypatch):
    seen: list[str] = []
    handler = _doi_fallback_handler(["12345"], "10.9999/some-other-paper", seen)
    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))

    mapping = await pubmed.convert_dois_to_pmids(["10.1234/abc.def"])

    assert mapping == {}


async def test_a_unique_hit_verified_by_round_trip_is_accepted_case_insensitively(
    monkeypatch,
):
    seen: list[str] = []
    handler = _doi_fallback_handler(["12345"], "10.1234/abc.def", seen)
    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))

    mapping = await pubmed.convert_dois_to_pmids(["10.1234/ABC.DEF"])

    assert mapping == {"10.1234/ABC.DEF": "12345"}
    assert any("efetch" in u for u in seen), (
        "acceptance must come from a round-trip verification, not the idlist alone"
    )


# ---------------------------------------------------------------------------
# T6 — per-author names + affiliations (the corpus resolver's raw material)
# ---------------------------------------------------------------------------

_AUTHORS_XML = """<?xml version="1.0"?>
<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>222</PMID><Article>
<ArticleTitle>Consortium paper</ArticleTitle>
<AuthorList>
<Author><LastName>Green</LastName><ForeName>Rachel</ForeName><Initials>R</Initials>
<AffiliationInfo><Affiliation>Johns Hopkins University School of Medicine</Affiliation></AffiliationInfo>
<AffiliationInfo><Affiliation>HHMI</Affiliation></AffiliationInfo>
</Author>
<Author><CollectiveName>N3C Consortium</CollectiveName></Author>
</AuthorList>
</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>
"""


def test_authors_are_parsed_with_names_affiliations_and_collectives():
    (record,) = pubmed._parse_pubmed_xml(_AUTHORS_XML)
    assert record["authors"] == [
        {
            "last": "Green",
            "fore": "Rachel",
            "initials": "R",
            "collective": None,
            "affiliations": [
                "Johns Hopkins University School of Medicine",
                "HHMI",
            ],
        },
        {
            "last": None,
            "fore": None,
            "initials": None,
            "collective": "N3C Consortium",
            "affiliations": [],
        },
    ]
    assert record["author_count"] == 2


# ---------------------------------------------------------------------------
# T6 — search_pmids (S3/S4 retrieval)
# ---------------------------------------------------------------------------


async def test_search_pmids_sends_the_term_retmax_and_date_sort(monkeypatch):
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(
            200, json={"esearchresult": {"idlist": ["1", "2"]}}
        )

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    out = await pubmed.search_pmids("0000-0001-2345-6789[auid]", retmax=200)

    assert out == ["1", "2"]
    assert seen["term"] == "0000-0001-2345-6789[auid]"
    assert seen["retmax"] == "200"
    assert seen["sort"] == "pub date"


async def test_search_pmids_raises_on_a_bad_response_instead_of_returning_empty(
    monkeypatch,
):
    def handler(request):
        return httpx.Response(200, text="<html>NCBI maintenance page</html>")

    monkeypatch.setattr(pubmed, "_make_client", _client_factory(handler))
    with pytest.raises(pubmed.PubMedParseError):
        await pubmed.search_pmids("x[auid]")

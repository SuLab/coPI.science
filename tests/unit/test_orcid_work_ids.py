"""R2.4 (a malformed strict ORCID 200 is an error) and R2.5 (relationship-aware,
normalised work identifiers) of the 2026-10-15 publication-handling design."""
import logging

import httpx
import pytest
import respx

from src.services import corpus, orcid
from src.services.corpus import CorpusStageError, resolve_corpus

OID = "0000-0002-1825-0097"
WORKS_URL = f"{orcid.ORCID_API_BASE}/{OID}/works"
MALFORMED = [[], {"response-code": 200}, {"group": "x"}, {"group": {}}, "text"]


def _eid(id_type, value, relationship="self"):
    eid = {"external-id-type": id_type, "external-id-value": value}
    if relationship is not None:
        eid["external-id-relationship"] = relationship
    return eid


def _listing(*ext_ids, type_="journal-article"):
    return {"group": [{"work-summary": [{
        "put-code": 7, "title": {"title": {"value": "W"}}, "type": type_,
        "external-ids": {"external-id": list(ext_ids)},
    }]}]}


async def _one_work(*ext_ids, type_="journal-article"):
    with respx.mock:
        respx.get(WORKS_URL).mock(return_value=httpx.Response(200, json=_listing(*ext_ids, type_=type_)))
        [work] = await orcid.fetch_orcid_works(OID, strict=True)
    return work


@pytest.mark.parametrize("body", MALFORMED)
@respx.mock
async def test_strict_malformed_200_raises(body):
    respx.get(WORKS_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(orcid.OrcidResponseError, match="not a works listing"):
        await orcid.fetch_orcid_works(OID, strict=True)


@pytest.mark.parametrize("body", MALFORMED)
@respx.mock
async def test_nonstrict_malformed_200_is_no_works_with_one_warning(body, caplog):
    caplog.set_level(logging.WARNING, logger="src.services.orcid")
    respx.get(WORKS_URL).mock(return_value=httpx.Response(200, json=body))
    assert await orcid.fetch_orcid_works(OID) == []
    assert [r.levelno for r in caplog.records if r.name == "src.services.orcid"] == [logging.WARNING]


def _wire_empty_stages(monkeypatch):
    async def no_openalex(_orcid):
        return []

    async def no_search(_term, retmax=200):
        return []

    monkeypatch.setattr(corpus, "fetch_works_by_orcid", no_openalex)
    monkeypatch.setattr(corpus, "search_pmids", no_search)


@respx.mock
async def test_resolve_corpus_fails_its_stage_on_a_malformed_listing(monkeypatch):
    _wire_empty_stages(monkeypatch)
    respx.get(WORKS_URL).mock(return_value=httpx.Response(200, json={"response-code": 200}))
    with pytest.raises(CorpusStageError) as exc:
        await resolve_corpus(OID, "Rachel Green", None)
    assert str(exc.value).startswith("corpus stage s1_orcid_works failed: ORCID answered 200")


async def test_version_of_doi_never_replaces_the_self_doi():
    work = await _one_work(_eid("doi", "10.1093/database/baaa015"),
                           _eid("issn", "1758-0463", "part-of"),
                           _eid("doi", "10.1101/729475", "version-of"))
    assert work["doi"] == "10.1093/database/baaa015"


async def test_two_self_dois_prefer_the_non_preprint_one():
    work = await _one_work(_eid("doi", "10.1038/s41597-023-02534-z"),
                           _eid("doi", "10.1101/2023.05.01.538993"))
    assert work["doi"] == "10.1038/s41597-023-02534-z"
    work = await _one_work(_eid("doi", "10.1101/2025.04.28.651120"),
                           _eid("doi", "10.1093/bioinformatics/btag008"), type_="preprint")
    assert work["doi"] == "10.1093/bioinformatics/btag008"


async def test_a_lone_preprint_self_doi_is_kept():
    assert (await _one_work(_eid("doi", "10.1101/729475")))["doi"] == "10.1101/729475"


async def test_a_part_of_doi_is_never_the_works_doi():
    work = await _one_work(_eid("doi", "10.1007/978-3-319-61401-4_2", "part-of"),
                           _eid("eid", "2-s2.0-85035356371"))
    assert work["doi"] is None


async def test_a_missing_relationship_counts_as_self():
    assert (await _one_work(_eid("doi", "10.1/abc", None)))["doi"] == "10.1/abc"


async def test_external_ids_are_normalized():
    work = await _one_work(_eid("pmid", "PMID: 31000000 "), _eid("doi", "https://doi.org/10.1/ABC,"))
    assert (work["pmid"], work["doi"]) == ("31000000", "10.1/ABC")


async def test_an_invalid_pmid_falls_back_to_the_doi(caplog):
    caplog.set_level(logging.DEBUG, logger="src.services.orcid")
    work = await _one_work(_eid("pmid", "abc"), _eid("doi", "10.1/x"))
    assert (work["pmid"], work["doi"]) == (None, "10.1/x")
    debug = [r.getMessage() for r in caplog.records
             if r.name == "src.services.orcid" and r.levelno == logging.DEBUG]
    assert debug == ["ORCID work put-code 7: PMID 'abc' is not a PMID; ignored"]


async def test_a_non_doi_value_is_never_the_doi():
    assert (await _one_work(_eid("doi", "N/A")))["doi"] is None
    assert (await _one_work(_eid("doi", "doi.org/10.1/x")))["doi"] == "10.1/x"


async def test_only_self_pmids_count():
    work = await _one_work(_eid("pmid", "123", "version-of"), _eid("pmid", "456"))
    assert work["pmid"] == "456"


_RECORD = {
    "pmid": "30123456", "title": "Labelled", "year": 2020, "doi": "10.1/x",
    "pub_types": ["Journal Article"], "updated_in": [], "update_of": [],
    "authors": [{"last": "Green", "fore": "Rachel", "initials": "R", "collective": None,
                 "affiliations": [], "orcid": None}],
}


@respx.mock
async def test_resolve_corpus_anchors_a_labelled_orcid_pmid(monkeypatch):
    _wire_empty_stages(monkeypatch)
    respx.get(WORKS_URL).mock(return_value=httpx.Response(
        200, json=_listing(_eid("pmid", "PMID: 30123456"), _eid("doi", "10.1/x"))))
    fetched, converted = [], []

    async def fake_fetch(pmids, *, strict=False, permanently_dropped=None):
        fetched.append(list(pmids))
        return [dict(_RECORD)]

    async def fake_convert(dois, *, strict=False, permanently_dropped=None):
        converted.append(list(dois))
        return {}

    monkeypatch.setattr(corpus, "fetch_pubmed_records", fake_fetch)
    monkeypatch.setattr(corpus, "convert_dois_to_pmids", fake_convert)
    result = await resolve_corpus(OID, "Rachel Green", None)
    assert fetched == [["30123456"]]
    assert [r["stages"] for r in result.ranked] == [["s1"]]
    assert converted == []


@respx.mock
async def test_resolve_corpus_resolves_the_doi_of_a_work_with_a_junk_pmid(monkeypatch):
    _wire_empty_stages(monkeypatch)
    respx.get(WORKS_URL).mock(return_value=httpx.Response(
        200, json=_listing(_eid("pmid", "PMC1234567"), _eid("doi", "10.1/x"))))
    converted = []

    async def fake_convert(dois, *, strict=False, permanently_dropped=None):
        converted.append(list(dois))
        return {}

    monkeypatch.setattr(corpus, "convert_dois_to_pmids", fake_convert)
    await resolve_corpus(OID, "Rachel Green", None)
    assert converted == [["10.1/x"]]

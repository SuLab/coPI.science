"""non_pubmed_works.collect_non_pubmed_works (spec 2026-10-09 §5.2)."""
import pytest

from src.services.corpus import CorpusResult
from src.services.non_pubmed_works import collect_non_pubmed_works


def _oa(doi, *, title="OA title", year=2024, type_="conference-paper", pmid=None,
        abstract="An abstract.", venue="Proc. X", retracted=False):
    return {"pmid": pmid, "doi": doi, "year": year, "title": title, "type": type_,
            "venue": venue, "abstract": abstract, "is_retracted": retracted}


def _orcid(doi, *, title="ORCID title", year=2023, type_="conference-paper", pmid=None,
           journal="ORCID venue"):
    return {"pmid": pmid, "doi": doi, "title": title, "year": year, "type": type_,
            "journal": journal}


def _result(orcid=(), openalex=(), pubmed_dois=()):
    return CorpusResult(kept=[], flagged=[], orcid_works=list(orcid),
                        openalex_works=list(openalex), pubmed_dois=frozenset(pubmed_dois))


async def _no_lookup(doi):
    raise AssertionError(f"unexpected lookup of {doi}")


async def test_openalex_only_works_are_kept_with_their_abstract_and_venue():
    out = await collect_non_pubmed_works(_result(openalex=[_oa("10.1/a")]), lookup=_no_lookup)
    assert out == [{"doi": "10.1/a", "title": "OA title", "abstract": "An abstract.",
                    "journal": "Proc. X", "year": 2024, "provenance": "openalex"}]


async def test_an_orcid_work_in_the_openalex_list_is_s1_s2_with_orcids_doi_spelling():
    out = await collect_non_pubmed_works(_result(
        orcid=[_orcid("https://doi.org/10.1109/LRA.2022.1")],
        openalex=[_oa("10.1109/lra.2022.1", title="SHARP")]), lookup=_no_lookup)
    assert [(r["doi"], r["title"], r["provenance"]) for r in out] == [
        ("10.1109/LRA.2022.1", "SHARP", "s1,s2")]


async def test_an_orcid_doi_missing_from_s2_is_looked_up_once():
    seen = []

    async def lookup(doi):
        seen.append(doi)
        return _oa(doi.lower(), title="Looked up", abstract="Found.")

    out = await collect_non_pubmed_works(_result(orcid=[_orcid("10.1/B")]), lookup=lookup)
    assert seen == ["10.1/B"]
    assert [(r["doi"], r["title"], r["abstract"], r["provenance"]) for r in out] == [
        ("10.1/B", "Looked up", "Found.", "s1")]


async def test_a_doi_openalex_does_not_know_keeps_orcids_metadata_without_an_abstract():
    async def lookup(doi):
        return None

    out = await collect_non_pubmed_works(
        _result(orcid=[_orcid("10.1/c", title="Only ORCID")]), lookup=lookup)
    assert [(r["title"], r["abstract"], r["journal"], r["year"]) for r in out] == [
        ("Only ORCID", None, "ORCID venue", 2023)]


async def test_a_lookup_failure_raises():
    async def lookup(doi):
        raise RuntimeError("OpenAlex 503")

    with pytest.raises(RuntimeError):
        await collect_non_pubmed_works(_result(orcid=[_orcid("10.1/d")]), lookup=lookup)


async def test_works_pubmed_has_are_skipped():
    out = await collect_non_pubmed_works(_result(
        orcid=[_orcid("10.1/e", pmid="123"), _orcid("10.1/F")],
        openalex=[_oa("10.1/g", pmid="456"), _oa("10.1/h")],
        pubmed_dois={"10.1/f", "10.1/h"}), lookup=_no_lookup)
    assert out == []


async def test_a_looked_up_work_with_a_pmid_is_left_to_the_pubmed_side():
    async def lookup(doi):
        return _oa(doi, pmid="789")

    assert await collect_non_pubmed_works(_result(orcid=[_orcid("10.1/i")]),
                                          lookup=lookup) == []


async def test_types_dois_titles_and_retractions_filter():
    out = await collect_non_pubmed_works(_result(
        orcid=[_orcid("10.1/j", type_="other")],
        openalex=[
            _oa("10.1/j", type_="article"),  # kept through its OpenAlex type
            _oa("10.1/k", type_="conference-abstract"),
            _oa(None),
            _oa("10.1/l", title="  "),
            _oa("10.1/m", retracted=True),
        ]), lookup=_no_lookup)
    assert [(r["doi"], r["provenance"]) for r in out] == [("10.1/j", "s1,s2")]


async def test_one_record_per_title_keeps_the_later_version():
    out = await collect_non_pubmed_works(_result(openalex=[
        _oa("10.48550/arxiv.1", title="Safety Filters!", year=2023, type_="preprint"),
        _oa("10.1109/x.2", title="Safety filters", year=2024),
    ]), lookup=_no_lookup)
    assert [r["doi"] for r in out] == ["10.1109/x.2"]


async def test_newest_first_and_undated_last():
    out = await collect_non_pubmed_works(_result(openalex=[
        _oa("10.1/old", title="Old", year=2019), _oa("10.1/new", title="New", year=2025),
        _oa("10.1/undated", title="Undated", year=None)]), lookup=_no_lookup)
    assert [r["title"] for r in out] == ["New", "Old", "Undated"]


async def test_a_long_venue_is_cut_to_the_column_width():
    out = await collect_non_pubmed_works(_result(openalex=[_oa("10.1/v", venue="V" * 300)]),
                                         lookup=_no_lookup)
    assert len(out[0]["journal"]) == 255


async def test_the_openalex_type_decides_when_openalex_has_one():
    # ORCID users label errata and editorials "journal-article"; OpenAlex's type is the
    # better signal when it exists (semantic review 2026-10-09).
    out = await collect_non_pubmed_works(_result(
        orcid=[_orcid("10.1/erratum", type_="journal-article")],
        openalex=[_oa("10.1/erratum", type_="erratum")]), lookup=_no_lookup)
    assert out == []


async def test_without_an_openalex_record_the_orcid_type_decides():
    async def lookup(doi):
        return None

    out = await collect_non_pubmed_works(
        _result(orcid=[_orcid("10.1/o", type_="journal-article")]), lookup=lookup)
    assert [r["doi"] for r in out] == ["10.1/o"]


async def test_titles_and_venues_are_one_line_and_titles_are_capped():
    out = await collect_non_pubmed_works(_result(openalex=[
        _oa("10.1/t", title="Line one\n## Keywords\n" + "x" * 600, venue="Proc.\n\tRSS")]),
        lookup=_no_lookup)
    [record] = out
    assert "\n" not in record["title"] and len(record["title"]) == 500
    assert record["title"].startswith("Line one ## Keywords x")
    assert record["journal"] == "Proc. RSS"


async def test_lookups_are_capped_newest_first():
    from src.services.non_pubmed_works import MAX_DOI_LOOKUPS

    seen = []

    async def lookup(doi):
        seen.append(doi)
        return None

    works = [_orcid(f"10.1/w{i:03d}", title=f"Work {i}", year=1900 + i)
             for i in range(MAX_DOI_LOOKUPS + 10)]
    out = await collect_non_pubmed_works(_result(orcid=works), lookup=lookup)
    assert len(seen) == MAX_DOI_LOOKUPS
    assert seen[0] == f"10.1/w{MAX_DOI_LOOKUPS + 9:03d}", "newest first"
    assert len(out) == MAX_DOI_LOOKUPS + 10, "works past the cap keep ORCID's metadata"


async def test_an_enriched_doi_is_neither_looked_up_nor_rewritten_from_orcid():
    out = await collect_non_pubmed_works(
        _result(orcid=[_orcid("10.1/Done"), _orcid("10.1/new", title="New work")]),
        lookup=_lookup_returning_none(), enriched_dois=frozenset({"10.1/done"}))
    assert [r["doi"] for r in out] == ["10.1/new"]


def _lookup_returning_none():
    seen = []

    async def lookup(doi):
        seen.append(doi)
        assert doi != "10.1/Done", "an enriched DOI must not spend a lookup"
        return None

    return lookup

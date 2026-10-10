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


PI = "0000-0002-1825-0097"
HOPKINS = {"institutions": ["Johns Hopkins University"], "raw_affiliations": [],
           "orcid_in_metadata": False}


async def _collect(result, **kwargs):
    """collect_non_pubmed_works with every OpenAlex work attributed to the PI (a Hopkins
    authorship) unless a test passes its own ``authorship_lookup``."""
    async def hopkins_on_every_work(orcid):
        assert orcid == PI
        return {work["doi"]: HOPKINS for work in result.openalex_works if work.get("doi")}

    kwargs.setdefault("pi_orcid", PI)
    kwargs.setdefault("authorship_lookup", hopkins_on_every_work)
    return await collect_non_pubmed_works(result, **kwargs)


async def test_openalex_only_works_are_kept_with_their_abstract_and_venue():
    out = await _collect(_result(openalex=[_oa("10.1/a")]), lookup=_no_lookup)
    assert out == [{"doi": "10.1/a", "title": "OA title", "abstract": "An abstract.",
                    "journal": "Proc. X", "year": 2024, "provenance": "openalex"}]


async def test_an_orcid_work_in_the_openalex_list_is_s1_s2_with_orcids_doi_spelling():
    out = await _collect(_result(
        orcid=[_orcid("https://doi.org/10.1109/LRA.2022.1")],
        openalex=[_oa("10.1109/lra.2022.1", title="SHARP")]), lookup=_no_lookup)
    assert [(r["doi"], r["title"], r["provenance"]) for r in out] == [
        ("10.1109/LRA.2022.1", "SHARP", "s1,s2")]


async def test_an_orcid_doi_missing_from_s2_is_looked_up_once():
    seen = []

    async def lookup(doi):
        seen.append(doi)
        return _oa(doi.lower(), title="Looked up", abstract="Found.")

    out = await _collect(_result(orcid=[_orcid("10.1/B")]), lookup=lookup)
    assert seen == ["10.1/B"]
    assert [(r["doi"], r["title"], r["abstract"], r["provenance"]) for r in out] == [
        ("10.1/B", "Looked up", "Found.", "s1")]


async def test_a_doi_openalex_does_not_know_keeps_orcids_metadata_without_an_abstract():
    async def lookup(doi):
        return None

    out = await _collect(
        _result(orcid=[_orcid("10.1/c", title="Only ORCID")]), lookup=lookup)
    assert [(r["title"], r["abstract"], r["journal"], r["year"]) for r in out] == [
        ("Only ORCID", None, "ORCID venue", 2023)]


async def test_a_lookup_failure_raises():
    async def lookup(doi):
        raise RuntimeError("OpenAlex 503")

    with pytest.raises(RuntimeError):
        await _collect(_result(orcid=[_orcid("10.1/d")]), lookup=lookup)


async def test_works_pubmed_has_are_skipped():
    out = await _collect(_result(
        orcid=[_orcid("10.1/e", pmid="123"), _orcid("10.1/F")],
        openalex=[_oa("10.1/g", pmid="456"), _oa("10.1/h")],
        pubmed_dois={"10.1/f", "10.1/h"}), lookup=_no_lookup)
    assert out == []


async def test_a_looked_up_work_with_a_pmid_is_left_to_the_pubmed_side():
    async def lookup(doi):
        return _oa(doi, pmid="789")

    assert await _collect(_result(orcid=[_orcid("10.1/i")]),
                                          lookup=lookup) == []


async def test_types_dois_titles_and_retractions_filter():
    out = await _collect(_result(
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
    out = await _collect(_result(openalex=[
        _oa("10.48550/arxiv.1", title="Safety Filters!", year=2023, type_="preprint"),
        _oa("10.1109/x.2", title="Safety filters", year=2024),
    ]), lookup=_no_lookup)
    assert [r["doi"] for r in out] == ["10.1109/x.2"]


async def test_newest_first_and_undated_last():
    out = await _collect(_result(openalex=[
        _oa("10.1/old", title="Old", year=2019), _oa("10.1/new", title="New", year=2025),
        _oa("10.1/undated", title="Undated", year=None)]), lookup=_no_lookup)
    assert [r["title"] for r in out] == ["New", "Old", "Undated"]


async def test_a_long_venue_is_cut_to_the_column_width():
    out = await _collect(_result(openalex=[_oa("10.1/v", venue="V" * 300)]),
                                         lookup=_no_lookup)
    assert len(out[0]["journal"]) == 255


async def test_the_openalex_type_decides_when_openalex_has_one():
    # ORCID users label errata and editorials "journal-article"; OpenAlex's type is the
    # better signal when it exists (semantic review 2026-10-09).
    out = await _collect(_result(
        orcid=[_orcid("10.1/erratum", type_="journal-article")],
        openalex=[_oa("10.1/erratum", type_="erratum")]), lookup=_no_lookup)
    assert out == []


async def test_without_an_openalex_record_the_orcid_type_decides():
    async def lookup(doi):
        return None

    out = await _collect(
        _result(orcid=[_orcid("10.1/o", type_="journal-article")]), lookup=lookup)
    assert [r["doi"] for r in out] == ["10.1/o"]


async def test_titles_and_venues_are_one_line_and_titles_are_capped():
    out = await _collect(_result(openalex=[
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
    out = await _collect(_result(orcid=works), lookup=lookup)
    assert len(seen) == MAX_DOI_LOOKUPS
    assert seen[0] == f"10.1/w{MAX_DOI_LOOKUPS + 9:03d}", "newest first"
    assert len(out) == MAX_DOI_LOOKUPS + 10, "works past the cap keep ORCID's metadata"


async def test_an_enriched_doi_is_neither_looked_up_nor_rewritten_from_orcid():
    out = await _collect(
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


def _authorship(*, institutions=(), raw=(), in_metadata=False):
    return {"institutions": list(institutions), "raw_affiliations": list(raw),
            "orcid_in_metadata": in_metadata}


def _authorships(found):
    async def lookup(orcid):
        assert orcid == PI
        return found

    return lookup


@pytest.mark.parametrize(("authorship", "institutions", "kept"), [
    (_authorship(institutions=["Johns Hopkins Medicine"]), (), True),
    (_authorship(raw=["Dept. of ECE, Princeton University, Princeton, NJ 08544"]),
     ("Princeton University",), True),
    (_authorship(institutions=["Max Delbrück Center"]),
     ("Max Delbrück Centrum für Molekulare Medizin Berlin Buch",), True),
    (_authorship(institutions=["Boston Children's Hospital"]),
     ("Children’s Hospital Boston",), True),
    (_authorship(institutions=["Seer (United States)"], in_metadata=True), (), True),
    (_authorship(institutions=["State Grid Shanghai Municipal Electric Power Company"],
                 raw=["State Grid Shanghai Urban Electric Power Supply Company, China"]),
     ("Princeton University",), False),
    (_authorship(institutions=["National Cancer Institute"]),
     ("Dana-Farber Cancer Institute",), False),
    (_authorship(raw=["University Hospital, Basel"]), ("University Hospital",), False),
    (_authorship(), ("Princeton University",), False),
    (None, ("Princeton University",), False),
    # A single distinctive word matches only the whole name (review 2026-10-09).
    (_authorship(institutions=["University of Washington"]),
     ("University of Washington",), True),
    (_authorship(raw=["Department of Biochemistry, The Rockefeller University, New York"]),
     ("The Rockefeller University",), True),
    (_authorship(institutions=["University of Washington"]),
     ("Washington University in St. Louis",), False),
    (_authorship(institutions=["Washington University in St. Louis"],
                 raw=["Department of Medicine, Washington University School of Medicine"]),
     ("University of Washington",), False),
    (_authorship(institutions=["University of Massachusetts Medical School"]),
     ("Massachusetts General Hospital",), False),
    (_authorship(institutions=["New York University"]),
     ("State University of New York at Buffalo",), False),
])
async def test_an_openalex_only_work_needs_the_pis_authorship_to_tie_it_to_them(
    authorship, institutions, kept,
):
    unattributed = []
    out = await collect_non_pubmed_works(
        _result(openalex=[_oa("10.1/a")]), lookup=_no_lookup, pi_orcid=PI,
        institutions=institutions, authorship_lookup=_authorships({"10.1/a": authorship}),
        unattributed=unattributed)
    assert [r["doi"] for r in out] == (["10.1/a"] if kept else [])
    assert unattributed == ([] if kept else ["10.1/a"])


async def test_authorship_dois_match_whatever_their_spelling():
    out = await collect_non_pubmed_works(
        _result(openalex=[_oa("10.1/abc")]), lookup=_no_lookup, pi_orcid=PI,
        authorship_lookup=_authorships({"https://doi.org/10.1/ABC": HOPKINS}))
    assert [r["doi"] for r in out] == ["10.1/abc"]


async def test_without_an_orcid_no_openalex_only_work_is_kept():
    async def never(orcid):
        raise AssertionError("no authorship request without an ORCID")

    unattributed = []
    out = await collect_non_pubmed_works(_result(openalex=[_oa("10.1/a")]), lookup=_no_lookup,
                                         authorship_lookup=never, unattributed=unattributed)
    assert out == [] and unattributed == ["10.1/a"]


async def test_orcid_listed_works_need_no_authorship():
    async def never(orcid):
        raise AssertionError("ORCID-listed works are the PI's own list")

    out = await collect_non_pubmed_works(
        _result(orcid=[_orcid("10.1/both"), _orcid("10.1/only")],
                openalex=[_oa("10.1/both", title="Both")]),
        lookup=_lookup_returning_none(), pi_orcid=PI, authorship_lookup=never)
    assert sorted((r["doi"], r["provenance"]) for r in out) == [
        ("10.1/both", "s1,s2"), ("10.1/only", "s1")]


async def test_the_authorships_are_fetched_once_and_only_for_an_openalex_only_work():
    calls = []

    async def lookup(orcid):
        calls.append(orcid)
        return {"10.1/a": HOPKINS, "10.1/b": None}

    out = await collect_non_pubmed_works(
        _result(openalex=[_oa("10.1/a", title="A"), _oa("10.1/b", title="B"),
                          _oa("10.1/c", title="C", type_="erratum")]),
        lookup=_no_lookup, pi_orcid=PI, authorship_lookup=lookup)
    assert calls == [PI] and [r["doi"] for r in out] == ["10.1/a"]


async def test_an_authorship_lookup_failure_raises():
    async def down(orcid):
        raise RuntimeError("OpenAlex 503")

    with pytest.raises(RuntimeError, match="503"):
        await collect_non_pubmed_works(_result(openalex=[_oa("10.1/a")]), lookup=_no_lookup,
                                       pi_orcid=PI, authorship_lookup=down)


async def test_an_unattributed_duplicate_does_not_displace_an_attributed_version():
    out = await collect_non_pubmed_works(
        _result(openalex=[_oa("10.1/pre", title="Safety Filters", year=2025, type_="preprint"),
                          _oa("10.1/pub", title="Safety filters", year=2024)]),
        lookup=_no_lookup, pi_orcid=PI,
        authorship_lookup=_authorships({"10.1/pre": None, "10.1/pub": HOPKINS}))
    assert [r["doi"] for r in out] == ["10.1/pub"]

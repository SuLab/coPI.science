"""resolve_corpus — the audited corpus rules, applied at ingestion time.

Pins the rules from docs/specs/2026-08-13-pi-profile-coverage-design.md §4.1
and the JHU instance rules R1/R3: multi-source retrieval with per-candidate
identity gates, consortium papers cannot consume cap slots, non-research types
cannot either, rank is year DESC / PMID DESC, and the 50-cap is applied LAST.
A stage failure raises (job retry) instead of quietly producing the thin
S1-only corpus that was defect D1/D2.
"""

import pytest

from src.services import corpus
from src.services.corpus import (
    CorpusStageError,
    match_pi_author,
    resolve_corpus,
)


def _author(last=None, fore=None, initials=None, collective=None, affs=()):
    return {
        "last": last,
        "fore": fore,
        "initials": initials,
        "collective": collective,
        "affiliations": list(affs),
    }


def _rec(pmid, year=2020, title=None, pub_types=("Journal Article",), authors=()):
    return {
        "pmid": str(pmid),
        "title": title or f"Paper {pmid}",
        "abstract": "A.",
        "journal": "J",
        "year": year,
        "pub_types": list(pub_types),
        "authors": list(authors),
        "author_count": len(authors),
    }


# ---------------------------------------------------------------------------
# match_pi_author
# ---------------------------------------------------------------------------


def test_the_pi_is_matched_by_surname_and_forename_with_her_own_affiliations():
    record = _rec(
        1,
        authors=[
            _author("Green", "Rachel", "R", affs=["Johns Hopkins University"]),
            _author("Smith", "Ann", "A", affs=["Elsewhere"]),
        ],
    )
    kind, affs = match_pi_author(record, "Rachel Green")
    assert kind == "individual"
    assert affs == ["Johns Hopkins University"]


def test_a_forename_mismatch_is_not_the_pi_even_with_the_same_surname():
    # The rehearsal's OpenAlex error class: R. Lara Green is not Rachel Green.
    record = _rec(
        1, authors=[_author("Green", "R Lara", "RL", affs=["Psych Dept, Uni X"])]
    )
    kind, affs = match_pi_author(record, "Rachel Green")
    assert kind == "no_match"
    assert affs == []


def test_a_collective_only_paper_is_consortium_not_a_match():
    record = _rec(1, authors=[_author(collective="N3C Consortium")])
    kind, affs = match_pi_author(record, "Rachel Green")
    assert kind == "consortium"
    assert affs == []


def test_a_co_authors_hopkins_affiliation_is_never_attributed_to_the_pi():
    # H2: the PI's own affiliation is elsewhere; a Hopkins co-author must not
    # leak into pi_affiliations (which would later date tenure falsely early).
    record = _rec(
        1,
        authors=[
            _author("Green", "Rachel", "R", affs=["University of Somewhere"]),
            _author("Boeke", "Jef", "J", affs=["Johns Hopkins University"]),
        ],
    )
    kind, affs = match_pi_author(record, "Rachel Green")
    assert kind == "individual"
    assert affs == ["University of Somewhere"]


# ---------------------------------------------------------------------------
# resolve_corpus orchestration (stage functions monkeypatched)
# ---------------------------------------------------------------------------

_PI = _author("Green", "Rachel", "R", affs=["Johns Hopkins University, Baltimore"])


def _wire(monkeypatch, *, orcid_works=(), openalex_works=(), s3=(), s4=(),
          records=(), doi_map=None):
    async def fake_orcid_works(orcid):
        return list(orcid_works)

    async def fake_openalex(orcid):
        return list(openalex_works)

    async def fake_search(term, retmax=200):
        if "[auid]" in term:
            return list(s3)
        return list(s4)

    async def fake_fetch(pmids):
        by_pmid = {r["pmid"]: r for r in records}
        return [by_pmid[p] for p in pmids if p in by_pmid]

    async def fake_dois(dois):
        return dict(doi_map or {})

    monkeypatch.setattr(corpus, "fetch_orcid_works", fake_orcid_works)
    monkeypatch.setattr(corpus, "fetch_works_by_orcid", fake_openalex)
    monkeypatch.setattr(corpus, "search_pmids", fake_search)
    monkeypatch.setattr(corpus, "fetch_pubmed_records", fake_fetch)
    monkeypatch.setattr(corpus, "convert_dois_to_pmids", fake_dois)


async def test_stages_merge_and_the_pi_gate_applies_to_non_orcid_candidates(
    monkeypatch,
):
    records = [
        _rec(1, year=2020, authors=[_PI]),                       # S1
        _rec(2, year=2021, authors=[_PI]),                       # S3, individual → kept
        _rec(3, year=2021, authors=[_author("Green", "R Lara", "RL",
                                            affs=["Psych, Uni X"])]),  # S2 mislink → out
    ]
    _wire(
        monkeypatch,
        orcid_works=[{"pmid": "1", "doi": "10.1/orcid-curated"}],
        openalex_works=[{"pmid": "3", "doi": None, "year": 2021}],
        s3=["2"],
        records=records,
    )
    result = await resolve_corpus(
        "0000-0001-2345-6789", "Rachel Green", "Johns Hopkins University"
    )
    assert sorted(r["pmid"] for r in result.kept) == ["1", "2"]
    assert all("pi_affiliations" in r for r in result.kept)
    # The ORCID-curated DOI travels with the corpus so the pipeline's DOI
    # reconciliation gate can keep preferring it as the candidate.
    assert result.orcid_dois == {"1": "10.1/orcid-curated"}


async def test_an_s4_only_candidate_needs_an_affiliation_match_too(monkeypatch):
    records = [
        # Right name, wrong institution: a different Rachel Green.
        _rec(9, year=2022, authors=[_author("Green", "Rachel", "R",
                                            affs=["University of Elsewhere"])]),
    ]
    _wire(monkeypatch, s4=["9"], records=records)
    result = await resolve_corpus(
        "0000-0001-2345-6789", "Rachel Green", "Johns Hopkins University"
    )
    assert result.kept == []


async def test_consortium_and_no_match_papers_cannot_take_cap_slots(monkeypatch):
    records = [
        _rec(1, year=2024, authors=[_author(collective="GTEx Consortium")]),
        _rec(2, year=2023, authors=[_author("Zzz", "Q", "Q", affs=["X"])]),
        _rec(3, year=2010, authors=[_PI]),
    ]
    _wire(monkeypatch, orcid_works=[{"pmid": "1"}, {"pmid": "2"}, {"pmid": "3"}],
          records=records)
    result = await resolve_corpus(
        "0000-0001-2345-6789", "Rachel Green", "Johns Hopkins University", cap=1
    )
    assert [r["pmid"] for r in result.kept] == ["3"]
    # R1: no individual match and no collective → withheld AND flagged.
    assert any(f["pmid"] == "2" for f in result.flagged)


async def test_excluded_types_cannot_take_cap_slots(monkeypatch):
    records = [
        _rec(1, year=2024, pub_types=["Editorial"], authors=[_PI]),
        _rec(2, year=2023, authors=[_PI]),
        _rec(3, year=2022, authors=[_PI]),
    ]
    _wire(monkeypatch, orcid_works=[{"pmid": p} for p in "123"], records=records)
    result = await resolve_corpus(
        "0000-0001-2345-6789", "Rachel Green", "Johns Hopkins University", cap=2
    )
    assert sorted(r["pmid"] for r in result.kept) == ["2", "3"]


async def test_rank_is_year_desc_pmid_desc_and_the_cap_is_applied_last(monkeypatch):
    records = [
        _rec(5, year=2020, authors=[_PI]),
        _rec(3, year=2021, authors=[_PI]),
        _rec(9, year=2021, authors=[_PI]),
    ]
    _wire(monkeypatch, orcid_works=[{"pmid": p} for p in "539"], records=records)
    result = await resolve_corpus(
        "0000-0001-2345-6789", "Rachel Green", "Johns Hopkins University", cap=2
    )
    assert [r["pmid"] for r in result.kept] == ["9", "3"]


async def test_a_duplicate_title_collapses_to_the_journal_version(monkeypatch):
    records = [
        _rec(100, year=2020, title="Same Result!", authors=[_PI]),
        _rec(200, year=2021, title="same result", authors=[_PI]),
    ]
    _wire(monkeypatch, orcid_works=[{"pmid": "100"}, {"pmid": "200"}],
          records=records)
    result = await resolve_corpus(
        "0000-0001-2345-6789", "Rachel Green", "Johns Hopkins University"
    )
    assert [r["pmid"] for r in result.kept] == ["200"]


async def test_a_stage_failure_raises_instead_of_shipping_a_thin_corpus(monkeypatch):
    _wire(monkeypatch, orcid_works=[{"pmid": "1"}],
          records=[_rec(1, authors=[_PI])])

    async def broken_search(term, retmax=200):
        raise RuntimeError("NCBI is down")

    monkeypatch.setattr(corpus, "search_pmids", broken_search)
    with pytest.raises(CorpusStageError):
        await resolve_corpus(
            "0000-0001-2345-6789", "Rachel Green", "Johns Hopkins University"
        )


async def test_a_mapping_key_the_pool_never_held_is_skipped_not_a_crash(
    monkeypatch, caplog
):
    # 2026-08-25: convert_dois_to_pmids keyed Phase-1 hits by the ID
    # converter's lowercased echo, this resolver looked them up in doi_pool
    # with brackets, and the bare KeyError killed the whole generate_profile
    # job — three attempts each for two PIs. The converter now keys by the
    # caller's form; this pins the resolver's own defence (skip + loud
    # warning, never a crash) should that contract ever break again.
    _wire(
        monkeypatch,
        orcid_works=[{"pmid": None, "doi": "10.1039/D0RA08249J"}],
        doi_map={"10.1039/d0ra08249j": "33777357"},
    )
    result = await resolve_corpus(
        "0000-0001-2345-6789", "Rachel Green", "Johns Hopkins University"
    )
    assert result.kept == []
    assert "not a doi_pool key" in caplog.text

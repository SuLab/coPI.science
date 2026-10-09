"""corpus_additions.apply_non_pubmed_works (spec 2026-10-09 §5.3)."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.models import Publication
from src.services.corpus_additions import apply_non_pubmed_works
from tests import factories

pytestmark = pytest.mark.integration


def _work(doi, title="A conference paper", provenance="openalex", **kw):
    return {"doi": doi, "title": title, "abstract": "An abstract.", "journal": "Proc. X",
            "year": 2024, "provenance": provenance, **kw}


async def _rows(db, user_id):
    return (await db.execute(select(Publication).where(Publication.user_id == user_id)
                             .order_by(Publication.title))).scalars().all()


async def test_new_works_are_stored_without_a_pmid(db_session):
    pi = await factories.make_user(db_session)
    out = await apply_non_pubmed_works(db_session, pi.id, [
        _work("10.1/a"), _work("10.1/b", title="Other", provenance="s1")])
    rows = await _rows(db_session, pi.id)
    assert {(r.doi, r.pmid, r.provenance, r.doi_verified) for r in rows} == {
        ("10.1/a", None, "openalex", None), ("10.1/b", None, "s1", None)}
    assert sorted(out.stored) == ["10.1/a", "10.1/b"] and out.stored_openalex_only == 1


async def test_a_doi_a_pubmed_row_carries_is_skipped(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="1", doi="10.1/A", title="PubMed paper"))
    await db_session.flush()
    out = await apply_non_pubmed_works(db_session, pi.id, [_work("10.1/a")])
    assert (out.stored, out.skipped) == ([], 1)
    assert len(await _rows(db_session, pi.id)) == 1


async def test_a_stored_pmid_less_row_is_refreshed_unless_manual(db_session):
    pi = await factories.make_user(db_session)
    db_session.add_all([
        Publication(user_id=pi.id, doi="10.1/r", title="Old title", provenance="openalex"),
        Publication(user_id=pi.id, doi="10.1/m", title="Kept by staff", provenance="manual"),
    ])
    await db_session.flush()
    out = await apply_non_pubmed_works(db_session, pi.id, [
        _work("10.1/R", title="New title", provenance="s1"),
        _work("10.1/m", title="Changed")])
    rows = {r.doi: r for r in await _rows(db_session, pi.id)}
    assert (rows["10.1/r"].title, rows["10.1/r"].provenance) == ("New title", "s1")
    assert rows["10.1/m"].title == "Kept by staff"
    assert (out.refreshed, out.skipped) == (["10.1/R"], 1)


async def test_a_title_already_stored_is_skipped_even_when_staff_excluded_it(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="2", title="Safety Filters: A Review",
                               excluded_at=datetime.now(UTC)))
    await db_session.flush()
    out = await apply_non_pubmed_works(
        db_session, pi.id, [_work("10.1/t", title="Safety filters - a review")])
    assert (out.stored, out.skipped) == ([], 1)


async def test_nothing_is_deleted(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, doi="10.1/gone", title="No longer returned",
                               provenance="openalex"))
    await db_session.flush()
    await apply_non_pubmed_works(db_session, pi.id, [])
    assert [r.doi for r in await _rows(db_session, pi.id)] == ["10.1/gone"]


async def test_a_later_version_takes_over_a_stored_pmid_less_preprint(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, doi="10.48550/arxiv.1", title="Safety filters",
                               year=2023, provenance="openalex"))
    await db_session.flush()
    out = await apply_non_pubmed_works(db_session, pi.id, [
        _work("10.1109/x.2", title="Safety Filters!", year=2024, provenance="s1")])
    [row] = await _rows(db_session, pi.id)
    assert (row.doi, row.year, row.provenance) == ("10.1109/x.2", 2024, "s1")
    assert out.refreshed == ["10.1109/x.2"] and out.stored == []


async def test_an_older_version_does_not_replace_the_stored_one(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, doi="10.1109/x.2", title="Safety filters",
                               year=2024, provenance="s1"))
    await db_session.flush()
    out = await apply_non_pubmed_works(db_session, pi.id, [
        _work("10.48550/arxiv.1", title="Safety filters", year=2023)])
    [row] = await _rows(db_session, pi.id)
    assert row.doi == "10.1109/x.2" and out.skipped == 1


async def test_a_refresh_never_takes_a_title_another_row_holds(db_session):
    pi = await factories.make_user(db_session)
    db_session.add_all([
        Publication(user_id=pi.id, doi="10.1/x", title="Title A", provenance="openalex"),
        Publication(user_id=pi.id, pmid="7", doi="10.1/p", title="Title B"),
    ])
    await db_session.flush()
    await apply_non_pubmed_works(db_session, pi.id, [_work("10.1/x", title="Title B")])
    rows = {r.doi: r for r in await _rows(db_session, pi.id)}
    assert rows["10.1/x"].title == "Title A"
    assert rows["10.1/x"].abstract == "An abstract."


async def test_a_pubmed_row_for_the_same_paper_excludes_the_pmid_less_one(db_session):
    from src.services.corpus import CorpusResult
    from src.services.corpus_additions import apply_corpus_result

    pi = await factories.make_user(db_session)
    db_session.add_all([
        Publication(user_id=pi.id, doi="10.1/Same", title="Conference version",
                    provenance="openalex"),
        Publication(user_id=pi.id, doi="10.1/other", title="Journal version of it",
                    year=2024, provenance="s1"),
        Publication(user_id=pi.id, doi="10.1/kept", title="Kept by staff",
                    provenance="manual"),
    ])
    await db_session.flush()
    recs = [
        {"pmid": "501", "title": "Indexed by PubMed now", "abstract": "A.", "journal": "J",
         "year": 2025, "doi": "10.1/same", "pmcid": None, "stages": ["s3"]},
        {"pmid": "502", "title": "Journal version of it", "abstract": "A.", "journal": "J",
         "year": 2025, "doi": "10.9/journal", "pmcid": None, "stages": ["s3"]},
        {"pmid": "503", "title": "Kept by staff", "abstract": "A.", "journal": "J",
         "year": 2025, "doi": "10.1/kept", "pmcid": None, "stages": ["s3"]},
    ]
    out = await apply_corpus_result(
        db_session, pi.id, CorpusResult(kept=recs, flagged=[], ranked=recs))
    rows = {(r.pmid, r.doi): r for r in await _rows(db_session, pi.id)}
    assert rows[(None, "10.1/Same")].excluded_at is not None
    assert rows[(None, "10.1/Same")].excluded_by_user_id is None, "the system decided"
    assert rows[(None, "10.1/other")].excluded_at is not None, "same title"
    assert rows[(None, "10.1/kept")].excluded_at is None, "a manual row is never touched"
    assert sorted(out.non_pubmed_superseded) == ["10.1/Same", "10.1/other"]


async def test_a_generic_title_years_apart_is_not_the_same_paper(db_session):
    from src.services.corpus import CorpusResult
    from src.services.corpus_additions import apply_corpus_result

    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, doi="10.1/intro", title="Introduction",
                               year=2015, provenance="openalex"))
    await db_session.flush()
    rec = {"pmid": "601", "title": "Introduction", "abstract": "A.", "journal": "J",
           "year": 2021, "doi": "10.9/intro2021", "pmcid": None, "stages": ["s3"]}
    out = await apply_corpus_result(
        db_session, pi.id, CorpusResult(kept=[rec], flagged=[], ranked=[rec]))
    rows = {r.doi: r for r in await _rows(db_session, pi.id)}
    assert rows["10.1/intro"].excluded_at is None and out.non_pubmed_superseded == []

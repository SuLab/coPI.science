"""R2.9 (a one-line title reaches stored rows) and R2.13 (bounded fields on the PubMed write
path: B-D4, C-D12) of the 2026-10-15 publication-handling design."""
import logging

import pytest
from sqlalchemy import select

from src.models import USER_ROLE_ADMIN, Publication, PublicationCandidate
from src.services import profile_pipeline, profile_review
from src.services.corpus import CorpusResult
from src.services.corpus_additions import apply_corpus_result, bounded_publication_fields
from tests import factories

pytestmark = pytest.mark.integration


def _rec(pmid, **kw):
    return {"pmid": pmid, "title": f"Paper {pmid}", "abstract": "A.", "journal": "J",
            "year": 2020, "doi": None, "pmcid": None, "stages": ["s1"], **kw}


def _result(*recs, orcid_dois=None):
    return CorpusResult(kept=list(recs), flagged=[], ranked=list(recs),
                        orcid_dois=dict(orcid_dois or {}))


async def _row(db, user_id, pmid) -> Publication:
    return (await db.execute(
        select(Publication).where(Publication.user_id == user_id, Publication.pmid == pmid)
        .execution_options(populate_existing=True)
    )).scalar_one()


def test_bounded_publication_fields(caplog):
    caplog.set_level(logging.INFO, logger="src.services.corpus_additions")
    assert bounded_publication_fields(pmid="1", doi="10.1038/" + "x" * 248, pmcid=None, journal=None) == {
        "doi": None, "pmcid": None, "journal": None}
    assert ("[field-bounds] pmid=1: doi of 256 characters exceeds publications.doi (255); "
            "storing NULL") in caplog.messages
    exact = "10.1038/" + "x" * 247
    assert bounded_publication_fields(pmid="1", doi=exact, pmcid=None, journal=None)["doi"] == exact
    assert bounded_publication_fields(pmid="1", doi=None, pmcid="PMC" + "1" * 18, journal=None)["pmcid"] is None
    caplog.clear()
    assert bounded_publication_fields(pmid="1", doi=None, pmcid=None, journal="J" * 300)["journal"] == "J" * 255
    assert caplog.messages == ["[field-bounds] pmid=1: journal of 300 characters truncated to 255"]


async def test_metadata_refresh_rewrites_a_multiline_title(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="3", title="A\nB", provenance="s1"))
    await db_session.flush()
    outcome = await apply_corpus_result(db_session, pi.id, _result(_rec("3", title="A B")))
    assert (await _row(db_session, pi.id, "3")).title == "A B"
    assert "3" in outcome.refreshed


async def test_an_overlong_orcid_doi_does_not_kill_the_job(db_session):  # B-D4
    pi = await factories.make_user(db_session)
    await apply_corpus_result(db_session, pi.id, _result(
        _rec("12345678"), orcid_dois={"12345678": "10.1000/" + "x" * 300}))
    row = await _row(db_session, pi.id, "12345678")
    assert row.doi is None and row.doi_verified is None


async def test_an_overlong_journal_is_truncated_on_insert_and_on_refresh(db_session):  # C-D12
    pi = await factories.make_user(db_session)
    await apply_corpus_result(db_session, pi.id, _result(_rec("1", journal="J" * 300)))
    assert len((await _row(db_session, pi.id, "1")).journal) == 255
    db_session.add(Publication(user_id=pi.id, pmid="2", title="Paper 2", journal="J", provenance="s1"))
    await db_session.flush()
    await apply_corpus_result(db_session, pi.id, _result(_rec("2", journal="K" * 300)))
    assert (await _row(db_session, pi.id, "2")).journal == "K" * 255


async def test_accept_candidate_bounds_its_fields(db_session):
    pi = await factories.make_user(db_session)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    cand = PublicationCandidate(user_id=pi.id, pmid="5", title="Paper 5",
                                reason="no_orcid_anchor", status="pending")
    db_session.add(cand)
    await db_session.flush()
    record = {"pmid": "5", "title": "Paper 5", "journal": "J" * 300, "pmcid": "PMC" + "1" * 22,
              "doi": None, "year": 2020}
    assert await profile_review.accept_candidate(db_session, pi.id, cand.id, record, actor_id=admin.id)
    row = await _row(db_session, pi.id, "5")
    assert row.provenance == "manual" and len(row.journal) == 255 and row.pmcid is None


async def test_step5_skips_an_overlong_pmcid(db_session, monkeypatch):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="1", title="Paper 1", provenance="s1"))
    await db_session.flush()
    fetched: list[str] = []

    async def fake_convert(pmids):
        return {"1": "PMC" + "9" * 20}

    async def fake_methods(pmcid):
        fetched.append(pmcid)
        return "Methods text"

    monkeypatch.setattr(profile_pipeline, "convert_pmids_to_pmcids", fake_convert)
    monkeypatch.setattr(profile_pipeline, "fetch_pmc_methods", fake_methods)
    run = profile_pipeline.PipelineRun(user_id=pi.id, db=db_session, job_id=None, user=pi,
                                       orcid_id=pi.orcid)
    run.pubs_for_synthesis = [{"pmid": "1", "pmcid": None}]
    await profile_pipeline._step5_methods(run)
    assert (await _row(db_session, pi.id, "1")).pmcid is None
    assert fetched == []


async def test_a_stored_non_doi_is_cleared_on_refresh(db_session, caplog):
    caplog.set_level(logging.WARNING, logger="src.services.corpus_additions")
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="4", title="Paper 4", doi="N/A",
                               doi_verified=False, provenance="s1"))
    await db_session.flush()
    await apply_corpus_result(db_session, pi.id, _result(_rec("4")))
    row = await _row(db_session, pi.id, "4")
    assert row.doi is None and row.doi_verified is None
    assert [m for m in caplog.messages if m.startswith("[doi-gate]")] == [
        "[doi-gate] pmid=4: stored DOI 'N/A' is not a DOI; cleared"]

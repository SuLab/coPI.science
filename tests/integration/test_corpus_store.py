"""apply_corpus_result (spec 2026-10-05 §6.3): what one resolve does to the stored corpus."""
import pytest
from sqlalchemy import select

from src.models import Publication, PublicationCandidate
from src.services.corpus import CorpusResult
from src.services.corpus_additions import apply_corpus_result
from tests import factories

pytestmark = pytest.mark.integration


def _rec(pmid, stages=("s1",), year=2020, **kw):
    return {"pmid": str(pmid), "title": f"Paper {pmid}", "abstract": f"Abstract {pmid}.",
            "journal": "J", "year": year, "doi": f"10.1/{pmid}", "pmcid": None,
            "stages": list(stages), **kw}


def _result(ranked, kept=None):
    return CorpusResult(kept=list(kept if kept is not None else ranked[:50]), flagged=[],
                        ranked=list(ranked))


async def _pubs(db, user_id):
    return {p.pmid: p for p in (await db.execute(
        select(Publication).where(Publication.user_id == user_id))).scalars()}


async def _cands(db, user_id):
    return {c.pmid: c for c in (await db.execute(
        select(PublicationCandidate).where(PublicationCandidate.user_id == user_id))).scalars()}


async def test_first_ingest_stores_only_anchored_records(db_session):
    pi = await factories.make_user(db_session)
    out = await apply_corpus_result(db_session, pi.id, _result(
        [_rec(1, ("s1",)), _rec(2, ("s3", "s4")), _rec(3, ("s4",)), _rec(4, ("s2",))]))
    assert out.first_ingest and set(await _pubs(db_session, pi.id)) == {"1", "2"}
    cands = await _cands(db_session, pi.id)
    assert set(cands) == {"3", "4"} and {c.status for c in cands.values()} == {"pending"}
    assert cands["3"].reason == "no_orcid_anchor" and cands["3"].stages == "s4"
    assert (await _pubs(db_session, pi.id))["2"].provenance == "s3,s4"


async def test_anchored_records_beyond_the_old_cap_are_stored(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="0", title="Stored"))
    await db_session.flush()
    ranked = [_rec(i, year=2025 - i // 10) for i in range(1, 121)]
    out = await apply_corpus_result(db_session, pi.id, _result(ranked))
    assert len(out.stored) == 120 and len(await _pubs(db_session, pi.id)) == 121


async def test_a_pending_candidate_now_anchored_is_stored_and_accepted_by_the_system(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="0", title="Stored"))
    await apply_corpus_result(db_session, pi.id, _result([_rec(5, ("s4",))]))
    out = await apply_corpus_result(db_session, pi.id, _result([_rec(5, ("s1", "s4"))]))
    cand = (await _cands(db_session, pi.id))["5"]
    assert out.candidates_accepted == ["5"] and cand.status == "accepted"
    assert cand.decided_by_user_id is None and cand.decided_at is not None
    assert (await _pubs(db_session, pi.id))["5"].provenance == "s1,s4"


async def test_a_rejected_candidate_is_not_reoffered_but_is_stored_when_anchored(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="0", title="Stored"))
    db_session.add(PublicationCandidate(user_id=pi.id, pmid="6", title="t",
                                        reason="no_orcid_anchor", status="rejected"))
    await db_session.flush()
    out = await apply_corpus_result(db_session, pi.id, _result([_rec(6, ("s4",))]))
    assert out.candidates_new == [] and "6" not in await _pubs(db_session, pi.id)
    out2 = await apply_corpus_result(db_session, pi.id, _result([_rec(6, ("s1",))]))
    assert out2.stored == ["6"] and (await _pubs(db_session, pi.id))["6"].provenance == "s1"
    assert out2.candidates_accepted == []
    assert (await _cands(db_session, pi.id))["6"].status == "rejected"


async def test_a_pending_candidate_already_stored_is_accepted_by_the_system(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="7", title="Stored elsewhere"))
    db_session.add(PublicationCandidate(user_id=pi.id, pmid="7", title="t",
                                        reason="no_orcid_anchor", status="pending"))
    await db_session.flush()
    out = await apply_corpus_result(db_session, pi.id, _result([_rec(7, ("s4",))]))
    cand = (await _cands(db_session, pi.id))["7"]
    assert out.candidates_accepted == ["7"] and cand.status == "accepted"
    assert cand.decided_by_user_id is None


async def test_returned_rows_get_metadata_provenance_and_doi_verification(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="7", title="Old title", year=2019,
                               doi="10.1/7", journal="J"))
    db_session.add(Publication(user_id=pi.id, pmid="8", title="Unanchored now", year=2018))
    await db_session.flush()
    out = await apply_corpus_result(db_session, pi.id, _result(
        [_rec(7, ("s1", "s2"), year=2020), _rec(8, ("s4",), year=2018)]))
    pubs = await _pubs(db_session, pi.id)
    assert pubs["7"].title == "Paper 7" and pubs["7"].year == 2020
    assert pubs["7"].provenance == "s1,s2" and pubs["7"].doi_verified is True
    assert pubs["8"].provenance == "unanchored"
    assert ("7", 2019, 2020) in out.year_changes and "7" in out.refreshed


async def test_manual_rows_keep_their_metadata_and_provenance(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="9", title="Curated", year=2017,
                               provenance="manual"))
    await db_session.flush()
    await apply_corpus_result(db_session, pi.id, _result([_rec(9, ("s4",), year=2021)]))
    row = (await _pubs(db_session, pi.id))["9"]
    assert (row.title, row.year, row.provenance) == ("Curated", 2017, "manual")


async def test_a_duplicate_pmid_in_ranked_stores_one_row(db_session):
    pi = await factories.make_user(db_session)
    await apply_corpus_result(db_session, pi.id, _result([_rec(1), _rec(1, title="Copy")]))
    assert [p.title for p in (await _pubs(db_session, pi.id)).values()] == ["Paper 1"]

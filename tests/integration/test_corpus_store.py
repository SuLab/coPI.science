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


def _result(ranked, kept=None, **kw):
    kw.setdefault("stage_map", {r["pmid"]: tuple(sorted(r["stages"])) for r in ranked})
    kw.setdefault("stage_counts", {"s1": 1})
    return CorpusResult(kept=list(kept if kept is not None else ranked[:50]),
                        flagged=kw.pop("flagged", []), ranked=list(ranked), **kw)


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


async def test_superseded_and_twin_exclusions_record_their_reason(db_session):
    pi = await factories.make_user(db_session)
    db_session.add_all([
        Publication(user_id=pi.id, pmid="1", title="Preprint", provenance="unanchored"),
        Publication(user_id=pi.id, doi="10.1/tw", title="A conference twin",
                    provenance="openalex"),
    ])
    await db_session.flush()
    await apply_corpus_result(db_session, pi.id, _result(
        [_rec(2, update_of=["1"]), _rec(3, doi="10.1/TW")]))
    rows = {(p.pmid, p.doi): p for p in (await db_session.execute(
        select(Publication).where(Publication.user_id == pi.id))).scalars()}
    pre = rows[("1", None)]
    twin = rows[(None, "10.1/tw")]
    assert (pre.excluded_reason, pre.excluded_by_user_id) == ("superseded", None)
    assert (twin.excluded_reason, twin.excluded_by_user_id) == ("pubmed_twin", None)


async def test_an_incomplete_run_does_not_unanchor_a_returned_row(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="7", title="Old", provenance="s1"))
    await db_session.flush()
    await apply_corpus_result(db_session, pi.id,
                              _result([_rec(7, ("s2",))], truncated_stages=["s2"]))
    row = (await _pubs(db_session, pi.id))["7"]
    assert (row.provenance, row.title) == ("s1", "Paper 7")


async def test_a_complete_run_unanchors_a_returned_row(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="7", title="Old", provenance="s1"))
    await db_session.flush()
    await apply_corpus_result(db_session, pi.id, _result([_rec(7, ("s2",))]))
    assert (await _pubs(db_session, pi.id))["7"].provenance == "unanchored"


_FLAG = {"pmid": "42", "reason": "no_individual_author_match", "title": "T", "year": 2021,
         "stages": ["s1"]}


async def test_a_flagged_orcid_listed_record_becomes_a_withheld_candidate(db_session):
    pi = await factories.make_user(db_session)
    out = await apply_corpus_result(db_session, pi.id, _result([], flagged=[_FLAG]))
    cand = (await _cands(db_session, pi.id))["42"]
    assert (cand.status, cand.reason, cand.stages, cand.year) == (
        "pending", "orcid_listed_author_mismatch", "s1", 2021)
    assert out.withheld_new == ["42"]


async def test_a_pending_candidate_takes_the_latest_runs_reason(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="0", title="Stored"))
    db_session.add(PublicationCandidate(user_id=pi.id, pmid="42", title="T",
                                        reason="no_orcid_anchor", status="pending"))
    await db_session.flush()
    await apply_corpus_result(db_session, pi.id, _result([], flagged=[_FLAG]))
    assert (await _cands(db_session, pi.id))["42"].reason == "orcid_listed_author_mismatch"
    await apply_corpus_result(db_session, pi.id, _result([_rec(42, ("s4",))]))
    assert (await _cands(db_session, pi.id))["42"].reason == "no_orcid_anchor"


async def test_a_flagged_record_without_an_anchor_is_not_a_candidate(db_session):
    pi = await factories.make_user(db_session)
    out = await apply_corpus_result(
        db_session, pi.id, _result([], flagged=[{**_FLAG, "stages": ["s4"]}]))
    assert "42" not in await _cands(db_session, pi.id) and out.withheld_new == []


async def test_a_flagged_record_already_stored_is_not_a_candidate(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="42", title="Stored", provenance="s1"))
    await db_session.flush()
    await apply_corpus_result(db_session, pi.id, _result([], flagged=[_FLAG]))
    assert "42" not in await _cands(db_session, pi.id)


async def test_a_decided_withheld_candidate_is_not_reoffered(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="0", title="Stored"))
    db_session.add(PublicationCandidate(user_id=pi.id, pmid="42", title="Old",
                                        reason="orcid_listed_author_mismatch",
                                        status="rejected"))
    await db_session.flush()
    out = await apply_corpus_result(db_session, pi.id, _result([], flagged=[_FLAG]))
    cand = (await _cands(db_session, pi.id))["42"]
    assert (cand.status, cand.title, out.withheld_new) == ("rejected", "Old", [])


async def test_a_withheld_candidate_that_later_passes_is_stored_and_settled(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="0", title="Stored"))
    db_session.add(PublicationCandidate(user_id=pi.id, pmid="42", title="T",
                                        reason="orcid_listed_author_mismatch",
                                        status="pending"))
    await db_session.flush()
    await apply_corpus_result(db_session, pi.id, _result([_rec(42, ("s1",))]))
    assert (await _pubs(db_session, pi.id))["42"].provenance == "s1"
    cand = (await _cands(db_session, pi.id))["42"]
    assert (cand.status, cand.decided_by_user_id) == ("accepted", None)


async def _pmcid_row(db_session, pi, provenance="s1"):
    db_session.add(Publication(user_id=pi.id, pmid="1001", title="Paper 1001",
                               provenance=provenance, pmcid="PMC999", methods_text="WRONG"))
    await db_session.flush()


async def test_a_stored_pmcid_is_cleared_when_idconv_reports_none(db_session):
    pi = await factories.make_user(db_session)
    await _pmcid_row(db_session, pi)
    out = await apply_corpus_result(db_session, pi.id, _result([_rec(1001)]),
                                    pmcid_absent={"1001"})
    row = (await _pubs(db_session, pi.id))["1001"]
    assert (row.pmcid, row.methods_text) == (None, None)
    assert out.pmcid_cleared == ["1001"]


async def test_no_positive_answer_keeps_the_pmcid(db_session):
    pi = await factories.make_user(db_session)
    await _pmcid_row(db_session, pi)
    await apply_corpus_result(db_session, pi.id, _result([_rec(1001)]), pmcid_absent=set())
    row = (await _pubs(db_session, pi.id))["1001"]
    assert (row.pmcid, row.methods_text) == ("PMC999", "WRONG")


async def test_a_changed_pmcid_drops_the_methods(db_session):
    pi = await factories.make_user(db_session)
    await _pmcid_row(db_session, pi)
    out = await apply_corpus_result(db_session, pi.id, _result([_rec(1001, pmcid="PMC1")]))
    row = (await _pubs(db_session, pi.id))["1001"]
    assert (row.pmcid, row.methods_text) == ("PMC1", None)
    assert out.methods_cleared == ["1001"]


async def test_manual_rows_keep_their_pmcid(db_session):
    pi = await factories.make_user(db_session)
    await _pmcid_row(db_session, pi, provenance="manual")
    await apply_corpus_result(db_session, pi.id, _result([_rec(1001)]),
                              pmcid_absent={"1001"})
    row = (await _pubs(db_session, pi.id))["1001"]
    assert (row.pmcid, row.methods_text) == ("PMC999", "WRONG")


_TITLE = "Mechanism of X in Y cells"


async def _dup_world(db_session, pi, *, stored_provenance="s1", keeper_excluded=False,
                     with_record=False):
    db_session.add(Publication(user_id=pi.id, pmid="100", title=_TITLE, year=2019,
                               provenance=stored_provenance))
    if keeper_excluded:
        from datetime import UTC, datetime
        db_session.add(Publication(user_id=pi.id, pmid="200", title=_TITLE, year=2020,
                                   provenance="s1", excluded_at=datetime.now(UTC),
                                   excluded_reason="staff"))
    await db_session.flush()
    kw = {"title_duplicates": [("100", "200")]}
    if with_record:
        kw["title_duplicate_records"] = [_rec(100, title=_TITLE, year=2019)]
    return _result([_rec(200, title=_TITLE, year=2020)], **kw)


async def test_a_stored_earlier_same_title_row_is_excluded(db_session):
    pi = await factories.make_user(db_session)
    result = await _dup_world(db_session, pi)
    out = await apply_corpus_result(db_session, pi.id, result)
    pubs = await _pubs(db_session, pi.id)
    assert pubs["100"].excluded_reason == "duplicate_title"
    assert pubs["200"].excluded_at is None
    assert out.title_duplicates_excluded == ["100"]


async def test_a_new_anchored_duplicate_is_stored_excluded(db_session):
    pi = await factories.make_user(db_session)
    db_session.add(Publication(user_id=pi.id, pmid="0", title="Stored"))
    await db_session.flush()
    result = _result([_rec(200, title=_TITLE, year=2020)],
                     title_duplicates=[("100", "200")],
                     title_duplicate_records=[_rec(100, title=_TITLE, year=2019)])
    out = await apply_corpus_result(db_session, pi.id, result)
    pubs = await _pubs(db_session, pi.id)
    assert pubs["200"].excluded_at is None
    assert pubs["100"].excluded_reason == "duplicate_title"
    assert out.stored == ["200"]


async def test_a_manual_duplicate_is_never_excluded(db_session):
    pi = await factories.make_user(db_session)
    result = await _dup_world(db_session, pi, stored_provenance="manual")
    await apply_corpus_result(db_session, pi.id, result)
    assert (await _pubs(db_session, pi.id))["100"].excluded_at is None


async def test_no_exclusion_when_the_keeper_is_not_in_use(db_session):
    pi = await factories.make_user(db_session)
    result = await _dup_world(db_session, pi, keeper_excluded=True)
    await apply_corpus_result(db_session, pi.id, result)
    assert (await _pubs(db_session, pi.id))["100"].excluded_at is None

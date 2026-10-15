"""corpus_recheck (contract C4 as amended; Q7, F12, F26, F27, F28)."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from src.models import Publication, PublicationCandidate
from src.services.corpus import CorpusResult
from src.services.corpus_additions import CorpusStoreOutcome, apply_corpus_result
from src.services.corpus_recheck import (
    RecheckOutcome,
    recheck_progress_detail,
    recheck_stored_publications,
)
from tests import factories

pytestmark = pytest.mark.integration

ORGS = ["Example Institute of Science"]


def _result(**kw):
    kw.setdefault("kept", [])
    kw.setdefault("flagged", [])
    kw.setdefault("stage_map", {})
    kw.setdefault("stage_counts", {"s1": 1})
    return CorpusResult(**kw)


def _typed(pmid, types):
    return {"pmid": pmid, "title": "T", "year": 2020, "stages": ["s1"], "pub_types": types}


async def _pi_with(db, *rows):
    pi = await factories.make_user(db)
    for row in rows:
        row.user_id = pi.id
    db.add_all(rows)
    await db.flush()
    return pi


async def _run(db, pi, result, *, authorships=None, reason=None, retracted_dois=frozenset()):
    return await recheck_stored_publications(
        db, pi.id, result, authorships=authorships, organizations=ORGS,
        retracted_dois=retracted_dois, openalex_skip_reason=reason)


async def test_a_stored_row_now_retracted_is_excluded(db_session):
    row = Publication(pmid="30000000", title="T", provenance="s1")
    pi = await _pi_with(db_session, row)
    out = await _run(db_session, pi, _result(
        type_excluded=[_typed("30000000", ["Journal Article", "Retracted Publication"])]))
    assert (row.excluded_reason, row.excluded_by_user_id) == ("publication_type", None)
    assert row.excluded_at is not None and out.excluded_type == ["30000000"]


async def test_manual_and_excluded_rows_are_never_touched(db_session):
    when = datetime(2026, 1, 1, tzinfo=UTC)
    manual = Publication(pmid="1", title="T", provenance="manual")
    manual_doi = Publication(doi="10.1/r", title="R", provenance="manual")
    staff = Publication(pmid="2", title="T", provenance="s1", excluded_at=when,
                        excluded_reason="staff")
    pi = await _pi_with(db_session, manual, manual_doi, staff)
    result = _result(
        type_excluded=[_typed("1", ["Comment"]), _typed("2", ["Comment"])],
        flagged=[{"pmid": "1", "reason": "no_individual_author_match", "title": "T",
                  "stages": ["s1"], "year": 2020}],
        openalex_works=[{"pmid": None, "doi": "10.1/R", "is_retracted": True}])
    out = await _run(db_session, pi, result)
    assert (manual.provenance, manual.excluded_at) == ("manual", None)
    assert manual_doi.excluded_at is None
    assert (staff.excluded_reason, staff.excluded_at) == ("staff", when)
    assert out == RecheckOutcome()


async def test_a_stored_row_failing_the_gate_is_relabelled(db_session):
    row = Publication(pmid="7", title="T", provenance="s1")
    pi = await _pi_with(db_session, row)
    out = await _run(db_session, pi, _result(flagged=[{
        "pmid": "7", "reason": "no_individual_author_match", "stages": ["s1"], "title": "T",
        "year": 2020}]))
    assert (row.provenance, row.excluded_at) == ("unanchored", None)
    assert out.unanchored_gate == ["7"]
    assert (await db_session.execute(select(PublicationCandidate).where(
        PublicationCandidate.user_id == pi.id))).scalars().all() == []


@pytest.mark.parametrize(("kw", "reason"), [
    ({"stage_map": None}, "the resolve recorded no stage map"),
    ({"truncated_stages": ["s3"]}, "a search stopped at its cap (s3)"),
    ({"permanently_dropped": ["9"]}, "1 records could not be fetched"),
    ({"stage_counts": {"s1": 0}}, "the ORCID record listed no works"),
], ids=["no-stage-map", "truncated", "dropped", "empty-orcid"])
async def test_an_absent_row_is_kept_on_an_incomplete_run(db_session, kw, reason):
    row = Publication(pmid="8", title="T", provenance="s1,s4")
    pi = await _pi_with(db_session, row)
    out = await _run(db_session, pi, _result(**kw))
    assert row.provenance == "s1,s4"
    assert (out.absence_skipped, out.unanchored_absent) == (reason, [])


async def test_a_weak_flag_on_an_incomplete_run_keeps_the_anchor(db_session):
    # I13 (audit defect 3): ORCID listed nothing this run, so the stored s1 row came back only
    # through s4 with a weak match; that is anchor loss on an incomplete lookup, not an
    # identity failure.
    row = Publication(pmid="11", title="T", provenance="s1")
    pi = await _pi_with(db_session, row)
    out = await _run(db_session, pi, _result(
        stage_counts={"s1": 0}, stage_map={"11": ("s4",)},
        flagged=[{"pmid": "11", "reason": "s4_affiliation_mismatch", "stages": ["s4"],
                  "title": "T", "year": 2020}]))
    assert row.provenance == "s1"
    assert out.unanchored_gate == [] and out.absence_skipped == "the ORCID record listed no works"


async def test_an_empty_organisation_list_skips_the_check(db_session):
    # F36 (audit defect 4): direct call, so no ORCID fake is needed.
    row = Publication(doi="10.1/b", title="B", provenance="openalex")
    pi = await _pi_with(db_session, row)
    out = await recheck_stored_publications(
        db_session, pi.id, _result(), authorships={"10.1/b": None}, organizations=[],
        openalex_skip_reason=None)
    assert row.excluded_at is None
    assert out.openalex_skipped == "the ORCID profile lists no organisations"


async def test_a_retracted_pmid_less_row_is_excluded(db_session):
    listed = Publication(doi="10.1/r", title="R", provenance="s1")
    oa_only = Publication(doi="10.1/s", title="S", provenance="openalex")
    pi = await _pi_with(db_session, listed, oa_only)
    out = await _run(
        db_session, pi,
        _result(openalex_works=[{"pmid": None, "doi": "10.1/R", "is_retracted": True}]),
        authorships={}, retracted_dois={"10.1/s"})
    assert listed.excluded_reason == "publication_type"
    assert oa_only.excluded_reason == "publication_type"
    # Rows flushed together share created_at, so the walk order is the random id: sort.
    assert sorted(out.excluded_retracted) == ["10.1/r", "10.1/s"]
    assert (out.excluded_openalex, out.openalex_rows) == ([], 0), "no attribution check"


async def test_an_unanchored_row_returned_anchored_is_reanchored(db_session):
    row = Publication(pmid="11", title="T", provenance="unanchored")
    pi = await _pi_with(db_session, row)
    rec = {"pmid": "11", "title": "T", "abstract": "A.", "journal": "J", "year": 2020,
           "doi": None, "pmcid": None, "stages": ["s1"]}
    store = await apply_corpus_result(db_session, pi.id, _result(
        kept=[rec], ranked=[rec], stage_map={"11": ("s1",)}))
    assert row.provenance == "s1" and store.reanchored == ["11"]


async def test_openalex_rows(db_session):
    a, b, c = (Publication(doi=f"10.1/{x}", title=f"Work {x}", provenance="openalex")
               for x in "abc")
    pi = await _pi_with(db_session, a, b, c)
    skipped = await _run(db_session, pi, _result(), authorships=None,
                         reason="the OpenAlex authorship list could not be fetched")
    assert [r.excluded_at for r in (a, b, c)] == [None, None, None]
    assert skipped.openalex_skipped == "the OpenAlex authorship list could not be fetched"
    assert skipped.openalex_rows == 3
    authorships = {
        "10.1/a": {"institutions": ["Johns Hopkins University"], "raw_affiliations": [],
                   "orcid_in_metadata": False},
        "10.1/b": {"institutions": ["University of Elsewhere"], "raw_affiliations": [],
                   "orcid_in_metadata": False},
    }
    out = await _run(db_session, pi, _result(), authorships=authorships)
    assert (a.excluded_at, c.excluded_at) == (None, None), "attributed, and unknown"
    assert b.excluded_reason == "openalex_affiliation"
    assert (out.excluded_openalex, out.openalex_skipped) == (["10.1/b"], None)


def test_recheck_progress_detail_text():
    recheck = RecheckOutcome(excluded_type=["1"], excluded_retracted=["10.1/r"],
                             unanchored_gate=["2"], excluded_openalex=["10.1/x"],
                             absence_skipped="a search stopped at its cap (s3)")
    assert recheck_progress_detail(CorpusStoreOutcome(reanchored=["3"]), recheck) == (
        "Stored publications re-checked: 1 excluded (PubMed lists a publication type "
        "Blackbird leaves out), 1 excluded (OpenAlex marks the work retracted), 1 unanchored "
        "(the PubMed author list does not name the PI), 0 unanchored (no ORCID anchor in this "
        "run), 1 anchored again, 1 excluded (OpenAlex's authorship no longer ties the work to "
        "the PI). Absence not checked: a search stopped at its cap (s3). 1, 10.1/r, 2, 3, "
        "10.1/x")


async def test_a_mixed_type_record_is_excluded(db_session):
    row = Publication(pmid="31000000", title="T", provenance="s1")
    pi = await _pi_with(db_session, row)
    out = await _run(db_session, pi, _result(
        type_excluded=[_typed("31000000", ["Journal Article", "Comment"])]))
    assert row.excluded_reason == "publication_type"
    assert out.excluded_type == ["31000000"]


async def test_an_unanchored_or_null_row_of_an_excluded_type_is_excluded_too(db_session):
    unanchored = Publication(pmid="1", title="T", provenance="unanchored")
    null = Publication(pmid="2", title="T", provenance=None)
    pi = await _pi_with(db_session, unanchored, null)
    await _run(db_session, pi, _result(
        type_excluded=[_typed("1", ["Editorial"]), _typed("2", ["Editorial"])]))
    assert unanchored.excluded_reason == "publication_type"
    assert null.excluded_reason == "publication_type"


async def test_a_null_row_failing_the_gate_is_relabelled(db_session):
    row = Publication(pmid="7", title="T", provenance=None)
    pi = await _pi_with(db_session, row)
    out = await _run(db_session, pi, _result(flagged=[{
        "pmid": "7", "reason": "no_individual_author_match", "stages": ["s1"], "title": "T",
        "year": 2020}]))
    assert row.provenance == "unanchored"
    assert out.unanchored_gate == ["7"]


async def test_the_gate_relabel_runs_on_an_incomplete_run(db_session):
    row = Publication(pmid="7", title="T", provenance="s1")
    pi = await _pi_with(db_session, row)
    await _run(db_session, pi, _result(truncated_stages=["s3"], flagged=[{
        "pmid": "7", "reason": "no_individual_author_match", "stages": ["s1"], "title": "T",
        "year": 2020}], stage_map={"7": ("s1",)}))
    assert row.provenance == "unanchored"


async def test_an_absent_row_is_relabelled_on_a_complete_run(db_session):
    row = Publication(pmid="8", title="T", provenance="s1,s4")
    pi = await _pi_with(db_session, row)
    out = await _run(db_session, pi, _result())
    assert row.provenance == "unanchored"
    assert out.unanchored_absent == ["8"]


async def test_an_absent_null_row_is_relabelled_on_a_complete_run(db_session):
    row = Publication(pmid="12", title="T", provenance=None)
    pi = await _pi_with(db_session, row)
    await _run(db_session, pi, _result())
    assert row.provenance == "unanchored"


async def test_a_row_whose_anchor_stage_returned_it_without_a_record_is_kept(db_session):
    row = Publication(pmid="10", title="T", provenance="s1")
    pi = await _pi_with(db_session, row)
    await _run(db_session, pi, _result(stage_map={"10": ("s1",)}))
    assert row.provenance == "s1"


async def test_a_row_only_a_non_anchor_stage_returned_is_absent(db_session):
    row = Publication(pmid="9", title="T", provenance="s1")
    pi = await _pi_with(db_session, row)
    await _run(db_session, pi, _result(stage_map={"9": ("s4",)}))
    assert row.provenance == "unanchored"


async def test_pmid_less_rows_are_never_unanchored(db_session):
    row = Publication(doi="10.1/p", title="P", provenance="s1")
    pi = await _pi_with(db_session, row)
    await _run(db_session, pi, _result())
    assert (row.provenance, row.excluded_at) == ("s1", None)


def test_recheck_progress_detail_is_none_when_nothing_happened():
    assert recheck_progress_detail(CorpusStoreOutcome(), RecheckOutcome()) is None
    assert recheck_progress_detail(
        CorpusStoreOutcome(), RecheckOutcome(openalex_skipped="x", openalex_rows=0)) is None

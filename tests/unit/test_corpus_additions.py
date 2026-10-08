"""The corpus storage rule (spec 2026-10-05 §6.3, D14, D17): no cap; anchored records are
stored, unanchored ones held for review; provenance; DOI verification; metadata refresh."""
import asyncio
from types import SimpleNamespace

from src.models import Publication, PublicationCandidate
from src.services.corpus_additions import (
    apply_corpus_result,
    is_anchored,
    metadata_changes,
    provenance_for,
    reconcile_record_doi,
    select_corpus_additions,
)


def _r(pmid, stages, **kw):
    return {"pmid": pmid, "stages": stages, **kw}


def test_anchored_only_and_uncapped():
    ranked = [_r(str(i), ["s1"]) for i in range(80)] + [_r("x", ["s4"]), _r("y", ["s2"])]
    got = select_corpus_additions(ranked, stored_pmids={"3"})
    assert len(got.to_store) == 79 and "3" not in [r["pmid"] for r in got.to_store]
    assert [r["pmid"] for r in got.review_only] == ["x", "y"]


def test_nothing_new_and_missing_pmids_are_ignored():
    got = select_corpus_additions([_r("1", ["s1"]), {"stages": ["s1"]}, _r("", ["s1"])], {"1"})
    assert got.to_store == [] and got.review_only == []


def test_record_without_stages_is_review_only():
    got = select_corpus_additions([{"pmid": "9"}], stored_pmids=set())
    assert got.to_store == [] and [r["pmid"] for r in got.review_only] == ["9"]


def test_provenance_is_the_sorted_stage_list_or_unanchored():
    assert provenance_for(_r("1", ["s3", "s1", "s2"])) == "s1,s2,s3"
    assert provenance_for(_r("1", ["s2", "s4"])) == "unanchored"
    assert is_anchored(_r("1", ["s3"])) and not is_anchored(_r("1", ["s4"]))


def test_reconcile_record_doi_reports_verification():
    rec = {"pmid": "1", "doi": "10.1/pubmed"}
    assert reconcile_record_doi(rec, {"1": "10.1/PUBMED"}) == ("10.1/PUBMED", True)
    assert reconcile_record_doi(rec, {"1": "10.1/other"}) == ("10.1/pubmed", True)
    assert reconcile_record_doi({"pmid": "1", "doi": None}, {"1": "10.1/x"}) == ("10.1/x", False)
    assert reconcile_record_doi({"pmid": "1"}, {}) == (None, None)


def test_metadata_changes_ignore_empty_values_and_manual_rows():
    row = Publication(pmid="1", title="Old", abstract="A", journal="J", year=2019, pmcid=None)
    rec = {"title": "New", "abstract": "", "journal": "J", "year": 2020, "pmcid": "PMC1"}
    assert metadata_changes(row, rec) == {"title": "New", "year": 2020, "pmcid": "PMC1"}
    row.provenance = "manual"
    assert metadata_changes(row, rec) == {}


class _Scalars:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class _FakeDb:
    """Serves apply_corpus_result's two selects (publications, then candidates)."""

    def __init__(self, rows, candidates=()):
        self._results = [rows, list(candidates)]
        self.added = []

    async def execute(self, _stmt):
        return _Scalars(self._results.pop(0))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def _row(pmid, provenance, excluded_at=None):
    return Publication(pmid=pmid, title="t", provenance=provenance, excluded_at=excluded_at)


def _apply(rows, ranked, superseded=(), candidates=()):
    db = _FakeDb(rows, candidates)
    result = SimpleNamespace(ranked=ranked, orcid_dois={}, superseded=list(superseded))
    return asyncio.run(apply_corpus_result(db, "u", result)), db


def test_stored_preprint_is_excluded_by_the_system_when_published_version_is_anchored():
    pre, null_pre = _row("1", "unanchored"), _row("3", None)
    pub = _row("2", "s1")
    out, _ = _apply([pre, null_pre, pub], [_r("2", ["s1"], update_of=["1", "3", "2"])])
    assert out.superseded_excluded == ["1", "3"]
    assert pre.excluded_at is not None and null_pre.excluded_at is not None
    assert pre.excluded_by_user_id is None and null_pre.excluded_by_user_id is None
    assert pub.excluded_at is None


def test_published_version_inserted_this_run_excludes_stored_preprint():
    pre = _row("1", "unanchored")
    out, db = _apply([pre], [_r("2", ["s3"], update_of=["1"])])
    assert [p.pmid for p in db.added] == ["2"] and out.stored == ["2"]
    assert out.superseded_excluded == ["1"] and pre.excluded_at is not None


def test_manual_row_is_never_excluded():
    pre = _row("1", "manual")
    out, _ = _apply([pre, _row("2", "s1")], [_r("2", ["s1"], update_of=["1"])])
    assert out.superseded_excluded == [] and pre.excluded_at is None


def test_a_staff_accepted_successor_retires_even_an_anchored_preprint():
    # The successor's record is unanchored (s4) but staff accepted its row (manual).
    anchored_pre = _row("1", "s1,s4")
    unanchored_pre = _row("3", "unanchored")
    pub = _row("2", "manual")
    out, _ = _apply(
        [anchored_pre, unanchored_pre, pub], [_r("2", ["s4"], update_of=["1", "3"])],
    )
    assert out.superseded_excluded == ["1", "3"]
    assert anchored_pre.excluded_at is not None and unanchored_pre.excluded_at is not None


def test_an_unanchored_unaccepted_successor_excludes_nothing():
    anchored_pre = _row("1", "s1,s4")
    unanchored_pre = _row("3", "unanchored")
    pub = _row("2", "unanchored")
    out, _ = _apply(
        [anchored_pre, unanchored_pre, pub], [_r("2", ["s4"], update_of=["1", "3"])],
    )
    assert out.superseded_excluded == []
    assert anchored_pre.excluded_at is None and unanchored_pre.excluded_at is None


def test_already_excluded_row_is_untouched():
    from datetime import UTC, datetime

    when = datetime(2026, 1, 1, tzinfo=UTC)
    pre = _row("1", "unanchored", excluded_at=when)
    out, _ = _apply([pre, _row("2", "s1")], [_r("2", ["s1"], update_of=["1"])])
    assert out.superseded_excluded == [] and pre.excluded_at == when


def test_unstored_successor_held_as_candidate_excludes_nothing():
    pre = _row("1", "unanchored")
    out, db = _apply([pre], [_r("2", ["s4"], update_of=["1"])])
    assert out.candidates_new == ["2"] and out.superseded_excluded == []
    assert pre.excluded_at is None


def test_records_without_update_of_change_nothing():
    pre = _row("1", "unanchored")
    out, _ = _apply([pre, _row("2", "s1")], [_r("2", ["s1"]), _r("1", ["s4"], update_of=None)])
    assert out.superseded_excluded == [] and pre.excluded_at is None


def test_one_sided_link_from_the_resolver_excludes_the_stored_preprint():
    pre, pub = _row("1", "unanchored"), _row("2", "s1")
    out, _ = _apply([pre, pub], [_r("2", ["s1"])], superseded=[("1", "2")])
    assert out.superseded_excluded == ["1"] and pre.excluded_at is not None
    assert pre.excluded_by_user_id is None


def test_pending_candidate_of_a_superseded_pmid_is_rejected_by_the_system():
    cand = PublicationCandidate(pmid="1", title="t", status="pending")
    kept = PublicationCandidate(pmid="7", title="t", status="pending")
    out, _ = _apply(
        [_row("2", "s1")], [_r("2", ["s1"])], superseded=[("1", "2")], candidates=[cand, kept],
    )
    assert out.candidates_superseded == ["1"]
    assert cand.status == "rejected" and cand.decided_by_user_id is None
    assert cand.decided_at is not None and kept.status == "pending"

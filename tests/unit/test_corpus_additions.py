"""The corpus storage rule (spec 2026-10-05 §6.3, D14, D17): no cap; anchored records are
stored, unanchored ones held for review; provenance; DOI verification; metadata refresh."""
from src.models import Publication
from src.services.corpus_additions import (
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

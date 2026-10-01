from src.services.corpus_additions import select_corpus_additions


def _r(pmid, stages):
    return {"pmid": pmid, "stages": stages}


def test_pipeline_rule_anchored_only_and_capped():
    kept = [_r("1", ["s1"]), _r("2", ["s4"]), _r("3", ["s3"]), _r("4", ["s1", "s2"]), _r("5", ["s2"])]
    got = select_corpus_additions(kept, stored_pmids={"4"}, stored_count=48, cap=50)
    assert [r["pmid"] for r in got.to_store] == ["1", "3"]
    assert got.over_cap == []
    assert [r["pmid"] for r in got.review_only] == ["2", "5"]


def test_budget_is_cap_minus_stored():
    kept = [_r(str(i), ["s1"]) for i in range(5)]
    got = select_corpus_additions(kept, stored_pmids=set(), stored_count=48, cap=50)
    assert len(got.to_store) == 2 and len(got.over_cap) == 3


def test_over_cap_trims_the_oldest_additions_never_the_stored_rows():
    # Rothstein's measured shape: 15 survivors + 42 candidates = 57 over a cap of 50.
    kept = [_r(str(i), ["s1"]) for i in range(42)]
    got = select_corpus_additions(kept, stored_pmids=set(), stored_count=15, cap=50)
    assert len(got.to_store) == 35
    assert [r["pmid"] for r in got.over_cap] == [str(i) for i in range(35, 42)]


def test_nothing_new_and_missing_pmids_are_ignored():
    kept = [_r("1", ["s1"]), {"stages": ["s1"]}, _r("", ["s1"])]
    got = select_corpus_additions(kept, stored_pmids={"1"}, stored_count=1, cap=50)
    assert got.to_store == [] and got.over_cap == [] and got.review_only == []


def test_record_without_stages_is_review_only():
    got = select_corpus_additions([{"pmid": "9"}], stored_pmids=set(), stored_count=0, cap=50)
    assert got.to_store == [] and [r["pmid"] for r in got.review_only] == ["9"]
